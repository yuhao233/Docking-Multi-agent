"""点选候选即续跑同一个运行（设计约束），不另起运行记录。

共晶配体阳性对照的问题被回答后，该答案属于发起提问的那次运行，
界面因此不生成新的运行记录。

实现要点：
* 请求带 `resume_run_id` 时复用该运行，不新建目录、不重新受理，
  `task_spec` / `param_plan` 沿用第一次受理的结果；
* 答案作为一条 `HumanMessage` 注入线程，并以 `None` 入参从 checkpoint 继续；
* 被回答的候选（`positive_control` 等）连同阻断标记一起清掉，否则工具护栏继续拒绝开跑。
"""
from __future__ import annotations

import json
from typing import Any, List

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage


class _StubGraph:
    """最小假图：记录 astream 的入参（None = 从 checkpoint 续跑）与注入的消息。"""

    def __init__(self) -> None:
        self.payloads: List[Any] = []
        self.injected: List[Any] = []
        self.updated: List[Any] = []

    async def aget_state(self, config: Any = None) -> Any:
        values = {"messages": [HumanMessage("原始问题", id="h1"),
                               AIMessage("等待用户选择", id="a1")]}
        return type("_Snapshot", (), {"values": values})()

    async def aupdate_state(self, config: Any, values: Any, **kwargs: Any) -> Any:
        self.updated.append(values)
        messages = (values or {}).get("messages") or []
        self.injected.extend(messages)
        return {"configurable": config}

    async def astream(self, payload: Any, config: Any = None, **kwargs: Any) -> Any:
        self.payloads.append(payload)
        yield "messages", (AIMessage(content="已按你的选择继续（测试桩）", id="a2"), {})


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("LLM_API_KEY", "sk-test-secret-1234")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    from docking_agent.api.app import app

    with TestClient(app) as c:
        yield c


def _paused_run(store: Any, *, conversation_id: str) -> Any:
    """造一个停在候选问题上的运行，字段与阻断暂停后的状态一致。"""
    run = store.new("agent", {"message": "使用上传的文件完成对接", "mode": "chat"})
    run.set(status="needs_user_input", conversation_id=conversation_id, thread_id=conversation_id,
            task_spec={"task_type": "screening"}, param_plan={"exhaustiveness": 32})
    run.data["choices"] = [
        {"id": "positive_control:E:2:Z9N", "kind": "positive_control", "label": "把 Z9N 作为对照",
         "value": "OC[C@H]1O[C@@](O)(CO)[C@@H](O)[C@@H]1O",
         "prompt": "把共晶配体 Z9N 作为阳性对照，重新完成对接"},
        {"id": "positive_control:none", "kind": "positive_control", "label": "不使用阳性对照",
         "value": "", "prompt": "不使用阳性对照，直接完成筛选"},
    ]
    run.data["choices_blocking"] = "positive_control"
    run.save()
    return run


