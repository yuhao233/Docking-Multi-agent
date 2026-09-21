"""P1 ④：LangGraph 规范化运行上下文（`context_schema` + `Runtime`，双读期）。

覆盖：
1. `AgentContext` 与 `active_*()` 的**优先/回退**语义（有 runtime 用之，没有才回退 ContextVar）；
2. 两个 Agent 图都声明了 `context_schema`，并且 `graph.invoke(..., context=...)` 能把上下文
   一路送到**工具内部**（用真实 `create_agent` + 假模型发起 tool_call 验证，不是纸面配置）；
3. 所有 `@tool` 都接受注入的 `runtime` 但**不把它暴露给模型**（schema 里没有 runtime）；
4. 图调用方（流式 / CLI / API）确实把 context 透传下去；
5. 没有 context 时行为不变（CLI 直调 / 单测路径继续靠 ContextVar 兜底）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

import pytest
from langchain.agents import create_agent
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from docking_agent.runtime.context import (AgentContext, active_blackboard, active_request,
                                           active_run, context_of, current_agent_context)


class _Runtime:
    """最小 `ToolRuntime` 替身：只要有 `.context` 就够 `active_*()` 用。"""

    def __init__(self, context: Any) -> None:
        self.context = context


# --------------------------------------------------------------------------- #
# 1) 双读语义
# --------------------------------------------------------------------------- #
def test_active_helpers_prefer_runtime_over_contextvar() -> None:
    from docking_agent.agents.blackboard import current_blackboard
    from docking_agent.runs import current_run
    from docking_agent.runtime.context import new_context, request_context

    marker_run, marker_board = object(), object()
    ctx = AgentContext(run=marker_run, blackboard=marker_board, request=new_context(method="rt"))
    runtime = _Runtime(ctx)

    assert active_run(runtime) is marker_run
    assert active_blackboard(runtime) is marker_board
    assert active_request(runtime) is ctx.request
    assert context_of(runtime) is ctx

    # 回退：没有 runtime（或类型不对）时读 ContextVar
    token = current_run.set("var-run")
    board_token = current_blackboard.set("var-board")
    req_token = request_context.set(new_context(method="var"))
    try:
        assert active_run() == "var-run"
        assert active_run(_Runtime(AgentContext())) == "var-run", "context 为空也要回退"
        assert active_run(object()) == "var-run", "非 AgentContext 的 runtime 要回退"
        assert active_blackboard() == "var-board"
        assert active_request().method == "var"
    finally:
        current_run.reset(token)
        current_blackboard.reset(board_token)
        request_context.reset(req_token)


def test_current_agent_context_snapshots_contextvars() -> None:
    from docking_agent.runs import current_run

    @dataclass
    class _Run:
        id: str = "run-9"

    run = _Run()
    token = current_run.set(run)
    try:
        ctx = current_agent_context()
        assert ctx.run is run
    finally:
        current_run.reset(token)
    assert ctx.to_dict()["run_id"] == "run-9"


# --------------------------------------------------------------------------- #
# 2) 端到端：context 真的进了工具
# --------------------------------------------------------------------------- #
def _fake_model_with_tool_call(tool_name: str, args: Dict[str, Any]) -> type:
    class _Fake(GenericFakeChatModel):
        def bind_tools(self, *a: Any, **k: Any) -> "_Fake":
            return self

        def _generate(self, messages: List[Any], **kw: Any) -> ChatResult:
            if not any(type(m).__name__ == "ToolMessage" for m in messages):
                msg = AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": "c1"}])
            else:
                msg = AIMessage(content="done")
            return ChatResult(generations=[ChatGeneration(message=msg)])

    return _Fake


def test_context_reaches_tool_through_graph() -> None:
    seen: Dict[str, Any] = {}

    @tool
    def record(runtime: ToolRuntime[AgentContext] = None, text: str = "") -> str:
        """记录工具收到的运行上下文。"""
        seen["run"] = getattr(active_run(runtime), "id", None)
        seen["board"] = active_blackboard(runtime)
        return "ok"

    graph = create_agent(model=_fake_model_with_tool_call("record", {"text": "hi"})(messages=iter(["x"])),
                         tools=[record], context_schema=AgentContext, name="probe")

    @dataclass
    class _Run:
        id: str = "R-CTX"

    graph.invoke({"messages": [HumanMessage("hi")]},
                 context=AgentContext(run=_Run(), blackboard="BOARD"))
    assert seen["run"] == "R-CTX", seen
    assert seen["board"] == "BOARD", seen


def test_context_reaches_real_project_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实工具（molecular_property_assessment）也要从 runtime 里拿到上下文。"""
    from docking_agent.tools import properties

    captured: Dict[str, Any] = {}
    real_request = properties.active_request

    def _fake_request(runtime: Any = None) -> Any:
        captured["runtime_ctx"] = context_of(runtime)
        return real_request(runtime)

    monkeypatch.setattr(properties, "active_request", _fake_request)

    marker_run = object()
    properties.molecular_property_assessment.func(
        molecules_json='[{"name":"乙醇","smiles":"CCO"}]',
        runtime=_Runtime(AgentContext(run=marker_run)))
    assert captured["runtime_ctx"] is not None, "工具没有把 runtime 传给 active_request"
    assert captured["runtime_ctx"].run is marker_run


