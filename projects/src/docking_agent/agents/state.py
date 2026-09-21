"""Agent 状态与上下文窗口（规范分层：state 独立成模块，避免循环导入）。

**为什么单独一个模块**：`state` 同时被 `coordinator`（协调 Agent）与 `middleware`
（摘要/裁剪中间件）需要，放在 `coordinator.py` 里会形成循环导入。这里作为唯一的定义处，
`coordinator.py` 再把它**再导出**（`from docking_agent.agents.state import AgentState`），
因此既有调用方（含测试）的 import 路径不变。
"""
from __future__ import annotations

import logging
from typing import Annotated, Any, List

from langchain.agents import AgentState as LangChainAgentState
from langchain_core.messages import AnyMessage
from langchain_core.messages.utils import trim_messages
from langgraph.graph.message import add_messages

logger = logging.getLogger(__name__)

#: 上下文窗口上限（条）：默认保留最近 20 轮对话（40 条消息）
MAX_MESSAGES = 40


def windowed_messages(old: Any, new: Any) -> List[AnyMessage]:
    """`messages` 通道的 reducer：保留最近 MAX_MESSAGES 条，且**不切断工具调用配对**。

    旧实现是 `add_messages(old, new)[-MAX_MESSAGES:]` —— 按下标硬切会把
    「AI(tool_calls) + ToolMessage」从中间截断，留下**悬空 ToolMessage**，OpenAI 兼容端点
    会直接 400（insufficient tool messages）；`agents/threads.py` 的线程自愈要处理的
    正是这类被截断的状态（真实缺陷）。

    现在改用 LangChain 官方 `trim_messages`：
      - `token_counter=len`：按**条数**而不是 token 数计算（与既有 MAX_MESSAGES 语义一致，
        不引入 tokenizer 依赖、也不改变每轮送给模型的规模）；
      - `start_on="human"`：窗口必须从一条人类消息开始 → 工具调用配对完整；
      - `include_system=True`：万一 state 里有 SystemMessage 也不会被当成"配对残片"丢掉。
    任何异常都退化为原来的按下标滑动（宁可少留，也不能让一次对话直接失败）。
    """
    merged: List[AnyMessage] = add_messages(old, new)  # type: ignore[assignment]
    if len(merged) <= MAX_MESSAGES:
        return merged
    try:
        trimmed = trim_messages(
            merged,
            max_tokens=MAX_MESSAGES,
            token_counter=len,
            strategy="last",
            allow_partial=False,
            start_on="human",
            include_system=True,
        )
    except Exception:  # noqa: BLE001 - 兜底：绝不因为裁剪失败让整轮对话崩掉
        logger.warning("trim_messages 失败，退化为按下标滑动窗口", exc_info=True)
        return merged[-MAX_MESSAGES:]
    return list(trimmed) if trimmed else merged[-MAX_MESSAGES:]


class AgentState(LangChainAgentState):
    """协调 Agent 的状态。

    **必须继承 `langchain.agents.AgentState`**（不是 `langgraph.graph.MessagesState`）：
    前者带 `structured_response` / `jump_to` 两个通道，`create_agent(response_format=...)`
    的结构化输出才可能终止；只继承 MessagesState 时结构化输出会一直循环到
    `GraphRecursionError`。这里只覆写 `messages` 的 reducer（上下文窗口），其余通道原样继承。
    """

    messages: Annotated[list[AnyMessage], windowed_messages]


#: 兼容旧名（原实现在 coordinator.py，测试与外部按 `_windowed_messages` 引用）
_windowed_messages = windowed_messages

__all__ = ["AgentState", "MAX_MESSAGES", "windowed_messages", "_windowed_messages"]
