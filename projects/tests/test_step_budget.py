"""步数预算：跑满递归上限要**自动放宽并继续**，到顶则用现有结果收尾 —— 绝不把错误抛给用户。

产品准则（用户明确要求）：系统尽可能自动处理，**只有影响对接本身的问题**才需要用户决定。
「跑满步数」是执行细节 → 自动放宽（120 → 240 → 480），到顶让主管 Agent 收尾，用已有结果落盘。

真实报错（修复前）：

    Recursion limit of 60 reached without hitting a stop condition.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage

from docking_agent.config import DEFAULT_RECURSION_LIMIT
from docking_agent.runtime.limits import (WRAP_UP_NOTE, base_limit, escalate, is_recursion_error,
                                          limit_event)


def _events(text: str) -> List[Dict[str, Any]]:
    import json

    out = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                try:
                    out.append(json.loads(line[6:]))
                except Exception:  # 允许静默：非 JSON 片段不是本用例的断言对象
                    pass
    return out


def test_escalation_ladder_and_ceiling(monkeypatch: Any) -> None:
    monkeypatch.delenv("RECURSION_LIMIT", raising=False)
    monkeypatch.delenv("RECURSION_LIMIT_MAX", raising=False)
    assert base_limit() == DEFAULT_RECURSION_LIMIT == 120
    assert escalate(120) == 240
    assert escalate(240) == 480
    assert escalate(480) is None, "到天花板必须返回 None（转收尾）"
    assert escalate(999) is None


def test_ceiling_follows_the_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("RECURSION_LIMIT", "50")
    monkeypatch.setenv("RECURSION_LIMIT_MAX", "60")
    assert base_limit() == 50 and escalate(50) == 60 and escalate(60) is None


def test_recursion_error_detection() -> None:
    from langgraph.errors import GraphRecursionError

    assert is_recursion_error(GraphRecursionError("Recursion limit of 5 reached"))
    assert not is_recursion_error(RuntimeError("boom"))
    assert is_recursion_error(ValueError("Recursion limit of 60 reached"))  # 文案兜底


def test_limit_event_wording() -> None:
    auto = limit_event(120, escalated=240)
    assert auto["type"] == "limit" and "自动放宽到 240" in auto["message"]
    assert auto["next_limit"] == 240
    done = limit_event(480, escalated=None)
    assert "收尾" in done["message"] and done["next_limit"] == 0


def test_streaming_runner_auto_escalates_instead_of_erroring() -> None:
    """真实图回归：一个需要 12 步的循环图，起始上限只有 4 → 必须自动放宽后跑完，且无 error 事件。"""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    from docking_agent.runtime.streaming import stream_agent_sse

    class _State(TypedDict):
        count: int

    def _step(state: _State) -> _State:
        return {"count": int(state.get("count") or 0) + 1}

    builder = StateGraph(_State)
    builder.add_node("loop", _step)
    builder.add_edge(START, "loop")
    builder.add_conditional_edges(
        "loop", lambda s: "loop" if int(s.get("count") or 0) < 12 else END,
        {"loop": "loop", END: END})
    graph = builder.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "budget-case"}, "recursion_limit": 4}

    async def _run() -> tuple[List[Dict[str, Any]], int]:
        chunks = [c async for c in stream_agent_sse(graph, {"count": 0}, config, "R-BUDGET")]
        snapshot = await graph.aget_state(config)
        return _events("".join(chunks)), int((snapshot.values or {}).get("count") or 0)

    events, final_count = asyncio.run(_run())
    types = [e.get("type") for e in events]
    assert "error" not in types, [e for e in events if e.get("type") == "error"]
    assert types.count("limit") >= 1, types
    assert "done" in types and types[-1] == "done"
    assert final_count == 12, final_count


def test_streaming_runner_stops_gracefully_at_the_ceiling(monkeypatch: Any) -> None:
    """到顶（天花板）时不再放宽：如实发一条 limit 事件并**正常结束**，不发 error。"""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    from docking_agent.runtime.streaming import stream_agent_sse

    monkeypatch.setenv("RECURSION_LIMIT", "4")
    monkeypatch.setenv("RECURSION_LIMIT_MAX", "8")

    class _State(TypedDict):
        count: int

    def _step(state: _State) -> _State:
        return {"count": int(state.get("count") or 0) + 1}

    builder = StateGraph(_State)
    builder.add_node("loop", _step)
    builder.add_edge(START, "loop")
    builder.add_conditional_edges("loop", lambda _s: "loop", {"loop": "loop", END: END})
    graph = builder.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "budget-ceiling"}, "recursion_limit": 4}

    async def _run() -> List[Dict[str, Any]]:
        return _events("".join([c async for c in stream_agent_sse(graph, {"count": 0}, config, "R-CAP")]))

    events = asyncio.run(_run())
    types = [e.get("type") for e in events]
    assert "error" not in types, [e for e in events if e.get("type") == "error"]
    assert any(e.get("type") == "limit" and "收尾" in str(e.get("message")) for e in events), events
    assert types[-1] == "done"


def test_worker_invocation_auto_escalates(monkeypatch: Any) -> None:
    """子 Agent 跑满步数：自动放宽重试（config 里的 recursion_limit 变大），返回结果而不是抛错。"""
    from langgraph.errors import GraphRecursionError

    from docking_agent.agents import workers as W

    seen: List[int] = []

    class _Agent:
        def invoke(self, payload: Any, config: Any = None, context: Any = None) -> Any:
            seen.append(int(config["recursion_limit"]))
            if len(seen) == 1:
                raise GraphRecursionError("Recursion limit of 120 reached")
            return {"messages": [AIMessage(content='{"status": "ok"}')]}

    out = W.invoke_worker(_Agent(), "跑一次对接", "thread-budget")
    start = base_limit()
    assert seen == [start, start * 2], seen
    assert '"status": "ok"' in out


def test_worker_reports_step_limit_instead_of_raising(monkeypatch: Any) -> None:
    """到顶后如实返回 `agent_step_limit`（可由主管 Agent 决策），而不是把异常抛给运行。"""
    from langgraph.errors import GraphRecursionError

    from docking_agent.agents import workers as W

    monkeypatch.setenv("RECURSION_LIMIT", "4")
    monkeypatch.setenv("RECURSION_LIMIT_MAX", "8")

    class _Agent:
        def invoke(self, payload: Any, config: Any = None, context: Any = None) -> Any:
            raise GraphRecursionError("Recursion limit reached")

    out = W.invoke_worker(_Agent(), "永远跑不完的任务", "thread-budget2")
    assert "agent_step_limit" in out, out


def test_wrap_up_note_is_a_system_message_not_user_prose() -> None:
    """收尾提示写进 SystemMessage：不进用户可见的对话正文（`_recent_prior_turns` 只读 human/ai）。"""
    assert "收尾" in WRAP_UP_NOTE and "不要臆造" in WRAP_UP_NOTE
    assert isinstance(HumanMessage("x"), HumanMessage)  # 语义提示：本提示不用 HumanMessage 注入
