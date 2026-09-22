"""大库（上万分子）在 Agent 模式下的上下文安全与数据完整性。

背景（量化）：Agent 的上下文会被分子数线性撑大 ——
  - 分子清单 ~50 B/分子 → 1 万条 ≈ 0.5 MB ≈ 13 万 tokens；
  - 对接结果 ~491 B/分子 → 1 万条 ≈ **4.7 MB ≈ 130 万 tokens**；
  - 子 Agent 还要把结果再输出一遍（输出上限更低）→ 必然截断 → JSON 不合法。
任何上下文窗口都装不下，因此约定：

  工具把**完整结果落盘**（运行目录 + 共享黑板），
  小结果（≤ AGENT_TOOL_TOP_N 条）保持原有完整结构回传，
  大结果只回 `summary + top N + artifacts`（`detail_omitted: true`），
  落盘层与报告工具**从产物/黑板读全量**。

本文件用合成数据（不跑真实 Vina）验证这条链路，包含：
  1. 1 万条对接结果：回传给模型的载荷有界（≤ 64 KB），产物里有全部 1 万条；
  2. 落盘层能从产物恢复全部 1 万条（不依赖模型回显）；
  3. 1 万条分子清单同样有界，且子 Agent 工具能留空参数直接读黑板；
  4. 报告工具留空参数也能从黑板出图出表；
  5. 小库行为完全不变（向后兼容：原有完整结构）。
"""
from __future__ import annotations

from typing import Optional

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

from docking_agent.runtime import tool_io  # noqa: E402
from docking_agent.runs import Run  # noqa: E402

BIG = 10_000
MODEL_PAYLOAD_LIMIT = 64 * 1024      # 单次回传给模型的载荷上限（保守值）


def _smiles(i: int) -> str:
    """生成互不相同的 SMILES（两个取代基轴：1..100 × 0..99 = 1 万种组合）。

    注意不能用「链长」当唯一轴：链长相同但支链不同的写法会被 RDKit 规范化成同一个分子，
    在去重后数量会缩水，测不出「1 万条」。
    """
    a = 1 + (i % 100)
    b = i // 100
    return f"c1ccc({'C' * a})cc1{'C' * b}O"


def _synth_results(n: int = BIG):
    return [{"name": f"MOL{i:05d}", "smiles": _smiles(i),
             "affinity_kcal_mol": -round(3.0 + (i % 90) * 0.1, 2),
             "intermolecular_kcal_mol": -4.1, "intramolecular_kcal_mol": 0.3,
             "torsion_kcal_mol": 0.5, "engine": "vina", "exhaustiveness": 6,
             "pose_file": f"poses/pose_MOL{i:05d}.pdbqt"} for i in range(n)]


def _docking_output(rows):
    return {"status": "ok", "poses_saved": True, "notes": [],
            "receptors": [{"receptor_key": "thrombin", "receptor": "thrombin(1DWC)",
                           "protein": "人α-凝血酶", "pdbqt": "assets/x.pdbqt",
                           "box_center": [31.5, 13.74, 24.36], "box_size": [22.0, 22.0, 22.0],
                           "site": {"source": "实验位点（共晶配体质心）", "engine": "p2rank"},
                           "box_source": "实验位点（共晶配体质心）· p2rank 预测一致（相距 4.5 Å）",
                           "box_chosen_by": "experimental_site", "pockets": [],
                           "box_validation": {"status": "consistent"}, "box_warnings": [],
                           "results": rows}]}


#: `run_ctx`（真实 Run + 黑板上下文）定义在 `tests/conftest.py` —— 非引擎用例也要用它。


