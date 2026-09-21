"""跨轮次会话自愈回归：中断留下的悬空 tool_calls 必须补齐，否则下一轮被 OpenAI 400 拒绝。

真实故障（用户报告）：

    400 - An assistant message with 'tool_calls' must be followed by tool messages
          responding to each 'tool_call_id'. (insufficient tool messages …)

成因：点「停止」或运行失败时，模型已经发出 `tool_calls`，工具回执永远不会产生；
checkpointer 把那条 AIMessage 记进 thread 历史 → 同一会话的**下一轮**直接 400，整段对话卡死。

看护三件事：
1. `dangling_tool_calls()` 能准确找出没有回执的调用（含中间位置与部分回执两种形态）；
2. `repair_messages()` 把占位回执插到**所属 AIMessage 正后方**（不是简单追加到末尾）；
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
    repair_messages,
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


def test_repair_inserts_placeholder_right_after_its_ai_message() -> None:
    """占位回执必须在所属 AIMessage **正后方**：OpenAI 校验顺序，追加到末尾会继续报错。"""
    messages = [HumanMessage("x", id="h1"), AIMessage("", tool_calls=[_call(9)], id="a2"),
                HumanMessage("继续", id="h2"), AIMessage("结论", id="a3")]
    repaired, added = repair_messages(messages)

    assert added == 1
    assert _kinds(repaired) == ["HumanMessage", "AIMessage", "ToolMessage",
                                "HumanMessage", "AIMessage"]
    placeholder = repaired[2]
    assert placeholder.tool_call_id == "call_9" and placeholder.name == "run_docking"
    assert "中断" in placeholder.content and "没有产生结果" in placeholder.content
    assert dangling_tool_calls(repaired) == []
    assert len(repaired) == len(messages) + 1, "原有消息一条都不能丢"


def test_repair_is_noop_when_history_is_valid() -> None:
    messages = [HumanMessage("x", id="h1"),
                AIMessage("", tool_calls=[_call(1)], id="a1"),
                ToolMessage("result", tool_call_id="call_1", id="t1"),
                AIMessage("结论", id="a2")]
    repaired, added = repair_messages(messages)
    assert added == 0 and [m.id for m in repaired] == [m.id for m in messages]


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
        assert healed["repaired"] == 2, healed

        after = (await graph.aget_state(config)).values["messages"]
        assert dangling_tool_calls(after) == [], _kinds(after)
        assert _kinds(after) == ["HumanMessage", "AIMessage", "ToolMessage", "ToolMessage"]
        assert [m.content for m in after[2:]] == [INTERRUPT_NOTE, INTERRUPT_NOTE]

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
