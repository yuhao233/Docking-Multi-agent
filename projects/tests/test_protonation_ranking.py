"""质子化态策略（运行级）+ 推荐化合物排行（综合分 + Agent 理由）的回归护栏。

对应本轮需求与真实缺陷：
  1. **质子化**：库里大量盐/羧酸根/多质子化碱，同一分子的离子态与中性态对接行为差别很大；
     系统过去只在结果里告警「请确认质子化态」，却不提供任何统一口径 → 同一批筛选里混着两种化学形式。
     现在策略是**运行级**的（`neutralize` 默认 / `keep`），只中和带净电荷的分子，
     逐分子记录 `{policy, applied, charge_before, charge_after, method}`，原始 SMILES 始终保留，
     且**理化性质与对接用同一种形式**。
  2. **推荐排行**：只看对接分数会把"大而黏"的分子排到最前；综合分把亲和力、配体效率、
     类药性与理化窗口按透明权重合成，Agent 通过 `submit_recommendations` 只补"为什么"。
  3. **报告不重复**：协调 Agent 的整段报告只摘录「结论/建议」类小节。
"""
from __future__ import annotations

import json
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


# --------------------------------------------------------------------------- #
# 1. 质子化：核心处理
# --------------------------------------------------------------------------- #
def test_apply_protonation_only_touches_charged_species() -> None:
    from docking_agent.core.ligands import apply_protonation

    neutral, info = apply_protonation("CCO", "neutralize")
    assert neutral == "CCO" and info["applied"] is False and "无需处理" in info["note"]

    fixed, info = apply_protonation("CC(=O)[O-]", "neutralize")
    assert fixed == "CC(=O)O"
    assert info["applied"] is True and info["charge_before"] == -1 and info["charge_after"] == 0
    assert info["method"] == "rdkit.Uncharger" and info["policy"] == "neutralize"

    double, info = apply_protonation("O=C([O-])CCCCC(=O)[O-]", "neutralize")
    assert double == "O=C(O)CCCCC(=O)O" and info["charge_before"] == -2

    # 季铵是永久正电荷：中和不了就**如实说**，不假装处理过
    quat, info = apply_protonation("C[N+](C)(C)C", "neutralize")
    assert quat == "C[N+](C)(C)C" and info["applied"] is False and "无法中和" in info["note"]

    kept, info = apply_protonation("CC(=O)[O-]", "keep")
    assert kept == "CC(=O)[O-]" and info["policy"] == "keep" and info["applied"] is False


def test_describe_ligand_records_protonation_provenance() -> None:
    from docking_agent.core.ligands import describe_ligand

    d = describe_ligand("CC(=O)[O-].[Na+]", protonation="neutralize")
    assert d["smiles"] == "CC(=O)O", d
    assert d["original_smiles"] == "CC(=O)[O-].[Na+]"
    assert d["input_smiles"] == "CC(=O)[O-]"
    prot = d["protonation"]
    assert prot["applied"] and prot["charge_before"] == -1 and prot["charge_after"] == 0
    assert d["facts"]["formal_charge"] == 0 and d["facts"]["formal_charge_input"] == -1
    assert any("质子化态已按运行级策略调整" in w for w in d["warnings"])

    keep = describe_ligand("CC(=O)[O-]", protonation="keep")
    assert keep["smiles"] == "CC(=O)[O-]"
    assert any("当前策略 keep" in w for w in keep["warnings"])


def test_properties_use_same_form_as_docking() -> None:
    from docking_agent.core.chemistry import compute_properties

    prop = compute_properties("CC(=O)[O-]", protonation="neutralize")
    assert prop["smiles"] == "CC(=O)[O-]", "smiles 必须保持原始输入（合并/排序的稳定主键）"
    assert prop["protonated_smiles"] == "CC(=O)O", "实际参与计算的形式要单独给出"
    assert prop["protonation"]["applied"] is True

    kept = compute_properties("CC(=O)[O-]", protonation="keep")
    assert kept["smiles"] == "CC(=O)[O-]" and "protonated_smiles" not in kept


