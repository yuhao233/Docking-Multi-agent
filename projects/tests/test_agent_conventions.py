"""LangChain 1.x / LangGraph 1.x 规范约定回归（P0）。

覆盖本轮规范改造的每一项，避免以后回退：

1. 协调 Agent 的 `state_schema` 必须继承 `langchain.agents.AgentState`
   —— 只继承 `langgraph.graph.MessagesState` 时 `response_format` 结构化输出永远无法终止
   （实测 GraphRecursionError）；继承正确基类后编译图里会有 `structured_response` / `jump_to` 通道。
2. 5 个 Agent 图都必须有**显式图名**（默认名会退化成 `'LangGraph'`，Studio / 回调归属无法区分）。
3. checkpointer 用规范名 `InMemorySaver`（`MemorySaver` 只是向后兼容别名）。
4. `agents/prompts.py` 用显式拼接（不再 `globals()[name] = ...` 改写），并提供 `COORDINATOR_SP` 兜底。
5. 工具的 `args_schema` 必须与函数签名一致；docstring 里按 `- 参数名:` 写的条目必须是真参数
   （真实缺陷：`molecular_property_assessment` 的 docstring 曾写了并不存在的 `protonation_ph`）。
"""
from __future__ import annotations

import inspect
import re
from typing import Any, Dict, List, Set

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel


class _FakeToolModel(GenericFakeChatModel):
    """最小假模型：`create_agent` 会调用 `bind_tools`，这里原样返回自己（零网络）。"""

    def bind_tools(self, *args: Any, **kwargs: Any) -> "_FakeToolModel":  # noqa: D102
        return self


def _fake_model() -> _FakeToolModel:
    return _FakeToolModel(messages=iter(["ok"]))


# --------------------------------------------------------------------------- #
# 1) state_schema / 图名 / checkpointer
# --------------------------------------------------------------------------- #
def test_coordinator_state_schema_extends_langchain_agent_state() -> None:
    from docking_agent.agents.coordinator import AgentState

    # TypedDict 不支持 issubclass/isinstance，且 __mro__ 不体现基类；但继承来的键会并入
    # __annotations__。`structured_response` / `jump_to` 只有 langchain.agents.AgentState 提供：
    # 只继承 langgraph.graph.MessagesState 时这两个键不存在（那正是结构化输出无法终止的版本）。
    annotations = set(AgentState.__annotations__)
    assert {"messages", "structured_response", "jump_to"} <= annotations, annotations


def test_coordinator_graph_has_structured_output_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.agents import coordinator

    monkeypatch.setattr(coordinator, "init_workers", lambda ctx=None: None)
    monkeypatch.setattr(coordinator, "build_chat_llm", lambda ctx=None, role="": _fake_model())
    monkeypatch.setattr(coordinator, "get_memory_saver", lambda: None)
    graph = coordinator.build_agent(None)

    assert graph.name == "coordinator", f"图名必须是 coordinator，实际 {graph.name!r}"
    for channel in ("messages", "structured_response", "jump_to"):
        assert channel in graph.channels, f"缺通道 {channel}"


