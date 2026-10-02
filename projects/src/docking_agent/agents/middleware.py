"""Agent 中间件：LangChain 1.x 的规范设施，在一处统一构建。

协调 Agent 与 4 个子 Agent 共用同一套，避免各 Agent 重复实现治理逻辑：

| 中间件 | 作用 | 技术原因 |
| --- | --- | --- |
| `ModelRetryMiddleware` | 模型调用瞬时失败（429/5xx/网络抖动）自动退避重试 | 一次筛选要执行几十次模型调用，原实现没有重试，一次抖动即整轮失败 |
| `ModelCallLimitMiddleware` | 单次运行的模型调用上限（安全网） | 防止运行失控消耗预算；阈值取较高值，正常筛选与长对话不会触发 |
| `ToolCallLimitMiddleware` | 单次运行的工具调用上限（安全网） | 同上（工具中包含真实对接，失控代价更高） |
| `SummarizationMiddleware` | 长对话把旧消息摘要成一条，而不是直接丢弃 | 原实现超出窗口即静默丢弃历史；摘要在保留关键上下文的前提下继续对话 |
| `ToolCallPairingMiddleware` | 模型调用前把非法序列修成合法（包裹式 `wrap_model_call`，不占递归步数） | 取消、限额与工具异常会在历史里留下「AI(tool_calls) 但没有回执」（或孤儿回执），下一次调用被模型端 400 拒绝（子 Agent 的固定角色线程受影响更明显，详见 `agents/threads.py`） |

设计口径（与项目「如实告知、不编造」一致）：
  - 限额命中时用 `exit_behavior="end"`（结束运行并交由协调 Agent 收尾），而不是 `error`，
    已算出的对接结果不因触发安全网而整轮丢弃；
  - 摘要只在远超正常对话长度时触发（默认 30 条，超过窗口 40 的一半），
    短任务（多数筛选）不产生额外模型调用；
  - 不引入 `ToolRetryMiddleware`：本项目工具执行真实计算（Vina/RDKit），失败重试等于重复消耗算力，
    且工具自身已返回结构化 `{status,error}` 交由模型判断（这是既定的工具契约）。

全部阈值可用环境变量覆盖（`.env` 与设置页均生效）：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `AGENT_RETRY_MAX` | 2 | 模型调用重试次数；0 = 关闭重试中间件 |
| `AGENT_MODEL_CALL_LIMIT` | 200 | 单次运行模型调用上限；0 = 不限制 |
| `AGENT_TOOL_CALL_LIMIT` | 400 | 单次运行工具调用上限；0 = 不限制 |
| `AGENT_SUMMARY_ENABLED` | on | 是否启用长对话摘要 |
| `AGENT_SUMMARY_TRIGGER_MESSAGES` | 30 | 超过多少条消息触发摘要 |
| `AGENT_SUMMARY_KEEP_MESSAGES` | 20 | 摘要时保留最近多少条原文 |
"""
from __future__ import annotations

import logging
from typing import Any, List

from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ModelRetryMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
)

from docking_agent.config import env_bool, env_int

logger = logging.getLogger(__name__)

#: 默认阈值（安全网级：正常筛选与长对话不会触发）
DEFAULT_RETRY_MAX = 2
DEFAULT_MODEL_CALL_LIMIT = 200
DEFAULT_TOOL_CALL_LIMIT = 400
DEFAULT_SUMMARY_TRIGGER = 30
DEFAULT_SUMMARY_KEEP = 20


def build_agent_middleware(llm: Any, *, role: str = "") -> List[Any]:
    """构建该角色的中间件列表（顺序即执行顺序，重试位于最外层）。

    `llm` 用于摘要。摘要本身也是一次模型调用，因此复用该角色的实例，按角色配置模型与端点。
    """
    out: List[Any] = []

    # 0) 工具调用配对自愈（置于最前）：任何一次模型调用都不应看到悬空的 `tool_calls`，
    #    否则模型端直接返回 400，而重试中间件只会把同一段非法历史再发一次。
    from docking_agent.agents.threads import ToolCallPairingMiddleware  # noqa: PLC0415

    out.append(ToolCallPairingMiddleware())

    retry_max = env_int("AGENT_RETRY_MAX", DEFAULT_RETRY_MAX)
    if retry_max > 0:
        # on_failure="continue"：重试仍失败时把错误作为消息交回模型或上层，不中断整轮运行
        out.append(ModelRetryMiddleware(max_retries=retry_max, on_failure="continue"))

    model_limit = env_int("AGENT_MODEL_CALL_LIMIT", DEFAULT_MODEL_CALL_LIMIT)
    if model_limit > 0:
        out.append(ModelCallLimitMiddleware(run_limit=model_limit, exit_behavior="end"))

    tool_limit = env_int("AGENT_TOOL_CALL_LIMIT", DEFAULT_TOOL_CALL_LIMIT)
    if tool_limit > 0:
        out.append(ToolCallLimitMiddleware(run_limit=tool_limit, exit_behavior="end"))

    if env_bool("AGENT_SUMMARY_ENABLED", True):
        trigger = max(4, env_int("AGENT_SUMMARY_TRIGGER_MESSAGES", DEFAULT_SUMMARY_TRIGGER))
        keep = max(2, env_int("AGENT_SUMMARY_KEEP_MESSAGES", DEFAULT_SUMMARY_KEEP))
        if trigger <= keep:
            # 语义保护：触发阈值需大于保留条数，否则会每轮都触发摘要
            trigger = keep + 2
            logger.warning("AGENT_SUMMARY_TRIGGER_MESSAGES(%s) <= KEEP(%s)，已调整为 %s",
                           trigger, keep, trigger)
        out.append(SummarizationMiddleware(model=llm, trigger=("messages", trigger),
                                           keep=("messages", keep)))

    logger.info("Agent 中间件（role=%s）：%s", role or "-",
                ", ".join(type(m).__name__ for m in out) or "（无）")
    return out


__all__ = ["build_agent_middleware", "DEFAULT_RETRY_MAX", "DEFAULT_MODEL_CALL_LIMIT",
           "DEFAULT_TOOL_CALL_LIMIT", "DEFAULT_SUMMARY_TRIGGER", "DEFAULT_SUMMARY_KEEP"]