def test_policy_layers_explicit_then_run_request_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.core.ligands import _protonation_policy
    from docking_agent.runs import current_run

    class _Run:
        data = {"request": {"protonation": "keep"}}

    monkeypatch.setenv("LIGAND_PROTONATION", "neutralize")
    token = current_run.set(_Run())
    try:
        assert _protonation_policy() == "keep"          # 运行请求优先于环境变量
        assert _protonation_policy("neutralize") == "neutralize"  # 显式参数优先于运行请求
    finally:
        current_run.reset(token)
    assert _protonation_policy() == "neutralize"        # 没有运行上下文 → 环境变量


# --------------------------------------------------------------------------- #
# 2. 推荐排行：综合分、门槛、理由匹配
# --------------------------------------------------------------------------- #
def _row(name: str, aff: float | None, **kw: Any) -> dict:
    row = {"name": name, "smiles": kw.pop("smiles", "CCO"), "affinity_kcal_mol": aff,
           "heavy_atoms": kw.pop("heavy_atoms", 20), "molecular_weight": kw.pop("mw", 300.0),
           "logP": kw.pop("logp", 2.0), "tpsa": kw.pop("tpsa", 60.0),
           "rotatable_bonds": kw.pop("rot", 4), "lipinski_violations": kw.pop("viol", 0),
           "drug_likeness_pass": kw.pop("viol", 0) <= 1}
    row.update(kw)
    return row


def test_weights_parse_and_fall_back() -> None:
    from docking_agent.reporting.recommend import DEFAULT_WEIGHTS, parse_weights

    w, notes = parse_weights("0.5,0.2,0.2,0.1")
    assert notes == [] and w["affinity"] == 0.5 and abs(sum(w.values()) - 1) < 1e-9

    named, _ = parse_weights("affinity=0.6,le=0.2,lipinski=0.1,physchem=0.1")
    assert named["ligand_efficiency"] == 0.2 and named["drug_likeness"] == 0.1

    norm, notes = parse_weights("2,1,1,0")           # 自动归一化
    assert abs(sum(norm.values()) - 1) < 1e-9 and any("归一化" in n for n in notes)

    fallback, notes = parse_weights("这不是权重")
    assert fallback == DEFAULT_WEIGHTS and notes


def test_recommendations_gate_reasons_and_exclusions() -> None:
    from docking_agent.reporting.recommend import build_recommendations

    rows = [
        _row("强且类药", -10.0, smiles="AAA", heavy_atoms=30),          # A 级候选
        _row("弱但类药", -3.0, smiles="BBB", heavy_atoms=5),            # 亲和力门槛 → 封顶 C
        _row("大而黏", -11.5, smiles="CCC", heavy_atoms=90, mw=1269.0, tpsa=300.0,
             rot=22, viol=3, logp=2.0, drug_likeness_pass=False),
        {"name": "对接失败", "smiles": "DDD", "error": "docking 失败"},
    ]
    rec = build_recommendations(rows, top_n=2, reasons=[
        {"name": "强且类药", "reason": "亲和力 −10.0 且 LE 0.33", "suggestion": "优先复算"},
        {"name": "不存在的分子", "reason": "编造"},
    ])
    assert rec["status"] == "ok" and rec["scored_total"] == 3 and rec["excluded_total"] == 1
    assert [r["name"] for r in rec["rows"]] == ["强且类药", "大而黏"]      # top_n 生效、按综合分降序
    assert rec["rows"][0]["agent_reason"].startswith("亲和力")
    assert rec["rows"][0]["grade"] == "A"
    assert [u["name"] for u in rec["unmatched_reasons"]] == ["不存在的分子"]
    weak = [r for r in [rec["rows"][0]] if r["name"] == "弱但类药"]
    assert weak == [] or weak[0]["grade"] == "C"
    assert any("亲和力弱于" in n for n in rec["notes"])
    assert any("未进入排行" in n for n in rec["notes"])

    gated = build_recommendations(rows)["rows"]
    weak_row = [r for r in gated if r["name"] == "弱但类药"][0]
    assert weak_row["grade"] == "C" and "封顶" in weak_row["grade_gate"]
    assert any("封顶" in tip for tip in weak_row["suggestions"])


