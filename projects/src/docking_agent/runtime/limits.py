"""运行步数预算：递归上限的自动放宽与收尾策略。

判定准则：不影响对接本身的问题由系统自动处理，需要调用方决定的只有受体不可用或歧义、
多组分分子的代表结构、共晶配体是否作对照等事项。

「跑满步数」属于执行细节，命中 `GRAPH_RECURSION_LIMIT` 时按以下步骤处理：

1. 自动放宽：120、240、480（默认天花板为基准的 4 倍，`RECURSION_LIMIT_MAX` 可覆盖），
   从 checkpoint 继续执行，调用方无需操作；
2. 主管 Agent 收尾：放宽到天花板时向图注入一条 SystemMessage，要求它用已有结果给出结论
   （不再发起长流程工具调用），未完成的部分如实说明；
3. 到顶不报错：用现有结果落盘（报告与排行照常产出），运行状态如实标注。

`GraphRecursionError` 不抛给调用方，它既不影响对接结论，也不属于调用方可处理的信息。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from docking_agent.config import DEFAULT_RECURSION_LIMIT, env_int

logger = logging.getLogger(__name__)

#: 交给模型的收尾指令（SystemMessage，不进入对话正文）
WRAP_UP_NOTE = (
    "【系统提示】本次运行已到步数上限（系统已自动放宽多次）。请**立即用现有结果收尾**："
    "不要再发起新的长流程工具调用；把已经拿到的结果整理成最终答复 —— 完成了什么、"
    "关键数值是什么、还差什么，未完成的部分如实说明，**不要臆造任何数据**。")


def base_limit() -> int:
    """本次运行的起始递归上限（与协调 Agent 一致）。"""
    return max(4, env_int("RECURSION_LIMIT", DEFAULT_RECURSION_LIMIT))


def limit_ceiling() -> int:
    """自动放宽的天花板（默认 = 起始值的 4 倍）。"""
    return max(base_limit(), env_int("RECURSION_LIMIT_MAX", base_limit() * 4))


def escalate(current: int) -> Optional[int]:
    """下一步的递归上限；已到天花板返回 `None`，此时由主管 Agent 收尾。"""
    ceiling = limit_ceiling()
    if current >= ceiling:
        return None
    return min(int(current) * 2, ceiling)


def is_recursion_error(exc: BaseException) -> bool:
    """判断是否「跑满步数」这一类错误（按类型判，类型不可用时按文案兜底）。"""
    try:
        from langgraph.errors import GraphRecursionError

        if isinstance(exc, GraphRecursionError):
            return True
    except Exception:  # 允许静默：langgraph 版本差异导致取不到异常类时，退回文案判断
        pass
    return "recursion limit" in str(exc).lower()


def limit_event(limit: int, *, escalated: Optional[int]) -> dict:
    """统一的步数事件（前端显示为进度文案，同时写进运行日志，供事后查阅）。"""
    if escalated is None:
        message = f"已达步数上限（{limit} 步）：用现有结果收尾，未完成部分如实说明"
    else:
        message = f"已达步数上限（{limit} 步）：已自动放宽到 {escalated} 步继续，无需用户操作"
    return {"type": "limit", "limit": int(limit),
            "next_limit": int(escalated) if escalated else 0, "message": message}


def log_escalation(run: Any, limit: int, escalated: Optional[int]) -> None:
    """把放宽/收尾写进运行日志（有 run 上下文时才写）。"""
    if run is None:
        return
    try:
        run.log(limit_event(limit, escalated=escalated)["message"])
    except Exception:  # noqa: BLE001 - 记日志失败不该影响运行
        logger.debug("写步数日志失败", exc_info=True)
