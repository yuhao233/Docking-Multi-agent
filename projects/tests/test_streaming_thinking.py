"""思考（thinking）输出必须**单独成轨**：正文里不能混进推理，避免聊天区被刷屏。

真实场景：思考模式下，部分供应商/端点会把推理直接拼进 `content`（`<thinking>…</thinking>`），
或放在 `additional_kwargs.reasoning_content`（DeepSeek/豆包等）。前端要把它折叠成可展开的
「思考」气泡 —— 前提是服务端**分开**下发：`token` 只带正文，`thinking` 带推理。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

from langchain_core.messages import AIMessageChunk


def _frames(text: str) -> List[Dict[str, Any]]:
    out = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                try:
                    out.append(json.loads(line[6:]))
                except json.JSONDecodeError:  # 允许静默：非 JSON 片段不是本用例的断言对象
                    pass
    return out


class _FakeGraph:
    """最小假图：按 `stream_mode=["messages","updates","custom"]` 的契约吐块。"""

    def __init__(self, chunks: List[Any]) -> None:
        self._chunks = chunks

    async def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                      context: Any = None) -> Any:
        for msg in self._chunks:
            yield "messages", (msg, {"langgraph_node": "agent"})

    async def aget_state(self, config: Any = None) -> Any:
        return type("S", (), {"values": {"messages": [AIMessageChunk(content="完成")]}})()


def _run(chunks: List[Any]) -> List[Dict[str, Any]]:
    from docking_agent.runtime.streaming import stream_agent_sse

    async def _collect() -> str:
        return "".join([chunk async for chunk in stream_agent_sse(
            _FakeGraph(chunks), {"messages": []}, {}, "R-THINK")])

    return _frames(asyncio.run(_collect()))


def test_reasoning_content_goes_to_thinking_not_to_tokens() -> None:
    events = _run([
        AIMessageChunk(content="", additional_kwargs={"reasoning_content": "先看靶点口袋"}),
        AIMessageChunk(content="结论：推荐 MOL1。"),
    ])
    think = "".join(e.get("content") or "" for e in events if e.get("type") == "thinking")
    tokens = "".join(e.get("content") or "" for e in events if e.get("type") == "token")
    assert think == "先看靶点口袋", events
    assert tokens == "结论：推荐 MOL1。", events
    assert "先看靶点口袋" not in tokens, "推理绝不能混进正文"


def test_inline_think_tag_is_stripped_from_tokens() -> None:
    events = _run([AIMessageChunk(content="<thinking>内部推理</thinking>正文结论")])
    think = "".join(e.get("content") or "" for e in events if e.get("type") == "thinking")
    tokens = "".join(e.get("content") or "" for e in events if e.get("type") == "token")
    assert think == "内部推理" and tokens == "正文结论", events


def test_thinking_content_block_is_recognized() -> None:
    events = _run([AIMessageChunk(content=[{"type": "thinking", "thinking": "块式思考"},
                                          {"type": "text", "text": "正文"}])])
    types = [e.get("type") for e in events]
    assert "thinking" in types, events
    think = "".join(e.get("content") or "" for e in events if e.get("type") == "thinking")
    assert "块式思考" in think, events


def test_standard_envelope_carries_thinking_as_custom_frame() -> None:
    """标准 Agent Protocol 面：thinking 必须走 custom 帧（不能混进 messages/partial）。"""
    from docking_agent.api.agent_service import _DOMAIN_TYPES, _map_legacy_event

    assert "thinking" in _DOMAIN_TYPES
    frames = _map_legacy_event({"type": "thinking", "content": "推理", "node": "agent"}, state={})
    assert frames and "custom" in frames[0], frames
    assert "推理" in frames[0] and "messages/partial" not in frames[0]