# --------------------------------------------------------------------------- #
# 1) 对接结果：回传有界 + 全量落盘
# --------------------------------------------------------------------------- #
def test_big_docking_payload_is_bounded_but_full_detail_is_saved(run_ctx, monkeypatch):
    from docking_agent.tools import docking as dock_tool

    run, board = run_ctx
    rows = _synth_results()
    full = _docking_output(rows)
    monkeypatch.setattr(dock_tool, "dock_library", lambda *a, **k: full)

    out = json.loads(dock_tool.molecular_docking.invoke({"molecules_json": json.dumps(
        [{"name": "x", "smiles": "CCO"}]), "receptor_sources": "thrombin"}))

    # 回传给模型的载荷必须有界
    raw = json.dumps(out, ensure_ascii=False)
    assert len(raw.encode()) <= MODEL_PAYLOAD_LIMIT, f"回传载荷过大：{len(raw.encode())} 字节"
    assert out["detail_omitted"] is True
    assert out["summary"]["molecules"] == BIG
    assert out["summary"]["with_affinity"] == BIG
    assert out["summary"]["affinity"]["best"] == -11.9  # 真实统计，不是编的
    assert len(out["top"]) == tool_io.summary_limit()
    assert out["top"][0]["affinity_kcal_mol"] == -11.9, "top 应按亲和力升序取最优"

    # 完整明细必须落在运行产物里
    saved = json.loads((run.dir / "docking_tool.json").read_text(encoding="utf-8"))
    assert len(saved["receptors"][0]["results"]) == BIG
    assert saved["receptors"][0]["box_source"].startswith("实验位点")


def test_persist_recovers_all_rows_from_tool_artifact(run_ctx, monkeypatch):
    """落盘层必须能从产物恢复全部 1 万条 —— 即使模型一个字都没回显。"""
    from docking_agent.agents import persistence

    run, board = run_ctx
    rows = _synth_results()
    tool_io.record("docking", _docking_output(rows))
    molecules = [{"name": r["name"], "smiles": r["smiles"]} for r in rows]
    tool_io.record("molecules", molecules)

    # 消息历史里**没有**任何结果（模拟模型只回了摘要）
    result = persistence.persist_agent_run(
        run, [], "已按摘要汇报，明细见产物")

    assert result["data_sources"]["docking"] == "tool_file"
    assert result["data_sources"]["molecules"] == "tool_file"
    block = result["docking"]["receptors"][0]
    assert len(block["results"]) == BIG
    assert len(result["molecules"]) == BIG
    assert len(result["ranking"]) == BIG, "融合排序也要基于全量"
    assert result["ranking"][0]["affinity_kcal_mol"] == -11.9
    assert (run.dir / "ranking.csv").is_file()
    assert (run.dir / "report.md").is_file()


# --------------------------------------------------------------------------- #
# 2) 分子清单：回传有界 + 子 Agent 留空读黑板
# --------------------------------------------------------------------------- #
def test_big_molecule_list_is_bounded_and_blackboard_keeps_all(run_ctx, monkeypatch):
    from docking_agent.agents import dispatch

    run, board = run_ctx
    text = "\n".join(f"MOL{i:05d}:{_smiles(i)}" for i in range(BIG))

    out = json.loads(dispatch.import_molecule_library.invoke({"query_or_text": text}))

    assert out["detail_omitted"] is True
    assert out["molecules_total"] == BIG
    assert len(out["molecules"]) == tool_io.summary_limit()
    assert len(json.dumps(out, ensure_ascii=False).encode()) <= MODEL_PAYLOAD_LIMIT
    saved = json.loads((run.dir / "molecules_tool.json").read_text(encoding="utf-8"))
    assert len(saved) == BIG


def test_property_tool_can_run_without_the_list(run_ctx):
    """清单留空时属性工具直接读共享黑板（协调 Agent 不必搬运清单）。"""
    from docking_agent.tools.properties import molecular_property_assessment

    run, board = run_ctx
    board.add_molecules([{"name": "乙醇", "smiles": "CCO"},
                         {"name": "苯", "smiles": "c1ccccc1"}])
    out = json.loads(molecular_property_assessment.invoke({"molecules_json": ""}))
    assert out["status"] == "ok"
    names = {r["name"] for r in out["assessment"]}
    assert names == {"乙醇", "苯"}


