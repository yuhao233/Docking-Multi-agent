"""`tools/online.py` 的离线单测：结构来源链、溯源、名称别名与混合物选项。

真实联网链路由端到端证据覆盖；这里用注入的假 HTTP / 假下载，保证单测零网络、可重复。
"""
from __future__ import annotations

import json
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

from docking_agent.core import parse_smiles_text  # noqa: E402
from docking_agent.core.ligands import extract_smiles  # noqa: E402
from docking_agent.tools import online  # noqa: E402

MANCOZEB_SMILES = ("C(CNC(=S)[S-])NC(=S)[S-].C(CNC(=S)[S-])NC(=S)[S-].[Mn+2].[Zn+2]")
PUBCHEM_PAYLOAD = {"PropertyTable": {"Properties": [{
    "CID": 3034368, "MolecularFormula": "C8H12MnN4S8Zn", "MolecularWeight": "541.1",
    "SMILES": MANCOZEB_SMILES, "ConnectivitySMILES": MANCOZEB_SMILES,
    "IUPACName": "zinc;manganese(2+);bis(N-[2-(sulfidocarbothioylamino)ethyl]carbamodithioate)",
    "Title": "Mancozeb"}]}}

RESOLVED = {
    "status": "resolved",
    "query": {"raw": "植物去甲基化酶ROS1", "genes": ["ROS1"],
              "species": {"name": "Arabidopsis thaliana", "taxid": 3702, "is_plant": True,
                          "matched": "植物"}, "family_terms": ["demethylase"], "accession": ""},
    "selected": {"accession": "Q9SJQ6", "entry_id": "ROS1_ARATH",
                 "protein": "DNA glycosylase/AP lyase ROS1",
                 "organism": "Arabidopsis thaliana", "organism_id": 3702,
                 "gene_names": ["ROS1"], "reviewed": True, "length": 1393,
                 "pdb_ids": ["7YHP"], "is_plant": True, "score": 114.0,
                 "reasons": ["Swiss-Prot 已审阅 +40"], "strategies": ["gene+species"]},
    "candidates": [], "attempts": [{"strategy": "gene+species",
                                    "query": "gene_exact:ROS1 AND organism_id:3702", "hits": 1}],
    "total_candidates": 1,
}


def _run_ctx(tmp, spec):
    from docking_agent.runs import Run, current_run

    run = Run(Path(tmp), "R-ONLINE", "agent", {"mode": "chat"})
    run.data["task_spec"] = spec
    return run, current_run


# --------------------------------------------------------------------------- #
# 小分子：中文别名 + 多组分/聚合物如实报告 + 结构化选项
# --------------------------------------------------------------------------- #
def test_fetch_molecule_record_maps_chinese_alias_and_reports_mixture(monkeypatch) -> None:
    def fake_http(url: str) -> Any:
        if "/name/" in url:
            name = urllib.parse.unquote(url.split("/name/")[1].split("/")[0])
            if name == "Mancozeb":
                return PUBCHEM_PAYLOAD
        raise online.OnlineError("PubChem 未找到对应记录（HTTP 404）")

    monkeypatch.setattr(online, "_http_json", fake_http)
    out = json.loads(online.fetch_molecule_record.func("代森猛锌"))

    # 命中多组分结构 → 状态必须是 needs_user_input：记录查到了，但**没有**导入任何单分子，
    # 必须等用户确认代表结构（旧的 `status=ok` + choices 会诱导模型直接拿某个片段去对接）。
    assert out["status"] == "needs_user_input"
    assert out["choices_published"]["count"] >= 3
    assert out["alias_used"] == "代森猛锌" and out["resolved_query"] == "Mancozeb"
    comp = out["compounds"][0]
    assert comp["cid"] == 3034368 and comp["formula"] == "C8H12MnN4S8Zn"
    assert comp["is_mixture"] is True
    assert comp["smiles"] == MANCOZEB_SMILES, "原始多组分 SMILES 必须原样保留，不得臆造单一结构"
    assert comp["representative_smiles"] == "S=C([S-])NCCNC(=S)[S-]"
    assert "多组分" in out["mixture_note"] and "配位聚合物" in out["mixture_note"]
    roles = {c["role"] for c in comp["components"]}
    assert any("最大有机片段" in r for r in roles) and any("反离子" in r for r in roles)


