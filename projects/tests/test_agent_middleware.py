"""Agent 中间件（P1 规范改造）回归测试。

覆盖：
1. `build_agent_middleware` 的四件套与默认阈值；
2. 环境变量可覆盖 / 可关闭（限额 0 = 不限制，摘要 off = 不加中间件）；
3. 触发阈值 ≤ 保留条数时自动纠正（否则会"每轮都摘要"）；
4. 协调 Agent 与子 Agent 的 `create_agent` 真的带上了这套中间件；
5. 摘要中间件在**短对话**下不触发（不对正常筛选引入额外模型调用/历史改写）。
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest
from langchain.agents.middleware import (ModelCallLimitMiddleware, ModelRetryMiddleware,
                                         SummarizationMiddleware, ToolCallLimitMiddleware)
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import MessagesState

from docking_agent.agents.middleware import (DEFAULT_MODEL_CALL_LIMIT, DEFAULT_RETRY_MAX,
                                             DEFAULT_SUMMARY_KEEP, DEFAULT_SUMMARY_TRIGGER,
                                             DEFAULT_TOOL_CALL_LIMIT, build_agent_middleware)


class _Fake(GenericFakeChatModel):
    def bind_tools(self, *args: Any, **kwargs: Any) -> "_Fake":
        return self


def _fake() -> _Fake:
    return _Fake(messages=iter(["ok"]))


def _by_type(mw: List[Any], cls: type) -> Any:
    return next((m for m in mw if isinstance(m, cls)), None)


def test_default_middleware_set_and_thresholds() -> None:
    mw = build_agent_middleware(_fake(), role="coordinator")
    # 第一件是「工具调用配对」自愈（模型调用前补齐悬空 tool_calls，否则 400）
    assert [type(m).__name__ for m in mw] == [
        "ToolCallPairingMiddleware", "ModelRetryMiddleware", "ModelCallLimitMiddleware",
        "ToolCallLimitMiddleware", "SummarizationMiddleware"], [type(m).__name__ for m in mw]

    retry = _by_type(mw, ModelRetryMiddleware)
    assert retry.max_retries == DEFAULT_RETRY_MAX and retry.on_failure == "continue"

    model_limit = _by_type(mw, ModelCallLimitMiddleware)
    assert model_limit.run_limit == DEFAULT_MODEL_CALL_LIMIT
    assert model_limit.thread_limit is None, "只用 run 级限额，避免跨轮累计误伤"
    assert model_limit.exit_behavior == "end", "安全网命中要优雅结束，不能 error 丢掉已有结果"

    tool_limit = _by_type(mw, ToolCallLimitMiddleware)
    assert tool_limit.run_limit == DEFAULT_TOOL_CALL_LIMIT
    assert tool_limit.exit_behavior == "end"

    summary = _by_type(mw, SummarizationMiddleware)
    assert summary.trigger == ("messages", DEFAULT_SUMMARY_TRIGGER)
    assert summary.keep == ("messages", DEFAULT_SUMMARY_KEEP)
    assert DEFAULT_SUMMARY_TRIGGER < 40, "触发阈值应低于上下文窗口上限，否则永远不会触发"
    assert DEFAULT_SUMMARY_KEEP < DEFAULT_SUMMARY_TRIGGER


def test_middleware_thresholds_are_env_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_RETRY_MAX", "5")
    monkeypatch.setenv("AGENT_MODEL_CALL_LIMIT", "7")
    monkeypatch.setenv("AGENT_TOOL_CALL_LIMIT", "9")
    monkeypatch.setenv("AGENT_SUMMARY_TRIGGER_MESSAGES", "44")
    monkeypatch.setenv("AGENT_SUMMARY_KEEP_MESSAGES", "11")
    mw = build_agent_middleware(_fake(), role="probe")

    assert _by_type(mw, ModelRetryMiddleware).max_retries == 5
    assert _by_type(mw, ModelCallLimitMiddleware).run_limit == 7
    assert _by_type(mw, ToolCallLimitMiddleware).run_limit == 9
    assert _by_type(mw, SummarizationMiddleware).trigger == ("messages", 44)
    assert _by_type(mw, SummarizationMiddleware).keep == ("messages", 11)


def test_middleware_can_be_fully_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """四件套都能关掉；**工具调用配对自愈关不掉** —— 它防的是模型端 400（正确性，不是预算）。"""
    monkeypatch.setenv("AGENT_RETRY_MAX", "0")
    monkeypatch.setenv("AGENT_MODEL_CALL_LIMIT", "0")
    monkeypatch.setenv("AGENT_TOOL_CALL_LIMIT", "0")
    monkeypatch.setenv("AGENT_SUMMARY_ENABLED", "off")
    assert [type(m).__name__ for m in build_agent_middleware(_fake(), role="probe")] == [
        "ToolCallPairingMiddleware"]


def test_summary_trigger_is_corrected_when_not_greater_than_keep(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_SUMMARY_TRIGGER_MESSAGES", "5")
    monkeypatch.setenv("AGENT_SUMMARY_KEEP_MESSAGES", "20")
    summary = _by_type(build_agent_middleware(_fake(), role="probe"), SummarizationMiddleware)
    assert summary.trigger == ("messages", 22), "触发阈值必须大于保留条数，否则每轮都摘要"
    assert summary.keep == ("messages", 20)


def test_coordinator_passes_middleware_to_create_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.agents import coordinator

    seen: Dict[str, Any] = {}

    def _capture(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return "graph"

    monkeypatch.setattr(coordinator, "init_workers", lambda ctx=None: None)
    monkeypatch.setattr(coordinator, "build_chat_llm", lambda ctx=None, role="": _fake())
    monkeypatch.setattr(coordinator, "get_memory_saver", lambda: None)
    monkeypatch.setattr(coordinator, "create_agent", _capture)
    coordinator.build_agent(None)

    names = [type(m).__name__ for m in seen["middleware"]]
    # 协调 Agent 比子 Agent 多一个**条件系统提示词**中间件（按运行事实抽掉用不到的纪律段；
    # 见 `agents/prompt_blocks.py`）——它排在最后（最内层，改的是最终下发的系统消息）。
    assert names == ["ToolCallPairingMiddleware", "ModelRetryMiddleware", "ModelCallLimitMiddleware",
                     "ToolCallLimitMiddleware", "SummarizationMiddleware",
                     "conditional_discipline"], names
    assert seen["name"] == "coordinator"


def test_worker_agents_pass_middleware_to_create_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.agents import workers as W

    captured: List[Dict[str, Any]] = []

    def _capture(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return {"name": kwargs.get("name")}

    monkeypatch.setattr(W, "build_chat_llm", lambda ctx=None, role="": _fake())
    monkeypatch.setattr(W, "create_agent", _capture)
    W.reset_workers()
    try:
        W.init_workers(None)
    finally:
        W.reset_workers()

    assert [c["name"] for c in captured] == ["property", "pocket", "docking", "binding"]
    for c in captured:
        assert [type(m).__name__ for m in c["middleware"]] == [
            "ToolCallPairingMiddleware", "ModelRetryMiddleware", "ModelCallLimitMiddleware",
            "ToolCallLimitMiddleware", "SummarizationMiddleware"], c["name"]


def test_summarization_is_inert_on_short_conversations() -> None:
    """摘要中间件挂在图上，但短对话（< 触发条数）不得改写历史、不得额外调用模型。"""
    from langchain.agents import create_agent

    calls = {"n": 0}

    class _Counting(GenericFakeChatModel):
        def bind_tools(self, *args: Any, **kwargs: Any) -> "_Counting":
            return self

        def _generate(self, *args: Any, **kwargs: Any) -> Any:  # noqa: D102
            calls["n"] += 1
            return super()._generate(*args, **kwargs)

    llm = _Counting(messages=iter(["回答"] * 10))
    graph = create_agent(model=llm, tools=[], name="probe",
                         middleware=build_agent_middleware(llm, role="probe"),
                         state_schema=MessagesState)
    out = graph.invoke({"messages": [HumanMessage(content="你好")]})
    texts = [getattr(m, "content", "") for m in out["messages"]]
    assert any("你好" in str(t) for t in texts), texts
    assert not any("SESSION INTENT" in str(t) for t in texts), "短对话不该出现摘要内容"
    assert calls["n"] == 1, f"短对话只应有一次模型调用，实际 {calls['n']}"


def test_tiny_graph_sanity_for_summary_threshold() -> None:
    """自证：把触发阈值调到 2 时同一条路径**确实**会摘要（证明上面的"不触发"不是空转）。"""
    from langchain.agents import create_agent
    from langchain.agents.middleware import SummarizationMiddleware

    # 摘要本身也要调一次模型 → 假模型必须备足响应，否则迭代器耗尽会抛 StopIteration
    llm = _Fake(messages=iter(["摘要占位"] * 10))
    summary = SummarizationMiddleware(model=llm, trigger=("messages", 2), keep=("messages", 1))
    graph = create_agent(model=llm, tools=[], name="probe", middleware=[summary],
                         state_schema=MessagesState)
    out = graph.invoke({"messages": [HumanMessage(content="一"), AIMessage(content="二"),
                                     HumanMessage(content="三")]})
    joined = " ".join(str(getattr(m, "content", "")) for m in out["messages"])
    # 官方实现把摘要包成 "Here is a summary of the conversation to date: ..." 的 HumanMessage
    assert "summary of the conversation" in joined.lower(), joined
    # 原文被摘要**替换**（不再是逐条历史）
    contents = [str(getattr(m, "content", "")) for m in out["messages"]]
    assert not any(c == "一" for c in contents), contents
