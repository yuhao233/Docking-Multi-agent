"""环境变量读取的**唯一底层实现**（`.env` + 进程环境变量）。

从 `config.py` 拆出：`settings.py` 只需要「读环境变量」这一层，却因此与 `config`
形成函数级循环（审计 SCC-1 `config ↔ settings`）。拆开后分层是单向的：

    envs（读写原语） ← config（运行时环境 bootstrap） ← settings（界面设置覆盖层）

`config.py` 仍 re-export 这些名字，因此历史导入路径
`from docking_agent.config import env_int` 不受影响。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from docking_agent.paths import project_root

logger = logging.getLogger(__name__)

_LOADED = False


def load_env(force: bool = False) -> None:
    """加载 projects/.env（幂等）。已存在的进程环境变量优先，不会被覆盖。"""
    global _LOADED
    if _LOADED and not force:
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        env_file = project_root() / ".env"
        if env_file.exists():
            load_dotenv(dotenv_path=env_file, override=False)
    except Exception:  # noqa: BLE001
        # 无 python-dotenv 时退化为手写解析
        env_file = project_root() / ".env"
        if env_file.exists():
            for raw in env_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)
    _LOADED = True


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    load_env()
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v


def env_bool(name: str, default: bool = False) -> bool:
    v = env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = env(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    v = env(name)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default