def test_mancozeb_choices_are_structured_and_parse_to_one_molecule(monkeypatch) -> None:
    """选项本身（走界面）必须结构化、每个 prompt 都能确定性地解析出 1 个分子。

    注意：选项明细**不再回流给模型**（见下一个用例），所以这里直接测 `mixture_choices()`
    这个纯函数，而不是工具的 JSON 返回。
    """
    from docking_agent.tools.choices import mixture_choices

    def fake_http(url: str) -> Any:
        if "Mancozeb" in url:
            return PUBCHEM_PAYLOAD
        raise online.OnlineError("PubChem 未找到对应记录（HTTP 404）")

    monkeypatch.setattr(online, "_http_json", fake_http)
    out = json.loads(online.fetch_molecule_record.func("代森锰锌"))
    choices = mixture_choices(out["compounds"][0], "代森锰锌")
    assert len(choices) >= 3
    assert all(c["kind"] == "molecule" for c in choices)
    assert {c["detail"]["mode"] for c in choices} >= {"raw-mixture", "metal-monomer",
                                                     "organic-fragment"}
    for choice in choices:
        assert choice["value"] and choice["label"] and choice["prompt"]
        mols = parse_smiles_text(choice["prompt"])
        assert len(mols) == 1, f"点选项必须能确定地解析出 1 个分子：{choice['label']} → {mols}"
        assert extract_smiles(choice["prompt"]), "intake 的确定性抽取也必须能拿到该分子"


def test_mixture_tool_payload_hides_option_details_from_model(monkeypatch) -> None:
    """真实反馈回归：同一批选项不能既进界面按钮、又被主管 Agent 抄成正文表格。

    工具的**模型侧**返回只给「数量 + 原因 + 不要复述」的指令；选项明细只走
    `run.data["choices"]` → SSE → 前端按钮。
    """
    def fake_http(url: str) -> Any:
        if "Mancozeb" in url:
            return PUBCHEM_PAYLOAD
        raise online.OnlineError("PubChem 未找到对应记录（HTTP 404）")

    monkeypatch.setattr(online, "_http_json", fake_http)
    from docking_agent.runs import Run, current_run
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        run = Run(Path(td), "R-MIX", "agent", {"mode": "chat"})
        token = current_run.set(run)
        try:
            out = json.loads(online.fetch_molecule_record.func("代森锰锌"))
            # 模型侧：没有选项明细
            assert "choices" not in out, "选项明细不得回流给模型（会被抄进正文）"
            assert out["choices_published"]["count"] >= 3
            assert "不要" in out["message"] and "界面" in out["message"]
            blob = json.dumps(out, ensure_ascii=False)
            assert "molecule:3034368:raw" not in blob, \
                "模型侧载荷不得包含任何「可点选选项」的 id/明细"
            # 界面侧：选项仍在运行数据里（前端按钮 + 点选后续跑）
            published = run.data.get("choices") or []
            assert len(published) >= 3
            assert all(c["kind"] == "molecule" and c["prompt"] for c in published)
        finally:
            current_run.reset(token)


def test_fetch_molecule_record_unknown_name_returns_actionable_error(monkeypatch) -> None:
    def always_404(url: str) -> Any:
        raise online.OnlineError("PubChem 未找到对应记录（HTTP 404）")

    monkeypatch.setattr(online, "_http_json", always_404)
    out = json.loads(online.fetch_molecule_record.func("不存在的化合物QQ"))
    assert out["status"] == "error"
    assert "未找到" in out["message"]
    assert out["attempts"], "必须记录已尝试的查询名"


# --------------------------------------------------------------------------- #
# 受体：结构来源链（RCSB / AlphaFold）+ 溯源
# --------------------------------------------------------------------------- #
def test_fetch_protein_structure_uses_rcsb_with_full_provenance(monkeypatch) -> None:
    monkeypatch.setattr(online, "resolve_receptor_name", lambda src, **kw: dict(RESOLVED))
    monkeypatch.setattr(online, "_rcsb_entry_info",
                        lambda pid: {"method": "EM", "resolution": "3.1 A"})

    def fake_prepare(url: str, dest: str) -> Dict[str, Any]:
        assert url.endswith("7YHP.pdb")
        return {"pdbqt": "/tmp/7YHP.pdbqt", "center": [1.0, 2.0, 3.0], "size": [22, 22, 22],
                "name": "7YHP", "protein": "DNA glycosylase/AP lyase ROS1"}

    monkeypatch.setattr(online, "_download_and_prepare", fake_prepare)
    with tempfile.TemporaryDirectory() as tmp:
        run, current_run = _run_ctx(tmp, {"receptor": {"name": "植物去甲基化酶ROS1",
                                                       "file": "", "source": "named"}})
        token = current_run.set(run)
        try:
            out = json.loads(online.fetch_protein_structure.func("植物去甲基化酶ROS1"))
        finally:
            current_run.reset(token)

    assert out["status"] == "ok"
    assert out["structure_source"] == "rcsb" and out["pdb_id"] == "7YHP"
    assert out["accession"] == "Q9SJQ6" and out["organism"] == "Arabidopsis thaliana"
    assert out["resolution"] == "3.1 A"
    assert out["receptor_file"] == "/tmp/7YHP.pdbqt"
    prov = out["provenance"]
    assert prov["database"] == "RCSB PDB" and prov["pdb_id"] == "7YHP"
    assert prov["uniprot_attempts"], "溯源里必须带 UniProt 已尝试检索"
    assert prov["score_reasons"], "溯源里必须带候选打分理由"
    assert not run.data.get("choices"), "解析成功必须清空陈旧 choices"
    # 溯源必须随运行落盘（报告 §1.2 直接渲染）：否则结论节一精简就从报告里丢了
    recorded = run.data.get("receptor_provenance") or {}
    assert recorded.get("accession") == "Q9SJQ6" and recorded.get("pdb_id") == "7YHP"
    assert recorded.get("organism") == "Arabidopsis thaliana"
    assert recorded.get("structure_method") == "EM" and recorded.get("structure_resolution") == "3.1 A"
    assert "structure_url" not in recorded, "URL 不进运行小状态（报告正文不出现裸链接）"


