"""受体质子化必须与配体**同一目标 pH**（v0.22）。

真实缺口：配体按目标 pH 分配了质子化态，受体却停在 meeko 残基模板的默认态（≈pH 7 固定），
两侧不是同一套化学条件 —— 而 HIS 互变异构、ASP/GLU 质子化直接决定氢键/静电互补
（凝血酶 S1 的 ASP189，PROPKA pKa ≈ 6.6，就是典型例子）。

本文件看护：
  1. pH 规则解析（PROPKA 摘要 → 各残基质子化判定 / HIS 状态从 PQR 读出）；
  2. 主链不全残基的剔除（pdb2pqr 会因此整条失败）；
  3. PQR 归一化（插入码必须拆成独立字段，删掉会造成残基键冲突）；
  4. 端到端：真实受体按 pH 准备出 PDBQT，且不同 pH 给出不同状态；
  5. 工具缺失/失败时必须**回退并如实记录**，绝不假装做过。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

THROMBIN = PROJECT_ROOT / "assets" / "receptors" / "structures" / "thrombin.pdb"


# --------------------------------------------------------------------------- #
# 1. 规则解析（不需要外部工具）
# --------------------------------------------------------------------------- #
def test_propka_summary_parsed_and_states_judged() -> None:
    from docking_agent.core.receptor_ph import parse_propka_summary, titratable_states

    text = ("propka3.0, revision 182\n\nSUMMARY OF THIS PREDICTION\n"
            "     RESIDUE    pKa   pKmodel   ligand atom-type\n"
            "   ASP 189 H    6.60      3.80\n"
            "   GLU  97 H    2.83      4.50\n"
            "   HIS  57 H    7.20      6.50\n"
            "   LYS 107 H   10.40     10.50\n")
    rows = parse_propka_summary(text)
    assert [r["resname"] for r in rows] == ["ASP", "GLU", "HIS", "LYS"]
    assert rows[0]["resnum"] == 189 and rows[0]["chain"] == "H" and rows[0]["pka"] == 6.60

    at74 = titratable_states(rows, 7.4)
    assert at74["by_residue"]["ASP"] == {"total": 1, "protonated": 0}, "pH 7.4 下 Asp189 应去质子化"
    assert at74["by_residue"]["GLU"] == {"total": 1, "protonated": 0}
    assert at74["by_residue"]["HIS"] == {"total": 1, "protonated": 0}, "pH 7.4 > pKa 7.2 → 中性"
    assert at74["by_residue"]["LYS"] == {"total": 1, "protonated": 1}

    at60 = titratable_states(rows, 6.0)
    assert at60["by_residue"]["ASP"] == {"total": 1, "protonated": 1}, "pH 6.0 < pKa 6.6 → 质子化"
    assert at60["by_residue"]["HIS"] == {"total": 1, "protonated": 1}
    # 边界残基要单独点出来（|pKa - pH| < 0.5）
    at70 = titratable_states(rows, 7.0)
    assert any(b["resname"] == "HIS" for b in at70["boundary"])


def test_his_states_read_from_applied_pqr() -> None:
    from docking_agent.core.receptor_ph import his_states_from_pqr

    pqr = ("ATOM 1 N HIS H 57 0.0 0.0 0.0 0.0 1.5\n"
           "ATOM 2 HD1 HIS H 57 0.0 0.0 0.0 0.1 1.0\n"          # HID
           "ATOM 3 N HIS H 41 0.0 0.0 0.0 0.0 1.5\n"
           "ATOM 4 HE2 HIS H 41 0.0 0.0 0.0 0.1 1.0\n"          # HIE
           "ATOM 5 N HIS H 87 0.0 0.0 0.0 0.0 1.5\n"
           "ATOM 6 HD1 HIS H 87 0.0 0.0 0.0 0.1 1.0\n"
           "ATOM 7 HE2 HIS H 87 0.0 0.0 0.0 0.1 1.0\n")         # HIP
    assert his_states_from_pqr(pqr) == {"HID": 1, "HIE": 1, "HIP": 1, "未判定": 0}


def test_incomplete_residues_are_dropped_with_record() -> None:
    """pdb2pqr 遇到主链不全的残基会直接报错退出（1DWC 的 GLY H 246 就是这样）。"""
    from docking_agent.core.receptor_ph import filter_incomplete_residues

    text = ("ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\n"
            "ATOM      2  CA  ALA A   1       1.000   0.000   0.000  1.00  0.00           C\n"
            "ATOM      3  C   ALA A   1       2.000   0.000   0.000  1.00  0.00           C\n"
            "ATOM      4  O   ALA A   1       3.000   0.000   0.000  1.00  0.00           O\n"
            "ATOM      5  CA  GLY B 246       4.000   0.000   0.000  1.00  0.00           C\n")
    filtered, dropped = filter_incomplete_residues(text)
    assert dropped == [{"chain": "B", "resnum": "246", "resname": "GLY"}], dropped
    assert "ALA A   1" in filtered and "GLY B 246" not in filtered


def test_pqr_insertion_codes_split_into_own_field() -> None:
    """插入码必须拆成独立字段：删掉会让 36 与 36A 撞成同一残基键（meeko 报「一个键两个残基名」）。"""
    from docking_agent.core.receptor_ph import normalize_pqr_for_meeko

    text = "ATOM 1 N ILE H 36A 31.4 25.9 19.2 0.03 1.82\n"
    fixed, split = normalize_pqr_for_meeko(text)
    assert split == 1
    assert fixed.split()[5:7] == ["36", "A"], fixed


def test_simple_ion_check() -> None:
    from docking_agent.core.receptor_ph import hetatm_are_simple_ions

    ok, bad = hetatm_are_simple_ions("HETATM    1 ZN   ZN A 200       0.0 0.0 0.0  1.00 0.00\n")
    assert ok and bad == []
    ok, bad = hetatm_are_simple_ions("HETATM    1 FE   HEM A 200       0.0 0.0 0.0  1.00 0.00\n")
    assert not ok and bad == ["HEM"], "有机辅因子不能走 pH 路径（pdb2pqr 无法参数化）"


# --------------------------------------------------------------------------- #
# 2. 端到端（需要 pdb2pqr；缺失时跳过而不是假装通过）
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not THROMBIN.is_file(), reason="缺少测试用受体结构")
def test_receptor_prepared_at_target_ph_and_states_change(tmp_path: Path) -> None:
    from docking_agent.core import receptor_ph

    if not receptor_ph.is_available():
        pytest.skip("本机未安装 pdb2pqr（PDB2PQR_BIN 可指定）")

    text = "".join(l + "\n" for l in THROMBIN.read_text().splitlines() if l.startswith("ATOM"))
    prot = tmp_path / "prot.pdb"
    prot.write_text(text, encoding="utf-8")

    out74 = receptor_ph.prepare_pdbqt_at_ph(str(prot), str(tmp_path / "rec"), 7.4)
    assert out74["ok"], out74.get("error")
    assert Path(out74["pdbqt"]).is_file() and Path(out74["pqr"]).is_file()
    info74: dict = out74["info"]
    assert info74["applied"] and info74["ph"] == 7.4
    assert "pdb2pqr" in str(info74["tool"])
    his74 = info74.get("his_states") or {}
    assert sum(v for k, v in his74.items() if k != "未判定") == 5, his74
    assert (info74.get("titratable") or {}).get("by_residue"), "PROPKA 逐残基质子化必须留痕"
    # 缓存命中：第二次调用不再跑工具
    again = receptor_ph.prepare_pdbqt_at_ph(str(prot), str(tmp_path / "rec"), 7.4)
    assert again["info"].get("cached") is True

    out40 = receptor_ph.prepare_pdbqt_at_ph(str(prot), str(tmp_path / "rec"), 4.0)
    assert out40["ok"], out40.get("error")
    his40 = (out40["info"].get("his_states") or {})
    # pH 4 时所有 HIS 双质子化（HIP）；pH 7.4 时是 HID/HIE —— 受体确实随 pH 变了
    assert his40.get("HIP", 0) == 5 and his74.get("HIP", 0) == 0, (his40, his74)
    glu40 = (out40["info"].get("titratable") or {}).get("by_residue", {}).get("GLU", {})
    glu74 = (info74.get("titratable") or {}).get("by_residue", {}).get("GLU", {})
    assert glu40.get("protonated", 0) > glu74.get("protonated", 0), (glu40, glu74)
    assert receptor_ph.describe(info74).startswith("受体按目标 pH 7.4 准备")


def test_missing_tool_falls_back_and_records_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具不可用时必须回退标准流程，并把「未按 pH 准备」写进溯源（绝不假装做过）。"""
    from docking_agent.core import receptor_ph
    from docking_agent.core.receptors import resolve_receptor_specs

    monkeypatch.setattr(receptor_ph, "pdb2pqr_bin", lambda: "")
    monkeypatch.setitem(sys.modules, "docking_agent.core.receptor_ph", receptor_ph)
    if not THROMBIN.is_file():
        pytest.skip("缺少测试用受体结构")

    specs, notes = resolve_receptor_specs(str(THROMBIN), protonation="ph", ph=7.4)
    info = specs[0].get("receptor_protonation") or {}
    assert info.get("applied") is False and "pdb2pqr" in str(info.get("reason"))
    assert any("未" in n and "对齐" in n for n in notes), notes
    assert Path(specs[0]["pdbqt"]).is_file(), "回退后仍必须给出可用受体"


