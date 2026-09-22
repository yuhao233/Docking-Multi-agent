"""搜索强度「未指定」哨兵（0）的回归 —— 防止运行**静默退回** exhaustiveness=16。

## 为什么值得一组用例

`run_docking(exhaustiveness=16)` / `molecular_docking(exhaustiveness=16)` 的旧默认值
无法区分两件事：

- 用户/协调层**显式**要求 `exhaustiveness=16`；
- 谁都没给值（本该用受理层 `plan_docking_params` 算出的运行级值，
  即 `clamp(round(16×柔性系数×盒体积系数), 2, 32)`，实测常为 20–32）。

两者撞车的后果不是报错而是**静默降级**：协调层一旦漏传规划值（措辞/渲染任一环失效），
运行就用 16 采样、分数偏低，而报告里看不出发生过降级。现在默认值是
`UNSET_EXHAUSTIVENESS = 0`，由 `core/params.resolve_exhaustiveness` 显式解析。

本文件**不属于** `ENGINE_TEST_FILES`：这些是纯解析用例，在 `DOCKING_ENGINE_TESTS=0`
的 CI 里同样必须跑（旧默认值正是 CI 抓不到的那种缺陷）。
"""
from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from typing import Any, List

import pytest

from tests.support.fake_llm import parse_agent_params


# --------------------------------------------------------------------------- #
# 1) 纯函数：优先级 显式 > 运行级规划 > None（不编数字）
# --------------------------------------------------------------------------- #
def test_resolve_exhaustiveness_precedence() -> None:
    from docking_agent.core.params import (UNSET_EXHAUSTIVENESS,
                                           resolve_exhaustiveness)

    assert UNSET_EXHAUSTIVENESS == 0, "哨兵必须是 0（历史缺陷就是把 16 当成了「未指定」）"
    # 显式值优先（用户要求复算/对比）
    assert resolve_exhaustiveness(8, {"exhaustiveness": 24}) == 8
    # 未指定 → 用运行级规划值
    assert resolve_exhaustiveness(0, {"exhaustiveness": 24}) == 24
    assert resolve_exhaustiveness(None, {"exhaustiveness": 24}) == 24
    # 既没给值也没规划值 → None（下游按设置页默认执行，这里不写第二个默认数字）
    assert resolve_exhaustiveness(0, {}) is None
    assert resolve_exhaustiveness(None, None) is None
    # 字符串形态（模型常把数字写成字符串）与坏值都要稳
    assert resolve_exhaustiveness("0", {"exhaustiveness": "24"}) == 24
    assert resolve_exhaustiveness("bad", {"exhaustiveness": "bad"}) is None
    assert resolve_exhaustiveness(-3, {"exhaustiveness": 7}) == 7


def test_tool_signatures_default_to_unset_not_16() -> None:
    """两个对接工具的签名默认值必须是哨兵 0 —— 这条断言就是那次缺陷的看门人。"""
    from docking_agent.agents.dispatch import run_docking
    from docking_agent.tools.docking import molecular_docking

    for tool in (run_docking, molecular_docking):
        default = inspect.signature(tool.func).parameters["exhaustiveness"].default
        assert default == 0, f"{tool.name} 的 exhaustiveness 默认值应为 0（未指定），实为 {default!r}"
        # 参数说明必须写明「留空=自动规划」，否则模型会把 0 当成「强度 0」
        assert "自动规划" in (tool.description or ""), tool.name


# --------------------------------------------------------------------------- #
# 2) 协调层：未指定时把**规划值**写进给子 Agent 的参数块
# --------------------------------------------------------------------------- #
def _capture_dispatch(monkeypatch: pytest.MonkeyPatch, plan: Any) -> List[str]:
    """拦住下发给子 Agent 的任务消息（不启动子 Agent、零网络）。"""
    from docking_agent.agents import dispatch

    seen: List[str] = []
    monkeypatch.setattr(dispatch, "_invoke_checked",
                        lambda agent, msg, **kw: seen.append(msg) or "{}")
    monkeypatch.setattr(dispatch, "get_docking_agent", lambda: object())
    monkeypatch.setattr(dispatch, "active_run",
                        lambda runtime=None: SimpleNamespace(data={"param_plan": plan}))
    return seen


def test_run_docking_defers_to_run_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.agents import dispatch

    seen = _capture_dispatch(monkeypatch, {"exhaustiveness": 24, "source": "auto"})
    dispatch.run_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]')
    params = parse_agent_params(seen[0])
    assert params["exhaustiveness"] == 24, "未指定时必须用受理层规划值，不能退回 16"
    assert "exhaustiveness=24" in seen[0], "用到的强度必须写进指令（可追溯）"


