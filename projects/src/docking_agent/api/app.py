"""FastAPI 应用：交互式网页 + REST/SSE 接口（实现 docs/api.md 契约）。

分层：本模块只做「HTTP 适配」——参数校验、SSE 转发、文件下载；
计算在 `docking_agent.core`，编排在 `docking_agent.agents`，
产物与运行记录在 `docking_agent.runs` / `docking_agent.reporting`。

**结构**（第 2 波拆分）：原先这里是一个 700+ 行的 `create_app()` 闭包，~36 条路由与
全部辅助函数挤在一起。现在：

* 各功能路由 → `api/routers/*.py`（模块级 `APIRouter`，见 `register_routers`）；
* 共享支撑（状态 / 安全边界 / 脱敏 / 上传准备 / 产物下载名）→ `api/support.py`；
* Agent 编排胶水（SSE 心跳 / 线程自愈 / 多轮上下文）→ `api/agent_flow.py`；
* 本模块只负责**组装**：lifespan、安全中间件、挂路由、静态前端、标准 Agent Protocol 面。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from docking_agent import __version__
from docking_agent.api.routers import register_routers
from docking_agent.api.routers.agent import api_agent_stream
from docking_agent.api.support import (
    _CSP,
    _MUTATING_METHODS,
    _engine_available,
    _same_origin,
    state,
)
from docking_agent.config import env_bool, env_float, env_int, load_env
from docking_agent.paths import web_dir
from docking_agent.runs import get_run_store
from docking_agent.runtime.llm import load_llm_config

# 兼容再导出：历史测试/脚本直接 `from docking_agent.api.app import <name>`。
# 用 `as` 别名显式声明再导出（避免 ruff F401 误判为未使用导入）。
from docking_agent.api.support import _completeness as _completeness
from docking_agent.api.support import _serialize as _serialize
from docking_agent.api.routers.settings import _fetch_endpoint_models as _fetch_endpoint_models
from docking_agent.api.routers.settings import _test_role_llm as _test_role_llm

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_env()
    # 布局自检（非严格）：缺 `config/`/`web/`/`assets/` 时响亮告警，而不是等用户
    # 遇到「前端 404 / 找不到受体注册表」再排查（wheel 安装就会这样）。
    from docking_agent.paths import assert_runtime_layout

    assert_runtime_layout()
    cfg = load_llm_config()["config"]
    if cfg.get("api_key"):
        logger.info("LLM 已配置：model=%s base_url=%s", cfg.get("model"), cfg.get("base_url"))
    else:
        logger.warning("未配置 LLM_API_KEY：多 Agent 模式不可用（Agent 链路是唯一执行入口）")
    logger.info("引擎可用性：%s", _engine_available())
    try:
        # 进程重启后不可能还有运行在执行：把残留的 running 如实标记为 interrupted
        fixed = get_run_store().reconcile_interrupted()
        if fixed:
            logger.info("已收尾 %d 个被中断的运行：%s", len(fixed), ", ".join(fixed[:5]))
    except Exception as exc:  # noqa: BLE001 - 收尾失败不能影响服务启动
        logger.warning("收尾被中断的运行失败（忽略）：%s", exc)
    # 运行目录保留策略（**默认关闭**，显式 RUNS_AUTO_PRUNE=1 才启用）：
    # 不设上限时 `var/runs` 会无限增长（实测 2.2 GB / 4900+ 目录），拖慢启动扫描与检索。
    if env_bool("RUNS_AUTO_PRUNE", False):
        try:
            report = get_run_store().prune(
                keep_last=env_int("RUNS_KEEP_LAST", 200),
                max_age_days=env_float("RUNS_MAX_AGE_DAYS", 30.0), dry_run=False)
            if report["deleted"]:
                logger.info("启动清理：删除 %d 个旧运行（释放 %.1f MB）",
                            len(report["deleted"]), report["freed_bytes"] / (1024 * 1024))
            for err in report["errors"]:
                logger.warning("清理旧运行失败：%s", err)
        except Exception as exc:  # noqa: BLE001 - 清理失败不能影响服务启动
            logger.warning("运行目录清理失败（忽略）：%s", exc)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Docking Multi-Agent", version=__version__, lifespan=lifespan)

    # ---------------- 安全边界：同源校验 + 安全响应头 ----------------
    # 规则与常量见 `api/support.py`：带 Origin 且与 Host 不同源 → 拒绝；
    # 不带 Origin（curl / 测试 / 同源导航）放行；反向代理可用 DOCKING_ALLOWED_ORIGINS。
    @app.middleware("http")
    async def _security_boundary(request: Request, call_next):
        if request.method in _MUTATING_METHODS and not _same_origin(request):
            return JSONResponse(
                status_code=403,
                content={"status": "error",
                         "error": "跨站请求被拒绝（服务无鉴权，只接受同源请求）。"
                                  "如经反向代理部署，请用 DOCKING_ALLOWED_ORIGINS 声明允许的来源。"},
            )
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", _CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    # ---------------- 功能路由 ----------------
    register_routers(app)

    # 标准 Agent Protocol 面复用这条链路（同一个 Run / 产物 / 黑板），因此把生成器挂到 app.state
    app.state.legacy_agent_stream = api_agent_stream
    app.state.get_graph = state.get_graph

    # ---------------- 静态前端 ----------------
    def _serve_page(filename: str) -> Any:
        """直出 web/ 下的页面（两套界面共用同一份后端契约与产物）。"""
        html = web_dir() / filename
        if not html.is_file():
            return JSONResponse(status_code=503, content={
                "error_message": f"前端资源缺失：未找到 web/{filename}",
                "hint": "请确认 web/ 目录完整；API 文档见 docs/api.md",
            })
        return FileResponse(str(html))

    @app.get("/")
    async def index() -> Any:
        """**默认首页 = 简易模式**（只有对话与结果；参数走默认与自动规划）。

        两套界面是**同一份后端契约的两个视图**：都走标准 Agent Protocol
        （`/threads/{tid}/runs/stream`）与同一批 `/api/*` 产物接口。
        """
        return _serve_page("simple.html")

    @app.get("/advanced")
    async def advanced() -> Any:
        """高级模式（参数表单 / 历史检索 / 完整报告 / 中间数据 / 设置页）。"""
        return _serve_page("index.html")

    @app.get("/simple")
    async def simple_alias() -> Any:
        """简易模式的别名：默认首页已经是它，这里只为兼容既有链接/书签。"""
        return _serve_page("simple.html")

    static_dir = web_dir()
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 标准 Agent Protocol 面（纯增量：assistants / threads / runs + 标准 SSE 帧）
    from docking_agent.api.agent_service import register_agent_service

    register_agent_service(app)
    return app


app = create_app()