def test_registry_pdbqt_is_marked_as_not_ph_matched() -> None:
    """注册表预置 PDBQT 的质子化态由文件本身决定 → 必须如实标注，而不是默认"已对齐"。"""
    from docking_agent.core.receptors import resolve_receptor_specs

    specs, notes = resolve_receptor_specs("thrombin", protonation="ph", ph=7.4)
    info = specs[0].get("receptor_protonation") or {}
    assert info.get("applied") is False and "预置 PDBQT" in str(info.get("reason"))
    assert any("质子化" in n for n in notes), notes


def test_report_mentions_receptor_protonation_and_mismatch() -> None:
    from docking_agent.reporting import build_markdown_report

    matched = {"receptor": "thrombin", "receptor_protonation": {
        "applied": True, "policy": "ph", "ph": 7.4, "tool": "pdb2pqr + PROPKA + meeko",
        "his_states": {"HID": 3, "HIE": 2, "HIP": 0}}}
    md = build_markdown_report({"ranking": [], "receptors": [matched]}, kind="pipeline", run_id="T")
    assert "受体质子化（thrombin）：受体按目标 pH 7.4 准备" in md
    assert "质子化态按**目标 pH 重算**" in md

    # 未对齐时必须给出「口径不一致」提示与可执行建议
    mismatched = {"receptor": "thrombin", "receptor_protonation": {
        "applied": False, "policy": "ph", "ph": 7.4, "reason": "注册表预置 PDBQT"}}
    md2 = build_markdown_report(
        {"ranking": [{"name": "A", "smiles": "CCO", "affinity_kcal_mol": -5.0,
                      "ligand_facts": {"protonation": {"policy": "ph", "ph": 7.4, "applied": True,
                                                       "charge_before": 0, "charge_after": 0}}}],
         "receptors": [mismatched]}, kind="pipeline", run_id="T")
    assert "**未按目标 pH 重新准备**" in md2
    assert "两侧质子化条件**不完全一致**" in md2
    assert "PDB2PQR_BIN" in md2


