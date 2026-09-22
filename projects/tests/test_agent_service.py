"""标准 Agent Protocol 服务面（P1 后端标准化）回归测试。

覆盖（全部**离线**：内部链路用假 SSE 生成器替换，不调 LLM、不做对接）：
1. `_map_legacy_event` / `parse_frame` / `_wait_payload` 的映射语义（含 run_id 不被覆盖）；
2. `_agent_request` 把标准 `input.messages` 转成内部 `AgentRequest`（消息文本 + 表单字段）；
3. System / Assistants 端点（清单、单取、schema、404）；
4. Threads 端点（创建/取/删/state/history + 404）；
5. Thread Runs / Stateless Runs：`wait` 的返回值、`runs` 列表、`cancel`；
6. **标准 SSE 帧**：`metadata → updates → messages/partial → messages/complete → custom → values → end`，
   以及错误路径的 `error` 帧；
7. 既有端点不受影响（`/api/agent/stream` 仍挂在 app 上，标准面是纯增量）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from docking_agent.api import agent_service as AS  # noqa: E402


def _frame_events(text: str) -> List[Dict[str, Any]]:
    """把 SSE 文本切成 [(event, data)] 列表。"""
    out: List[Dict[str, Any]] = []
    event = ""
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            raw = line[len("data:"):].strip()
            try:
                out.append({"event": event, "data": json.loads(raw)})
            except json.JSONDecodeError:
                out.append({"event": event, "data": None})
    return out


def _fake_sse(events: Iterable[Dict[str, Any]]) -> Any:
    """伪造一个 `StreamingResponse`：`body_iterator` 必须是**属性**（真实实现就是异步生成器对象）。"""

    class _Resp:
        def __init__(self) -> None:
            self.body_iterator = self._gen()

        async def _gen(self):  # type: ignore[no-untyped-def]
            for ev in events:
                yield AS.sse_event(ev)

    return _Resp()


def _fake_legacy(events: Iterable[Dict[str, Any]]):  # noqa: ANN202
    async def _call(*_args: Any, **_kwargs: Any) -> Any:
        return _fake_sse(events)

    return _call


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(AS, "reset_platform_runs", AS.reset_platform_runs)
    AS.reset_platform_runs()
    from docking_agent.api.app import app

    with TestClient(app) as c:
        yield c
    AS.reset_platform_runs()


# --------------------------------------------------------------------------- #
# 1) 映射层
# --------------------------------------------------------------------------- #
def test_map_legacy_event_covers_standard_frames() -> None:
    state: Dict[str, Any] = {"seq": 0}
    # 注意：`sse_event` 会给 data 补一个 ts 字段（前端据此算真实起止时间），断言只看关心的键
    event, data = AS.parse_frame(AS.frame("metadata", {"run_id": "run_x"}))
    assert event == "metadata" and data["run_id"] == "run_x" and "ts" in data

    # token → messages/partial（且累加文本）
    frames = AS._map_legacy_event({"type": "token", "content": "你好", "node": "model"}, state=state)
    assert AS.parse_frame(frames[0])[0] == "messages/partial"
    assert AS.parse_frame(frames[0])[1][0]["content"] == "你好"
    assert state["text"] == "你好"

    # update → updates（节点名作 key）
    frames = AS._map_legacy_event({"type": "update", "node": "tools", "keys": ["messages"]}, state=state)
    event, data = AS.parse_frame(frames[0])
    assert event == "updates" and data["tools"] == {"keys": ["messages"]}

    # 领域事件 → custom
    frames = AS._map_legacy_event({"type": "molecules", "rows": [{"name": "乙醇"}]}, state=state)
    assert AS.parse_frame(frames[0])[0] == "custom"

    # final → messages/complete（内容优先用 final 文本）
    frames = AS._map_legacy_event({"type": "final", "content": "最终回答", "run_id": "2026-x"}, state=state)
    event, data = AS.parse_frame(frames[0])
    assert event == "messages/complete" and data[0]["content"] == "最终回答"
    assert state["run_id"] == "2026-x"

    # error → error 帧
    frames = AS._map_legacy_event({"type": "error", "error_code": "X", "error_message": "炸了"}, state=state)
    event, data = AS.parse_frame(frames[0])
    assert event == "error" and data["message"] == "炸了" and state["failed"] is True


def test_wait_payload_never_clobbers_business_run_id() -> None:
    """真实缺陷回归：`{"run_id": platform_id, **values}` 会把业务 run id 覆盖掉。"""
    values = {"run_id": "20260918-120000-0001", "summary": {"status": "ok"}}
    out = AS._wait_payload(values, "run_abc", "tid-1", {"business_run_id": "20260918-120000-0001"})
    assert out["run_id"] == "20260918-120000-0001", "values 里的业务 run id 必须保留"
    assert out["business_run_id"] == "20260918-120000-0001"
    assert out["platform_run_id"] == "run_abc" and out["thread_id"] == "tid-1"


def test_agent_request_maps_messages_and_form_fields() -> None:
    req = AS._agent_request({"messages": [{"type": "human", "content": "筛选这两个分子"}],
                             "exhaustiveness": 8, "mode": "chat"}, "tid-9")
    assert req.message == "筛选这两个分子"
    assert req.exhaustiveness == 8 and req.conversation_id == "tid-9" and req.mode == "chat"
    # 未给的字段保持服务端默认（不猜、不注入）
    assert req.engine == "" and req.n_poses is None and req.save_poses is None


# --------------------------------------------------------------------------- #
# 2) System / Assistants
# --------------------------------------------------------------------------- #
def test_system_endpoints(client: TestClient) -> None:
    assert client.get("/ok").json() == {"ok": True}
    info = client.get("/info").json()
    assert info["version"] and "coordinator" in info["assistants"]
    assert set(info["assistants"]) == set(AS.ASSISTANTS)


def test_assistants_listing_and_schema(client: TestClient) -> None:
    rows = client.post("/assistants/search", json={}).json()
    assert {r["graph_id"] for r in rows} == set(AS.ASSISTANTS)
    assert all(r["assistant_id"] == AS.assistant_id_for(r["graph_id"]) for r in rows)
    assert len(rows) == 6, f"流水线助手已移除，只剩 6 个：{sorted(r['graph_id'] for r in rows)}"

    one = client.post("/assistants/search", json={"graph_id": "coordinator"}).json()
    assert len(one) == 1 and one[0]["graph_id"] == "coordinator"
    assert client.post("/assistants/search", json={"graph_id": "pipeline"}).json() == []

    by_id = client.get(f"/assistants/{AS.assistant_id_for('coordinator')}").json()
    assert by_id["graph_id"] == "coordinator"

    # 已移除的 pipeline 助手在标准面上一律 404
    assert client.get("/assistants/pipeline").status_code == 404
    assert client.get(f"/assistants/{AS.assistant_id_for('pipeline')}/schemas").status_code == 404
    schemas = client.get(f"/assistants/{AS.assistant_id_for('coordinator')}/schemas").json()
    props = schemas["input_schema"]["properties"]
    for field in ("ligands_text", "receptor", "exhaustiveness", "site_center", "save_poses",
                  "messages"):
        assert field in props, field
    coord = client.get(f"/assistants/{AS.assistant_id_for('coordinator')}/schemas").json()
    assert "messages" in coord["input_schema"]["properties"]

    assert client.get("/assistants/nope").status_code == 404
    assert client.get("/assistants/nope/schemas").status_code == 404


# --------------------------------------------------------------------------- #
# 3) Threads
# --------------------------------------------------------------------------- #
def test_threads_lifecycle(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    tid = client.post("/threads", json={}).json()["thread_id"]
    assert client.get(f"/threads/{tid}").json()["thread_id"] == tid
    assert client.get("/threads/unknown-thread").status_code == 404
    assert client.post("/threads", json={"thread_id": tid}).status_code == 409
    assert client.post("/threads", json={"thread_id": tid, "if_exists": "do_nothing"}).json()["thread_id"] == tid

    # state：用一个假图（不构建真图、不调 LLM）
    class _Snapshot:
        values = {"messages": [{"type": "ai", "content": "hi"}]}
        next: tuple = ()
        metadata: Dict[str, Any] = {}
        created_at = None
        config = {"configurable": {"thread_id": tid, "checkpoint_id": "cp-1"}}

    class _Graph:
        async def aget_state(self, *_a: Any, **_k: Any) -> Any:
            return _Snapshot()

        async def aget_state_history(self, *_a: Any, **_k: Any) -> Any:
            for snap in (_Snapshot(), _Snapshot()):
                yield snap

        async def aupdate_state(self, *_a: Any, **_k: Any) -> Any:
            return None

    from docking_agent.api.app import app

    monkeypatch.setattr(app.state, "get_graph", lambda: _Graph(), raising=False)
    state = client.get(f"/threads/{tid}/state").json()
    assert state["values"]["messages"][0]["content"] == "hi"
    assert state["checkpoint"]["checkpoint_id"] == "cp-1"
    assert len(client.get(f"/threads/{tid}/history").json()) == 2
    assert client.post(f"/threads/{tid}/state", json={"values": {"x": 1}}).status_code == 200

    assert client.delete(f"/threads/{tid}").status_code == 200
    assert client.get(f"/threads/{tid}").status_code == 404


# --------------------------------------------------------------------------- #
# 4) Runs：标准 SSE 帧 + wait/list/cancel
# --------------------------------------------------------------------------- #
def test_agent_stream_maps_tokens_updates_and_final(client: TestClient,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.api.app import app

    monkeypatch.setattr(app.state, "legacy_agent_stream", _fake_legacy([
        {"type": "start", "run_id": "20260918-110000-0002"},
        {"type": "token", "content": "你", "node": "model"},
        {"type": "token", "content": "好", "node": "model"},
        {"type": "update", "node": "tools", "keys": ["messages"]},
        {"type": "tool_call", "name": "list_known_receptors", "node": "model"},
        {"type": "final", "run_id": "20260918-110000-0002", "content": "你好"},
        {"type": "done", "run_id": "20260918-110000-0002", "summary": {"status": "ok"}},
    ]), raising=False)

    tid = client.post("/threads", json={}).json()["thread_id"]
    with client.stream("POST", f"/threads/{tid}/runs/stream",
                       json={"assistant_id": "coordinator",
                             "input": {"messages": [{"type": "human", "content": "hi"}]}}) as resp:
        body = "".join(resp.iter_text())
    frames = _frame_events(body)
    names = [f["event"] for f in frames]
    assert names[0] == "metadata" and names[-1] == "end"
    assert names.count("messages/partial") == 2
    assert any(f["event"] == "updates" and "tools" in f["data"] for f in frames)
    complete = next(f["data"] for f in frames if f["event"] == "messages/complete")
    assert complete[0]["content"] == "你好"
    assert any(f["event"] == "custom" and f["data"]["type"] == "tool_call" for f in frames)


def test_agent_stream_error_frame(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.api.app import app

    monkeypatch.setattr(app.state, "legacy_agent_stream", _fake_legacy([
        {"type": "start", "run_id": "20260918-110000-0003"},
        {"type": "error", "error_code": "provider_error", "error_message": "模型 400"},
    ]), raising=False)
    tid = client.post("/threads", json={}).json()["thread_id"]
    with client.stream("POST", f"/threads/{tid}/runs/stream",
                       json={"assistant_id": "coordinator", "input": {"message": "x"}}) as resp:
        body = "".join(resp.iter_text())
    frames = _frame_events(body)
    err = next(f for f in frames if f["event"] == "error")
    assert err["data"]["message"] == "模型 400"
    assert frames[-1]["event"] == "end"


def test_wait_list_and_cancel(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.api.app import app

    monkeypatch.setattr(app.state, "legacy_agent_stream", _fake_legacy([
        {"type": "start", "run_id": "20260918-110000-0004"},
        {"type": "done", "run_id": "20260918-110000-0004", "summary": {"status": "ok"}},
    ]), raising=False)

    tid = client.post("/threads", json={}).json()["thread_id"]
    out = client.post(f"/threads/{tid}/runs/wait",
                      json={"assistant_id": "coordinator",
                            "input": {"ligands_text": "乙醇,CCO"}}).json()
    assert out["business_run_id"] == "20260918-110000-0004"
    assert out["run_id"] == "20260918-110000-0004", "values 里的业务 run id 不能被平台 id 覆盖"
    assert out["platform_run_id"].startswith("run_")

    # 无状态端点同样返回标准字段
    stateless = client.post("/runs/wait",
                            json={"assistant_id": "coordinator", "input": {}}).json()
    assert stateless["platform_run_id"].startswith("run_")

    # 取消：平台 id 与业务 id 都接受
    assert client.post(f"/threads/{tid}/runs/{out['platform_run_id']}/cancel").json()["status"] == "cancelling"
    assert client.post(f"/threads/{tid}/runs/{out['business_run_id']}/cancel").status_code == 200
    assert client.get(f"/threads/{tid}/runs/{out['business_run_id']}").status_code == 200
    assert client.get(f"/threads/{tid}/runs/{out['platform_run_id']}").status_code == 200

    # 未知助手 / 未知运行
    assert client.post("/runs/wait", json={"assistant_id": "nope", "input": {}}).status_code == 404
    assert client.get(f"/threads/{tid}/runs/does-not-exist").status_code == 404


def test_legacy_endpoints_still_registered(client: TestClient) -> None:
    """标准面是**纯增量**：既有端点一个都不能少（前端与外部调用者不受影响）。"""
    # 第 2 波把 app.routes 拆成懒加载的 _IncludedRouter（没有 .path 属性），
    # 因此按 OpenAPI 的实际路径断言「端点仍然注册」。
    paths = set(client.get("/openapi.json").json()["paths"])
    for legacy in ("/api/agent/stream", "/api/runs", "/api/settings",
                   "/api/uploads", "/api/uploads/inspect", "/health"):
        assert legacy in paths, legacy
    # 流水线端点已随「确定性流水线」一起移除
    assert "/api/pipeline/stream" not in paths


def test_legacy_endpoints_are_marked_deprecated(client: TestClient) -> None:
    """规范收敛：老端点保留可用，但必须在 OpenAPI 里显式标成 deprecated（不再演进）。"""
    schema = client.get("/openapi.json").json()
    legacy = {
        ("/api/agent/stream", "post"),
        ("/run", "post"),
        ("/stream_run", "post"),
        ("/cancel/{run_id}", "post"),
        ("/health", "get"),
        ("/graph_parameter", "get"),
    }
    for path, method in sorted(legacy):
        op = schema["paths"][path][method]
        assert op.get("deprecated") is True, f"{method.upper()} {path} 未标注 deprecated"
        assert op.get("summary", "").startswith("[已废弃]"), f"{method.upper()} {path} 缺少废弃摘要"


def test_standard_surface_is_tagged_and_documented(client: TestClient) -> None:
    """标准面（Agent Protocol 子集）必须有 tag + summary，方便 /docs 里成组阅读。"""
    schema = client.get("/openapi.json").json()
    expected = {
        ("/ok", "get"): "System",
        ("/info", "get"): "System",
        ("/assistants/search", "post"): "Assistants",
        ("/threads", "post"): "Threads",
        ("/threads/{thread_id}/runs/stream", "post"): "Thread Runs",
        ("/threads/{thread_id}/runs/wait", "post"): "Thread Runs",
        ("/runs/stream", "post"): "Stateless Runs",
    }
    for (path, method), tag in expected.items():
        op = schema["paths"][path][method]
        assert tag in (op.get("tags") or []), f"{method.upper()} {path} 缺少 tag {tag}"
        assert op.get("summary"), f"{method.upper()} {path} 缺少 summary"
        assert op.get("deprecated") is not True


def test_offline_intake_run_drops_its_store_blackboard_view(client: TestClient) -> None:
    """运行结束后必须丢弃该 run 的 store 黑板视图（审计 §2.1：长驻服务里 `_store_boards` 会泄漏）。

    走**离线** intake（`use_llm: False`）：不调模型、不做对接，因此 CI 的快跑也能看护这条不变量。
    """
    from docking_agent.runtime.blackboard import _store_boards

    out = client.post("/runs/wait", json={
        "assistant_id": "intake",
        "input": {"message": "乙醇:CCO", "use_llm": False},
    }).json()
    run_id = str(out.get("business_run_id") or out.get("run_id") or "")
    assert run_id, out
    leftovers = [key for key in _store_boards if key[1] == run_id]
    assert not leftovers, f"运行 {run_id} 结束后黑板视图仍在缓存：{leftovers}"
