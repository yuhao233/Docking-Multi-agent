"""会话记忆与运行上下文的直接覆盖（审计指出这几处原先零直接测试）。

1. `coordinator._windowed_messages` / `MAX_MESSAGES`：滑动窗口行为与顺序；
2. `runtime/context.py`：`new_context` / `request_context` 的 ContextVar 语义（含异常复位）；
3. `runtime/streaming.py`：SSE 编解码、截断、最终回答提取，以及 `stream_agent_sse` 的完整帧序列。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from docking_agent.agents.coordinator import MAX_MESSAGES, _windowed_messages
from docking_agent.runtime import streaming as S


# --------------------------------------------------------------------------- #
# 1) 滑动窗口
# --------------------------------------------------------------------------- #
def test_windowed_messages_keeps_recent_and_preserves_order() -> None:
    messages: List[Any] = [HumanMessage(content=f"m{i}", id=f"id{i}") for i in range(MAX_MESSAGES + 7)]
    out = _windowed_messages([], messages)
    assert len(out) == MAX_MESSAGES
    assert [m.content for m in out] == [f"m{i}" for i in range(7, MAX_MESSAGES + 7)]
    assert out[-1].content == f"m{MAX_MESSAGES + 6}"


def test_windowed_messages_appends_below_cap() -> None:
    old = [HumanMessage(content="hi", id="a")]
    new = [AIMessage(content="hello", id="b")]
    out = _windowed_messages(old, new)
    assert [m.content for m in out] == ["hi", "hello"]


def test_windowed_messages_never_splits_tool_call_pairs() -> None:
    """回归（真实缺陷）：按条数硬切会把 AI(tool_calls)+ToolMessage 截断，留下悬空 ToolMessage，
    OpenAI 兼容端点直接 400。裁剪必须从人类消息开始，配对完整。"""
    msgs: List[Any] = [HumanMessage(content="sys-like", id="h0")]
    for i in range(6):
        msgs.append(AIMessage(content="", tool_calls=[
            {"name": "run_docking", "args": {}, "id": f"call{i}"}], id=f"ai{i}"))
        msgs.append(ToolMessage(content=f"结果{i}", tool_call_id=f"call{i}", id=f"t{i}"))
        msgs.append(AIMessage(content=f"回答{i}", id=f"a{i}"))
    msgs.append(HumanMessage(content="最后的问题", id="hlast"))

    out = _windowed_messages([], msgs)
    assert len(out) <= MAX_MESSAGES
    call_ids = {c["id"] for m in out if type(m).__name__ == "AIMessage"
                for c in (getattr(m, "tool_calls", None) or [])}
    tool_ids = {m.tool_call_id for m in out if type(m).__name__ == "ToolMessage"}
    assert not (tool_ids - call_ids), f"出现悬空 ToolMessage：{sorted(tool_ids - call_ids)}"
    assert type(out[0]).__name__ in ("HumanMessage", "SystemMessage"), type(out[0]).__name__
    assert out[-1].content == "最后的问题"


def test_windowed_messages_keeps_system_message_when_present() -> None:
    from langchain_core.messages import SystemMessage

    msgs = [SystemMessage(content="系统提示", id="s0")]
    msgs += [HumanMessage(content=f"h{i}", id=f"h{i}") for i in range(MAX_MESSAGES + 5)]
    out = _windowed_messages([], msgs)
    assert any(type(m).__name__ == "SystemMessage" for m in out), "SystemMessage 不应被裁掉"


def test_windowed_messages_is_incremental() -> None:
    """逐条追加（模拟多轮）时不会丢新消息，且窗口长度恒定。"""
    state: List[Any] = []
    for i in range(MAX_MESSAGES + 15):
        state = _windowed_messages(state, [HumanMessage(content=str(i), id=f"x{i}")])
    assert len(state) == MAX_MESSAGES
    assert state[-1].content == str(MAX_MESSAGES + 14)


# --------------------------------------------------------------------------- #
# 2) 运行上下文
# --------------------------------------------------------------------------- #
def test_new_context_is_unique_and_carries_method() -> None:
    from docking_agent.runtime.context import new_context

    a, b = new_context(method="unit"), new_context(method="unit")
    assert a.run_id and b.run_id and a.run_id != b.run_id
    assert a.method == "unit" and a.to_dict()["method"] == "unit"
    assert new_context(headers={"X": 1}).headers == {"X": "1"}


def test_request_context_set_and_reset_semantics() -> None:
    from docking_agent.runtime.context import new_context, request_context

    assert request_context.get() is None
    ctx = new_context(method="probe")
    token = request_context.set(ctx)
    try:
        assert request_context.get() is ctx
        inner = new_context(method="inner")
        inner_token = request_context.set(inner)
        assert request_context.get() is inner
        request_context.reset(inner_token)
        assert request_context.get() is ctx
    finally:
        request_context.reset(token)
    assert request_context.get() is None


def test_request_context_isolated_between_tasks() -> None:
    """ContextVar 的语义保证：并发任务之间互不串味（多用户场景的底线）。"""
    from docking_agent.runtime.context import new_context, request_context

    async def worker(tag: str) -> str:
        request_context.set(new_context(method=tag))
        await asyncio.sleep(0)
        return request_context.get().method

    async def main() -> List[str]:
        return list(await asyncio.gather(*(worker(t) for t in ("a", "b", "c"))))

    assert asyncio.run(main()) == ["a", "b", "c"]
    assert request_context.get() is None


# --------------------------------------------------------------------------- #
# 3) SSE 编解码与事件流
# --------------------------------------------------------------------------- #
def test_sse_event_roundtrip_adds_timestamp() -> None:
    raw = S.sse_event({"type": "start", "run_id": "R1"})
    assert raw.startswith("event: message\n")
    assert raw.endswith("\n\n")
    data = S.parse_sse_data(raw)
    assert data is not None and data["type"] == "start" and data["run_id"] == "R1"
    assert isinstance(data["ts"], int) and data["ts"] > 0


def test_sse_event_with_id_and_bad_payload() -> None:
    raw = S.sse_event({"a": 1}, event="error", event_id="e-1")
    assert raw.startswith("id: e-1\n") and "event: error\n" in raw
    assert S.parse_sse_data("data: {not json") is None
    assert S.parse_sse_data("event: message\n\n") is None
    assert S.parse_sse_data("") is None


def test_truncate_respects_limit(monkeypatch: Any) -> None:
    monkeypatch.setattr(S, "TRUNCATE", 10)
    assert S._truncate("abcdefghij") == "abcdefghij"
    out = S._truncate("abcdefghijkLMNOP")
    assert out.startswith("abcdefghij") and "截断" in out
    monkeypatch.setattr(S, "TRUNCATE", 0)
    assert S._truncate("x" * 100) == "x" * 100


def test_final_from_state_picks_last_ai_message() -> None:
    values = {"messages": [HumanMessage(content="q"), AIMessage(content="  answer  "),
                           ToolMessage(content="tool", tool_call_id="t")]}
    assert S._final_from_state(values) == "answer"
    assert S._final_from_state({"messages": [HumanMessage(content="only human")]}) == "only human"
    assert S._final_from_state({"messages": []}) == ""
    assert S._final_from_state(None) == ""


def _tiny_graph() -> Any:
    """最小真实图：一个节点追加一条 AIMessage，带 checkpointer（aget_state 需要）。"""

    def node(state: MessagesState) -> Dict[str, Any]:
        return {"messages": [AIMessage(content="最终回答")]}

    builder = StateGraph(MessagesState)
    builder.add_node("reply", node)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)
    return builder.compile(checkpointer=InMemorySaver())


def test_stream_agent_sse_emits_start_update_final() -> None:
    async def collect() -> List[Dict[str, Any]]:
        graph = _tiny_graph()
        frames: List[Dict[str, Any]] = []
        async for chunk in S.stream_agent_sse(
                graph, {"messages": [HumanMessage(content="hi")]},
                {"configurable": {"thread_id": "sse-test"}}, "R-1"):
            parsed = S.parse_sse_data(chunk)
            assert parsed is not None, chunk
            frames.append(parsed)
        return frames

    frames = asyncio.run(collect())
    types = [f["type"] for f in frames]
    # 契约：start → (token / update / tool_call / tool_result)* → final → done
    assert types[0] == "start" and frames[0]["run_id"] == "R-1"
    assert "token" in types and "update" in types, types
    assert types[-2:] == ["final", "done"], types
    assert frames[-2]["content"] == "最终回答"
    assert all("ts" in f for f in frames)
    assert json.dumps(frames, ensure_ascii=False)  # 全部可序列化（前端契约）


def test_custom_stream_channel_surfaces_progress() -> None:
    """P1 新增：节点内 `get_stream_writer()` 上报的进度以 `type=custom` 帧出现，
    且**既有事件类型一个都没变**（start/token/update/final/done）。"""
    from langgraph.config import get_stream_writer

    def node(state: MessagesState) -> Dict[str, Any]:
        writer = get_stream_writer()
        writer({"stage": "docking", "molecule": "乙醇", "index": 1, "total": 2})
        return {"messages": [AIMessage(content="完成")]}

    builder = StateGraph(MessagesState)
    builder.add_node("work", node)
    builder.add_edge(START, "work")
    builder.add_edge("work", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    async def collect() -> List[Dict[str, Any]]:
        frames: List[Dict[str, Any]] = []
        async for chunk in S.stream_agent_sse(
                graph, {"messages": [HumanMessage(content="hi")]},
                {"configurable": {"thread_id": "custom-test"}}, "R-3"):
            frames.append(S.parse_sse_data(chunk))
        return frames

    frames = asyncio.run(collect())
    types = [f["type"] for f in frames]
    assert types[0] == "start" and types[-1] == "done", types
    assert types[-2] == "final", types
    custom = [f for f in frames if f["type"] == "custom"]
    assert custom, f"没有 custom 帧：{types}"
    assert custom[0]["payload"]["molecule"] == "乙醇"
    assert custom[0]["payload"]["stage"] == "docking"


def test_stream_agent_sse_reports_error_frame() -> None:
    class Broken:
        async def astream(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("模拟图失败")
            yield  # pragma: no cover

    async def collect() -> List[Dict[str, Any]]:
        out = []
        async for chunk in S.stream_agent_sse(Broken(), {}, {}, "R-2"):
            out.append(S.parse_sse_data(chunk))
        return out

    frames = asyncio.run(collect())
    types = [f["type"] for f in frames]
    # 契约：出错时 start → error → done（前端据此收尾）
    assert types == ["start", "error", "done"], types
    assert "模拟图失败" in json.dumps(frames[1], ensure_ascii=False)
