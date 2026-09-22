"""图递归上限（`GRAPH_RECURSION_LIMIT`）回归：默认值、子 Agent 的显式额度、以及"步数不会平白变多"。

真实故障（用户报错）：

    Recursion limit of 60 reached without hitting a stop condition.

三次成因叠加：

1. **子 Agent 调用根本没给 `recursion_limit`** —— `workers.py::invoke_worker` 只传了
   `configurable.thread_id`，于是用它的是 LangGraph 的默认值（25）。子 Agent 要连着调
   「解析受体 → 口袋 → 对接（分批）→ 结合模式」多轮工具，很快就顶到上限。
2. **默认 60 对长任务偏紧**：每个模型调用至少消耗 2 个 super-step（model + tools），
   60 步只够 ~30 轮；现在提到 120（`RECURSION_LIMIT` 仍可覆盖）。
3. **工具调用配对自愈不能多占步数**：`before_model` 钩子会变成图里的一个**节点**，
   每次模型调用多走一步；改成 `wrap_model_call`（包裹式，不改图结构）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from docking_agent.config import DEFAULT_RECURSION_LIMIT, env_int
from docking_agent.settings import SPECS


def test_default_recursion_limit_is_120() -> None:
    assert DEFAULT_RECURSION_LIMIT == 120


def test_settings_spec_agrees_with_the_constant() -> None:
    """settings 里写字面量（它只依赖 envs.py，不反向导入 config）→ 两者必须一致。"""
    spec = next(s for s in SPECS if s.env == "RECURSION_LIMIT")
    assert spec.default == DEFAULT_RECURSION_LIMIT, (spec.default, DEFAULT_RECURSION_LIMIT)


def test_workers_get_an_explicit_recursion_limit(monkeypatch: Any) -> None:
    """子 Agent 调用必须显式带上 recursion_limit（缺省只有 25，长任务必炸）。"""
    from docking_agent.agents import workers as W

    seen: Dict[str, Any] = {}

    class _FakeAgent:
        def invoke(self, payload: Any, config: Any = None, context: Any = None) -> Any:
            seen["config"] = config
            from langchain_core.messages import AIMessage

            return {"messages": [AIMessage(content='{"status": "ok"}')]}

    out = W.invoke_worker(_FakeAgent(), "做一次口袋分析", "thread-x")
    assert "config" in seen, "没调用到 agent.invoke"
    assert seen["config"]["recursion_limit"] == env_int("RECURSION_LIMIT", DEFAULT_RECURSION_LIMIT)
    assert seen["config"]["configurable"]["thread_id"].startswith("thread-x")
    assert out, "返回结果不应为空"


def test_worker_recursion_limit_follows_the_env(monkeypatch: Any) -> None:
    """环境变量覆盖要真的生效（运维可临时放宽长任务）。"""
    from docking_agent.agents import workers as W

    monkeypatch.setenv("RECURSION_LIMIT", "321")
    seen: Dict[str, Any] = {}

    class _FakeAgent:
        def invoke(self, payload: Any, config: Any = None, context: Any = None) -> Any:
            seen["config"] = config
            from langchain_core.messages import AIMessage

            return {"messages": [AIMessage(content='{"status": "ok"}')]}

    W.invoke_worker(_FakeAgent(), "x", "thread-y")
    assert seen["config"]["recursion_limit"] == 321


def test_pairing_middleware_does_not_add_a_graph_node() -> None:
    """配对自愈用 `wrap_model_call`（包裹式）：**不得**在图里多出一个节点。

    `before_model` 钩子会变成独立节点 —— 每次模型调用多一个 super-step，
    在 `recursion_limit` 面前等于把可用轮数砍掉三分之一（真实故障的放大器）。
    """
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import tool

    from docking_agent.agents.threads import ToolCallPairingMiddleware

    class _Fake(GenericFakeChatModel):
        def bind_tools(self, *args: Any, **kwargs: Any) -> "_Fake":
            return self

    @tool
    def noop(x: str = "") -> str:
        """占位工具。"""
        return "ok"

    graph = create_agent(model=_Fake(messages=iter(["结论"])), tools=[noop],
                         middleware=[ToolCallPairingMiddleware()])
    nodes: List[str] = list(graph.get_graph().nodes)
    assert not [n for n in nodes if "ToolCallPairing" in n or "before_model" in n], nodes
    assert {"model", "tools"} <= set(nodes), nodes