def test_recommend_tools_roundtrip(tmp_path: Path) -> None:
    """工具链：recommend_compounds 算排行 → submit_recommendations 只接受在榜分子。"""
    from docking_agent.agents import tool_io
    from docking_agent.agents.blackboard import Blackboard, current_blackboard
    from docking_agent.runs import current_run
    from docking_agent.tools.recommend import recommend_compounds, submit_recommendations

    class _Run:
        id = "REC-RUN"

        def __init__(self) -> None:
            self.data: dict = {}
            self.dir = tmp_path

        def save(self) -> None:
            return None

        def log(self, *_a: Any, **_k: Any) -> None:
            return None

        def write_json(self, name: str, obj: Any, **kw: Any) -> Path:
            path = self.dir / (kw.get("rel_path") or f"{name}.json")
            path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            return path

        def write_bytes(self, *_a: Any, **_k: Any) -> None:  # pragma: no cover
            return None

        def add_artifact(self, *_a: Any, **_k: Any) -> None:  # pragma: no cover
            return None

    run = _Run()
    board = Blackboard("REC-BOARD")
    token, btoken = current_run.set(run), current_blackboard.set(board)
    try:
        tool_io.record("docking", {"status": "ok", "receptors": [{"results": [
            _row("甲", -9.5, smiles="AAA", heavy_atoms=30),
            _row("乙", -7.0, smiles="BBB", heavy_atoms=25),
            {"name": "PositiveControl", "smiles": "PC", "affinity_kcal_mol": -6.0},
        ]}]})
        out = json.loads(recommend_compounds.func(top_n=2))
        assert out["status"] == "ok" and len(out["rows"]) == 2
        assert all(r["name"] != "PositiveControl" for r in out["rows"]), "阳性对照不进推荐排行"
        assert "submit_recommendations" in out["next_step"]
        assert "rank" in out["ordering"] and "不要按亲和力重排" in out["ordering"]

        bad = json.loads(submit_recommendations.func(json.dumps(
            [{"name": "甲", "reason": "亲和力 −9.5，LE 0.32，Lipinski 0 违例"},
             {"name": "查无此物", "reason": "编造"}], ensure_ascii=False)))
        assert bad["status"] == "partial" and bad["saved"] == 1
        assert [u["name"] for u in bad["unmatched"]] == ["查无此物"]
        assert run.data["recommendation_reasons"][0]["name"] == "甲"

        again = json.loads(recommend_compounds.func(top_n=2))
        assert again["rows"][0]["agent_reason"].startswith("亲和力")
        stored = run.data["recommendations"]["rows"]
        assert any(r["name"] == "甲" for r in stored)

        empty = json.loads(submit_recommendations.func("[]"))
        assert empty["status"] == "error"
    finally:
        current_run.reset(token)
        current_blackboard.reset(btoken)


# --------------------------------------------------------------------------- #
# 3. 报告：新章节、结构图、去重
# --------------------------------------------------------------------------- #
def _demo_result(narrative: str = "") -> dict:
    rows = [
        {**_row("Fluralaner", -10.14, smiles="F", heavy_atoms=38, mw=556.3, logp=5.6, tpsa=60.1,
                viol=1),
         "engine": "vina", "exhaustiveness": 12, "n_poses": 1, "seed": 42,
         "seed_policy": "session", "box_group": "main", "box_size": [22, 22, 22],
         "ligand_facts": {"protonation": {"policy": "neutralize", "applied": False,
                                          "charge_before": 0, "charge_after": 0}},
         "similarity_to_positive_control": 0.71},
        {**_row("Adipic acid", -4.2, smiles="A", heavy_atoms=10, mw=146.1, logp=0.2, tpsa=74.6),
         "engine": "vina", "exhaustiveness": 12, "n_poses": 1, "seed": 42,
         "seed_policy": "session", "box_group": "main", "box_size": [22, 22, 22],
         "ligand_facts": {"protonation": {"policy": "neutralize", "applied": True,
                                          "charge_before": -2, "charge_after": 0}}},
    ]
    return {"ranking": rows,
            "molecules": [{"name": r["name"], "smiles": r["smiles"]} for r in rows],
            "positive_control": {"name": "PositiveControl", "smiles": "PC",
                                 "affinity_kcal_mol": -7.5},
            "notes": [], "param_plan": {}, "task_spec": {},
            "agent_recommendations": [{"name": "Fluralaner", "reason": "亲和力最优且 LE 合理",
                                       "suggestion": "提高 exhaustiveness 复算"}]}


