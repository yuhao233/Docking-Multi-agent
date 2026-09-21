"""agent 包装图（`_wrap_agent` 单节点）：用假的 create_agent 图打桩内层 Agent。

真实验证点：
  * 内层图在运行作用域里被调用（ContextVar 生效）；
  * 收尾调用 `persist_agent_run` 并 `run.finish("ok")`；
  * 图返回值只回传**新增**消息（不回显输入、不翻倍历史）；
  * 内层抛错时 run 被标 error 且异常向上抛。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, TypedDict

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

pytestmark = pytest.mark.offline


class _InnerState(TypedDict, total=False):
    messages: Annotated[list, add_messages]


def _make_reply_agent():
    """手工搭一个「往 messages 追加一条新 AIMessage」的假内层 Agent。"""
    state = {"n": 0}

    async def model_node(payload):
        state["n"] += 1
        return {"messages": [AIMessage(content=f"reply-{state['n']}",
                                       id=f"ai-{state['n']}")]}

    builder = StateGraph(_InnerState)
    builder.add_node("model", model_node)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    return builder.compile(name="fake-agent")


def _make_raising_agent():
    async def boom(payload):
        raise RuntimeError("inner boom")

    builder = StateGraph(_InnerState)
    builder.add_node("model", boom)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    return builder.compile(name="raising-agent")


@pytest.fixture
def wrap_coordinator(monkeypatch, graphs_module):
    """把 coordinator 的内层构建重定向到一个假 create_agent，并记录 persist 调用。"""
    import docking_agent.agents.coordinator as coordinator_mod

    persisted: list[dict] = []

    def install(inner_graph):
        monkeypatch.setattr(coordinator_mod, "create_agent",
                            lambda **kwargs: inner_graph)
        monkeypatch.setattr(graphs_module, "_coordinator", None, raising=False)

        def record(run, messages, text):
            persisted.append({"run_id": run.id, "messages": list(messages), "text": text})
            return {"status": "ok"}

        monkeypatch.setattr(graphs_module, "persist_agent_run", record)
        return graphs_module.coordinator(), persisted

    return install


async def test_wrapper_persists_finishes_and_returns_only_new_messages(
        workspace, wrap_coordinator):
    graph, persisted = wrap_coordinator(_make_reply_agent())

    out = await graph.ainvoke({"messages": [HumanMessage(content="hi", id="h1")]})

    # 产物被重定向到临时工作区，没有写进 projects/
    assert Path(out["run_dir"]).parent == workspace / "var" / "runs"

    kinds = [type(m).__name__ for m in out["messages"]]
    assert kinds == ["HumanMessage", "AIMessage"], "只应新增一条 AI 消息，不回显输入"
    assert out["messages"][0].id == "h1"
    assert out["messages"][1].id == "ai-1"
    assert out["messages"][1].content == "reply-1"

    # persist_agent_run 被调用，且拿到的是内层完整消息 + 最后一条 AI 文本
    assert len(persisted) == 1
    assert persisted[0]["text"] == "reply-1"
    assert len(persisted[0]["messages"]) == 2

    # run.finish("ok") 生效
    run_dir = Path(out["run_dir"])
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "ok"
    assert meta["kind"] == "studio"
    assert meta["request"]["graph"] == "coordinator"
    assert meta["finished_at"] is not None
    assert out["run_id"] == meta["run_id"]


async def test_wrapper_does_not_double_history_across_turns(wrap_coordinator):
    graph, persisted = wrap_coordinator(_make_reply_agent())

    first = await graph.ainvoke({"messages": [HumanMessage(content="hi", id="h1")]})
    second = await graph.ainvoke({
        "messages": [*first["messages"], HumanMessage(content="again", id="h2")]})

    ids = [m.id for m in second["messages"]]
    assert ids == ["h1", "ai-1", "h2", "ai-2"], f"历史被翻倍：{ids}"
    assert [type(m).__name__ for m in second["messages"]] == [
        "HumanMessage", "AIMessage", "HumanMessage", "AIMessage"]
    assert len(persisted) == 2
    assert persisted[1]["text"] == "reply-2"


async def test_wrapper_same_input_twice_does_not_double(wrap_coordinator):
    """同一输入连续调用两次：每次都从干净状态开始，历史不累加。"""
    graph, _ = wrap_coordinator(_make_reply_agent())

    def payload():
        return {"messages": [HumanMessage(content="hi", id="h1")]}

    first = await graph.ainvoke(payload())
    second = await graph.ainvoke(payload())

    for out in (first, second):
        assert [type(m).__name__ for m in out["messages"]] == ["HumanMessage", "AIMessage"]
    assert len(second["messages"]) == 2


async def test_wrapper_marks_error_and_reraises(wrap_coordinator):
    graph, persisted = wrap_coordinator(_make_raising_agent())

    with pytest.raises(RuntimeError, match="inner boom"):
        await graph.ainvoke({"messages": [HumanMessage(content="hi", id="h1")]})

    # 失败时不应落 Agent 结果
    assert persisted == []

    # run 目录由 open_run 建好；扫描临时工作区找到唯一一次运行
    import docking_agent.paths as paths

    run_dirs = sorted((Path(paths.workspace_dir()) / "var" / "runs").iterdir())
    assert len(run_dirs) == 1
    meta = json.loads((run_dirs[0] / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "error"
    assert "inner boom" in (meta["error"] or "")