def test_worker_graphs_have_role_names(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.agents import workers as W

    monkeypatch.setattr(W, "build_chat_llm", lambda ctx=None, role="": _fake_model())
    W.reset_workers()
    try:
        W.init_workers(None)
        for role, getter in (("property", W.get_property_agent), ("pocket", W.get_pocket_agent),
                             ("docking", W.get_docking_agent), ("binding", W.get_binding_agent)):
            graph = getter()
            assert graph is not None, f"{role} 子 Agent 未构建"
            assert graph.name == role, f"{role} 子 Agent 图名应为 {role!r}，实际 {graph.name!r}"
    finally:
        W.reset_workers()


def test_checkpointer_uses_inmemorysaver() -> None:
    from langgraph.checkpoint.memory import InMemorySaver, MemorySaver
    from docking_agent.runtime.checkpoints import get_memory_saver

    assert MemorySaver is InMemorySaver, "MemorySaver 应仍是 InMemorySaver 的兼容别名"
    saver = get_memory_saver()
    assert "InMemorySaver" in type(saver).__name__ or isinstance(saver, InMemorySaver), type(saver)


def test_sub_agent_calls_are_stateless_by_design() -> None:
    """子 Agent 是**无状态执行器**（P1 明确保留的既定设计，不是缺陷）。

    同一轮对话里反复调用同一子 Agent 时，必须拿到**不同的 thread_id**（看不到自己上一次的输出），
    以防两次筛选任务之间上下文串扰；跨步骤共享信息只走运行产物文件与共享黑板。
    """
    from langchain_core.messages import AIMessage
    from docking_agent.agents.workers import invoke_worker

    threads: List[str] = []

    class _FakeAgent:
        def invoke(self, payload: Any, config: Any = None, context: Any = None) -> Any:
            threads.append(((config or {}).get("configurable") or {}).get("thread_id", ""))
            return {"messages": [AIMessage(content='{"status":"ok"}')]}

    agent = _FakeAgent()
    out1 = invoke_worker(agent, "第一次", "run-1")
    out2 = invoke_worker(agent, "第二次", "run-1")

    assert out1 == out2 == '{"status":"ok"}'
    assert len(threads) == 2
    assert threads[0] != threads[1], "同一运行内两次调用不得复用 thread（否则会串味）"
    assert all(t.startswith("run-1-") for t in threads), threads


# --------------------------------------------------------------------------- #
# 2) 工具契约：args_schema 与签名一致 + docstring 不漂移
# --------------------------------------------------------------------------- #
def _all_tools() -> List[Any]:
    """收集项目里所有真正的 `@tool` 对象。

    用 `isinstance(obj, BaseTool)` 判定，而不是 `hasattr(obj, "args")` —— 后者会把
    `functools.partial`、类成员描述符之类的对象也算进来（真实踩到过：TypeError）。
    """
    from langchain_core.tools import BaseTool

    from docking_agent.tools import (binding, dispatch, docking, online, pockets, pose,
                                     properties, recommend, report)

    out: List[Any] = []
    for module in (dispatch, docking, properties, pockets, binding, pose, recommend, report, online):
        for name, obj in vars(module).items():
            if isinstance(obj, BaseTool) and not name.startswith("_"):
                out.append(obj)
    return out


def test_every_tool_args_schema_matches_signature() -> None:
    tools = _all_tools()
    assert len(tools) >= 15, f"工具数量异常：{len(tools)}"
    drift = {}
    for tool in tools:
        # `runtime` 是 LangGraph 注入参数（ToolRuntime）：不在模型可见 args 里，但确实在函数签名上
        params = set(inspect.signature(tool.func).parameters) - {"runtime"}
        if set(tool.args) != params:
            drift[tool.name or tool.func.__name__] = sorted(set(tool.args) ^ params)
    assert not drift, f"args_schema 与函数签名不一致：{drift}"


#: docstring 里按「- 名称:」列出、但属于**返回字段**而非入参的名字（人工核对过的白名单）
_RESULT_FIELDS_IN_DOC = {
    "check_binding_consistency": {"affinity_strong_low_similarity", "similar_but_weak_docking",
                                  "consistent"},
    "*": {"status", "message", "error", "note", "count", "name", "smiles"},
}


def test_tool_docstrings_do_not_document_phantom_params() -> None:
    """docstring 里写的入参必须真实存在（`molecular_property_assessment` 曾漂移出 `protonation_ph`）。"""
    problems: Dict[str, List[str]] = {}
    for tool in _all_tools():
        doc = tool.description or ""
        mentioned: Set[str] = set(re.findall(r"^\s*[-*]\s*([a-z_][a-z_0-9]*)\s*[:：]", doc, re.M))
        allowed = _RESULT_FIELDS_IN_DOC["*"] | _RESULT_FIELDS_IN_DOC.get(tool.name, set())
        phantom = mentioned - set(tool.args) - allowed
        if phantom:
            problems[tool.name] = sorted(phantom)
    assert not problems, (
        f"docstring 写了不存在的入参：{problems}。"
        "要么补参数，要么改文档（本轮就是这么修的）。")


# --------------------------------------------------------------------------- #
# 3) 提示词：显式组合 + 兜底协调提示词
# --------------------------------------------------------------------------- #
def test_prompts_compose_explicitly_and_offer_coordinator_fallback() -> None:
    from docking_agent.agents import prompts as P

    assert "COORDINATOR_SP" in P.__all__
    assert isinstance(P.COORDINATOR_SP, str) and len(P.COORDINATOR_SP) > 200
    assert P.COORDINATOR_SP.endswith(P.DATA_HANDOFF_RULE)
    for name in ("PROPERTY_SP", "DOCKING_SP", "BINDING_SP", "POCKET_SP"):
        base = getattr(P, f"_{name}_BASE")
        value = getattr(P, name)
        assert value == base + P.DATA_HANDOFF_RULE, f"{name} 必须由「基础提示词 + 数据交接纪律」显式拼成"
    assert not any(k == "_name" for k in vars(P)), "不应再有 globals() 改写用的临时变量"


def test_coordinator_uses_config_prompt_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置里有 `sp` 时优先用配置；为空时退到 COORDINATOR_SP（不能出现空系统提示词）。"""
    from docking_agent.agents import coordinator
    from docking_agent.agents.prompts import COORDINATOR_SP

    seen: Dict[str, Any] = {}

    def _capture(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return "graph"

    monkeypatch.setattr(coordinator, "init_workers", lambda ctx=None: None)
    monkeypatch.setattr(coordinator, "build_chat_llm", lambda ctx=None, role="": _fake_model())
    monkeypatch.setattr(coordinator, "get_memory_saver", lambda: None)
    monkeypatch.setattr(coordinator, "create_agent", _capture)

    monkeypatch.setattr(coordinator, "load_llm_config", lambda role="": {"sp": "配置里的提示词",
                                                                       "config": {"model": "m"}})
    coordinator.build_agent(None)
    assert seen["system_prompt"] == "配置里的提示词"

    monkeypatch.setattr(coordinator, "load_llm_config", lambda role="": {"sp": "", "config": {"model": "m"}})
    coordinator.build_agent(None)
    assert seen["system_prompt"] == COORDINATOR_SP