def test_fetch_protein_structure_falls_back_to_alphafold(monkeypatch) -> None:
    no_pdb = dict(RESOLVED)
    no_pdb["selected"] = {**RESOLVED["selected"], "pdb_ids": []}
    monkeypatch.setattr(online, "resolve_receptor_name", lambda src, **kw: no_pdb)
    monkeypatch.setattr(online, "_rcsb_search_by_uniprot", lambda acc, rows=10: [])
    monkeypatch.setattr(online, "_alphafold_prediction", lambda acc: {
        "entry_id": "AF-Q9SJQ6-F1",
        "model_url": "https://alphafold.ebi.ac.uk/files/AF-Q9SJQ6-F1-model_v6.pdb",
        "cif_url": "https://alphafold.ebi.ac.uk/files/AF-Q9SJQ6-F1-model_v6.cif",
        "model_version": 6, "model_date": "2025-08-01", "description": "ROS1", "organism": "A. thaliana"})

    def fake_prepare(url: str, dest: str) -> Dict[str, Any]:
        assert "model_v6.pdb" in url, "必须用接口返回的 pdbUrl（版本不写死）"
        return {"pdbqt": "/tmp/AF.pdbqt", "center": [0, 0, 0], "size": [22, 22, 22],
                "name": "AF-Q9SJQ6-F1", "protein": "DNA glycosylase/AP lyase ROS1"}

    monkeypatch.setattr(online, "_download_and_prepare", fake_prepare)
    with tempfile.TemporaryDirectory() as tmp:
        run, current_run = _run_ctx(tmp, {"receptor": {"name": "植物去甲基化酶ROS1",
                                                       "file": "", "source": "named"}})
        token = current_run.set(run)
        try:
            out = json.loads(online.fetch_protein_structure.func("植物去甲基化酶ROS1"))
        finally:
            current_run.reset(token)

    assert out["status"] == "ok" and out["structure_source"] == "alphafold"
    assert out["pdb_id"] == ""
    assert out["structure_url"].endswith("AF-Q9SJQ6-F1-model_v6.pdb")
    assert out["alphafold"]["model_version"] == 6
    assert out["provenance"]["database"] == "AlphaFold DB"
    assert out["provenance"]["alphafold_model_version"] == 6
    assert out["provenance"]["structure_source"] == "alphafold"


def test_fetch_protein_structure_pdb_id_path_still_works(monkeypatch) -> None:
    monkeypatch.setattr(online, "_download_and_prepare",
                        lambda url, dest: {"pdbqt": "/tmp/1DWC.pdbqt", "center": [1, 1, 1],
                                           "size": [22, 22, 22], "name": "1DWC",
                                           "protein": "thrombin"})
    out = json.loads(online.fetch_protein_structure.func("1DWC"))
    assert out["status"] == "ok" and out["source_db"] == "pdb"
    assert out["structure_source"] == "rcsb" and out["pdb_id"] == "1DWC"


def test_receptor_success_does_not_clear_molecule_choices(monkeypatch) -> None:
    """同一次运行里「分子是混合物要选 + 受体高置信解析」是正常组合：受体成功不得抹掉分子的 choices。"""
    monkeypatch.setattr(online, "resolve_receptor_name", lambda src, **kw: dict(RESOLVED))
    monkeypatch.setattr(online, "_rcsb_entry_info",
                        lambda pid: {"method": "EM", "resolution": "3.1 A"})
    monkeypatch.setattr(online, "_download_and_prepare",
                        lambda url, dest: {"pdbqt": "/tmp/7YHP.pdbqt", "center": [0, 0, 0],
                                           "size": [22, 22, 22], "name": "7YHP", "protein": "x"})
    with tempfile.TemporaryDirectory() as tmp:
        run, current_run = _run_ctx(tmp, {"receptor": {"name": "植物去甲基化酶ROS1",
                                                       "file": "", "source": "named"}})
        run.data["choices"] = [{"kind": "molecule", "id": "molecule:3034368:raw",
                                "label": "Mancozeb 原始多组分", "value": MANCOZEB_SMILES,
                                "prompt": "代森锰锌 " + MANCOZEB_SMILES}]
        token = current_run.set(run)
        try:
            out = json.loads(online.fetch_protein_structure.func("植物去甲基化酶ROS1"))
        finally:
            current_run.reset(token)
    assert out["status"] == "ok"
    assert [c.get("kind") for c in run.data.get("choices") or []] == ["molecule"]
