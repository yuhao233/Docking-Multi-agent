"""「当前运行」的**只读获取点**：反转 `core/` → `runs/` 的反向依赖（审计 V2）。

问题：`core/normalize.py` 与 `core/protonation.py` 各有一处
`from docking_agent.runs import current_run`，于是最底层的**纯计算包**传递依赖了
`runs` → `reporting.store`（持久化层）。`core/` 反向依赖报告层是分层错误：
单测/脚本只想算个数，却被迫拉起运行记录与产物存储。

做法：上层在导入时把「怎么读当前运行」注册进来，`core/` 只读本模块。
本模块**不 import 任何项目内模块**（保证可以被任何层安全导入）：

    # runs.py（上层）导入时注册
    from docking_agent import run_context
    run_context.set_run_provider(lambda: current_run.get())

    # core/（下层）只读
    from docking_agent import run_context
    run = run_context.active_run_or_none()

没有注册（CLI / 脚本 / 单测）时返回 `None`，与原先「没有运行上下文」的行为一致。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

#: 「返回当前运行对象，或 None」的提供者；由 `runs.py` 在导入时注册。
_provider: Optional[Callable[[], Any]] = None


def set_run_provider(provider: Optional[Callable[[], Any]]) -> None:
    """注册/清除当前运行的读取方式（传 None 用于测试隔离）。"""
    global _provider
    _provider = provider


def active_run_or_none() -> Any:
    """当前运行对象；没有运行上下文时返回 None（提供者抛错也按无上下文处理）。"""
    provider = _provider
    if provider is None:
        return None
    try:
        return provider()
    except Exception:  # noqa: BLE001 - 缺上下文不是错误，脚本/单测会走到这里
        return None


def run_request_or_empty() -> dict:
    """当前运行请求体（`run.data["request"]`）；无运行上下文时返回空 dict。"""
    run = active_run_or_none()
    data = getattr(run, "data", None)
    if not isinstance(data, dict):
        return {}
    request = data.get("request")
    return dict(request) if isinstance(request, dict) else {}
