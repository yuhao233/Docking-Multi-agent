"""模型调用前的「工具调用配对」自愈：协调 Agent 与 4 个子 Agent 都不能把非法序列发给模型。

真实故障（用户报的 400）：

    400 - An assistant message with 'tool_calls' must be followed by tool messages
          responding to each 'tool_call_id'. (insufficient tool messages …)

`agents/threads.py::repair_thread_state()` 只在**每轮开始前**修**协调 Agent** 的 thread；
4 个子 Agent 用的是**固定角色线程**（`thread_id="property"/"pocket"/"docking"/"binding"`）
与各自的 `InMemorySaver` —— 一次取消 / 限额 / 工具异常把悬空 tool_calls 留在子 Agent 线程里，
下一次调用就直接 400，且当时没有任何补丁路径。本文件看护
`ToolCallPairingMiddleware`（包裹 `wrap_model_call` —— **不能**用 `before_model`：那是图里的一个
节点，每次模型调用多一个 super-step，会把长任务顶到 `GRAPH_RECURSION_LIMIT`）以及它真实的图内行为。

断言一律用 LangGraph 自己的 `add_messages` 当作 reducer（与运行时同一实现），
避免"测试里的合并规则比真实更宽松"。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.message import add_messages

from docking_agent.agents.threads import (INTERRUPT_NOTE, ToolCallPairingMiddleware,
                                          dangling_tool_calls, orphan_tool_call_ids)
from docking_agent.config import ensure_runtime_env

ensure_runtime_env()


def _call(index: int, name: str = "run_docking") -> dict:
    return {"id": f"call_{index}", "name": name, "args": {}, "type": "tool_call"}


def _mw() -> ToolCallPairingMiddleware:
    return ToolCallPairingMiddleware()


def _kinds(messages: List[Any]) -> List[str]:
    return [type(m).__name__ for m in messages]


class _Request:
    """最小 `ModelRequest` 替身：只用到 `messages` 与 `override(messages=…)`。"""

    def __init__(self, messages: List[Any]) -> None:
        self.messages = list(messages)
        self.overridden = False

    def override(self, **kwargs: Any) -> "_Request":
        clone = _Request(kwargs.get("messages") or self.messages)
        clone.overridden = True
        return clone


def _run(messages: List[Any], *, is_async: bool = False) -> Dict[str, Any]:
    """跑一次中间件：返回 handler 实际收到的消息与"是否被改写过"。"""
    seen: Dict[str, Any] = {}

    def handler(request: Any) -> str:
        seen["messages"] = list(request.messages)
        seen["overridden"] = bool(getattr(request, "overridden", False))
        return "response"

    mw = _mw()
    if is_async:
        import asyncio as _asyncio

        async def _ahandler(request: Any) -> str:
            return handler(request)

        _asyncio.run(mw.awrap_model_call(_Request(messages), _ahandler))
    else:
        mw.wrap_model_call(_Request(messages), handler)
    return seen


def _apply(messages: List[Any], update: Any) -> List[Any]:
    """（保留给 state 形式的修复用）用真实的 `add_messages` 应用更新。"""
    return list(add_messages(messages, update["messages"]))


def test_legal_history_is_left_untouched() -> None:
    """合法历史（含完整回执）不得被改写：返回 None，不产生额外消息。"""
    messages: List[Any] = [HumanMessage("跑一次", id="h1"),
                           AIMessage("", tool_calls=[_call(1)], id="a1"),
                           ToolMessage("ok", tool_call_id="call_1", id="t1"),
                           AIMessage("结论", id="a2")]
    seen = _run(messages)
    assert seen["overridden"] is False, "合法历史不该被改写"
    assert seen["messages"] == messages


def test_tail_dangling_call_is_dropped_in_place() -> None:
    """中断在尾部（最常见：取消 / 限额）→ 原地改写那条 AIMessage，序列变合法。"""
    messages: List[Any] = [HumanMessage("跑一次", id="h1"),
                           AIMessage("先做口袋分析", tool_calls=[_call(1), _call(2)], id="a1")]
    seen = _run(messages)
    assert seen["overridden"] is True, "非法历史必须被改写"
    merged = seen["messages"]
    assert _kinds(merged) == ["HumanMessage", "AIMessage"], _kinds(merged)
    assert merged[1].id == "a1", "必须是同 id 原地替换，不能新增一条"
    assert list(merged[1].tool_calls or []) == [], merged[1].tool_calls
    assert "先做口袋分析" in merged[1].content, "原有正文不能丢"
    assert INTERRUPT_NOTE[:12] in merged[1].content and "run_docking" in merged[1].content
    assert dangling_tool_calls(merged) == []


def test_middle_dangling_call_keeps_message_order() -> None:
    """中间位置悬空：改写后位置与后续消息顺序都不变（插消息是插不进去的，见模块说明）。"""
    messages: List[Any] = [HumanMessage("x", id="h1"),
                           AIMessage("", tool_calls=[_call(9, "run_pocket_analysis")], id="a2"),
                           HumanMessage("继续", id="h2"),
                           AIMessage("结论", id="a3")]
    merged = _run(messages)["messages"]
    assert [m.id for m in merged] == ["h1", "a2", "h2", "a3"]
    assert not (merged[1].tool_calls or [])
    assert "run_pocket_analysis" in merged[1].content
    assert dangling_tool_calls(merged) == []


def test_partial_batch_keeps_the_answered_call() -> None:
    """并行工具只回了一半：保留有回执的那个调用（它的回执还在后面）。"""
    messages: List[Any] = [HumanMessage("x", id="h1"),
                           AIMessage("", tool_calls=[_call(1), _call(2)], id="a1"),
                           ToolMessage("ok", tool_call_id="call_1", id="t1"),
                           AIMessage("结论", id="a2")]
    merged = _run(messages)["messages"]
    assert [c["id"] for c in (merged[1].tool_calls or [])] == ["call_1"]
    assert _kinds(merged) == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
    assert dangling_tool_calls(merged) == []


def test_orphan_tool_message_is_dropped() -> None:
    """另一半非法形态：回执还在、配对的 AIMessage 被摘要/裁剪切掉了 → 丢弃该回执。"""
    messages: List[Any] = [HumanMessage("x", id="h1"),
                           ToolMessage("旧回执", tool_call_id="gone", id="t1"),
                           AIMessage("结论", id="a2")]
    assert orphan_tool_call_ids(messages) == ["gone"]
    merged = _run(messages)["messages"]
    assert _kinds(merged) == ["HumanMessage", "AIMessage"], _kinds(merged)
    # 合法回执不能被误删
    ok = [HumanMessage("x", id="h1"), AIMessage("", tool_calls=[_call(1)], id="a1"),
          ToolMessage("ok", tool_call_id="call_1", id="t1")]
    assert orphan_tool_call_ids(ok) == []


def test_async_hook_matches_sync_hook() -> None:
    """`abefore_model` 与同步钩子行为一致（图里走的是异步路径）。"""
    messages: List[Any] = [AIMessage("", tool_calls=[_call(3)], id="a1")]
    merged = _run(messages, is_async=True)["messages"]
    assert _kinds(merged) == ["AIMessage"] and not (merged[0].tool_calls or [])
    assert dangling_tool_calls(merged) == []


def test_empty_and_plain_histories_pass_through() -> None:
    assert _run([])["overridden"] is False
    assert _run([HumanMessage("你好", id="h1")])["overridden"] is False


def test_middleware_is_built_for_every_role() -> None:
    """协调 Agent 与子 Agent 都必须带上它（子 Agent 的固定角色线程正是重灾区）。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from docking_agent.agents.middleware import build_agent_middleware

    class _Fake(GenericFakeChatModel):
        def bind_tools(self, *args: Any, **kwargs: Any) -> "_Fake":
            return self

    for role in ("coordinator", "property", "pocket", "docking", "binding"):
        names = [type(m).__name__ for m in build_agent_middleware(_Fake(messages=iter(["ok"])), role=role)]
        assert names[0] == "ToolCallPairingMiddleware", names


