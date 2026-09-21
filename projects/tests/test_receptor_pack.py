"""受体结构入包：整包下载（download.zip）必须能独立复现本次对接。

真实缺口（用户实测提问）：「你提供的打包文件里，有用于对接的受体结构文件吗？」
—— 答案当时是**没有**：运行目录里只有配体位姿与 JSON，受体只以绝对路径记在
`docking.json`（`assets/receptors/...` 或 `assets/cache/...`），换台机器就取不到。

约定（本轮修法）：运行目录新增 `receptor/`：
  - `receptor/<受体>.pdbqt`        —— 对接实际使用的受体（权威）
  - `receptor/<受体>_prepared.pdb` —— 准备阶段去水/去杂原子后的蛋白
  - `receptor/<受体>_source.<ext>` —— 原始结构（上传的原文件 / URL 下载的原文件）
三者都登记为可下载产物，因此会出现在「中间数据」页签、报告第 9 节产物清单与整包 ZIP 里。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Dict, List

from docking_agent.reporting.artifacts import copy_receptor_files
from docking_agent.runs import Run, RunStore


def _tool_msg(name: str, payload: Dict[str, Any]) -> Any:
    from langchain_core.messages import ToolMessage

    return ToolMessage(content=json.dumps(payload, ensure_ascii=False), name=name,
                       tool_call_id="t-" + name)


def _fake_receptor(cache: Path, *, key: str = "thrombin") -> Dict[str, str]:
    """造一组真实的受体准备产物：<stem>.pdbqt + <stem>_prot.pdb。"""
    cache.mkdir(parents=True, exist_ok=True)
    stem = cache / f"{key}_abc123"
    pdbqt = stem.with_suffix(".pdbqt")
    pdbqt.write_text("REMARK 对接用受体\nATOM      1  N   ALA A   1\n", encoding="utf-8")
    (cache / f"{stem.name}_prot.pdb").write_text("ATOM      1  N   ALA A   1\n", encoding="utf-8")
    return {"pdbqt": str(pdbqt), "prepared": str(cache / f"{stem.name}_prot.pdb")}


def test_copy_receptor_files_puts_structure_in_run_dir(tmp_path: Any) -> None:
    """受体 PDBQT / 准备后 PDB / 原始上传文件三类都进 receptor/ 并登记为产物。"""
    cache = tmp_path / "cache"
    made = _fake_receptor(cache)
    upload = tmp_path / "uploads" / "my_receptor.pdb"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_text("HEADER    UPLOADED\nATOM      1  N   ALA A   1\n", encoding="utf-8")

    run = Run(tmp_path / "runs", "R-REC", "agent", {"mode": "chat"})
    block = {"receptor_key": "thrombin", "receptor": "thrombin(1DWC)", "pdbqt": made["pdbqt"]}
    names = copy_receptor_files(run, [block], source_candidates=[str(upload), ""])

    assert names == ["receptor_pdbqt_thrombin", "receptor_prepared_thrombin",
                     "receptor_source_thrombin"]
    run_dir = run.dir
    assert (run_dir / "receptor" / "thrombin.pdbqt").is_file()
    assert (run_dir / "receptor" / "thrombin_prepared.pdb").is_file()
    assert (run_dir / "receptor" / "thrombin_source.pdb").is_file()
    assert "ATOM" in (run_dir / "receptor" / "thrombin.pdbqt").read_text(encoding="utf-8")
    saved = {a["name"] for a in run.artifacts()}
    assert {"receptor_pdbqt_thrombin", "receptor_prepared_thrombin",
            "receptor_source_thrombin"} <= saved


def test_prepared_pdb_found_for_ph_variant(tmp_path: Any) -> None:
    """按 pH 准备受体时文件名多一层 `_ph7.4`：仍要找到同一批的 `_prot.pdb`。"""
    from docking_agent.reporting.artifacts import _find_prepared_pdb

    cache = tmp_path / "cache"
    base = cache / "receptor_391427fb44da39a3"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "receptor_391427fb44da39a3_prot.pdb").write_text("ATOM\n", encoding="utf-8")
    ph_variant = cache / "receptor_391427fb44da39a3_ph7.4.pdbqt"
    ph_variant.write_text("REMARK\n", encoding="utf-8")
    assert _find_prepared_pdb(ph_variant) == cache / "receptor_391427fb44da39a3_prot.pdb"
    plain = cache / "receptor_391427fb44da39a3.pdbqt"
    plain.write_text("REMARK\n", encoding="utf-8")
    assert _find_prepared_pdb(plain) == cache / "receptor_391427fb44da39a3_prot.pdb"
    # 什么都没有时返回 None（不能瞎猜一个文件）
    empty = tmp_path / "empty" / "x.pdbqt"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text("REMARK\n", encoding="utf-8")
    assert _find_prepared_pdb(empty) is None


def test_copy_receptor_files_is_best_effort(tmp_path: Any) -> None:
    """文件缺失 / 路径是 URL / 没有受体块：只记 warning，绝不抛异常打断运行。"""
    run = Run(tmp_path / "runs", "R-MISS", "agent", {"mode": "chat"})
    assert copy_receptor_files(run, []) == []
    assert copy_receptor_files(run, [{"receptor_key": "x", "pdbqt": "/no/such/file.pdbqt"}]) == []
    assert copy_receptor_files(run, [{"receptor_key": "x", "pdbqt": "https://example.org/a.pdbqt"}]) == []
    # 只有 PDBQT 可用时也能入包（原始文件缺失不算错误）
    cache = tmp_path / "cache2"
    made = _fake_receptor(cache, key="trypsin")
    names = copy_receptor_files(run, [{"receptor_key": "trypsin", "pdbqt": made["pdbqt"]}],
                                source_candidates=["", "https://example.org/orig.pdb"])
    assert names == ["receptor_pdbqt_trypsin", "receptor_prepared_trypsin"]


def test_zip_contains_receptor_after_agent_run(tmp_path: Any, monkeypatch: Any) -> None:
    """多 Agent 落盘后，整包 ZIP 里必须同时有受体结构与配体位姿。"""
    cache = tmp_path / "cache"
    made = _fake_receptor(cache)
    block = {"receptor_key": "thrombin", "receptor": "thrombin(1DWC)",
             "pdbqt": made["pdbqt"],
             "box_center": [1.0, 2.0, 3.0], "box_size": [22.0, 22.0, 22.0],
             "box_source": "用户指定",
             "results": [{"name": "乙醇", "smiles": "CCO", "affinity_kcal_mol": -2.8,
                          "engine": "vina", "exhaustiveness": 1}]}
    pid = _tool_msg("run_docking", {"status": "ok", "receptors": [block]})
    run = Run(tmp_path / "runs", "R-ZIP", "agent", {"mode": "chat"})
    run.set(task_spec={"task_type": "screening", "decision": "accept"})
    from docking_agent.agents.persistence import persist_agent_run

    persist_agent_run(run, [pid], "结论")
    assert (run.dir / "receptor" / "thrombin.pdbqt").is_file(), "落盘阶段就要把受体放进运行目录"
    # 位姿由对接工具落盘（这里补一个替身，验证整包会带上 poses/ 与 receptor/ 两类结构）
    poses = run.dir / "poses"
    poses.mkdir(exist_ok=True)
    (poses / "pose_ligand_test.pdbqt").write_text("MODEL 1\nENDMDL\n", encoding="utf-8")

    store = RunStore(root=tmp_path / "runs")
    dest = store.zip("R-ZIP")
    assert dest is not None and dest.is_file()
    with zipfile.ZipFile(dest) as zf:
        names = zf.namelist()
    assert "receptor/thrombin.pdbqt" in names, names
    assert any(n.startswith("poses/") for n in names), "配体位姿也必须在包里"


def test_pipeline_path_also_packs_receptor(tmp_path: Any, monkeypatch: Any) -> None:
    """确定性流水线同样要把受体结构写进运行目录（两条路径共用同一实现）。"""
    from docking_agent import pipeline as P

    cache = tmp_path / "cache"
    made = _fake_receptor(cache)
    run = Run(tmp_path / "runs", "R-PIPE", "pipeline", {"receptor_file": made["pdbqt"]})
    result: Dict[str, Any] = {"ranking": [], "positive_control": {}, "receptors": [
        {"receptor_key": "thrombin", "pdbqt": made["pdbqt"], "ranking": []}]}
    monkeypatch.setattr(P, "write_report_charts", lambda *a, **k: None)
    monkeypatch.setattr(P, "write_report_pdf", lambda *a, **k: None)
    monkeypatch.setattr(P, "_write_report_artifacts", P._write_report_artifacts, raising=True)
    # 直接调用被测函数：只关心受体是否入包（图表/PDF 已打桩）
    import docking_agent.reporting.artifacts as A
    monkeypatch.setattr(A, "write_report_charts", lambda *a, **k: None)
    monkeypatch.setattr(A, "write_report_pdf", lambda *a, **k: None)
    P._write_report_artifacts(run, result)
    assert (run.dir / "receptor" / "thrombin.pdbqt").is_file()
    assert any(a["name"] == "receptor_pdbqt_thrombin" for a in run.artifacts())


def test_report_lists_receptor_artifacts(tmp_path: Any) -> None:
    """报告第 9 节产物清单要能看到受体文件（用户翻报告就知道包里有什么）。"""
    cache = tmp_path / "cache"
    made = _fake_receptor(cache)
    block = {"receptor_key": "thrombin", "receptor": "thrombin(1DWC)",
             "pdbqt": made["pdbqt"],
             "box_center": [1.0, 2.0, 3.0], "box_size": [22.0, 22.0, 22.0],
             "box_source": "用户指定",
             "results": [{"name": "乙醇", "smiles": "CCO", "affinity_kcal_mol": -2.8,
                          "engine": "vina", "exhaustiveness": 1}]}
    run = Run(tmp_path / "runs", "R-REPORT", "agent", {"mode": "chat"})
    run.set(task_spec={"task_type": "screening", "decision": "accept"})
    from docking_agent.agents.persistence import persist_agent_run

    persist_agent_run(run, [_tool_msg("run_docking", {"status": "ok", "receptors": [block]})], "结论")
    report = (run.dir / "report.md").read_text(encoding="utf-8")
    assert "receptor/thrombin.pdbqt" in report or "受体结构" in report, report[-1500:]


def _unused(_: List[Any]) -> None:  # pragma: no cover - 占位，避免 flake 未使用告警
    return None
