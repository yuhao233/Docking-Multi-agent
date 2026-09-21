"""入参归一化：把多种调用格式统一成 LangGraph Agent 需要的 {"messages": [...]}。

支持的格式：
  1. {"messages":[{"role":"user","content":"..."}]}            —— LangChain/OpenAI 风格
  2. {"text": "..."} / {"query": "..."} / {"content": "..."}  —— 纯文本简写
  3. Coze 风格 {"type":"query","content":{"query":{"prompt":[{"type":"text","content":{"text":"..."}}]}}}
  4. 直接传字符串
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

_ROLE_MAP = {
    "user": HumanMessage,
    "human": HumanMessage,
    "system": SystemMessage,
    "ai": AIMessage,
    "assistant": AIMessage,
    "tool": ToolMessage,
}


class PayloadError(ValueError):
    """无法从入参中解析出用户消息。"""


_TEXT_KEYS = ("text", "content", "query", "prompt", "value", "input", "message")


def _flatten_content(content: Any, _depth: int = 0) -> str:
    if content is None or _depth > 6:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        # Coze: {"type":"text","content":{"text":"..."}}；OpenAI: {"type":"text","text":"..."}
        for k in _TEXT_KEYS:
            if k in content:
                inner = _flatten_content(content[k], _depth + 1)
                if inner.strip():
                    return inner
        # 兜底：递归任意字段（如 {"query":{"prompt":[...]}}）
        for v in content.values():
            inner = _flatten_content(v, _depth + 1)
            if inner.strip():
                return inner
        return ""
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            parts.append(_flatten_content(item, _depth + 1))
        return "\n".join(p for p in parts if p)
    return str(content)


def parse_json_object(text: Any) -> Optional[Dict[str, Any]]:
    """从模型/子 Agent 输出里提取 JSON 对象（容忍 ```json 围栏与前后杂字符）。

    受理层与分发层的唯一实现 —— 两处各写一份曾导致行为漂移（一边接受列表、一边不接受）。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if "```" in raw[3:] else raw[3:]
        raw = raw[4:] if raw.lower().startswith("json") else raw
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def as_text(content: Any) -> str:
    """把 LangChain 消息内容（str / 分块列表 / dict 列表）拉平成纯文本。

    统一实现：`runtime/streaming.py`（SSE 最终回答）与 `agents/persistence.py`（落盘）
    原先各写一份，语义略有差异（一份会 join 换行、一份直接拼接）。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(p for p in parts if p)
    return str(content)


def extract_text(payload: Any) -> str:
    """从任意入参中提取纯文本提示词。"""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return str(payload)

    # 1) messages 数组（取最后一条 user/human 消息，或最后一条消息）
    msgs = payload.get("messages")
    if isinstance(msgs, list) and msgs:
        chosen = None
        for m in msgs:
            if isinstance(m, dict) and str(m.get("role", "")).lower() in ("user", "human"):
                chosen = m
        chosen = chosen or (msgs[-1] if isinstance(msgs[-1], dict) else None)
        if chosen is not None:
            text = _flatten_content(chosen.get("content"))
            if text:
                return text.strip()

    # 2) 常见纯文本字段
    for key in ("text", "query", "prompt", "input", "message", "content"):
        if key in payload:
            text = _flatten_content(payload[key])
            if text.strip():
                return text.strip()
    return ""


def normalize_agent_input(payload: Any) -> Dict[str, Any]:
    """返回 {"messages":[HumanMessage(...)]}；若已是 messages 结构则原样转换。"""
    if isinstance(payload, dict) and isinstance(payload.get("messages"), list) and payload["messages"]:
        converted = []
        for m in payload["messages"]:
            if isinstance(m, dict):
                cls = _ROLE_MAP.get(str(m.get("role", "user")).lower(), HumanMessage)
                converted.append(cls(content=_flatten_content(m.get("content"))))
            else:
                converted.append(m)
        if converted:
            return {"messages": converted}

    text = extract_text(payload)
    if not text:
        raise PayloadError(
            "无法从请求体中解析出用户消息。请使用 {\"messages\":[{\"role\":\"user\",\"content\":\"...\"}]}"
            " 或 {\"text\":\"...\"}。"
        )
    return {"messages": [HumanMessage(content=text)]}
