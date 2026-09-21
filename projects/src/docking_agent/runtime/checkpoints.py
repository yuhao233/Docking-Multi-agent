"""检查点存储（checkpointer）。

本地化改造：原实现优先连接 Coze 平台的 Postgres（AsyncPostgresSaver），失败再退化内存。
本地部署默认使用进程内 InMemorySaver（旧名 MemorySaver 是同一对象的别名）；如需跨进程持久化，可设置
`CHECKPOINT_BACKEND=sqlite`（需额外安装 langgraph-checkpoint-sqlite）。
"""
from __future__ import annotations

import logging
from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from docking_agent.config import env

logger = logging.getLogger(__name__)

_checkpointer: Optional[BaseCheckpointSaver] = None


def _try_sqlite() -> Optional[BaseCheckpointSaver]:
    try:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore

        from docking_agent.paths import workspace_dir

        path = workspace_dir() / "var" / "checkpoints.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        saver = SqliteSaver(conn)
        saver.setup()
        logger.info("使用 SQLite checkpointer: %s", path)
        return saver
    except Exception as e:  # noqa: BLE001
        logger.warning("SQLite checkpointer 不可用(%s)，回退 InMemorySaver", e)
        return None


def get_memory_saver() -> BaseCheckpointSaver:
    """获取 checkpointer 单例。"""
    global _checkpointer
    if _checkpointer is not None:
        return _checkpointer

    if (env("CHECKPOINT_BACKEND", "memory") or "memory").strip().lower() == "sqlite":
        saver = _try_sqlite()
        if saver is not None:
            _checkpointer = saver
            return _checkpointer

    _checkpointer = InMemorySaver()
    logger.info("使用内存 checkpointer（进程重启后多轮会话不保留）")
    return _checkpointer