def test_report_has_ranking_section_with_reasons_and_no_duplicate_dump() -> None:
    from docking_agent.reporting.report import build_markdown_report

    narrative = ("## 分子对接筛选报告\n\n### 1. 系统与参数配置摘要\n\n参数一堆（这是复读）\n\n"
                 "### 2. 筛选后分子列表及排序\n\n| 分子 | 分数 |\n| --- | --- |\n| X | -1 |\n\n"
                 "### 6. 优化建议\n\n1. 补做阳性对照。\n\n"
                 "**一句话结论**：Fluralaner 最优。\n")
    md = build_markdown_report(_demo_result(), kind="agent", run_id="T",
                               agent_narrative=narrative, artifacts=[])
    assert "### 3.1 推荐化合物排行" in md
    assert "### 3.2 优于阳性对照的分子" in md
    assert "**推荐理由**：亲和力最优且 LE 合理" in md
    # 每个推荐分子的 2D 结构卡（结构挨着数据），文件名与产物名一致（charts/<name>.png）
    assert "2D 结构 + 关键指标" in md
    assert "charts/recommend_card_01.png" in md, "结构卡文件名必须与产物名一致（charts/<name>.png）"
    # 质子化策略与逐分子溯源进入参数节
    assert "质子化态策略" in md and "charge_input" in md
    # 去重：数据复读小节不得出现在报告里，结论类小节保留
    assert "系统与参数配置摘要" not in md
    assert "筛选后分子列表及排序" not in md
    assert "优化建议" in md and "补做阳性对照" in md
    assert "一句话结论" in md
    # 无裸 URL
    assert "http://" not in md and "https://" not in md


def test_ranking_csv_carries_protonation_columns() -> None:
    from docking_agent.reporting.tables import build_ranking_csv

    csv_text = build_ranking_csv(_demo_result()["ranking"], {"affinity_kcal_mol": -7.5})
    header = csv_text.splitlines()[0]
    assert "protonation_policy" in header and "charge_input" in header and "charge_used" in header
    assert "neutralize" in csv_text


def test_extract_agent_conclusions_keeps_one_line_summary() -> None:
    from docking_agent.reporting.content import extract_agent_conclusions

    out = extract_agent_conclusions(
        "### 1. 参数配置摘要\n\n一堆参数\n\n### 7. 数据可用性说明\n\n数字若干\n\n"
        "**一句话结论**：分子 A 最优。\n")
    assert "参数配置摘要" not in out
    assert "一句话结论" in out
    assert extract_agent_conclusions("没有标题的一段结论") == "没有标题的一段结论"


def test_property_chart_top_only_and_recommend_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    """理化性质空间图只画 top（与排序同集合）；推荐 2D 结构图能真实生成 PNG。"""
    import matplotlib

    matplotlib.use("Agg")
    from docking_agent.reporting.charts import property_scatter_chart, recommendation_structure_grid

    monkeypatch.setenv("RECOMMEND_TOP_N", "3")
    rows = [_row(f"M{i}", -5.0 - i, smiles="CCO", mw=200.0 + i * 40, logp=1.0 + i,
                 heavy_atoms=15) for i in range(9)]
    png = property_scatter_chart(rows, top_only=True)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"

    rec_rows = [dict(r, rank=i + 1, composite=0.7 - i * 0.05, grade="A") for i, r in enumerate(rows)]
    grid = recommendation_structure_grid(rec_rows)
    assert grid and grid[:8] == b"\x89PNG\r\n\x1a\n"

    # 「只画 top」的集合口径：按综合分取前 N（与第 3.1 节同一集合），不是整库
    from docking_agent.reporting.charts import _top_property_points

    picked = _top_property_points(rec_rows, 3)
    assert [p["name"] for p in picked] == [r["name"] for r in rec_rows[:3]]
    assert len(_top_property_points([_row("A", -1.0)], 3)) == 1          # 不足 N 个时全画
    assert len(_top_property_points(rows, 0)) == len(rows)               # limit<=0 = 不裁剪


