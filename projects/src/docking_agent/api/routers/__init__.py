"""API 路由模块（按功能分组），由 `api/app.py:create_app()` 统一装配。

拆分动机：`create_app()` 原先在一个闭包里注册 ~36 条路由，无法单独测试、也无法
一眼看清接口面。现在每个模块自带 `router = APIRouter()`，路由函数是**模块级函数**
（可直接被标准 Agent Protocol 面复用，例如 `agent.api_agent_stream`）。
"""
from __future__ import annotations

from fastapi import FastAPI

from docking_agent.api.routers import agent, legacy, meta, runs, settings, uploads


def register_routers(app: FastAPI) -> None:
    """把各功能路由挂到应用上（顺序不影响路径匹配：没有重叠的通配路由）。"""
    for module in (settings, meta, uploads, runs, agent, legacy):
        app.include_router(module.router)