def test_binding_tool_can_run_without_args(run_ctx):
    """结合模式工具留空时用黑板上的分子与阳性对照。"""
    from docking_agent.tools.binding import binding_mode_analysis

    run, board = run_ctx
    board.add_molecules([{"name": "苯甲脒", "smiles": "NC(=N)c1ccccc1"}])
    board.set_positive_control("NC(=N)c1ccccc1")
    out = json.loads(binding_mode_analysis.invoke({}))
    assert out["status"] == "ok" and out["results"]


# --------------------------------------------------------------------------- #
# 3) 报告工具：留空也能出产物
# --------------------------------------------------------------------------- #
def test_report_tool_builds_from_blackboard_when_arg_empty(run_ctx):
    from docking_agent.tools.report import generate_screening_report

    run, board = run_ctx
    rows = _synth_results(50)
    board.add_molecules([{"name": r["name"], "smiles": r["smiles"]} for r in rows])
    board.set_properties([{"name": r["name"], "smiles": r["smiles"],
                           "molecular_weight": 46.07, "logP": -0.1, "tpsa": 20.2}
                          for r in rows])
    board.set_docking(rows)

    out = json.loads(generate_screening_report.invoke({"aggregated_json": ""}))
    assert out["status"] == "ok", out
    assert out["screening_csv_url"]
    assert "docking_chart" in out["artifacts"]


def test_report_tool_reports_no_data_clearly(run_ctx):
    from docking_agent.tools.report import generate_screening_report

    out = json.loads(generate_screening_report.invoke({"aggregated_json": ""}))
    assert out["status"] == "no_data" and "没有可生成报告的数据" in out["message"]


# --------------------------------------------------------------------------- #
# 4) 向后兼容：小库行为完全不变
# --------------------------------------------------------------------------- #
def test_small_library_keeps_legacy_payload(run_ctx, monkeypatch):
    from docking_agent.tools import docking as dock_tool

    run, board = run_ctx
    rows = _synth_results(3)
    monkeypatch.setattr(dock_tool, "dock_library", lambda *a, **k: _docking_output(rows))

    out = json.loads(dock_tool.molecular_docking.invoke({"molecules_json": json.dumps(
        [{"name": "x", "smiles": "CCO"}]), "receptor_sources": "thrombin"}))
    assert "receptors" in out and "detail_omitted" not in out, "小库必须保持原有完整结构"
    assert len(out["receptors"][0]["results"]) == 3
    assert len(json.loads((run.dir / "docking_tool.json").read_text(encoding="utf-8"))
               ["receptors"][0]["results"]) == 3


def test_dispatch_required_keys_accept_both_contracts():
    from docking_agent.agents.dispatch import _keys_ok

    assert _keys_ok({"receptors": []}, ("receptors", "summary"), "docking")
    assert _keys_ok({"summary": {}}, ("receptors", "summary"), "docking")
    assert not _keys_ok({"status": "ok"}, ("receptors", "summary"), "docking")
    assert _keys_ok({"assessment": []}, ("assessment",), "property")


def test_view_limit_is_configurable_and_is_not_a_molecule_cap(monkeypatch):
    """AGENT_TOOL_TOP_N 只影响给模型看的条数，不影响对接规模。"""
    monkeypatch.setenv("AGENT_TOOL_TOP_N", "5")
    assert tool_io.summary_limit() == 5
    monkeypatch.setenv("AGENT_TOOL_TOP_N", "9999")
    assert tool_io.summary_limit() == 500, "上限收敛到 500，避免又变成上下文炸弹"


def test_ranking_works_for_docking_only_runs(run_ctx):
    """只跑对接（没有理化性质）时也必须能出排序表 —— 否则报告会出现空档。"""
    from docking_agent.agents import persistence
    from docking_agent.core import merge_and_rank

    merged = merge_and_rank([], [{"name": "A", "smiles": "CCO", "affinity_kcal_mol": -4.2}], {})
    assert len(merged) == 1 and merged[0]["affinity_kcal_mol"] == -4.2

    run, board = run_ctx
    tool_io.record("molecules", [{"name": "A", "smiles": "CCO"}])
    tool_io.record("docking", _docking_output(
        [{"name": "A", "smiles": "CCO", "affinity_kcal_mol": -4.2, "engine": "vina"}]))
    result = persistence.persist_agent_run(run, [], "只做了对接")
    assert len(result["ranking"]) == 1, "仅对接的运行也要有排序结果"
    assert result["data_sources"]["docking"] == "tool_file"


