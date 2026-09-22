"""运行上下文：替代 coze_coding_utils.runtime_ctx.context。

语义与原平台保持一致：
  - `new_context(method=..., headers=...)` 生成一次运行的上下文，run_id 为 uuid hex；
  - `request_context` 是 ContextVar，供工具函数取当前上下文（原平台亦如此）；
  - `default_headers(ctx)` 原用于携带平台鉴权头，本地返回空 dict。
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional


@dataclass
class Context:
    run_id: str = ""
    method: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    # 兼容原平台 Context.run_id 类属性访问方式（Context.run_id == ""）
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "method": self.method, "headers": dict(self.headers)}


def new_context(method: str = "run", headers: Optional[Mapping[str, str]] = None) -> Context:
    hdrs: Dict[str, str] = {}
    if headers:
        try:
            hdrs = {str(k): str(v) for k, v in dict(headers).items()}
        except Exception:  # noqa: BLE001
            hdrs = {}
    return Context(run_id=uuid.uuid4().hex, method=method, headers=hdrs)



request_context: ContextVar[Optional[Context]] = ContextVar("request_context", default=None)


# --------------------------------------------------------------------------- #
# LangGraph 规范化运行上下文（P1）：随 `graph.invoke(..., context=...)` 传入
# --------------------------------------------------------------------------- #
@dataclass
class AgentContext:
    """一次 Agent 运行的上下文，交给 LangGraph 的 `context_schema` 机制传递。

    与既有 ContextVar 的关系（**双读期**，P2 会删掉 ContextVar）：

    | 通道 | 谁在用 | 地位 |
    | --- | --- | --- |
    | `AgentContext`（本类，经 `Runtime.context` 注入工具） | 所有走图的调用（网页 / CLI / Studio） | **权威来源** |
    | `current_run` / `current_blackboard` / `request_context` | CLI 直调、单测、不经图的工具调用 | 兼容兜底 |

    工具的读取一律走 `active_run(runtime)` / `active_blackboard(runtime)` /
    `active_request(runtime)`：有 runtime 就用它，没有才回退 ContextVar ——
    这样"图调用"与"直接调用"两条路径**行为一致**，迁移期间也不会出现半吊子状态。
    """

    run: Any = None
    blackboard: Any = None
    request: Optional[Context] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"run_id": getattr(self.run, "id", ""), **(self.request.to_dict() if self.request else {})}


def context_of(runtime: Any) -> Optional[AgentContext]:
    """从工具/中间件的 `runtime` 参数里取出 `AgentContext`（没有/类型不符则 None）。"""
    ctx = getattr(runtime, "context", None)
    return ctx if isinstance(ctx, AgentContext) else None


def active_run(runtime: Any = None) -> Any:
    """当前运行：优先 `runtime.context.run`，回退 `current_run` ContextVar。"""
    ctx = context_of(runtime)
    if ctx is not None and ctx.run is not None:
        return ctx.run
    from docking_agent.runs import current_run  # 延迟导入，避免循环依赖

    return current_run.get()


def active_blackboard(runtime: Any = None) -> Any:
    """当前共享黑板，按 **context → LangGraph store → ContextVar** 的优先级取。

    - `runtime.context.blackboard`：调用方显式给的黑板视图（生产路径都是 store 视图）；
    - `runtime.store` + run id：P2-c 的规范后端（父图与子 Agent 共享同一命名空间）；
    - ContextVar：CLI / 单测 / 不经图的直调兜底（按用户决策保留）。
    """
    ctx = context_of(runtime)
    if ctx is not None and ctx.blackboard is not None:
        return ctx.blackboard
    store = getattr(runtime, "store", None)
    run = ctx.run if ctx is not None else None
    if store is not None and run is not None:
        from docking_agent.runtime.blackboard import store_blackboard  # 延迟导入

        return store_blackboard(store, getattr(run, "id", "") or "")
    from docking_agent.runtime.blackboard import current_blackboard  # 延迟导入

    return current_blackboard.get()


def active_request(runtime: Any = None) -> Optional[Context]:
    """当前请求上下文：优先 `runtime.context.request`，回退 ContextVar。"""
    ctx = context_of(runtime)
    if ctx is not None and ctx.request is not None:
        return ctx.request
    return request_context.get()


def current_agent_context() -> AgentContext:
    """用当前进程内的 ContextVar 组装一个 `AgentContext`（图调用方在 invoke 时用）。"""
    from docking_agent.runtime.blackboard import current_blackboard  # 延迟导入
    from docking_agent.runs import current_run  # 延迟导入

    return AgentContext(run=current_run.get(), blackboard=current_blackboard.get(),
                        request=request_context.get())
