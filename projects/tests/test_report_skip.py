"""未执行任何计算的运行不产出规范报告。

调用方只发了一句「你好」时，受理层 `decision=reject`、零工具调用，落盘层却仍写出
11 KB 全空表格的报告、4 张空图与 328 KB PDF，网页还把这次 no-op 运行当成
「规范报告（report.md · 唯一权威版）」挂在对话气泡上，报告里出现
「本次输入 0 个分子全部成功对接」「图 1 图片加载失败」这类噪声。

约定：
- 只有实际算过东西（分子库 / 理化性质 / 对接行 / 口袋分析 / 排序）时才写
  排序 CSV、图表、`report.md`、PDF；
- 否则只留运行记录、协调 Agent 的对话原文（`agent_report.md`）与 `result.json`，
  并在 `run.json` 里写 `no_report_reason` 说明没有报告的原因；
- 正常的对接流程照旧产出报告，门禁不扩大到有结果的运行。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest


def _tool_msg(name: str, payload: Dict[str, Any]) -> Any:
    from langchain_core.messages import ToolMessage

    return ToolMessage(content=json.dumps(payload, ensure_ascii=False), name=name,
                       tool_call_id="t-" + name)


def test_greeting_run_writes_no_report(tmp_path: Any) -> None:
    """「你好」加零工具输出与 decision=reject：不产报告、图表与 PDF，但保留对话原文。"""
    from docking_agent.agents.persistence import persist_agent_run
    from docking_agent.runs import Run

    run = Run(tmp_path, "R-GREET", "agent", {"mode": "chat"})
    run.set(task_spec={"task_type": "screening", "decision": "reject"})
    result = persist_agent_run(run, [], "你好！我是分子筛选与优化协作系统……")
    report_dir = tmp_path / "R-GREET"

    assert not (report_dir / "report.md").is_file(), "没有计算就不该有规范报告"
    assert not (report_dir / "report.pdf").is_file(), "也不该有 PDF"
    assert not (report_dir / "ranking.csv").is_file(), "不该写空的排序 CSV"
    assert not (report_dir / "charts").is_dir(), "不该生成空图表"
    assert (report_dir / "agent_report.md").is_file(), "协调 Agent 的对话原文仍要留档"
    assert (report_dir / "result.json").is_file(), "result.json 供界面读取，必须保留"

    assert run.data.get("no_report_reason"), "run.json 必须说明为什么没有报告"
    assert "reject" in run.data["no_report_reason"]
    # 状态如实标成 no_op（历史列表显示 [ SKIP ]），不写成「成功但什么都没有」的 ok
    assert result.get("no_op") is True and result.get("status") == "no_op", result.get("status")
    # 报告产物登记也不能出现（否则页面仍会显示「报告」页签有内容）
    names = {a.get("name") for a in (run.data.get("artifacts") or [])}
    assert "report.md" not in names and "ranking_csv" not in names
    assert result.get("ranking") == [] and result.get("molecules") == []


def test_accepted_but_empty_run_also_skips_report(tmp_path: Any) -> None:
    """受理通过但未调起任何工具（例如模型直接回答）时不产报告。"""
    from docking_agent.agents.persistence import persist_agent_run
    from docking_agent.runs import Run

    run = Run(tmp_path, "R-EMPTY", "agent", {"mode": "chat"})
    run.set(task_spec={"task_type": "screening", "decision": "accept"})
    persist_agent_run(run, [], "请告诉我候选分子。")
    assert not (tmp_path / "R-EMPTY" / "report.md").is_file()
    reason = str(run.data.get("no_report_reason") or "")
    assert "没有任何工具产出" in reason and "accept" in reason, reason


def test_real_work_still_writes_report(tmp_path: Any) -> None:
    """有实际对接结果时，规范报告照旧产出（门禁不扩大到有结果的运行）。"""
    from docking_agent.agents.persistence import persist_agent_run
    from docking_agent.runs import Run

    block = {"receptor_key": "thrombin", "receptor": "thrombin(1DWC)",
             "box_center": [31.5, 13.74, 24.36], "box_size": [22.0, 22.0, 22.0],
             "box_source": "实验位点（共晶配体质心）",
             "results": [{"name": "乙醇", "smiles": "CCO", "affinity_kcal_mol": -2.8,
                          "engine": "vina", "exhaustiveness": 1}]}
    messages: List[Any] = [
        _tool_msg("import_molecule_library", {"status": "ok", "molecules": [
            {"name": "乙醇", "smiles": "CCO"}]}),
        _tool_msg("run_docking", {"status": "ok", "receptors": [block]}),
    ]
    run = Run(tmp_path, "R-REAL", "agent", {"mode": "chat"})
    run.set(task_spec={"task_type": "screening", "decision": "accept"})
    result = persist_agent_run(run, messages, "已按乙醇完成对接。")
    report_dir = tmp_path / "R-REAL"

    assert result.get("ranking"), "应当算出排序"
    assert (report_dir / "report.md").is_file(), "有真实结果就必须有规范报告"
    assert (report_dir / "ranking.csv").is_file()
    assert not run.data.get("no_report_reason"), "有报告时不该再写 no_report_reason"
    text = (report_dir / "report.md").read_text(encoding="utf-8")
    assert "乙醇" in text


@pytest.mark.parametrize("decision", ["reject", "unsupported", ""])
def test_no_report_reason_mentions_decision(tmp_path: Any, decision: str) -> None:
    """reason 文案带上受理决策，便于从 `run.json` 直接看出没有报告的原因。"""
    from docking_agent.agents.persistence import persist_agent_run
    from docking_agent.runs import Run

    run_id = "R-REASON-" + (decision or "none")
    run = Run(tmp_path, run_id, "agent", {"mode": "chat"})
    run.set(task_spec={"decision": decision})
    persist_agent_run(run, [], "（无工具调用）")
    reason = str(run.data.get("no_report_reason") or "")
    if decision:
        assert decision in reason, reason
    else:
        assert reason == "未执行任何计算", reason


# --------------------------------------------------------------------------- #
# 回归（Phase 2 收尾）：流水线删除后暴露的两处 Agent 路径缺口
# --------------------------------------------------------------------------- #
def test_inline_text_with_commas_is_parsed_as_smiles_not_csv() -> None:
    """「名称:SMILES,名称:SMILES」需解析出分子。

    逗号会被格式嗅探判成 CSV，`normalize_ligand_text` 返回 0 条；流水线删除后
    Agent 路径成为唯一入口，这种写法此前跑不出任何结果。解析不到时回退行内 SMILES 解析。
    """
    from docking_agent.core.normalize import normalize_ligand_text

    mols, meta = normalize_ligand_text("乙醇:CCO,甲醇:CO")
    assert [(m["name"], m["smiles"]) for m in mols] == [("乙醇", "CCO"), ("甲醇", "CO")]
    assert meta["format"] == "smi"

    # 真正的 CSV（有表头）仍按 CSV 解析，不受回退逻辑影响
    csv_mols, csv_meta = normalize_ligand_text("name,smiles\n乙醇,CCO")
    assert csv_meta["format"] == "csv"
    assert [(m["name"], m["smiles"]) for m in csv_mols] == [("乙醇", "CCO")]


def test_docking_tool_passes_save_poses_and_max_ligands(monkeypatch: pytest.MonkeyPatch) -> None:
    """表单里的「保存对接位姿」「最大分子数」需传到对接层（此前的 Agent 路径忽略它们）。"""

    from docking_agent.tools import docking as TD

    captured: Dict[str, Any] = {}

    def fake_dock_library(molecules: Any, **kwargs: Any) -> Dict[str, Any]:
        captured.update(kwargs)
        return {"status": "ok", "receptors": [], "notes": []}

    monkeypatch.setattr(TD, "dock_library", fake_dock_library)

    TD.molecular_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]',
                              receptor_sources="thrombin", save_poses=False, max_ligands=3)
    assert captured.get("pose_dir") is None, "save_poses=False 时不应写位姿目录"
    assert captured.get("max_ligands") == 3

    captured.clear()
    TD.molecular_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]',
                              receptor_sources="thrombin")
    assert captured.get("max_ligands") is None, "max_ligands=0 表示不限制"
