"""Agent 事件流 -> SSE：替代 coze_coding_utils.helper.stream_runner 的平台协议。

对外事件（每个 event 都是 `event: message` + JSON data，最后以 `done` 结束）：
  {"type":"start","run_id":...}
  {"type":"token","content":"..."}                 # 模型增量文本（**不含**思考内容）
  {"type":"thinking","content":"..."}              # 模型思考/推理增量（前端折叠到「思考」气泡）
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
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from langchain_core.messages import SystemMessage

from docking_agent.runtime.payload import as_text
from docking_agent.config import env_int
from docking_agent.runtime.errors import error_payload
from docking_agent.runtime.limits import (WRAP_UP_NOTE, base_limit, escalate,
                                          is_recursion_error, limit_event)

logger = logging.getLogger(__name__)

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


#: 常见供应商把「思考」放在独立字段或标签里（这里统一抽出来，**绝不混进正文**）
_THINK_TAGS = ("thinking", "think", "reasoning", "analysis")
_THINK_TAG_RE = None


def split_thinking(text: str) -> "tuple[str, str]":
    """把正文里的 `<thinking>…</thinking>` 段剥出来，返回 `(正文, 思考)`。

    为什么需要：部分供应商/端点在思考模式下把推理直接拼进 content（真实反馈：聊天区被
    大段推理刷屏）。这类文本对用户没有信息量，必须挪到可折叠的「思考」气泡里。
    """
    global _THINK_TAG_RE
    import re as _re

    if _THINK_TAG_RE is None:
        _THINK_TAG_RE = _re.compile(
            r"<\s*(?:" + "|".join(_THINK_TAGS) + r")\s*>(.*?)<\s*/\s*(?:"
            + "|".join(_THINK_TAGS) + r")\s*>", _re.S | _re.I)
    think_parts = [m.group(1) for m in _THINK_TAG_RE.finditer(text or "")]
    body = _THINK_TAG_RE.sub("", text or "")
    return body, "\n".join(p.strip() for p in think_parts if p.strip())


def reasoning_of(msg: Any) -> str:
    """从一条消息里取出**思考/推理**文本（没有则返回空串）。

    覆盖三类来源：
      1. `additional_kwargs.reasoning_content` / `reasoning`（DeepSeek、豆包等 OpenAI 兼容端点）；
      2. content 列表里的 `type in (thinking|reasoning)` 块（Responses 风格）；
      3. content 字符串里的 `<thinking>…</thinking>` 标签。
    """
    parts: list = []
    extra = getattr(msg, "additional_kwargs", None) or {}
    for key in ("reasoning_content", "reasoning"):
        value = extra.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
        elif isinstance(value, dict):
            summary = value.get("summary")
            if isinstance(summary, list):
                parts += [str(b.get("text") or "") for b in summary
                          if isinstance(b, dict) and b.get("text")]
            elif value.get("text"):
                parts.append(str(value["text"]))
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and str(block.get("type") or "") in (
                    "thinking", "reasoning", "reasoning_content"):
                parts.append(str(block.get("thinking") or block.get("reasoning")
                                 or block.get("text") or ""))
    return "\n".join(p for p in (str(x).strip() for x in parts) if p)

def _events_for(mode: str, chunk: Any, announced: set) -> List[Dict[str, Any]]:
    """把一个 LangGraph `(mode, chunk)` 映射成内部事件（与改造前逐字一致的行为）。

    单独成函数的原因：主循环要处理「跑满步数 → 自动放宽 → 从 checkpoint 继续」，
    把映射拆开后，重试逻辑才读得懂（缩进也不会失控）。
    """
    out: List[Dict[str, Any]] = []
    if mode == "messages":
        msg, meta = chunk  # type: ignore[misc]
        node = (meta or {}).get("langgraph_node", "")
        text = as_text(getattr(msg, "content", ""))
        if type(msg).__name__ in ("AIMessageChunk", "AIMessage"):
            # 思考内容单独走 `thinking` 事件：前端折叠成「思考」气泡，**不混进正文**
            text, inline_think = split_thinking(text)
            reasoning = "\n".join(x for x in (reasoning_of(msg), inline_think) if x)
            if reasoning:
                out.append({"type": "thinking", "content": reasoning, "node": node})
            if text:
                out.append({"type": "token", "content": text, "node": node})
        # 工具调用（子 Agent 调度）
        for tc in (getattr(msg, "tool_call_chunks", None) or []):
            name = tc.get("name")
            if name and (msg.id, name) not in announced:
                announced.add((msg.id, name))
                out.append({"type": "tool_call", "name": name, "node": node})
        if type(msg).__name__ == "ToolMessage":
            out.append({"type": "tool_result", "name": getattr(msg, "name", "") or "",
                        "content": _truncate(as_text(getattr(msg, "content", ""))), "node": node})
    elif mode == "updates":
        for node, update in (chunk or {}).items():
            keys = list(update.keys()) if isinstance(update, dict) else []
            out.append({"type": "update", "node": node, "keys": keys})
    elif mode == "custom":
        # 节点/工具通过 get_stream_writer() 主动上报的实时进度（新增通道，不改既有类型）
        out.append({"type": "custom", "payload": chunk})
    return out


async def stream_agent_sse(graph: Any, payload: Optional[Dict[str, Any]], run_config: Dict[str, Any],
                           run_id: str, context: Any = None) -> AsyncIterator[str]:
    """驱动一次 Agent 运行并输出 SSE。

    `context`：LangGraph 的 `context_schema` 实例（见 `runtime/context.AgentContext`）；
    为 None 时由调用方负责用 ContextVar 兜底（双读期两条路径等价）。

    **步数预算**：命中 `GRAPH_RECURSION_LIMIT` 不报错 —— 自动放宽上限（120 → 240 → 480）
    并从 checkpoint 继续；到顶时让主管 Agent 用现有结果收尾（注入收尾提示），最后用
    已有结果落盘。理由见 `runtime/limits.py`：跑满步数是执行细节，不该打扰用户。
    """
    yield sse_event({"type": "start", "run_id": run_id})
    announced: set = set()
    limit = int(run_config.get("recursion_limit") or base_limit())
    next_payload: Any = payload                    # 首次用原始入参，续跑用 None（读 checkpoint）
    failed = False                                 # 真失败时不发 `final`（保持既有事件契约）
    try:
        while True:
            try:
                async for mode, chunk in graph.astream(
                    next_payload,
                    config={**run_config, "recursion_limit": limit},
                    stream_mode=["messages", "updates", "custom"],
                    context=context,
                ):
                    for event in _events_for(mode, chunk, announced):
                        yield sse_event(event)
            except Exception as e:  # noqa: BLE001
                if not is_recursion_error(e):
                    raise
                escalated = escalate(limit)
                yield sse_event(limit_event(limit, escalated=escalated))
                if escalated is None:              # 到顶：用现有结果收尾
                    break
                limit = escalated
                next_payload = None
                if escalate(limit) is None:
                    # 已是最后一档：让主管 Agent 知道要收尾，用剩下的步数给结论
                    try:
                        await graph.aupdate_state(
                            {**run_config, "recursion_limit": limit},
                            {"messages": [SystemMessage(content=WRAP_UP_NOTE)]})
                    except Exception:  # noqa: BLE001 - 注入失败也要继续跑
                        logger.debug("注入收尾提示失败", exc_info=True)
                continue
            break                                  # 流正常结束
    except Exception as e:  # noqa: BLE001
        failed = True
        yield sse_event({**error_payload(e, {"node_name": "stream_run", "run_id": run_id}),
                         "type": "error"}, event="error")
    finally:
        if not failed:
            final = ""
            try:
                state = await graph.aget_state(run_config)
                final = _final_from_state(getattr(state, "values", None))
            except Exception:  # noqa: BLE001
                final = ""
            yield sse_event({"type": "final", "run_id": run_id, "content": final})
        yield sse_event({"type": "done", "run_id": run_id})