def test_huge_inline_library_is_offloaded_to_a_file(tmp_path):
    """用户直接粘贴上万条 SMILES 时，指令里不能内嵌全量清单（≈13 万 tokens）。"""
    from docking_agent import intake
    from docking_agent.api.schemas import AgentRequest

    run = Run(tmp_path, "R-INTAKE", "agent", {"mode": "manual"})
    text = "\n".join(f"MOL{i:05d}:{_smiles(i)}" for i in range(2000))
    req = AgentRequest(mode="manual", ligands_text=text)

    msg, spec = intake.build_message(req, allow_llm=False, run=run)
    assert len(msg.encode()) < 8 * 1024, f"指令仍然过大：{len(msg.encode())} 字节"
    assert "已写入文件" in msg and "ligands_input.csv" in msg
    assert "MOL00000" in msg, "至少要给前几条预览"
    assert "MOL01999" not in msg, "不得内嵌全量清单"
    saved = (run.dir / "ligands_input.csv")
    assert saved.is_file() and saved.read_text(encoding="utf-8").count("\n") >= 2000

    # 小清单保持原样内嵌（向后兼容）
    small = intake.build_message(AgentRequest(mode="manual", ligands_text="A:CCO"),
                                 allow_llm=False, run=run)[0]
    assert "A:CCO" in small


def test_ligand_readers_accept_colon_format(tmp_path):
    """「名称:SMILES」是系统自己导出的清单格式，.smi/.csv 两种入口都必须能读且保住名称。"""
    from docking_agent.core import read_molecule_file

    rows = [("阿司匹林", "CC(=O)Oc1ccccc1C(=O)O"), ("乙醇", "CCO")]
    smi = tmp_path / "lib.smi"
    smi.write_text("\n".join(f"{n}:{s}" for n, s in rows), encoding="utf-8")
    _fmt, mols = read_molecule_file(str(smi))
    assert {m["name"] for m in mols} == {"阿司匹林", "乙醇"}, mols

    csv_path = tmp_path / "lib.csv"
    csv_path.write_text("name,smiles\n" + "\n".join(f"{n},{s}" for n, s in rows), encoding="utf-8")
    _fmt, mols2 = read_molecule_file(str(csv_path))
    assert {m["name"] for m in mols2} == {"阿司匹林", "乙醇"}, mols2