def test_ph_pdbqt_provenance_travels_with_the_file(tmp_path: Path) -> None:
    """只把 pH 产物（.pdbqt）交给下游时，也必须能读出「确实按 pH 准备过」。

    真实场景（Agent 模式）：口袋分析先把受体准备成 PDBQT，对接阶段只拿到这个 PDBQT 路径，
    于是溯源丢失、报告把**做过** pH 处理的受体误报成「未按 pH 准备」。
    修法：pH 产物带同名溯源侧车，`_pdbqt_spec` 读它（位点侧车仍挂在基础名上，两份都读）。
    """
    from docking_agent.core import receptor_ph
    from docking_agent.core.receptors import resolve_receptor_specs

    if not receptor_ph.is_available():
        pytest.skip("本机未安装 pdb2pqr（PDB2PQR_BIN 可指定）")
    text = "".join(l + "\n" for l in THROMBIN.read_text().splitlines() if l.startswith("ATOM"))
    prot = tmp_path / "prot.pdb"
    prot.write_text(text, encoding="utf-8")
    out = receptor_ph.prepare_pdbqt_at_ph(str(prot), str(tmp_path / "rec"), 7.4)
    assert out["ok"], out.get("error")
    sidecar = Path(receptor_ph.sidecar_path(str(tmp_path / "rec"), 7.4))
    assert sidecar.is_file(), "pH 产物必须带溯源侧车"

    specs, _notes = resolve_receptor_specs(out["pdbqt"], protonation="ph", ph=7.4)
    info = specs[0].get("receptor_protonation") or {}
    assert info.get("applied") is True and info.get("ph") == 7.4, info
    assert info.get("his_states"), info

    # 外来 PDBQT（没有侧车）时必须如实说「无法确认」，不得谎称已对齐
    alien = tmp_path / "alien.pdbqt"
    alien.write_text(Path(out["pdbqt"]).read_text(encoding="utf-8"), encoding="utf-8")
    specs2, _ = resolve_receptor_specs(str(alien), protonation="ph", ph=7.4)
    info2 = specs2[0].get("receptor_protonation") or {}
    assert info2.get("applied") is False and "无法确认" in str(info2.get("reason")), info2


