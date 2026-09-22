"""运行取消：跨线程 / 跨进程的协作式取消。

为什么需要它：确定性流水线在**工作线程 + 多个子进程**里做对接，
`asyncio.Task.cancel()` 只能取消协程，无法让已经跑起来的 Vina 停下。
因此这里用「按 run_id 的线程可见标志」做协作式取消：

  * 父线程在收集结果时检查标志，一旦置位就**终止进程池**并停止继续对接；
  * 子进程内的对接待本轮任务结束即随进程退出而停止；
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
    """任务被用户取消。"""


def cancel_flag(run_id: str) -> threading.Event:
    """取得（或创建）某个运行的取消标志。"""
    with _lock:
        flag = _flags.get(run_id)
        if flag is None:
            flag = threading.Event()
            _flags[run_id] = flag
        return flag


def request_cancel(run_id: str) -> bool:
    """请求取消。返回 True 表示此前未取消（首次请求）。"""
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
    """运行结束后清理，避免标志表无限增长。"""
    with _lock:
        _flags.pop(run_id, None)