# --------------------------------------------------------------------------- #
# 3) schema 卫生：runtime 不暴露给模型
# --------------------------------------------------------------------------- #
def test_no_tool_exposes_runtime_to_the_model() -> None:
    from tests.test_agent_conventions import _all_tools  # type: ignore[import-not-found]

    tools = _all_tools()
    assert len(tools) >= 15
    for tl in tools:
        assert "runtime" not in tl.args, f"{tl.name} 把 runtime 暴露给了模型：{sorted(tl.args)}"
        # 模型侧 schema 必须能正常生成（`get_input_jsonschema` 对注入型工具会失败，属已知限制）
        props = tl.tool_call_schema.model_json_schema()["properties"]
        assert "runtime" not in props, (tl.name, props)


# --------------------------------------------------------------------------- #
# 4) 图调用方透传 context
# --------------------------------------------------------------------------- #
def test_agents_declare_context_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.agents import coordinator

    seen: Dict[str, Any] = {}
    monkeypatch.setattr(coordinator, "init_workers", lambda ctx=None: None)
    monkeypatch.setattr(coordinator, "build_chat_llm",
                        lambda ctx=None, role="": GenericFakeChatModel(messages=iter(["ok"])))
    monkeypatch.setattr(coordinator, "get_memory_saver", lambda: None)
    monkeypatch.setattr(coordinator, "create_agent", lambda **kw: seen.update(kw) or "graph")
    coordinator.build_agent(None)
    assert seen["context_schema"] is AgentContext


def test_stream_agent_sse_forwards_context() -> None:
    import asyncio

    from docking_agent.runtime import streaming as S

    captured: Dict[str, Any] = {}

    class _Graph:
        async def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                          context: Any = None) -> Any:
            captured["context"] = context
            captured["modes"] = stream_mode
            if False:  # pragma: no cover - 只为把它写成 async generator
                yield None

    marker = AgentContext(run="R-STREAM")
    async def _drain() -> None:
        async for _ in S.stream_agent_sse(_Graph(), {}, {}, "R-1", context=marker):
            pass

    asyncio.run(_drain())
    assert captured["context"] is marker
    assert "custom" in captured["modes"], captured["modes"]


def test_project_graph_callers_pass_context() -> None:
    """API / CLI 的图调用点必须传 context（源码级断言，防止以后漏掉）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "docking_agent"
    app = (root / "api" / "app.py").read_text(encoding="utf-8")
    cli = (root / "cli.py").read_text(encoding="utf-8")
    assert "context=current_agent_context()" in app
    assert "context=AgentContext(run=run)" in app, "兼容端点要显式传 run"
    assert app.count("context=current_agent_context()") >= 2
    assert cli.count("context=current_agent_context()") >= 2
