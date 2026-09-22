"""配置与环境变量：替代 coze_workload_identity（平台环境变量下发）。

本地从 `projects/.env` 读取，其次取进程环境变量。

**分层**（审计 SCC-1）：读取原语在 `docking_agent/envs.py`（`settings.py` 只依赖它，
从而不再与 `config` 互相 import）；本模块只负责**运行时环境 bootstrap**
（MPLCONFIGDIR、COZE_WORKSPACE_PATH、设置页运行参数的注入）。
历史导入路径 `from docking_agent.config import env_int` 依旧可用（下面是显式再导出）。
"""
from __future__ import annotations

import logging
import os

from docking_agent.paths import project_root

# 显式再导出（`as` 别名 + __all__ 让 ruff 不把它当未使用导入）
from docking_agent.envs import env as env
from docking_agent.envs import env_bool as env_bool
from docking_agent.envs import env_float as env_float
from docking_agent.envs import env_int as env_int
from docking_agent.envs import load_env as load_env

logger = logging.getLogger(__name__)

#: 图递归上限的默认值（super-step 数）。60 → 120 的原因：一次长筛选里子 Agent 要连续调用
#: 「解析受体 → 口袋 → 对接（分批）→ 结合模式」多轮工具，加上每个模型调用至少 2 个 super-step，
#: 60 在真实长任务上会顶到 `GRAPH_RECURSION_LIMIT`（用户实测报错）。仍可用 `RECURSION_LIMIT` 覆盖。
DEFAULT_RECURSION_LIMIT = 120

__all__ = ["ensure_runtime_env", "env", "env_bool", "env_float", "env_int", "load_env"]


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