# --------------------------------------------------------------------------- #
# 3. 准备阶梯（8ZE2 类"几何异常 → meeko 误判残基间共价键"必须能自愈）
# --------------------------------------------------------------------------- #
def test_parse_unmatched_residues_from_meeko_output() -> None:
    from docking_agent.core.receptor_ph import parse_unmatched_residues

    strict = ("Error: Creation of data structure for receptor failed.\n"
              "- Template matching failed for: ['A:402', 'A:406', 'B:402']")
    assert parse_unmatched_residues(strict) == ["A:402", "A:406", "B:402"]
    assert parse_unmatched_residues("- Template matching failed for: [\"C:36A\"]") == ["C:36A"]
    assert parse_unmatched_residues("no problem here") == []


def test_prep_ladder_prefers_noopt_over_deleting_residues(monkeypatch: pytest.MonkeyPatch,
                                                          tmp_path: Path) -> None:
    """第一档（氢键优化）失败时，应退到**几何摆氢**保住全部残基，而不是直接删残基。

    真实案例（8ZE2）：pdb2pqr 的氢键优化把 THR406 的羟基氢摆到 ILE402 羰基氧 1.15 Å 处，
    meeko 的距离法键感知判成残基间共价键 → 整条 PQR 被拒；`--noopt` 后全部残基保留。
    """
    from docking_agent.core import receptor_ph

    prot = tmp_path / "prot.pdb"
    prot.write_text("ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\n"
                    "ATOM      2  CA  ALA A   1       1.000   0.000   0.000  1.00  0.00           C\n"
                    "ATOM      3  C   ALA A   1       2.000   0.000   0.000  1.00  0.00           C\n"
                    "ATOM      4  O   ALA A   1       3.000   0.000   0.000  1.00  0.00           O\n",
                    encoding="utf-8")
    calls: list = []

    def fake_pdb2pqr(binary, filtered, pqr, ph, extra) -> tuple:
        calls.append(("pdb2pqr", tuple(extra)))
        # 用注释标记这一档是"氢键优化"还是"几何摆氢"，供 meeko 假实现区分
        marker = "REMARK noopt\n" if "--noopt" in extra else "REMARK opt\n"
        Path(pqr).write_text(marker + "ATOM 1 N ALA A 1 0.0 0.0 0.0 0.0 1.5\n", encoding="utf-8")
        Path(str(pqr).replace(".pqr", ".propka")).write_text(
            "SUMMARY OF THIS PREDICTION\n   HIS  57 H    7.20      6.50\n", encoding="utf-8")
        return True, ""

    def fake_meeko(meeko_pqr, out_base_ph, extra) -> tuple:
        calls.append(("meeko", tuple(extra)))
        # 只有"几何摆氢"那一档能过（模拟 8ZE2：氢键优化把羟基氢摆到羰基氧 1.15 Å）
        if "REMARK noopt" not in Path(meeko_pqr).read_text(encoding="utf-8"):
            return False, ("- Template matching failed for: ['A:402', 'A:406']"), "meeko 读取 PQR 失败"
        Path(out_base_ph + "_tmp.pdbqt").write_text("ATOM\n", encoding="utf-8")
        return True, "", ""

    monkeypatch.setattr(receptor_ph, "_run_pdb2pqr", fake_pdb2pqr)
    monkeypatch.setattr(receptor_ph, "_run_meeko_pqr", fake_meeko)
    monkeypatch.setattr(receptor_ph, "pdb2pqr_bin", lambda: "/fake/pdb2pqr")

    out = receptor_ph.prepare_pdbqt_at_ph(str(prot), str(tmp_path / "rec"), 7.4)
    assert out["ok"], out.get("error")
    info = out["info"]
    assert info["variant"] == "noopt", info
    assert info["best_variant_failed"] is True
    assert info.get("dropped_bad_residues") == [], "几何摆氢这一档不该丢任何残基"
    assert [(a["variant"], a["ok"]) for a in info["attempts"]] == [("opt", False), ("noopt", True)]
    assert ("pdb2pqr", ("--noopt",)) in calls
    # 溯源必须落盘（下游只拿 PDBQT 时也要能读到）
    side = receptor_ph.read_sidecar(str(tmp_path / "rec"), 7.4)
    assert side["receptor_protonation"]["variant"] == "noopt"


