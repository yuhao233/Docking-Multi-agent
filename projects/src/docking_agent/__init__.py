"""分子对接多 Agent 协作系统（本地部署版）。

包导入即完成运行时环境准备（MPLCONFIGDIR、.env 等），
确保任何入口（CLI / API / 脚本 / `uvicorn docking_agent.api.app:app`）
在导入 matplotlib、rdkit 之前环境已就绪。
"""
from docking_agent.config import ensure_runtime_env as _ensure_runtime_env

_ensure_runtime_env()

__version__ = "0.8.0"

__all__ = ["__version__"]