# --------------------------------------------------------------------------- #
# 对接并发：自适应「进程 × 线程」+ 大分子优先调度
# --------------------------------------------------------------------------- #
def test_concurrency_plan_is_threads_first() -> None:
    """并发规划必须「线程优先」（2026-09-17 实测重标定）。

    实测（32 逻辑核）：64 分子上 4 进程 × 8 线程 = 38.5s，比 24 进程 × 1 线程 = 70.4s 快 1.8×，
    且总 CPU 更少（716 vs 1003 CPU·s）。因此计划必须满足：
      · 线程数取到实测饱和点（≥4，通常 8）；
      · 进程数按「可用核 / 线程数」定（不超订），且不超过分子数与内存护栏；
      · 分子数少时进程数按分子数收缩（2 个分子不该起 4 个进程）。
    """
    from docking_agent.core.docking import machine_profile, plan_concurrency

    profile = machine_profile()
    budget = profile["budget"]
    t_max = min(8, budget)                          # 本机可用的线程上限（8 = Vina 饱和点）

    one = plan_concurrency(1)
    assert one["workers"] == 1, one
    assert one["threads"] == t_max, one              # 单分子把线程打满
    # 计划里必须带上机器画像（部署后能看清"为什么是这个并发"）
    assert one["logical"] == profile["logical"] and one["source"] == profile["source"], one

    small = plan_concurrency(2)
    assert small["workers"] == 2, small              # 两个分子就并行两个进程
    assert small["workers"] * small["threads"] <= budget + small["threads"], small

    many = plan_concurrency(500)
    assert many["threads"] == min(t_max, max(1, budget // 2)), many   # 线程优先
    assert many["workers"] >= 2, many
    assert many["workers"] == plan_concurrency(5000)["workers"], "进程数只由机器决定，不随分子数漂移"
    # 进程数不得超过分子数，也不能超过内存护栏
    assert plan_concurrency(3)["workers"] <= 3
    assert plan_concurrency(1)["workers"] * plan_concurrency(1)["threads"] <= budget


def test_plan_concurrency_respects_explicit_workers(monkeypatch):
    from docking_agent.core.docking import plan_concurrency

    monkeypatch.setenv("DOCKING_WORKERS", "4")
    plan = plan_concurrency(1000)
    assert plan["workers"] == 4, plan
    monkeypatch.setenv("DOCKING_WORKERS", "2")
    assert plan_concurrency(100)["workers"] == 2


def test_ligand_cost_ranks_flexible_molecules_higher():
    """成本估计要能区分柔性长链与刚性小环 —— 决定谁先跑。"""
    from docking_agent.core.docking import _ligand_cost

    assert _ligand_cost("CCO") < _ligand_cost("CCCCCCCCCCCCCCCCCCCC(=O)O")
    assert _ligand_cost("not_a_smiles") == 0


def _fake_worker(item):  # 模块级：只有模块级函数才能 pickle 进 worker 进程
    """假 worker：不真跑 Vina，只回一行结果（用于验证顺序与并发播报）。"""
    return {"name": item[0], "smiles": item[1], "affinity_kcal_mol": -1.0}


def test_dock_batch_reports_concurrency(monkeypatch, caplog):
    """结果里的顺序必须与输入一致，且**实际并发配置**要能观测到（日志播报）。

    注意：所有对接都在 worker 进程里跑（这样任何分子数都能被「停止」杀掉），
    因此这里必须用**模块级**假 worker（局部函数无法 pickle），不能再 monkeypatch 父进程的
    `DockingSession`。
    """
    import logging

    from docking_agent.core import docking as D

    monkeypatch.setattr(D, "_worker_dock", _fake_worker)
    spec = {"key": "t", "name": "t", "pdbqt": "x.pdbqt", "center": [0, 0, 0], "size": [20] * 3}
    mols = [{"name": f"M{i}", "smiles": "CCO"} for i in range(5)]
    with caplog.at_level(logging.INFO):
        out = D.dock_batch(spec, mols, engine="vina", save_poses=False)
    assert [r["name"] for r in out] == [m["name"] for m in mols], "输出顺序必须与输入一致"

    plan = D.plan_concurrency(5)
    assert plan["threads"] >= 1 and plan["workers"] >= 1, plan
    assert plan["workers"] <= 5, plan
    assert "并发规划" in caplog.text, caplog.text
    assert f"{plan['workers']} 个进程 × {plan['threads']} 线程" in caplog.text, caplog.text


def test_scores_are_independent_of_batch_and_workers():
    """同一分子的分数必须与「批次组成 / 提交顺序 / 进程数」无关（可复现性的硬要求）。

    背景：审计时担心 Vina 的随机数流是会话级的 —— 那么同一分子在 8 进程并行批里
    与单独对接时分数会不同，增量筛选（后加分子重跑）就没法对比。
    实测结论：Vina 每次 `dock()` 都按构造种子重新开始采样，分数只取决于
    (受体, 盒子, 分子, 参数)。这条测试把该结论固定下来，防止将来换引擎/改封装后悄悄退化。
    """
    import pytest

    pytest.importorskip("vina")
    from docking_agent.core.docking import dock_batch
    from docking_agent.core.receptors import read_receptor_file

    spec = read_receptor_file(str(PROJECT_ROOT / "assets/receptors/registry/thrombin_1DWC.pdbqt"))
    mols = [
        {"name": "M1", "smiles": "NC(=N)c1ccccc1"},
        {"name": "M2", "smiles": "CC(=O)Oc1ccccc1C(=O)O"},
        {"name": "M3", "smiles": "CCO"},
        {"name": "M4", "smiles": "NCCc1ccc(O)c(O)c1"},
        {"name": "M5", "smiles": "CC(=O)CC(c1ccccc1)c1c(O)c2ccccc2oc1=O"},
        {"name": "M6", "smiles": "CC(C)Cc1ccc(cc1)C(C)C(=O)O"},
        {"name": "M7", "smiles": "c1ccc2ccccc2c1"},
        {"name": "M8", "smiles": "CCCCCCCC(=O)O"},
    ]
    # 8 个分子 → 多进程并行（plan_concurrency: 8 → 8 进程）
    batch = {r["name"]: r for r in dock_batch(spec, mols, exhaustiveness=8, n_poses=1)}
    solo = dock_batch(spec, [mols[4]], exhaustiveness=8, n_poses=1)[0]
    assert batch["M5"]["affinity_kcal_mol"] == solo["affinity_kcal_mol"], \
        f"并行批里的 M5 与单独对接不一致：{batch['M5']['affinity_kcal_mol']} vs {solo['affinity_kcal_mol']}"
    # 拆两个进程池、改变顺序也不能影响结果
    reversed_batch = {r["name"]: r for r in dock_batch(spec, list(reversed(mols)),
                                                      exhaustiveness=8, n_poses=1)}
    assert {k: v["affinity_kcal_mol"] for k, v in batch.items()} == \
        {k: v["affinity_kcal_mol"] for k, v in reversed_batch.items()}, "颠倒提交顺序后分数发生了变化"
    # 每行都报告种子与策略，便于他人复算
    assert batch["M5"]["seed_policy"] == "session" and batch["M5"]["seed"] == 42


# --------------------------------------------------------------------------- #
# A 方案：两阶段漏斗（粗筛 → 精算）+ 精度优先合并 + 分片护栏
# --------------------------------------------------------------------------- #
def test_funnel_settings_and_advice(monkeypatch):
    from docking_agent.runtime import tool_io

    monkeypatch.setenv("AGENT_FUNNEL_MIN", "100")
    monkeypatch.setenv("AGENT_REFINE_TOP_N", "50")
    monkeypatch.setenv("AGENT_FINE_EXHAUSTIVENESS", "8")
    s = tool_io.funnel_settings()
    assert s["funnel_min"] == 100 and s["refine_top_n"] == 50 and s["fine_exhaustiveness"] == 8
    text = tool_io.funnel_advice(5000)
    assert "两阶段漏斗" in text and "exhaustiveness=1" in text and "top_from_previous=50" in text
    assert "不要" in text and "逐片" in text, "必须明确劝阻按数量切片"
    assert tool_io.funnel_advice(10) == "", "小库不该触发漏斗建议"


def test_merge_prefers_finer_precision():
    """漏斗核心：同一分子粗筛+精算两轮结果，必须保留精算那条（否则排序被低精度污染）。"""
    from docking_agent.agents.persistence import _merge_docking

    coarse = {"status": "ok", "receptors": [{"receptor_key": "thrombin", "results": [
        {"name": "A", "smiles": "CCO", "affinity_kcal_mol": -3.1, "pass": "coarse",
         "exhaustiveness": 1}]}]}
    fine = {"status": "ok", "receptors": [{"receptor_key": "thrombin", "results": [
        {"name": "A", "smiles": "CCO", "affinity_kcal_mol": -4.8, "pass": "fine",
         "exhaustiveness": 16, "affinity_coarse": -3.1}]}]}
    merged = _merge_docking({"run_docking": [coarse], "molecular_docking": [fine]})
    rows = merged["receptors"][0]["results"]
    assert len(rows) == 1, "同一分子只能留一条"
    assert rows[0]["affinity_kcal_mol"] == -4.8 and rows[0]["pass"] == "fine"
    assert rows[0]["affinity_coarse"] == -3.1, "粗筛分值要保留便于对比"

    # 反向顺序也要以精算为准
    merged2 = _merge_docking({"run_docking": [fine], "molecular_docking": [coarse]})
    assert merged2["receptors"][0]["results"][0]["affinity_kcal_mol"] == -4.8


def test_merge_backfills_coarse_value_when_missing():
    from docking_agent.agents.persistence import _merge_docking

    coarse = {"receptors": [{"receptor_key": "t", "results": [
        {"name": "A", "smiles": "CCO", "affinity_kcal_mol": -3.0, "pass": "coarse"}]}]}
    fine = {"receptors": [{"receptor_key": "t", "results": [
        {"name": "A", "smiles": "CCO", "affinity_kcal_mol": -4.9, "pass": "fine"}]}]}
    row = _merge_docking({"run_docking": [coarse], "molecular_docking": [fine]})["receptors"][0]["results"][0]
    assert row["affinity_kcal_mol"] == -4.9 and row["affinity_coarse"] == -3.0


def test_refine_picks_top_from_blackboard(run_ctx, monkeypatch):
    """精算轮次从共享黑板取头部 N 个（不经过模型上下文），并标注 pass/affinity_coarse。"""
    from docking_agent.tools import docking as dock_tool

    run, board = run_ctx
    board.set_docking([
        {"name": "good", "smiles": "CCO", "affinity_kcal_mol": -9.0},
        {"name": "mid", "smiles": "CCC", "affinity_kcal_mol": -6.0},
        {"name": "weak", "smiles": "CCCC", "affinity_kcal_mol": -2.0},
    ])
    picked = {}

    def _fake_dock(molecules, **kwargs):
        picked["molecules"] = [m["name"] for m in molecules]
        return _docking_output([{"name": m["name"], "smiles": m["smiles"],
                                 "affinity_kcal_mol": -10.0, "exhaustiveness": 16}
                                for m in molecules])

    monkeypatch.setattr(dock_tool, "dock_library", _fake_dock)
    out = json.loads(dock_tool.molecular_docking.invoke(
        {"molecules_json": "", "top_from_previous": 2, "exhaustiveness": 16,
         "receptor_sources": "thrombin"}))
    assert picked["molecules"] == ["good", "mid"], "必须按亲和力取头部 N 个"
    assert out["status"] == "ok"
    saved = json.loads((run.dir / "docking_tool.json").read_text(encoding="utf-8"))
    rows = saved["receptors"][0]["results"]
    assert all(r["pass"] == "fine" for r in rows)
    assert rows[0]["affinity_coarse"] == -9.0, "要保留粗筛分数"


def test_refine_without_previous_result_says_so(run_ctx, monkeypatch):
    from docking_agent.tools import docking as dock_tool

    run, board = run_ctx
    out = json.loads(dock_tool.molecular_docking.invoke(
        {"molecules_json": "", "top_from_previous": 5}))
    assert out["status"] == "no_previous" and "先做一次全库粗筛" in out["message"]


def test_plan_concurrency_adapts_to_deployed_machine(monkeypatch) -> None:
    """并发规划必须**由部署机器的属性推导**，而不是写死某台机器的值。

    三种真实部署形态都要给出合理配置（探针被替换成模拟值，走的是同一条真实代码路径）：
      · 容器 `--cpus=4`（宿主 32 核）：必须听 cgroup 配额，不能按宿主 32 核规划（否则超订 8 倍）；
      · cpuset 只给 6 核：听 CPU 亲和性；
      · 64 核大机器：进程数随核数放大，线程仍不超过 Vina 的饱和点。
    """
    from docking_agent.core import docking as D

    def sim(*, cpu_count: int, affinity: int = 0, quota: Optional[float] = None,
            physical: int = 16, total: int = 1000) -> tuple:
        monkeypatch.setattr(D.os, "cpu_count", lambda: cpu_count)
        monkeypatch.setattr(D.os, "sched_getaffinity",
                            lambda *_a: set(range(affinity or cpu_count)))
        monkeypatch.setattr(D, "_cpu_quota", lambda: quota)
        monkeypatch.setattr(D, "_physical_cores", lambda logical: min(physical, logical))
        return D.machine_profile(), D.plan_concurrency(total)

    prof, plan = sim(cpu_count=32, quota=4.0)          # 容器限 4 核
    assert prof["logical"] == 4 and prof["source"] == "cgroup 配额", prof
    assert plan["workers"] * plan["threads"] <= 5, plan      # 绝不按宿主的 32 核铺开

    prof, plan = sim(cpu_count=32, affinity=6)          # cpuset 6 核
    assert prof["logical"] == 6 and prof["source"] == "CPU 亲和性", prof
    assert plan["workers"] * plan["threads"] <= 7, plan

    prof, plan = sim(cpu_count=128, physical=64)        # 大机器
    assert prof["logical"] == 128, prof
    assert plan["workers"] >= 8 and plan["threads"] <= 8, plan

    prof, plan = sim(cpu_count=1, physical=1)           # 单核兜底
    assert (plan["workers"], plan["threads"]) == (1, 1), plan


def test_persist_merges_every_docking_call_not_only_the_last(run_ctx) -> None:
    """多次对接调用必须全部落盘：产物文件只保存**最后一次**调用的原文，
    历史调用只在消息历史里 —— 两者必须合并，否则分批对接/蛋白质库的早先受体块会被静默丢掉。

    真实场景：协调 Agent 先对受体 A 调一次 run_docking，再对受体 B 调一次；
    `docking_tool.json` 被第二次覆盖，若不合并，报告里就只剩 B。
    """
    from langchain_core.messages import ToolMessage

    from docking_agent.agents import persistence

    run, _board = run_ctx
    round_a = {"status": "ok", "notes": ["A 轮"],
               "receptors": [{"receptor_key": "A", "receptor": "A",
                              "box_size": [22, 22, 22], "box_center": [0, 0, 0],
                              "results": [{"name": "m1", "smiles": "CCO",
                                           "affinity_kcal_mol": -4.0, "engine": "vina",
                                           "exhaustiveness": 6, "box_group": "main"}]}]}
    round_b = {"status": "ok", "notes": ["B 轮"],
               "receptors": [{"receptor_key": "B", "receptor": "B",
                              "box_size": [22, 22, 22], "box_center": [0, 0, 0],
                              "results": [{"name": "m1", "smiles": "CCO",
                                           "affinity_kcal_mol": -6.0, "engine": "vina",
                                           "exhaustiveness": 6, "box_group": "main"}]}]}
    tool_io.record("molecules", [{"name": "m1", "smiles": "CCO"}])
    tool_io.record("docking", round_b)            # 产物 = 最后一次调用
    messages = [ToolMessage(content=json.dumps(round_a, ensure_ascii=False),
                            name="run_docking", tool_call_id="t1"),
                ToolMessage(content=json.dumps(round_b, ensure_ascii=False),
                            name="run_docking", tool_call_id="t2")]

    result = persistence.persist_agent_run(run, messages, "两次对接")

    keys = [b.get("receptor_key") for b in result["docking"]["receptors"]]
    assert keys == ["A", "B"], f"两次调用的受体块都要在（实际 {keys}）"
    assert result["data_sources"]["docking"] == "tool_file+tool_message", result["data_sources"]


def test_persist_keeps_tool_file_source_when_messages_add_nothing(run_ctx) -> None:
    """对照：消息里没有新数据时，来源标注仍是 tool_file（既有契约不漂移）。"""
    from langchain_core.messages import ToolMessage

    from docking_agent.agents import persistence

    run, _board = run_ctx
    payload = {"status": "ok", "receptors": [
        {"receptor_key": "A", "receptor": "A", "box_size": [22, 22, 22], "box_center": [0, 0, 0],
         "results": [{"name": "m1", "smiles": "CCO", "affinity_kcal_mol": -4.0, "engine": "vina",
                      "exhaustiveness": 6, "box_group": "main"}]}]}
    tool_io.record("molecules", [{"name": "m1", "smiles": "CCO"}])
    tool_io.record("docking", payload)
    messages = [ToolMessage(content=json.dumps(payload, ensure_ascii=False),
                            name="run_docking", tool_call_id="t1")]

    result = persistence.persist_agent_run(run, messages, "一次对接")
    assert result["data_sources"]["docking"] == "tool_file", result["data_sources"]
    assert len(result["docking"]["receptors"]) == 1
