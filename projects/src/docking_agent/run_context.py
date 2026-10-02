"""「当前运行」的只读获取点：反转 `core/` 到 `runs/` 的反向依赖。

`core/normalize.py` 与 `core/protonation.py` 各自 `from docking_agent.runs import current_run`，
最底层的纯计算包因此传递依赖 `runs` 与 `reporting.store`（持久化层）。`core/` 反向依赖
报告层属于分层错误：单测或脚本只做数值计算，仍会被迫拉起运行记录与产物存储。

约定：上层在导入时注册「怎么读当前运行」，`core/` 只读本模块。
本模块不 import 任何项目内模块，因此可以被任何层安全导入：

    # runs.py（上层）导入时注册
    from docking_agent import run_context
    run_context.set_run_provider(lambda: current_run.get())

    # core/（下层）只读
    from docking_agent import run_context
    run = run_context.active_run_or_none()

没有注册（CLI、脚本、单测）时返回 `None`，与没有运行上下文时的行为一致。
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