def test_protonated_molecules_still_join_with_docking_rows() -> None:
    """真实缺陷（本轮 e2e 实测）：性质按中和后的形式返回时主键漂移，
    带电分子的分子量/logP/Lipinski 在排序表与推荐评分里**整列为空**。

    主键纪律：`smiles` = 原始输入（对接行也用它），中和后的形式放 `protonated_smiles`。
    """
    from docking_agent.core import merge_and_rank
    from docking_agent.core.chemistry import compute_properties
    from docking_agent.reporting.tables import rank_molecules

    charged = "O=C([O-])CCCCC(=O)[O-]"
    props = [compute_properties(charged)]
    docking = [{"name": "Adipate", "smiles": charged, "affinity_kcal_mol": -4.98,
                "engine": "vina", "exhaustiveness": 4, "box_group": "main",
                "ligand_facts": {"protonation": props[0]["protonation"]}}]

    rows = rank_molecules(merge_and_rank(props, docking, {}))
    assert len(rows) == 1, f"性质行与对接行必须合并成一行，实际 {len(rows)} 行"
    row = rows[0]
    assert row["molecular_weight"] and row["affinity_kcal_mol"] == -4.98, row
    assert row.get("lipinski_violations") == 0, row
    assert row["smiles"] == charged, "排序行的 smiles 仍是原始输入（可回溯）"

    from docking_agent.reporting.recommend import build_recommendations

    rec = build_recommendations(rows)
    assert rec["rows"][0]["missing"] == [], f"质子化不该让分量缺失：{rec['rows'][0]['missing']}"
    assert rec["rows"][0]["components"]["physchem"] > 0


# --------------------------------------------------------------------------- #
# 4. 目标 pH 质子化（ph 策略）
# --------------------------------------------------------------------------- #
def test_ph_policy_assigns_states_by_target_ph() -> None:
    """按目标 pH 分配质子化态：酸在 pH>pKa 去质子、碱在 pH<pKa 加质子，方向绝不能反。"""
    from docking_agent.core.protonation import apply_protonation

    # 乙酸 pKa 4.5：酸性条件下保持中性，中性以上为阴离子
    assert apply_protonation("CC(=O)O", "ph", 1.5)[0] == "CC(=O)O"
    assert apply_protonation("CC(=O)O", "ph", 7.4)[0] == "CC(=O)[O-]"
    # 乙胺 pKa 10：生理条件下为阳离子，强碱下为中性
    assert apply_protonation("CCN", "ph", 7.4)[0] == "CC[NH3+]"
    assert apply_protonation("CCN", "ph", 10.0)[0] == "CCN"
    # 入口已是离子形式也能纠回目标态（先中和再按 pH 分配）
    assert apply_protonation("CC[NH3+]", "ph", 10.0)[0] == "CCN"
    assert apply_protonation("CC(=O)[O-]", "ph", 1.5)[0] == "CC(=O)O"

    # 甘氨酸 pKa 2.3/9.6 → 生理 pH 下是两性离子：净电荷仍是 0，但**化学形式变了**
    zwit, info = apply_protonation("NCC(=O)O", "ph", 7.4)
    assert zwit == "[NH3+]CC(=O)[O-]"
    assert info["applied"] is True and info["charge_before"] == 0 and info["charge_after"] == 0
    assert {r["name"] for r in info["rules"]} == {"羧酸", "脂肪胺"}

    # 胍（pKa 13）在生理 pH 下保持 +1，且不能被"脂肪胺"规则二次质子化
    guan, ginfo = apply_protonation("NC(N)=N", "ph", 7.4)
    assert ginfo["charge_after"] == 1, ginfo
    assert [r["name"] for r in ginfo["rules"]] == ["胍"]

    # 四氮唑（酸性五元唑）：生理 pH 去质子；咖啡因（N-取代咪唑）保持中性
    assert apply_protonation("c1nnn[nH]1", "ph", 7.4)[0] == "c1nnn[n-]1"
    assert apply_protonation("Cn1c(=O)c2c(ncn2C)n(C)c1=O", "ph", 7.4)[0] \
        == "Cn1c(=O)c2c(ncn2C)n(C)c1=O"
    # 永久电荷（季铵）在 ph 策略下也只能如实保留
    quat, qinfo = apply_protonation("C[N+](C)(C)C", "ph", 7.4)
    assert quat == "C[N+](C)(C)C" and qinfo["charge_after"] == 1