def test_choice_answer_resumes_the_same_run(client: TestClient,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.api import support
    from docking_agent.runs import get_run_store

    stub = _StubGraph()
    monkeypatch.setattr(support.state, "get_graph", lambda: stub)

    store = get_run_store()
    conversation = "conv-resume-test"
    run = _paused_run(store, conversation_id=conversation)
    before = {p.name for p in store.root.iterdir() if p.is_dir()}
    try:
        resp = client.post("/api/agent/stream", json={
            "mode": "chat", "advanced": False, "conversation_id": conversation,
            "message": "把共晶配体 Z9N 作为阳性对照，重新完成对接与结合模式对比分析",
            "positive_control": "OC[C@H]1O[C@@](O)(CO)[C@@H](O)[C@@H]1O",
            "positive_control_decision": "use",
            "resume_run_id": run.id, "resume_choice_kind": "positive_control",
        })
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert run.id in body, "SSE 必须回同一个 run_id（续跑而不是新建）"

        after = {p.name for p in store.root.iterdir() if p.is_dir()}
        assert after - before == set(), f"续跑不该新建运行目录：{sorted(after - before)}"

        # 续跑需要真正把图跑起来：暂停后图的 next 已空，
        # 传 astream(None) 不触发任何节点，运行会在 1 秒内标记完成
        assert stub.payloads, "续跑没有把图跑起来"
        payload = stub.payloads[0]
        text = json.dumps(payload, ensure_ascii=False, default=str) if payload else ""
        assert "阳性对照" in text, f"续跑的图输入必须带上用户答案：{text[:200]}"

        resumed = store.load(run.id)
        assert resumed is not None
        assert not resumed.data.get("choices"), "回答后必须清掉候选项（否则界面会重复问）"
        assert not resumed.data.get("choices_blocking"), "回答后必须解除阻断，否则工具拒绝开跑"
        assert (resumed.data.get("request") or {}).get("positive_control"), (
            "答案里的阳性对照 SMILES 必须并入同一运行的请求字段（工具要读到它）")
        assert (resumed.data.get("task_spec") or {}).get("task_type") == "screening", (
            "续跑不重新受理：task_spec / param_plan 沿用第一次受理的结果")
        assert (resumed.data.get("param_plan") or {}).get("exhaustiveness") == 32
    finally:
        import shutil

        for rid in (before ^ {p.name for p in store.root.iterdir() if p.is_dir()}):
            shutil.rmtree(store.root / rid, ignore_errors=True)
        shutil.rmtree(store.root / run.id, ignore_errors=True)
        shutil.rmtree(store.root / f".{run.id}.tmp", ignore_errors=True)


def test_resume_without_pending_choices_falls_back_to_a_new_run(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """没有待回答候选的 run_id（伪造或过期）按新运行处理，不劫持既有记录。"""
    from docking_agent.api import support
    from docking_agent.runs import get_run_store

    stub = _StubGraph()
    monkeypatch.setattr(support.state, "get_graph", lambda: stub)
    store = get_run_store()
    done = store.new("agent", {"message": "已经跑完的运行"})
    done.set(status="ok")
    done.save()
    before = {p.name for p in store.root.iterdir() if p.is_dir()}
    try:
        resp = client.post("/api/agent/stream", json={
            "mode": "chat", "message": "再来一次", "resume_run_id": done.id})
        assert resp.status_code == 200, resp.text
        after = {p.name for p in store.root.iterdir() if p.is_dir()}
        assert after - before, "没有待回答候选时应新建运行"
        assert json.loads((store.root / done.id / "run.json").read_text(encoding="utf-8"))[
            "status"] == "ok", "既有运行记录不得被改写"
    finally:
        import shutil

        for rid in ({p.name for p in store.root.iterdir() if p.is_dir()} - before) | {done.id}:
            shutil.rmtree(store.root / rid, ignore_errors=True)


def test_resume_keeps_user_parameters_and_applies_the_answer(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """续跑不覆盖调用方第一次设置的参数，只覆盖本次真正给出值的字段。

    第一次在表单里设置的 exhaustiveness、盒子与引擎，在点选候选时前端不再重发
    （对话模式只发附件与选择字段）；若续跑把旧请求字典与新字段做浅合并，
    新值为 None 或空串的字段会覆盖原设置，原设置丢失。
    """
    from docking_agent.api import support
    from docking_agent.runs import get_run_store

    stub = _StubGraph()
    monkeypatch.setattr(support.state, "get_graph", lambda: stub)
    store = get_run_store()
    conversation = "conv-param-keep"
    run = _paused_run(store, conversation_id=conversation)
    original = {
        "message": "使用上传的文件完成对接",
        "exhaustiveness": 8, "n_poses": 3, "engine": "external",
        "receptor_file": "/tmp/8ZE2_upload.pdb", "molecule_file": "/tmp/lib.sdf",
        "site_center": [86.63, 76.165, 91.953], "site_size": [22, 22, 22],
        "max_ligands": 500, "keep_hetatm": "HEM",
    }
    run.data["request"] = dict(original)
    run.save()
    try:
        resp = client.post("/api/agent/stream", json={
            "mode": "chat", "conversation_id": conversation,     # 本次请求不带任何表单参数
            "message": "把 Z9N 作为阳性对照继续",
            "positive_control": "OC[C@H]1O[C@@](O)(CO)[C@@H](O)[C@@H]1O",
            "positive_control_decision": "use",
            "resume_run_id": run.id, "resume_choice_kind": "positive_control",
        })
        assert resp.status_code == 200, resp.text
        merged = (store.load(run.id).data.get("request") or {})
        for key, value in original.items():
            if key == "message":
                continue
            assert merged.get(key) == value, f"用户参数 {key} 被续跑覆盖成了 {merged.get(key)!r}"
        assert merged["positive_control"].startswith("OC[C@H]1O"), "答案字段必须并入"
        assert merged["positive_control_decision"] == "use"
        assert merged["message"] == "把 Z9N 作为阳性对照继续", "本轮指令要更新为答案"
    finally:
        import shutil

        shutil.rmtree(store.root / run.id, ignore_errors=True)
        for rid in {p.name for p in store.root.iterdir()
                    if p.is_dir() and p.name.startswith("2026")} - {run.id}:
            pass


# --------------------------------------------------------------------------- #
# 暂停需要等当前步自然结束，不在中途掐断流
# --------------------------------------------------------------------------- #
class _BlockingStubGraph(_StubGraph):
    """第一步就下发阻断式候选（模拟 `molecular_docking` 在并行工具步里发问）。"""

    def __init__(self, run: Any) -> None:
        super().__init__()
        self._run = run

    async def astream(self, payload: Any, config: Any = None, **kwargs: Any) -> Any:
        self.payloads.append(payload)
        self._run.data["choices"] = [
            {"id": "positive_control:E:2:Z9N", "kind": "positive_control", "label": "用 Z9N 作对照",
             "value": "OC[C@H]1O[C@@](O)(CO)[C@@H](O)[C@@H]1O", "prompt": "把 Z9N 作为阳性对照继续"},
        ]
        self._run.data["choices_blocking"] = "positive_control"
        yield "messages", (AIMessage(content="第一段：正在并行评估与对接", id="a1"), {})
        yield "messages", (AIMessage(content="第二段：等你点选后再开跑", id="a2"), {})
        yield "messages", (AIMessage(content="第三段：结束", id="a3"), {})


def test_blocking_choice_does_not_cut_the_stream(client: TestClient,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """阻断式候选下发后不中断流，否则同一步里仍在执行的工具结果会丢。

    `run_property_assessment` 与 `run_docking` 并行进行、候选中途下发时，
    此前的实现会在此处以 `return` 掐断 SSE，性质评估已经跑完但其 ToolMessage
    未进 checkpoint；点选后只能补「该调用被中断、没有结果」的占位回执，
    模型据此直接收尾，对接未执行（`ranking.json` 是空数组）。
    """
    from docking_agent.api import support
    from docking_agent.runs import get_run_store

    store = get_run_store()
    run = store.new("agent", {"message": "上传文件做筛选", "mode": "chat"})
    stub = _BlockingStubGraph(run)
    monkeypatch.setattr(support.state, "get_graph", lambda: stub)
    try:
        resp = client.post("/api/agent/stream", json={
            "mode": "chat", "message": "上传文件做筛选", "conversation_id": "conv-blocking"})
        assert resp.status_code == 200, resp.text
        assert stub.payloads, "请求没有走到图执行"
        # 三段都在流里，可见没有中途掐断（此前的实现只输出到第一段之前）
        for marker in ("第一段", "第二段", "第三段"):
            assert marker in resp.text, f"流被提前掐断，缺少：{marker}"
    finally:
        run.data.pop("choices", None)
        run.data.pop("choices_blocking", None)
        import shutil

        shutil.rmtree(store.root / run.id, ignore_errors=True)


def test_pending_blocking_choice_marks_the_run_needs_user_input(tmp_path: Any) -> None:
    """存在待回答的阻断式问题时，即使别的工具已经出结果，运行状态也标为 needs_user_input。

    否则运行列表显示 ok，被误判为已完成，而对接未执行（工具按护栏拒绝开跑）。
    """
    from docking_agent.agents.persistence import persist_agent_run
    from docking_agent.runs import Run

    run = Run(tmp_path, "20260924-011325-0913", "agent", {"message": "上传文件做筛选"})
    run.data["choices"] = [{"id": "positive_control:none", "kind": "positive_control",
                            "label": "不使用对照", "value": "", "prompt": "不用对照"}]
    run.data["choices_blocking"] = "positive_control"
    from langchain_core.messages import AIMessage

    result = persist_agent_run(run, [AIMessage(content="等待用户选择", id="a1")], "")
    assert result["status"] == "needs_user_input", result["status"]
    assert result.get("needs_user_input") is True
    assert "暂停" in str(result.get("pause_reason") or "")
