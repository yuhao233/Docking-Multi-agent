"""运行取消：跨线程与跨进程的协作式取消。

确定性流水线在工作线程与多个子进程里执行对接，`asyncio.Task.cancel()` 只能取消协程，
无法停止已经启动的 Vina 进程。本模块因此以按 `run_id` 的线程可见标志实现协作式取消：

  * 父线程在收集结果时检查标志，标志置位即终止进程池并停止后续对接；
  * 子进程内的对接在本轮任务结束时随进程退出而停止；
  * 调用方（流水线 / API）据此把运行标记为 `cancelled` 并保留已完成的部分数据。
"""
from __future__ import annotations

import logging
import threading
from typing import Dict

logger = logging.getLogger(__name__)

_flags: Dict[str, threading.Event] = {}
_lock = threading.Lock()


class CancelledRun(RuntimeError):
    """任务已被调用方取消。"""


def cancel_flag(run_id: str) -> threading.Event:
    """取得或创建某个运行的取消标志。"""
    with _lock:
        flag = _flags.get(run_id)
        if flag is None:
            flag = threading.Event()
            _flags[run_id] = flag
        return flag


def request_cancel(run_id: str) -> bool:
    """请求取消。返回 True 表示本次请求之前该运行未被取消。"""
    flag = cancel_flag(run_id)
    first = not flag.is_set()
    flag.set()
    if first:
        logger.info("已请求取消运行 %s", run_id)
    return first


def is_cancelled(run_id: str) -> bool:
    with _lock:
        flag = _flags.get(run_id)
    return bool(flag and flag.is_set())


def clear_cancel(run_id: str) -> None:
    """运行结束后清理标志，避免标志表持续增长。"""
    with _lock:
        _flags.pop(run_id, None)




