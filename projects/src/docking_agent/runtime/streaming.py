"""Agent 事件流 -> SSE：替代 coze_coding_utils.helper.stream_runner 的平台协议。

对外事件（每个 event 都是 `event: message` + JSON data，最后以 `done` 结束）：
  {"type":"start","run_id":...}
  {"type":"token","content":"..."}                 # 模型增量文本
  {"type":"tool_call","name":"...","args":{...}}   # 子 Agent 调度工具调用
  {"type":"tool_result","name":"...","content":"..."}
  {"type":"update","node":"..."}                   # 图节点推进
  {"type":"custom","payload":{...}}                # 节点内 get_stream_writer() 主动上报（P1 新增）
  {"type":"final","content":"..."}                 # 最终回答（完整文本）
  {"type":"error",...} / {"type":"done","run_id":...}

**既有事件类型是不变量**（前端 web/app.js 已消费，见 architecture.md §5）：`custom` 只是在
原类型之外**追加**的通道，用于让工具/节点把"真实进度"（逐分子对接、长任务阶段）直接上报，
前端不认它也不会坏。
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from docking_agent.runtime.payload import as_text
from docking_agent.config import env_int
from docking_agent.runtime.errors import error_payload

TRUNCATE = env_int("STREAM_TOOL_RESULT_CHARS", 2000)


def sse_event(data: Any, event: str = "message", event_id: Optional[str] = None) -> str:
    """把事件编码为 SSE 报文。

    自动附加 `ts`（服务端毫秒时间戳）：前端用它计算每个节点的**真实起止时间**，
    进而判断同层子 Agent 到底是并行还是串行 —— 运行示意图按实测结果渲染，而不是画死流程。
    """
    if isinstance(data, dict) and "ts" not in data:
        data = {**data, "ts": int(time.time() * 1000)}
    id_line = f"id: {event_id}\n" if event_id else ""
    return f"{id_line}event: {event}\n" + "data: " + json.dumps(data, ensure_ascii=False, default=str) + "\n\n"


def parse_sse_data(chunk: str) -> Optional[Dict[str, Any]]:
    """从一条 SSE 报文（可能多行）中取出 data: 的 JSON 负载。"""
    for line in (chunk or "").splitlines():
        if line.startswith("data: "):
            try:
                return json.loads(line[len("data: "):].strip())
            except json.JSONDecodeError:
                return None
    return None


def _truncate(text: str) -> str:
    if TRUNCATE > 0 and len(text) > TRUNCATE:
        return text[:TRUNCATE] + f"...(截断，共 {len(text)} 字符)"
    return text


def _final_from_state(values: Any) -> str:
    if not isinstance(values, dict):
        return ""
    msgs = values.get("messages") or []
    for m in reversed(msgs):
        if type(m).__name__ in ("AIMessage", "AIMessageChunk"):
            text = as_text(getattr(m, "content", ""))
            if text.strip():
                return text.strip()
    if msgs:
        return as_text(getattr(msgs[-1], "content", ""))
    return ""


async def stream_agent_sse(graph: Any, payload: Dict[str, Any], run_config: Dict[str, Any],
                           run_id: str, context: Any = None) -> AsyncIterator[str]:
    """驱动一次 Agent 运行并输出 SSE。

    `context`：LangGraph 的 `context_schema` 实例（见 `runtime/context.AgentContext`）；
    为 None 时由调用方负责用 ContextVar 兜底（双读期两条路径等价）。
    """
    yield sse_event({"type": "start", "run_id": run_id})
    announced: set = set()
    try:
        async for mode, chunk in graph.astream(
            payload, config=run_config, stream_mode=["messages", "updates", "custom"],
            context=context,
        ):
            if mode == "messages":
                msg, meta = chunk  # type: ignore[misc]
                node = (meta or {}).get("langgraph_node", "")
                text = as_text(getattr(msg, "content", ""))
                if text and type(msg).__name__ in ("AIMessageChunk", "AIMessage"):
                    yield sse_event({"type": "token", "content": text, "node": node})
                # 工具调用（子 Agent 调度）
                for tc in (getattr(msg, "tool_call_chunks", None) or []):
                    name = tc.get("name")
                    if name and (msg.id, name) not in announced:
                        announced.add((msg.id, name))
                        yield sse_event({"type": "tool_call", "name": name, "node": node})
                if type(msg).__name__ == "ToolMessage":
                    yield sse_event({
                        "type": "tool_result",
                        "name": getattr(msg, "name", "") or "",
                        "content": _truncate(as_text(getattr(msg, "content", ""))),
                        "node": node,
                    })
            elif mode == "updates":
                for node, update in (chunk or {}).items():
                    keys = list(update.keys()) if isinstance(update, dict) else []
                    yield sse_event({"type": "update", "node": node, "keys": keys})
            elif mode == "custom":
                # 节点/工具通过 get_stream_writer() 主动上报的实时进度（新增通道，不改既有类型）
                yield sse_event({"type": "custom", "payload": chunk})

        final = ""
        try:
            state = await graph.aget_state(run_config)
            final = _final_from_state(getattr(state, "values", None))
        except Exception:  # noqa: BLE001
            final = ""
        yield sse_event({"type": "final", "run_id": run_id, "content": final})
    except Exception as e:  # noqa: BLE001
        yield sse_event({**error_payload(e, {"node_name": "stream_run", "run_id": run_id}), "type": "error"},
                        event="error")
    finally:
        yield sse_event({"type": "done", "run_id": run_id})