def test_run_docking_explicit_value_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.agents import dispatch

    seen = _capture_dispatch(monkeypatch, {"exhaustiveness": 24, "source": "auto"})
    dispatch.run_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]', exhaustiveness=8)
    assert parse_agent_params(seen[0])["exhaustiveness"] == 8
    assert "显式指定" in seen[0]


def test_run_docking_without_plan_does_not_invent_a_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有规划值时如实说「保持未指定」，**不得**在参数块里塞一个 16 冒充规划结果。"""
    from docking_agent.agents import dispatch

    seen = _capture_dispatch(monkeypatch, {})
    dispatch.run_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]')
    params = parse_agent_params(seen[0])
    # 「未指定」必须是**键缺失**（参数契约：没出现的键=未指定）；下发 null 会被 int 校验拒掉
    assert "exhaustiveness" not in params, params
    assert "没有搜索强度规划值" in seen[0], seen[0]


# --------------------------------------------------------------------------- #
# 3) 执行层：解析后的强度真的传给了计算引擎，且来源写进 notes
# --------------------------------------------------------------------------- #
def test_molecular_docking_passes_planned_value_to_engine(run_ctx, monkeypatch) -> None:
    from docking_agent.tools import docking as dock_tool

    run, _board = run_ctx
    run.data["param_plan"] = {"exhaustiveness": 24, "source": "auto"}
    seen: dict = {}

    def _fake_dock(molecules, **kwargs):
        seen.update(kwargs)
        return {"status": "ok", "notes": [], "receptors": [
            {"receptor_key": "thrombin", "results": [
                {"name": m["name"], "smiles": m["smiles"], "affinity_kcal_mol": -5.0}
                for m in molecules]}]}

    monkeypatch.setattr(dock_tool, "dock_library", _fake_dock)
    out = json.loads(dock_tool.molecular_docking.invoke(
        {"molecules_json": json.dumps([{"name": "A", "smiles": "CCO"}]),
         "receptor_sources": "thrombin"}))

    assert seen.get("exhaustiveness") == 24, f"规划值必须一路传到引擎：{seen.get('exhaustiveness')}"
    assert any("exhaustiveness=24" in n and "自动规划" in n for n in out.get("notes") or []), \
        f"搜索强度来源必须可追溯：{out.get('notes')}"


def test_molecular_docking_falls_back_to_setting_default_without_plan(run_ctx,
                                                                     monkeypatch) -> None:
    """没有规划值 → 用**全仓唯一来源**的设置页默认，并把「系统默认」写进 notes。"""
    from docking_agent.core.params import DEFAULT_EXHAUSTIVENESS
    from docking_agent.tools import docking as dock_tool

    run, _board = run_ctx
    run.data.pop("param_plan", None)
    seen: dict = {}
    monkeypatch.setattr(dock_tool, "dock_library", lambda molecules, **kw: (
        seen.update(kw) or {"status": "ok", "notes": [], "receptors": [
            {"receptor_key": "thrombin", "results": [
                {"name": m["name"], "smiles": m["smiles"], "affinity_kcal_mol": -5.0}
                for m in molecules]}]}))
    out = json.loads(dock_tool.molecular_docking.invoke(
        {"molecules_json": json.dumps([{"name": "A", "smiles": "CCO"}]),
         "receptor_sources": "thrombin"}))
    assert seen.get("exhaustiveness") == DEFAULT_EXHAUSTIVENESS
    assert any("系统默认" in n for n in out.get("notes") or []), out.get("notes")


def test_molecular_docking_explicit_value_beats_plan(run_ctx, monkeypatch) -> None:
    from docking_agent.tools import docking as dock_tool

    run, _board = run_ctx
    run.data["param_plan"] = {"exhaustiveness": 24, "source": "auto"}
    seen: dict = {}
    monkeypatch.setattr(dock_tool, "dock_library", lambda molecules, **kw: (
        seen.update(kw) or {"status": "ok", "notes": [], "receptors": [
            {"receptor_key": "thrombin", "results": []}]}))
    dock_tool.molecular_docking.invoke(
        {"molecules_json": json.dumps([{"name": "A", "smiles": "CCO"}]),
         "receptor_sources": "thrombin", "exhaustiveness": 4})
    assert seen.get("exhaustiveness") == 4
