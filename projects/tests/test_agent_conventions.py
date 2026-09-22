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

    # dispatch（协调 Agent 的分发工具）第 2 波已移到 agents/ 层；其余仍在 tools/
    from docking_agent.agents import dispatch
    from docking_agent.tools import (binding, docking, online, pockets, pose,
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
    assert P.COORDINATOR_SP.endswith(P.DATA_HANDOFF_COORDINATOR)
    # 每个角色的「数据交接」块必须只点名**该角色真的拥有**的工具（真实缺陷：共用一份块时
    # pocket Agent 拿到的是 4 个它根本没有的工具名，property/binding 也有 3/4 不相干）
    expected = {
        "PROPERTY_SP": P.DATA_HANDOFF_PROPERTY,
        "DOCKING_SP": P.DATA_HANDOFF_DOCKING,
        "BINDING_SP": P.DATA_HANDOFF_BINDING,
        "POCKET_SP": "",                     # 口袋 Agent 的工具不接收文件 → 不拼该块
    }
    for name, handoff in expected.items():
        base = getattr(P, f"_{name}_BASE")
        value = getattr(P, name)
        assert value == base + handoff + P.PARAMS_CONTRACT + P.OUTPUT_ECONOMY, \
            f"{name} 必须由「基础提示词 + 该角色自己的数据交接纪律 + 参数来源契约 + 输出经济性」显式拼成"
        # 参数契约必须真的写进每个子 Agent 提示词（否则模型不会去读 JSON 块）
        assert "任务参数(JSON)" in value, name
    # 每个角色只应看到自己的工具名；不得出现别的角色的工具名
    role_tools = {
        "PROPERTY_SP": ("molecular_property_assessment",),
        "DOCKING_SP": ("molecular_docking",),
        "BINDING_SP": ("binding_mode_analysis", "check_binding_consistency"),
        "POCKET_SP": (),                     # 口袋 Agent 不涉及文件交接
    }
    others = ("molecular_property_assessment", "molecular_docking",
              "binding_mode_analysis", "check_binding_consistency")
    for name, own in role_tools.items():
        text = getattr(P, name)
        for tool in others:
            if tool not in own:
                assert f"`{tool}(molecule" not in text, f"{name} 不该提及别的角色的工具 {tool}"
    assert not any(k == "_name" for k in vars(P)), "不应再有 globals() 改写用的临时变量"


def test_every_subagent_carries_output_economy() -> None:
    """每个子 Agent 都必须带「输出经济性」：它的输出会被主管 Agent 直接消费。

    只靠提示词要求模型少写是不可靠的，所以另有 `reports.py` 的 `agent_note` 校验把
    「一句话」变成机制（见 `tests/test_structured_worker_output.py`）。
    """
    from docking_agent.agents import prompts as P

    for name in ("PROPERTY_SP", "POCKET_SP", "DOCKING_SP", "BINDING_SP"):
        text = getattr(P, name)
        assert P.OUTPUT_ECONOMY in text, f"{name} 缺少输出经济性契约"
        assert "agent_note" in text and "一句" in text, name


# --------------------------------------------------------------------------- #
# 3b) 提示词里点名 `tool(param=...)` 时，param 必须是**该工具真的有的参数**
# --------------------------------------------------------------------------- #
#: 提示词里出现、但属于「返回字段 / JSON 块键」而非工具入参的名字（人工核对过的白名单）
_PROMPT_NON_PARAM_HINTS = {
    "run": {"data"},          # `run.data[...]` 之类的说明性写法
}


def _prompt_texts() -> Dict[str, str]:
    """所有**模型可见的提示词文本**（配置 sp / prompts 常量 / 受理层提示词 / 各工具说明）。"""
    import json as _json
    from pathlib import Path

    from docking_agent.agents import prompts as P
    from docking_agent.intake import INTAKE_SYSTEM_PROMPT

    out: Dict[str, str] = {"prompts.COORDINATOR_SP": P.COORDINATOR_SP,
                           "intake.INTAKE_SYSTEM_PROMPT": INTAKE_SYSTEM_PROMPT}
    for name in dir(P):
        if name.endswith(("_SP", "_BASE", "_RULE", "HANDOFF_COORDINATOR",
                          "HANDOFF_PROPERTY", "HANDOFF_DOCKING", "HANDOFF_BINDING")):
            value = getattr(P, name)
            if isinstance(value, str):
                out[f"prompts.{name}"] = value
    for tool in _all_tools():
        out[f"tool:{tool.name}"] = tool.description or ""
    cfg = Path("config/agent_llm_config.json")
    if cfg.is_file():
        raw = cfg.read_text(encoding="utf-8")
        try:
            out["config.agent_llm_config.sp"] = _json.loads(raw).get("sp") or ""
        except Exception:                                     # 允许静默：配置坏了由 test_docs_consistency 负责报错
            pass
    return out


def _phantom_prompt_params(texts: Dict[str, str],
                           known: Dict[str, Set[str]]) -> "tuple[Dict[str, List[str]], int]":
    """抽出「提示词点名了工具没有的参数」；返回 (问题表, 扫描到的参数写法数)。"""
    mention_re = re.compile(r"([a-z_][a-z_0-9]{3,})\(([^()`]{0,200})")
    key_re = re.compile(r"(?:^|[\s,(])([a-z_][a-z_0-9]*)\s*=")
    problems: Dict[str, List[str]] = {}
    scanned = 0
    for label, text in texts.items():
        for name, args in mention_re.findall(text):
            tool_args = known.get(name)
            if tool_args is None:
                continue                                       # 不是工具名（正文示例/函数名）
            for key in set(key_re.findall(args)):
                scanned += 1
                allowed = tool_args | _PROMPT_NON_PARAM_HINTS.get(name, set())
                if key not in allowed:
                    problems.setdefault(f"{label} → {name}", []).append(key)
    return problems, scanned


def test_prompt_tool_calls_only_mention_real_parameters() -> None:
    """提示词里 ``tool(param=...)`` 的 param 必须真实存在。

    真实缺陷：兜底协调提示词写了 ``run_property_assessment(molecules_file=...)``，
    而该 dispatch 工具**没有**这个参数（它自己把本次运行的产物路径交给子 Agent）——
    模型照着传会拿到 args_schema 校验错误，白烧一轮往返。
    """
    known = {tool.name: set(tool.args) for tool in _all_tools()}
    problems, scanned = _phantom_prompt_params(_prompt_texts(), known)
    assert scanned >= 8, f"扫描到的工具参数写法太少（{scanned}），守卫可能已失效"
    assert not problems, (
        f"提示词点名了工具没有的参数：{problems}。"
        "要么改提示词，要么给工具补参数（本轮就是这么发现的）。")


def test_prompt_param_guard_rejects_a_planted_phantom_parameter() -> None:
    """负向回归：守卫必须真的能抓到「点名不存在的参数」，否则它只是装饰。"""
    known = {"run_property_assessment": {"molecules_json"},
             "run_docking": {"molecules_file"}}
    bad = {"x": "请调用 `run_property_assessment(molecules_file=...)` 评估；"
                "再 `run_docking(molecules_file=...)` 对接。"}
    problems, scanned = _phantom_prompt_params(bad, known)
    assert scanned >= 2, scanned
    assert any("molecules_file" in v for v in problems.values()), problems

    good = {"x": "请调用 `run_docking(molecules_file=...)` 对接；"
                 "`run_property_assessment(molecules_json=...)` 评估。"}
    assert _phantom_prompt_params(good, known)[0] == {}


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
    # 动态系统提示词中间件必须真的挂上（否则条件纪律段就是死代码 / 或纪律被抽掉没人补）
    names = [type(m).__name__ for m in seen["middleware"]]
    assert "conditional_discipline" in names, f"条件提示词中间件没挂上：{names}"

    monkeypatch.setattr(coordinator, "load_llm_config", lambda role="": {"sp": "", "config": {"model": "m"}})
    coordinator.build_agent(None)
    assert seen["system_prompt"] == COORDINATOR_SP
