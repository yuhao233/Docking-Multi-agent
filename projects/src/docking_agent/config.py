"""配置与环境变量：替代 coze_workload_identity（平台环境变量下发）。

本地从 `projects/.env` 读取，其次取进程环境变量。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
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


def ensure_runtime_env() -> None:
    """在导入 rdkit/matplotlib 之前设置必要的运行时环境变量。"""
    load_env()
    # matplotlib 默认缓存目录可能不可写（容器/受控 HOME），统一指向工作区内；
    # 若外部已设置但不可写，则强制覆盖，避免图表绘制时报警并退化到 /tmp
    target = project_root() / "var" / "cache" / "matplotlib"
    target.mkdir(parents=True, exist_ok=True)
    current = os.environ.get("MPLCONFIGDIR")
    if (not current) or (not os.access(current, os.W_OK)):
        os.environ["MPLCONFIGDIR"] = str(target)
    # 兼容仍读取 COZE_WORKSPACE_PATH 的第三方片段
    os.environ.setdefault("COZE_WORKSPACE_PATH", str(project_root()))
    # 设置页面保存的运行类参数（config/local_settings.json）→ 进程环境变量，
    # 这样各模块既有的 env_int/env_bool 读取逻辑无需改动即可生效
    try:
        from docking_agent.settings import apply_runtime_env

        applied = apply_runtime_env()
        if applied:
            logger.info("已应用设置页面的运行参数：%s", ", ".join(applied))
    except Exception as e:  # noqa: BLE001
        logger.warning("应用设置页面的运行参数失败：%s", e)