def test_prep_ladder_reports_residues_when_it_must_delete(monkeypatch: pytest.MonkeyPatch,
                                                          tmp_path: Path) -> None:
    """连几何摆氢也失败时才删残基，并且**逐个上报**（绝不静默少一段受体）。"""
    from docking_agent.core import receptor_ph

    prot = tmp_path / "prot.pdb"
    prot.write_text("ATOM      1  N   ALA A   1       0.000   0.000   0.000  1.00  0.00           N\n"
                    "ATOM      2  CA  ALA A   1       1.000   0.000   0.000  1.00  0.00           C\n"
                    "ATOM      3  C   ALA A   1       2.000   0.000   0.000  1.00  0.00           C\n"
                    "ATOM      4  O   ALA A   1       3.000   0.000   0.000  1.00  0.00           O\n",
                    encoding="utf-8")

    def fake_pdb2pqr(binary, filtered, pqr, ph, extra) -> tuple:
        Path(pqr).write_text("ATOM 1 N ALA A 1 0.0 0.0 0.0 0.0 1.5\n", encoding="utf-8")
        return True, ""

    def fake_meeko(meeko_pqr, out_base_ph, extra) -> tuple:
        # 前两档（严格模板匹配）都失败，只有 -x（丢弃不匹配残基）这一档能过
        if "-x" not in extra:
            return False, "- Template matching failed for: ['C:402', 'C:406']", "meeko 失败"
        Path(out_base_ph + "_tmp.pdbqt").write_text("ATOM\n", encoding="utf-8")
        return True, "- Template matching failed for: ['C:402', 'C:406']", ""

    monkeypatch.setattr(receptor_ph, "_run_pdb2pqr", fake_pdb2pqr)
    monkeypatch.setattr(receptor_ph, "_run_meeko_pqr", fake_meeko)
    monkeypatch.setattr(receptor_ph, "pdb2pqr_bin", lambda: "/fake/pdb2pqr")

    out = receptor_ph.prepare_pdbqt_at_ph(str(prot), str(tmp_path / "rec"), 7.4)
    assert out["ok"] and out["info"]["variant"] == "delete"
    assert out["info"]["dropped_bad_residues"] == ["C:402", "C:406"]
    assert "丢弃的残基" in receptor_ph.describe(out["info"])