def test_ph_policy_provenance_and_fallbacks() -> None:
    from docking_agent.core.protonation import DEFAULT_PH, PKA_TABLE_VERSION, apply_protonation

    _out, info = apply_protonation("CC(=O)O", "ph", 7.4)
    assert info["policy"] == "ph" and info["ph"] == 7.4
    assert PKA_TABLE_VERSION in info["method"]
    assert info["rules"] and {"name", "pka", "action", "rule"} <= set(info["rules"][0])
    assert "pKa 4.5" in info["rules"][0]["rule"]

    # 非法 pH（非数字 / 0 哨兵 / 越界）回退默认，绝不改口径。
    # 真实事故：接口用 0 表示"未设置"，却与"pH 0"撞车 → 一次运行全部按极端强酸处理。
    for bad in ("abc", 99, 0, 0.0, "0", 0.4):
        assert apply_protonation("CC(=O)O", "ph", bad)[1]["ph"] == DEFAULT_PH, bad
    from docking_agent.core.protonation import PH_MIN

    assert apply_protonation("CC(=O)O", "ph", PH_MIN)[1]["ph"] == PH_MIN
    # keep / neutralize 不受 ph 参数影响
    assert apply_protonation("CC(=O)[O-]", "keep", 7.4)[0] == "CC(=O)[O-]"
    assert apply_protonation("CC(=O)[O-]", "neutralize", 7.4)[0] == "CC(=O)O"


def test_ph_policy_resolved_from_run_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.core.protonation import protonation_ph, protonation_policy
    from docking_agent.runs import current_run

    class _Run:
        data = {"request": {"protonation": "ph", "protonation_ph": 5.5}}

    token = current_run.set(_Run())
    try:
        assert protonation_policy() == "ph"
        assert protonation_ph() == 5.5
        assert protonation_ph(6.0) == 6.0        # 显式参数优先
    finally:
        current_run.reset(token)


def test_properties_keep_key_and_use_ph_form() -> None:
    from docking_agent.core.chemistry import compute_properties

    prop = compute_properties("CCN", "ph", 7.4)
    assert prop["smiles"] == "CCN", "主键仍是原始输入（合并/排序稳定）"
    assert prop["protonated_smiles"] == "CC[NH3+]"
    assert prop["protonation"]["policy"] == "ph" and prop["protonation"]["ph"] == 7.4


def test_dock_library_forwards_ph_policy_to_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """dock_library 必须把 ph 策略与目标 pH 写进 spec（随进程池下发到 worker）。"""
    from docking_agent.core import docking as D

    captured: dict = {}

    def fake_dock_batch(spec: dict, molecules: list, **kwargs: object) -> list:
        captured.update(spec)
        return []

    monkeypatch.setattr(D, "dock_batch", fake_dock_batch)
    out = D.dock_library(
        [{"name": "乙胺", "smiles": "CCN"}], receptor="thrombin", exhaustiveness=1,
        pocket_engine="known_site", protonation="ph", protonation_ph=5.5)

    assert captured.get("protonation") == "ph", captured
    assert captured.get("protonation_ph") == 5.5, captured
    assert any("目标 pH 5.5" in n for n in out.get("notes") or []), out.get("notes")
