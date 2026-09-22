"""跨轮次会话自愈回归：中断留下的悬空 tool_calls 必须补齐，否则下一轮被 OpenAI 400 拒绝。

真实故障（用户报告）：

    400 - An assistant message with 'tool_calls' must be followed by tool messages
          responding to each 'tool_call_id'. (insufficient tool messages …)

成因：点「停止」或运行失败时，模型已经发出 `tool_calls`，工具回执永远不会产生；
checkpointer 把那条 AIMessage 记进 thread 历史 → 同一会话的**下一轮**直接 400，整段对话卡死。

看护三件事：
1. `dangling_tool_calls()` / `orphan_tool_call_ids()` 能准确找出两种非法形态；
2. `pairing_updates()` 用**同 id 改写 AIMessage**（原地生效、顺序天然合法）修掉悬空调用，
   并移除孤儿回执 —— 而不是往中间插消息（`add_messages` 只会把新 id 追加到末尾，插不进去）；
3. `repair_thread_state()` 真的把修复写回 LangGraph checkpointer（用真实编译图 + MemorySaver）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.agents.threads import (  # noqa: E402
    INTERRUPT_NOTE,
    dangling_tool_calls,
    orphan_tool_call_ids,
    pairing_updates,
    repair_thread_state,
)
from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()


def _call(index: int, name: str = "run_docking") -> dict:
    return {"id": f"call_{index}", "name": name, "args": {}, "type": "tool_call"}


def _kinds(messages: list) -> list:
    return [type(m).__name__ for m in messages]


def test_dangling_detects_tail_partial_and_middle() -> None:
    tail = [HumanMessage("跑一次", id="h1"),
            AIMessage("", tool_calls=[_call(1, "run_pocket_analysis"), _call(2)], id="a1")]
    assert dangling_tool_calls(tail) == [("call_1", "run_pocket_analysis"), ("call_2", "run_docking")]

    partial = tail + [ToolMessage("ok", tool_call_id="call_1", id="t1")]
    assert dangling_tool_calls(partial) == [("call_2", "run_docking")]

    middle = [HumanMessage("x", id="h1"), AIMessage("", tool_calls=[_call(9)], id="a2"),
              HumanMessage("继续", id="h2"), AIMessage("结论", id="a3")]
    assert dangling_tool_calls(middle) == [("call_9", "run_docking")]

    assert dangling_tool_calls([HumanMessage("没问题", id="h1"), AIMessage("结论", id="a1")]) == []


def test_pairing_rewrites_the_ai_message_in_place() -> None:
    """悬空调用靠**同 id 改写 AIMessage**修掉：位置不变、顺序天然合法。

    为什么不是「插一条占位 ToolMessage」：`add_messages` 只把**新 id** 追加到末尾，
    往中间插消息做不到 —— 第一版就这样，占位回执落到了最新一条人类消息之后，模型端照样 400。
    """
    from langgraph.graph.message import add_messages

    messages = [HumanMessage("x", id="h1"), AIMessage("先看口袋", tool_calls=[_call(9)], id="a2"),
                HumanMessage("继续", id="h2"), AIMessage("结论", id="a3")]
    updates, stats = pairing_updates(messages)
    assert stats["rewritten"] == 1 and stats["dropped_calls"] == 1
    assert stats["removed_orphans"] == 0

    merged = add_messages(messages, updates)          # 与 LangGraph 的 reducer 同一实现
    assert _kinds(merged) == ["HumanMessage", "AIMessage", "HumanMessage", "AIMessage"], _kinds(merged)
    assert [m.id for m in merged][:4] == ["h1", "a2", "h2", "a3"], "位置必须保持不变"
    rewritten = merged[1]
    assert not (rewritten.tool_calls or []), "无回执的调用必须去掉"
    assert "先看口袋" in rewritten.content and "中断" in rewritten.content
    assert "run_docking" in rewritten.content, "说明里要写清是哪个工具被中断"
    assert dangling_tool_calls(merged) == []


def test_pairing_drops_orphan_tool_messages() -> None:
    """孤儿回执（配对的 AIMessage 被摘要/裁剪切掉）必须移除。"""
    from langgraph.graph.message import add_messages

    messages = [HumanMessage("x", id="h1"),
                ToolMessage("旧回执", tool_call_id="gone", id="t0"),
                AIMessage("结论", id="a1")]
    assert orphan_tool_call_ids(messages) == ["gone"]
    updates, stats = pairing_updates(messages)
    assert stats["removed_orphans"] == 1
    merged = add_messages(messages, updates)
    assert _kinds(merged) == ["HumanMessage", "AIMessage"], _kinds(merged)


def test_pairing_keeps_answered_calls_when_batch_is_partial() -> None:
    """并行工具调用只回了一半：保留有回执的那一个，只去掉没回执的。"""
    from langgraph.graph.message import add_messages

    messages = [HumanMessage("x", id="h1"),
                AIMessage("", tool_calls=[_call(1), _call(2)], id="a1"),
                ToolMessage("ok", tool_call_id="call_1", id="t1"),
                AIMessage("结论", id="a2")]
    merged = add_messages(messages, pairing_updates(messages)[0])
    calls = list(merged[1].tool_calls or [])
    assert [c["id"] for c in calls] == ["call_1"], calls
    assert dangling_tool_calls(merged) == []


def test_pairing_is_noop_when_history_is_valid() -> None:
    messages = [HumanMessage("x", id="h1"),
                AIMessage("", tool_calls=[_call(1)], id="a1"),
                ToolMessage("result", tool_call_id="call_1", id="t1"),
                AIMessage("结论", id="a2")]
    assert pairing_updates(messages)[0] == []
    assert orphan_tool_call_ids(messages) == []


def test_repair_thread_state_heals_real_checkpointer_state() -> None:
    """用真实编译图 + MemorySaver：注入悬空状态 → 自愈 → 状态里不再有悬空调用。"""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, MessagesState, StateGraph

    def _model(_state):  # 只用于把图编译出来，不参与本用例
        return {}

    builder = StateGraph(MessagesState)
    builder.add_node("model", _model)
    builder.add_node("tools", _model)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    graph = builder.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "case-dangling"}}

    async def _scenario() -> None:
        await graph.aupdate_state(config, {"messages": [
            HumanMessage("跑一次", id="h1"),
            AIMessage("", tool_calls=[_call(1, "run_pocket_analysis"), _call(2)], id="a1"),
        ]}, as_node="model")
        before = (await graph.aget_state(config)).values["messages"]
        assert dangling_tool_calls(before) == [("call_1", "run_pocket_analysis"),
                                               ("call_2", "run_docking")], _kinds(before)

        healed = await repair_thread_state(graph, config)
        assert healed["repaired"] == 1, healed

        after = (await graph.aget_state(config)).values["messages"]
        assert dangling_tool_calls(after) == [], _kinds(after)
        assert _kinds(after) == ["HumanMessage", "AIMessage"], _kinds(after)
        assert after[1].id == "a1", "必须是原地替换（同 id），不能新增一条"
        assert "run_pocket_analysis" in after[1].content and INTERRUPT_NOTE[:12] in after[1].content

        # 幂等：已是合法历史 → 不再改动
        assert (await repair_thread_state(graph, config))["repaired"] == 0

    asyncio.run(_scenario())


def test_repair_thread_state_survives_broken_graph() -> None:
    """自愈绝不能把运行带崩：图不可用时返回 repaired=0。"""
    from typing import Any

    class _Broken:
        async def aget_state(self, _config: Any) -> Any:
            raise RuntimeError("模拟 checkpointer 不可用")

        async def aupdate_state(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("不该被调用")

    out = asyncio.run(repair_thread_state(_Broken(), {"configurable": {"thread_id": "t"}}))
    assert out == {"repaired": 0, "ids": []}
