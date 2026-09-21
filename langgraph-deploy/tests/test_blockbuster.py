"""blockbuster 回归：`langgraph dev` 默认在事件循环里开着 blockbuster，
任何同步阻塞调用（`os.mkdir` / `os.stat` / 首次 `import matplotlib` …）都会让整次运行
以 `BlockingError` 失败。

本文件在 **asyncio 事件循环里** 打开 `blockbuster.blockbuster_ctx()`，对
agent 包装节点跑一次（真实计算/LLM 全部打桩），断言事件循环里不出现
`BlockingError`。

发现（见文件末尾 xfail）：agent 包装节点的 `build_inner()` 在事件循环内被调用，
而 `_build_coordinator_inner()` → `coordinator.build_agent()` 会同步读取
`config/agent_llm_config.json`（`pathlib.Path.exists()` → `os.stat`），因此在
`langgraph dev` 下首次运行 coordinator / 4 个子 Agent 时必然 `BlockingError`。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, TypedDict

import pytest
from blockbuster import BlockingError, blockbuster_ctx
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

pytestmark = [pytest.mark.offline, pytest.mark.blockbuster]


def _fake_agent():
    async def model_node(payload):
        return {"messages": [AIMessage(content="reply", id="ai-1")]}

    builder = StateGraph(_InnerState)
    builder.add_node("model", model_node)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    return builder.compile(name="fake-agent")


# --------------------------------------------------------------------------- #
# 自证：同一个 harness 确实能抓到事件循环里的同步 I/O（否则回归测试是空转）
# --------------------------------------------------------------------------- #
async def test_blockbuster_harness_detects_blocking_call(tmp_path):
    with blockbuster_ctx():
        with pytest.raises(BlockingError):
            os.mkdir(str(tmp_path / "should-block"))


# --------------------------------------------------------------------------- #
# agent 包装节点：事件循环里不出现阻塞调用
# --------------------------------------------------------------------------- #
class _InnerState(TypedDict, total=False):
    """内层假图的状态（只用到 messages）。"""

    messages: Annotated[list, add_messages]


async def test_agent_node_body_has_no_blocking_calls(workspace, graphs_module, monkeypatch):
    """隔离出「内层图构建」之后，验证包装节点主体在事件循环里没有阻塞 I/O。

    这里把 `_build_coordinator_inner` 直接换成返回假图（不读配置文件），
    因此覆盖的是：open_run(to_thread) → bind_run → inner.ainvoke → persist_agent_run
    (to_thread) → run.finish(to_thread) → save_run(to_thread)。
    """
    fake = _fake_agent()
    monkeypatch.setattr(graphs_module, "_build_coordinator_inner", lambda: fake)
    monkeypatch.setattr(graphs_module, "persist_agent_run",
                        lambda run, messages, text: {"status": "ok"})
    monkeypatch.setattr(graphs_module, "_coordinator", None, raising=False)
    graph = graphs_module.coordinator()

    with blockbuster_ctx():
        out = await graph.ainvoke({"messages": [HumanMessage(content="hi", id="h1")]})

    assert [type(m).__name__ for m in out["messages"]] == ["HumanMessage", "AIMessage"]
    meta = json.loads((Path(out["run_dir"]) / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "ok"


async def test_agent_node_real_build_is_not_loop_safe(workspace, graphs_module, monkeypatch):
    """不隔离内层构建：走真实 `_build_coordinator_inner`（只把 create_agent 换成假图）。

    回归保护（真实缺陷，已修）：内层图曾经在**节点里**（事件循环内）构建，
    构建会读 LLM 配置文件（`Path.exists()` → `os.stat`），被 blockbuster 判为
    BlockingError，coordinator 与 4 个子 Agent 首次运行必然失败。
    现在内层图在**图构建阶段**建好并缓存（`_wrap_agent(name, inner)`）。
    """
    import docking_agent.agents.coordinator as coordinator_mod

    fake = _fake_agent()
    monkeypatch.setattr(coordinator_mod, "create_agent", lambda **kwargs: fake)
    monkeypatch.setattr(graphs_module, "persist_agent_run",
                        lambda run, messages, text: {"status": "ok"})
    monkeypatch.setattr(graphs_module, "_coordinator", None, raising=False)
    graph = graphs_module.coordinator()

    with blockbuster_ctx():
        await graph.ainvoke({"messages": [HumanMessage(content="hi", id="h1")]})
