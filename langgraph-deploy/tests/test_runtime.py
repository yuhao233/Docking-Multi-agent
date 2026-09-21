"""runtime 适配层：运行作用域、ContextVar 复位、消息去重、纯文本提取。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from docking_agent.agents.blackboard import current_blackboard
from docking_agent.runs import current_run
from docking_agent.runtime.context import request_context
from docking_graphs.runtime import (
    bind_run,
    call_meta,
    create_run,
    detached_messages,
    last_user_text,
    studio_run,
)

pytestmark = pytest.mark.offline


# --------------------------------------------------------------------------- #
# create_run：目录 + run.json(kind=studio, request.graph)
# --------------------------------------------------------------------------- #
def test_create_run_builds_dir_and_studio_meta(workspace: Path):
    run = create_run("pipeline", {"ligands_text": "CCO", "engine": "vina"})

    assert run.dir.is_dir()
    assert run.dir.parent == workspace / "var" / "runs"
    assert (run.dir / "run.json").is_file()

    meta = json.loads((run.dir / "run.json").read_text(encoding="utf-8"))
    assert meta["kind"] == "studio"
    assert meta["run_id"] == run.id
    assert meta["request"]["graph"] == "pipeline"
    assert meta["request"]["mode"] == "studio"
    assert meta["request"]["ligands_text"] == "CCO"
    assert meta["request"]["engine"] == "vina"
    assert meta["status"] == "running"
    assert meta["log"], "create_run 应至少写入一条运行日志"


def test_create_run_filters_empty_request_fields(workspace: Path):
    run = create_run("intake", {"message": "hi", "receptor": "", "ligands_text": None})
    request = run.data["request"]
    assert request["graph"] == "intake"
    assert request["message"] == "hi"
    assert "receptor" not in request
    assert "ligands_text" not in request


def test_create_run_records_thread_id(workspace: Path):
    run = create_run("coordinator", {"message": "hi"}, thread_id="thread-1")
    assert run.data["thread_id"] == "thread-1"
    meta = json.loads((run.dir / "run.json").read_text(encoding="utf-8"))
    assert meta["thread_id"] == "thread-1"


# --------------------------------------------------------------------------- #
# bind_run：设置三个 ContextVar，退出（含异常路径）后复位
# --------------------------------------------------------------------------- #
def test_bind_run_sets_three_contextvars_and_resets(workspace: Path):
    run = create_run("pipeline", {})

    assert current_run.get() is None
    assert current_blackboard.get() is None
    assert request_context.get() is None

    with bind_run(run) as bound:
        assert bound is run
        assert current_run.get() is run
        board = current_blackboard.get()
        assert board is not None and board.run_id == run.id
        ctx = request_context.get()
        assert ctx is not None and ctx.method == "studio:pipeline"

    assert current_run.get() is None
    assert current_blackboard.get() is None
    assert request_context.get() is None


def test_bind_run_resets_contextvars_on_exception(workspace: Path):
    run = create_run("coordinator", {})

    with pytest.raises(RuntimeError, match="boom"):
        with bind_run(run):
            assert current_run.get() is run
            raise RuntimeError("boom")

    assert current_run.get() is None
    assert current_blackboard.get() is None
    assert request_context.get() is None


def test_bind_run_falls_back_to_studio_kind_without_graph(workspace: Path):
    run = create_run("pipeline", {})
    run.data["request"] = {}          # 模拟 request 被清空的异常情况
    with bind_run(run):
        ctx = request_context.get()
        assert ctx is not None and ctx.method == "studio:studio"


# --------------------------------------------------------------------------- #
# studio_run：同步版作用域
# --------------------------------------------------------------------------- #
def test_studio_run_context_manager(workspace: Path):
    with studio_run("intake", {"message": "hi"}) as run:
        assert current_run.get() is run
        assert request_context.get().method == "studio:intake"
        assert (run.dir / "run.json").is_file()
    assert current_run.get() is None
    assert current_blackboard.get() is None
    assert request_context.get() is None


# --------------------------------------------------------------------------- #
# detached_messages：按「对象身份 + 消息 id」去重
# --------------------------------------------------------------------------- #
def test_detached_messages_dedups_by_identity_and_id():
    a = HumanMessage(content="a", id="dup")
    b = HumanMessage(content="b", id="dup")          # 不同对象、相同 id
    new_ai = AIMessage(content="c", id="new-id")     # 新 id
    new_no_id = HumanMessage(content="fresh")        # 无 id、新对象

    before = [a, b]
    after = [a, b, new_ai, new_no_id]

    # a 命中「对象身份」，b 命中「消息 id」，都不算新增。
    assert detached_messages(before, after) == [new_ai, new_no_id]


def test_detached_messages_empty_history_returns_all():
    m1 = HumanMessage(content="a", id="1")
    m2 = AIMessage(content="b", id="2")
    assert detached_messages([], [m1, m2]) == [m1, m2]
    assert detached_messages([m1], None) == []
    assert detached_messages(None, [m1]) == [m1]


def test_detached_messages_survives_window_dropping_old_messages():
    """内层滑动窗口丢掉旧消息时，只有真正新增的才返回。"""
    old = HumanMessage(content="old", id="old")
    kept = AIMessage(content="kept", id="kept")
    fresh = AIMessage(content="fresh", id="fresh")
    # before 里有 old；after 里 old 被窗口丢掉，kept 是同一个对象，fresh 是新增
    assert detached_messages([old, kept], [kept, fresh]) == [fresh]


# --------------------------------------------------------------------------- #
# last_user_text：str 与 list 两种 content
# --------------------------------------------------------------------------- #
def test_last_user_text_prefers_last_non_empty_human():
    messages = [
        HumanMessage(content="first", id="h1"),
        AIMessage(content="answer", id="a1"),
        HumanMessage(content="  last  ", id="h2"),
    ]
    assert last_user_text(messages) == "last"


def test_last_user_text_handles_list_content():
    messages = [HumanMessage(content=[
        {"type": "text", "text": "hi "},
        {"type": "text", "text": "there"},
        "ignored-string-part",
    ])]
    assert last_user_text(messages) == "hi there"


def test_last_user_text_skips_ai_and_empty():
    assert last_user_text([AIMessage(content="only ai")]) == ""
    assert last_user_text([HumanMessage(content="   ")]) == ""
    assert last_user_text(None) == ""
    assert last_user_text([]) == ""


# --------------------------------------------------------------------------- #
# call_meta
# --------------------------------------------------------------------------- #
def test_call_meta_returns_run_id_and_dir(workspace: Path):
    run = create_run("docking", {"message": "x"})
    meta = call_meta(run)
    assert meta == {"run_id": run.id, "run_dir": str(run.dir)}
    assert Path(meta["run_dir"]).is_dir()