@pytest.mark.skipif(not (PROJECT_ROOT / "assets" / "uploads").is_dir(), reason="缺少上传目录")
def test_real_8ze2_geometrically_odd_region_is_handled(tmp_path: Path) -> None:
    """真实结构回归：8ZE2 的 ILE402···THR406（晶体 O···O 仅 2.15 Å）曾让 pH 准备整条失败。

    期望：阶梯自动退到几何摆氢，**不丢任何残基**，且 HIS/可滴定残基溯源齐全。
    """
    from docking_agent.core import receptor_ph

    cands = sorted((PROJECT_ROOT / "assets" / "uploads").glob("*8ZE2*.pdb"))
    if not cands or not receptor_ph.is_available():
        pytest.skip("缺少 8ZE2 测试结构或本机未安装 pdb2pqr")
    text = "".join(l + "\n" for l in cands[0].read_text(errors="ignore").splitlines()
                   if l.startswith("ATOM"))
    prot = tmp_path / "8ze2.pdb"
    prot.write_text(text, encoding="utf-8")
    out = receptor_ph.prepare_pdbqt_at_ph(str(prot), str(tmp_path / "rec"), 7.4)
    assert out["ok"], out.get("error")
    info = out["info"]
    assert info["variant"] in ("opt", "noopt"), info
    assert info.get("dropped_bad_residues") == [], "这一档不该丢残基"
    assert (info.get("his_states") or {}).get("HID"), info
    assert (info.get("titratable") or {}).get("by_residue"), info


def test_uploaded_receptor_wins_over_registry_name_in_dispatch(monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    """有上传受体时，子 Agent 传来的注册表受体名必须被忽略（receptor_file 优先）。

    真实事故（run 20260918-094607-3195）：用户上传 8ZE2，协调流程又对默认 thrombin 跑了一遍 ——
    白跑 6 个分子，报告里多出一个受体块，用户会以为跑了两个靶点。
    """
    from docking_agent.runs import current_run
    from docking_agent.tools import dispatch

    sent: list = []

    class _Agent:
        def invoke(self, payload, config=None, context=None) -> dict:  # noqa: ANN001
            sent.append(payload["messages"][0].content)
            return {"messages": [type("M", (), {"content": '{"status":"ok"}'})()]}

    monkeypatch.setattr(dispatch, "get_docking_agent", lambda: _Agent())
    monkeypatch.setattr(dispatch, "unresolved_receptor_message", lambda *_a, **_k: "")

    class _Run:
        id = "REC-WINS"

        def __init__(self) -> None:
            self.data = {"request": {"receptor_file": str(tmp_path / "rec.pdb"),
                                     "receptor": "thrombin"}}
            self.dir = tmp_path

        def log(self, *_a, **_k) -> None:  # pragma: no cover
            return None

    token = current_run.set(_Run())
    try:
        dispatch.run_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]',
                                  receptor_file=str(tmp_path / "rec.pdb"),
                                  receptor_sources="thrombin")
    finally:
        current_run.reset(token)

    assert sent, "应真的调用了对接子 Agent"
    message = sent[0]
    assert "receptor_sources=thrombin 已被忽略" in message, message[:400]
    assert "只对接上传的那个受体" in message, message[:400]