def test_real_agent_graph_heals_before_the_model_call() -> None:
    """端到端：真实 `create_agent` 图 + 悬空历史 → 模型**实际收到**的序列必须合法。

    这是用户报的那个 400 的正面回归：修复必须发生在模型调用之前，且顺序正确
    （第一版把占位回执追加到了末尾，模型收到的是 `[Human, AI(tool_calls), Human, ToolMessage]`，
    依旧非法 —— 本用例会直接抓到）。
    """
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import MemorySaver

    seen: List[List[str]] = []

    class _Fake(GenericFakeChatModel):
        def bind_tools(self, *args: Any, **kwargs: Any) -> "_Fake":
            return self

        def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None,
                      **kwargs: Any) -> Any:
            seen.append(_kinds(messages))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    @tool
    def noop(x: str = "") -> str:
        """占位工具（本用例不会真的调用到它）。"""
        return "ok"

    graph = create_agent(model=_Fake(messages=iter(["结论：完成。"])), tools=[noop],
                         middleware=[_mw()], checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "case-pairing"}}

    async def _scenario() -> List[Any]:
        from docking_agent.agents.threads import repair_thread_state

        await graph.aupdate_state(config, {"messages": [
            HumanMessage("跑一次", id="h1"),
            AIMessage("", tool_calls=[_call(1, "noop")], id="a1"),
        ]}, as_node="model")
        await graph.ainvoke({"messages": [HumanMessage("继续", id="h2")]}, config)
        after = list((await graph.aget_state(config)).values["messages"])
        # 包裹式修复**不改落盘状态**（改了就要多一个节点、多占步数）；下一轮开始前的
        # `repair_thread_state()` 仍会把它清干净 —— 两条路径互补，这里一起看护。
        await repair_thread_state(graph, config)
        healed = list((await graph.aget_state(config)).values["messages"])
        return after + ["---"] + healed

    parts = asyncio.run(_scenario())
    split = parts.index("---")
    after, healed = parts[:split], parts[split + 1:]

    assert seen, "模型没被调用"
    # 悬空调用所属的 AIMessage 被原地改写 → 模型收到的历史里没有任何 ToolMessage 悬空配对
    assert seen[0][:2] == ["HumanMessage", "AIMessage"], seen[0]
    assert seen[0].count("AIMessage") == 1 and "ToolMessage" not in seen[0], seen[0]
    # 落盘状态保留原始历史（不占步数），但新一轮开始前的自愈能清掉它
    assert dangling_tool_calls(after) == [("call_1", "noop")], _kinds(after)
    assert dangling_tool_calls(healed) == [], _kinds(healed)
    assert _kinds(healed)[:3] == ["HumanMessage", "AIMessage", "HumanMessage"], _kinds(healed)
