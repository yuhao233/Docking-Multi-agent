"""标准 Agent Protocol 服务面（LangGraph Platform / Agent Server 兼容子集）。

**为什么要它**：网页端原本只用项目自定义的 `/api/agent/stream`；而外部客户端
（LangGraph SDK、LangGraph Studio、其它 Agent 平台）期望的是**标准协议**：
`assistants` / `threads` / `runs` + 标准 SSE 帧。本模块把这套标准面**薄薄地适配**到
项目既有的执行链路上——同一个 Run、同一套产物、同一份黑板、同一套受理层——
因此**功能与产物完全不变**，只是多了一种标准的调用方式。

采用的标准子集（用户确认「最小可用集」）：

| 分组 | 端点 |
| --- | --- |
| System | `GET /ok`、`GET /info` |
| Assistants | `POST /assistants/search`、`GET /assistants/{id}`、`GET /assistants/{id}/schemas` |
| Threads | `POST /threads`、`GET/DELETE /threads/{id}`、`GET /threads/{id}/state`、`GET /threads/{id}/history`、`POST /threads/{id}/state` |
| Thread Runs | `POST /threads/{id}/runs/stream`、`POST /threads/{id}/runs/wait`、`GET /threads/{id}/runs`、`GET /threads/{id}/runs/{run_id}`、`POST /threads/{id}/runs/{run_id}/cancel` |
| Stateless Runs | `POST /runs/stream`、`POST /runs/wait` |

SSE 帧（按本机 `langgraph dev` 的实测格式）：

    event: metadata          data: {"run_id": "<platform uuid>", "attempt": 1, ...业务 id 作为扩展字段}
    event: messages/partial  data: [<AIMessageChunk 字典>]        # 增量 token
    event: updates           data: {"<node>": {...}}             # 节点推进
    event: custom            data: {...}                         # 领域事件（阶段/逐分子/候选选择）
    event: values            data: {...}                         # 最终状态
    event: error             data: {"error": ..., "message": ...}
    event: end               data: null

**run_id 语义（用户决策：双 id，业务 id 为准）**：标准面的 `run_id` 是平台风格 uuid
（进程内句柄，用于取消/查询）；真正的业务运行 id（`var/runs/<id>/`，产物与报告按它落盘）
放在 `metadata.business_run_id`，并且**取消/查询同时接受两种 id**。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from docking_agent import __version__
from docking_agent.api.schemas import AgentRequest
from docking_agent.paths import var_dir
from docking_agent.runtime.streaming import parse_sse_data, sse_event

logger = logging.getLogger(__name__)

#: 标准面 run_id 前缀（便于一眼区分平台 uuid 与业务 run id）
PLATFORM_RUN_PREFIX = "run_"
THREADS_DIR_NAME = "threads"

#: 图名 → 助手元信息（`kind` 决定走哪条内部执行链路）
ASSISTANTS: Dict[str, Dict[str, str]] = {
    "coordinator": {"kind": "agent", "description": "整体协调 Agent：把自然语言任务拆给 4 个子 Agent"},
    "intake": {"kind": "intake", "description": "任务受理层：自然语言 + 表单 → 结构化任务规约"},
    "property": {"kind": "subagent", "description": "分子属性评估 Agent"},
    "pocket": {"kind": "subagent", "description": "口袋分析 Agent"},
    "docking": {"kind": "subagent", "description": "Docking 执行 Agent"},
    "binding": {"kind": "subagent", "description": "结合模式检测 Agent"},
}


def assistant_id_for(name: str) -> str:
    """助手 id：由名字派生的**确定性** uuid5（重启不变，SDK 兼容）。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"docking-agent/assistant/{name}"))


def _assistant_by_id(assistant_id: str) -> Optional[str]:
    for name in ASSISTANTS:
        if assistant_id_for(name) == assistant_id or name == assistant_id:
            return name
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 线程登记（标准面的 thread 需要可 GET/DELETE；业务侧 thread_id 就是会话 id）
# --------------------------------------------------------------------------- #
def _threads_dir() -> Path:
    path = var_dir() / THREADS_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _thread_path(thread_id: str) -> Path:
    """线程文件路径 `var/threads/<thread_id>.json`。

    `thread_id` 来自请求体/URL，会被当成**单层文件名**使用，因此必须过白名单 +
    目录包含性断言：未校验时 `thread_id="../x"` 可写到 `var/threads/` 之外，
    覆盖工作区里任意 `*.json`（真实漏洞，已实测）。
    """
    from docking_agent.runs import ensure_inside, safe_run_component  # noqa: PLC0415

    try:
        tid = safe_run_component(thread_id, field="thread_id")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    threads = _threads_dir()
    try:
        return ensure_inside(threads, threads / f"{tid}.json", field="thread_id")
    except ValueError as e:  # pragma: no cover - 白名单已挡住，保留为兜底
        raise HTTPException(status_code=400, detail=str(e)) from e


def create_thread(thread_id: str = "", metadata: Optional[Dict[str, Any]] = None,
                  if_exists: str = "raise") -> Dict[str, Any]:
    tid = (thread_id or "").strip() or str(uuid.uuid4())
    path = _thread_path(tid)
    if path.is_file():
        if if_exists == "do_nothing":
            return read_thread(tid) or {}
        if if_exists != "create":
            raise HTTPException(status_code=409, detail=f"线程已存在: {tid}")
    record = {"thread_id": tid, "created_at": _now(), "updated_at": _now(),
              "metadata": metadata or {}, "status": "idle", "values": {}}
    _write_thread(record)
    return record


def _write_thread(record: Dict[str, Any]) -> None:
    import os

    path = _thread_path(record["thread_id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def read_thread(thread_id: str) -> Optional[Dict[str, Any]]:
    path = _thread_path(thread_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def touch_thread(thread_id: str, **fields: Any) -> None:
    record = read_thread(thread_id)
    if record is None:
        return
    record.update(fields)
    record["updated_at"] = _now()
    _write_thread(record)


def delete_thread(thread_id: str) -> None:
    path = _thread_path(thread_id)
    if path.is_file():
        path.unlink()


# --------------------------------------------------------------------------- #
# 平台 run 句柄（进程内）↔ 业务 run id
# --------------------------------------------------------------------------- #
_RUNS: Dict[str, Dict[str, Any]] = {}


def register_platform_run(thread_id: str, assistant_id: str) -> Tuple[str, Dict[str, Any]]:
    platform_id = PLATFORM_RUN_PREFIX + uuid.uuid4().hex
    record = {"run_id": platform_id, "thread_id": thread_id, "assistant_id": assistant_id,
              "created_at": _now(), "status": "pending", "business_run_id": None}
    _RUNS[platform_id] = record
    return platform_id, record


def _resolve_run_id(run_id: str) -> Optional[Dict[str, Any]]:
    """按平台 uuid 或**业务 run id** 找 run 句柄。"""
    if run_id in _RUNS:
        return _RUNS[run_id]
    for record in _RUNS.values():
        if record.get("business_run_id") == run_id:
            return record
    return None


def reset_platform_runs() -> None:
    """清空进程内 run 句柄（测试用）。"""
    _RUNS.clear()


# --------------------------------------------------------------------------- #
# SSE 帧构造
# --------------------------------------------------------------------------- #
def frame(event: str, data: Any) -> str:
    return sse_event(data, event=event)


def _message_dicts(messages: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in messages or []:
        if hasattr(m, "model_dump"):
            out.append(m.model_dump())
        elif isinstance(m, dict):
            out.append(m)
        else:
            out.append({"type": type(m).__name__, "content": str(m)})
    return out


#: 领域事件（网页端自定义契约）→ 标准面的 `custom` 通道
_DOMAIN_TYPES = {"stage", "molecules", "progress", "choices", "cancelled", "tool_call",
                 "tool_result", "update", "intake",
                 # 步数预算：跑满递归上限时自动放宽/收尾的如实告知（不是错误）
                 "limit",
                 # 思考/推理增量：走 custom 通道（**不**进 messages/partial），前端折叠到「思考」气泡
                 "thinking"}


def _map_legacy_event(data: Dict[str, Any], *, state: Dict[str, Any]) -> List[str]:
    """把内部（网页端）事件对象映射成**标准帧**。

    单帧 → 可能多帧（例如一个 final 同时给出 messages/complete 与 values）。
    """
    kind = str(data.get("type") or "")
    out: List[str] = []
    if kind == "token":
        # 增量文本 → messages/partial（与平台一致：data 是消息字典数组）
        out.append(frame("messages/partial", [{"type": "AIMessageChunk",
                                               "content": data.get("content") or "",
                                               "id": f"chunk-{state.get('seq', 0)}"}]))
        state["seq"] = int(state.get("seq", 0)) + 1
        state.setdefault("text", "")
        state["text"] += str(data.get("content") or "")
    elif kind == "update":
        out.append(frame("updates", {str(data.get("node") or "node"): {"keys": data.get("keys") or []}}))
    elif kind == "final":
        state["final"] = data.get("content") or state.get("text") or ""
        state["run_id"] = data.get("run_id") or state.get("run_id")
        out.append(frame("messages/complete", [{"type": "AIMessage",
                                                "content": state["final"],
                                                "id": f"final-{state.get('run_id') or ''}"}]))
    elif kind == "start":
        state["run_id"] = data.get("run_id") or state.get("run_id")
        state["task_spec"] = data.get("task_spec")
        state["request"] = data.get("request")
        # `start` 的受理信息（task_spec / request）在标准协议里没有对应事件，
        # 放进 `custom` 通道 —— 否则前端会丢掉「任务受理」横幅与参数来源展示。
        out.append(frame("custom", data))
    elif kind in _DOMAIN_TYPES:
        out.append(frame("custom", data))
    elif kind == "error":
        out.append(frame("error", {"error": data.get("error_code") or "error",
                                   "message": data.get("error_message") or "运行失败",
                                   "where": data.get("where")}))
        state["failed"] = True
    elif kind == "done":
        state["summary"] = data.get("summary")
        state["ranking_size"] = data.get("ranking_size")
    return out


# --------------------------------------------------------------------------- #
# 运行一条内部链路（标准面 → 既有端点/图）
# --------------------------------------------------------------------------- #
async def _stream_assistant(app: FastAPI, assistant: str, body: Dict[str, Any],
                            thread_id: str, request: Request,
                            platform_run: Dict[str, Any]) -> AsyncGenerator[str, None]:
    """产出标准 SSE 帧。内部复用网页端同一条链路（同一个 Run 与产物）。"""
    spec = ASSISTANTS[assistant]
    inp = dict(body.get("input") or {})
    state: Dict[str, Any] = {"seq": 0}
    yield frame("metadata", {"run_id": platform_run["run_id"], "attempt": 1,
                             "thread_id": thread_id, "assistant_id": assistant_id_for(assistant),
                             "graph_id": assistant, "created_at": platform_run["created_at"]})

    if spec["kind"] == "agent":
        req = _agent_request(inp, thread_id)
        legacy = getattr(app.state, "legacy_agent_stream", None)
        if legacy is None:  # pragma: no cover - 只在极端装配错误时
            yield frame("error", {"error": "not_configured", "message": "标准面未装配 legacy_agent_stream"})
            yield frame("end", None)
            return
        resp = await legacy(req, request)
        async for chunk in resp.body_iterator:  # type: ignore[attr-defined]
            data = parse_sse_data(chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace"))
            if not data:
                continue
            if data.get("type") == "start":
                platform_run["business_run_id"] = data.get("run_id")
                platform_run["status"] = "running"
                touch_thread(thread_id, status="busy")
            for f in _map_legacy_event(data, state=state):
                yield f
        yield frame("values", {"messages": [], "run_id": state.get("run_id"),
                               "business_run_id": state.get("run_id"),
                               "final": state.get("final") or "",
                               "summary": state.get("summary") or {}})
    else:  # subagent / intake：直接跑，一次性给出结果
        result = await _run_simple_assistant(assistant, inp, thread_id)
        platform_run["business_run_id"] = result.get("run_id")
        platform_run["status"] = "success"
        yield frame("messages/complete", [{"type": "AIMessage", "content": result.get("text") or "",
                                           "id": f"final-{platform_run['run_id']}"}])
        yield frame("values", result)

    platform_run["status"] = "success" if not state.get("failed") else "error"
    touch_thread(thread_id, status="idle")
    yield frame("end", None)


def _agent_request(inp: Dict[str, Any], thread_id: str) -> AgentRequest:
    """标准 `input` → 内部 `AgentRequest`：消息文本 + 既有表单字段（未给的字段用服务端默认）。"""
    payload = {k: v for k, v in inp.items() if k in AgentRequest.model_fields}
    messages = inp.get("messages") or []
    text = ""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("type", m.get("role")) in ("human", "user"):
            content = m.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            break
    payload["message"] = payload.get("message") or text
    payload["conversation_id"] = thread_id
    payload.setdefault("mode", "chat")
    return AgentRequest(**payload)


async def _run_simple_assistant(assistant: str, inp: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    """intake / 4 个子 Agent：一次性执行，返回文本与业务 run id。"""
    from docking_agent.runs import current_run, get_run_store
    from docking_agent.runtime.context import (AgentContext, new_context,
                                               request_context)

    if assistant == "intake":
        from docking_agent import intake
        from docking_agent.runtime.blackboard import (current_blackboard, forget_store_blackboard,
                                                     shared_store, store_blackboard)

        run = get_run_store().new("agent", {"mode": "standard:intake", "payload": inp})
        run.data["thread_id"] = thread_id
        token = current_run.set(run)
        board_token = current_blackboard.set(store_blackboard(shared_store(), run.id))
        try:
            req = _agent_request(inp, thread_id)
            message, spec = await asyncio.to_thread(
                intake.build_message, req, allow_llm=bool(inp.get("use_llm", True)), run=run)
            run.data["task_spec"] = spec
            run.finish("ok")
            return {"run_id": run.id, "thread_id": thread_id, "task_spec": spec,
                    "agent_message": message, "text": message}
        finally:
            current_blackboard.reset(board_token)
            current_run.reset(token)
            forget_store_blackboard(run.id)   # 运行结束即丢弃该 run 的黑板视图

    from docking_agent.agents import workers
    from docking_agent.runtime.blackboard import (current_blackboard, forget_store_blackboard,
                                                 shared_store, store_blackboard)
    from langchain_core.messages import HumanMessage

    workers.init_workers(None)
    getter = {"property": workers.get_property_agent, "pocket": workers.get_pocket_agent,
              "docking": workers.get_docking_agent, "binding": workers.get_binding_agent}[assistant]
    agent = getter()
    if agent is None:
        raise HTTPException(status_code=503, detail=f"子 Agent 未初始化：{assistant}")
    run = get_run_store().new("agent", {"mode": f"standard:{assistant}", "payload": inp})
    run.data["thread_id"] = thread_id
    token = current_run.set(run)
    board_token = current_blackboard.set(store_blackboard(shared_store(), run.id))
    ctx_token = request_context.set(new_context(method=f"standard:{assistant}"))
    try:
        text = ""
        for m in reversed(inp.get("messages") or []):
            if isinstance(m, dict) and m.get("type", m.get("role")) in ("human", "user"):
                text = str(m.get("content") or "")
                break
        text = text or str(inp.get("message") or "")
        out = await asyncio.to_thread(
            agent.invoke, {"messages": [HumanMessage(content=text)]},
            config={"configurable": {"thread_id": f"{thread_id}-{assistant}"}},
            context=AgentContext(run=run, blackboard=current_blackboard.get()))
        messages = (out or {}).get("messages") or []
        reply = _message_dicts(messages[-1:])
        run.finish("ok")
        return {"run_id": run.id, "thread_id": thread_id, "messages": reply,
                "text": (reply[0].get("content") if reply else "") or ""}
    finally:
        request_context.reset(ctx_token)
        current_blackboard.reset(board_token)
        current_run.reset(token)
        forget_store_blackboard(run.id)   # 运行结束即丢弃该 run 的黑板视图


def parse_frame(chunk: str) -> Tuple[str, Any]:
    """从标准 SSE 帧里取出 (event 名, data)。"""
    event = ""
    data: Any = None
    for line in (chunk or "").splitlines():
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            raw = line[len("data:"):].strip()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = None
    return event, data


async def _wait_assistant(app: FastAPI, assistant: str, body: Dict[str, Any], thread_id: str,
                          request: Request, platform_run: Dict[str, Any]) -> Dict[str, Any]:
    """`/runs/wait`：把同一条流跑到底，返回**最后一帧 `values`**（含业务 run id，便于取产物）。"""
    values: Dict[str, Any] = {}
    failed: Optional[str] = None
    async for chunk in _stream_assistant(app, assistant, body, thread_id, request, platform_run):
        event, data = parse_frame(chunk)
        if event == "values" and isinstance(data, dict):
            values = data
        elif event == "error" and isinstance(data, dict):
            failed = str(data.get("message") or data.get("error") or "运行失败")
    if failed:
        raise HTTPException(status_code=500, detail=failed)
    return values


# --------------------------------------------------------------------------- #
# 注册标准面
# --------------------------------------------------------------------------- #
def register_agent_service(app: FastAPI) -> None:
    """把标准 Agent Protocol 面注册到现有 FastAPI 应用上（纯增量，不改既有端点行为）。"""

    @app.get("/ok", tags=["System"], summary="服务存活探针")
    async def std_ok() -> Dict[str, Any]:
        return {"ok": True}

    @app.get("/info", tags=["System"], summary="实例信息（版本 / 助手清单）")
    async def std_info() -> Dict[str, Any]:
        return {"version": __version__, "assistants": sorted(ASSISTANTS),
                "graphs": sorted(ASSISTANTS), "protocol": "agent-protocol-minimal"}

    # ---------------- Assistants ----------------
    def _assistant_row(name: str) -> Dict[str, Any]:
        return {"assistant_id": assistant_id_for(name), "graph_id": name, "name": name,
                "description": ASSISTANTS[name]["description"], "config": {}, "metadata": {},
                "version": 1, "created_at": _now(), "updated_at": _now()}

    @app.post("/assistants/search", tags=["Assistants"], summary="列出可用助手")
    async def std_assistants_search(payload: Dict[str, Any] = Body(default={})) -> List[Dict[str, Any]]:
        rows = [_assistant_row(n) for n in sorted(ASSISTANTS)]
        graph_id = (payload or {}).get("graph_id")
        if graph_id:
            rows = [r for r in rows if r["graph_id"] == graph_id]
        limit = int((payload or {}).get("limit") or len(rows))
        offset = int((payload or {}).get("offset") or 0)
        return rows[offset:offset + limit]

    @app.get("/assistants/{assistant_id}", tags=["Assistants"], summary="取单个助手")
    async def std_assistant(assistant_id: str) -> Dict[str, Any]:
        name = _assistant_by_id(assistant_id)
        if name is None:
            raise HTTPException(status_code=404, detail=f"助手不存在: {assistant_id}")
        return _assistant_row(name)

    @app.get("/assistants/{assistant_id}/schemas", tags=["Assistants"], summary="助手的输入/输出 schema")
    async def std_assistant_schemas(assistant_id: str) -> Dict[str, Any]:
        name = _assistant_by_id(assistant_id)
        if name is None:
            raise HTTPException(status_code=404, detail=f"助手不存在: {assistant_id}")
        return {"graph_id": name, **_input_schemas(name)}

    # ---------------- Threads ----------------
    @app.post("/threads", tags=["Threads"], summary="创建线程")
    async def std_thread_create(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        payload = payload or {}
        return create_thread(str(payload.get("thread_id") or ""), payload.get("metadata"),
                             str(payload.get("if_exists") or "raise"))

    @app.get("/threads/{thread_id}", tags=["Threads"], summary="取线程")
    async def std_thread_get(thread_id: str) -> Dict[str, Any]:
        record = read_thread(thread_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"线程不存在: {thread_id}")
        return record

    @app.delete("/threads/{thread_id}", tags=["Threads"], summary="删除线程")
    async def std_thread_delete(thread_id: str) -> Dict[str, Any]:
        if read_thread(thread_id) is None:
            raise HTTPException(status_code=404, detail=f"线程不存在: {thread_id}")
        delete_thread(thread_id)
        return {}

    @app.get("/threads/{thread_id}/state", tags=["Threads"], summary="线程当前状态")
    async def std_thread_state(thread_id: str) -> Dict[str, Any]:
        if read_thread(thread_id) is None:
            raise HTTPException(status_code=404, detail=f"线程不存在: {thread_id}")
        return await _thread_state(app, thread_id)

    @app.post("/threads/{thread_id}/state", tags=["Threads"], summary="写入线程状态")
    async def std_thread_state_update(thread_id: str,
                                      payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        if read_thread(thread_id) is None:
            raise HTTPException(status_code=404, detail=f"线程不存在: {thread_id}")
        values = (payload or {}).get("values") or {}
        graph = app.state.get_graph()  # type: ignore[attr-defined]
        config = {"configurable": {"thread_id": thread_id}}
        try:
            await graph.aupdate_state(config, values)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"状态更新失败：{e}") from e
        return await _thread_state(app, thread_id)

    @app.get("/threads/{thread_id}/history", tags=["Threads"], summary="线程检查点历史")
    async def std_thread_history(thread_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        if read_thread(thread_id) is None:
            raise HTTPException(status_code=404, detail=f"线程不存在: {thread_id}")
        graph = app.state.get_graph()  # type: ignore[attr-defined]
        config = {"configurable": {"thread_id": thread_id}}
        out: List[Dict[str, Any]] = []
        try:
            async for snapshot in graph.aget_state_history(config, limit=max(1, min(limit, 100))):
                out.append(_snapshot_dict(snapshot))
        except Exception:  # noqa: BLE001 - 没有 checkpointer 时如实返回空历史
            logger.debug("读取线程历史失败（按空历史处理）", exc_info=True)
        return out

    # ---------------- Thread Runs ----------------
    @app.post("/threads/{thread_id}/runs/stream", tags=["Thread Runs"], summary="在线程上执行并流式返回")
    async def std_thread_run_stream(thread_id: str, request: Request,
                                    payload: Dict[str, Any] = Body(default={})) -> StreamingResponse:
        assistant = _require_assistant(payload)
        if read_thread(thread_id) is None:
            create_thread(thread_id, {"auto_created": True})
        _, platform_run = register_platform_run(thread_id, assistant)
        return _sse(_stream_assistant(app, assistant, payload, thread_id, request, platform_run))

    @app.post("/threads/{thread_id}/runs/wait", tags=["Thread Runs"], summary="在线程上执行并等待结果")
    async def std_thread_run_wait(thread_id: str, request: Request,
                                  payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        assistant = _require_assistant(payload)
        if read_thread(thread_id) is None:
            create_thread(thread_id, {"auto_created": True})
        platform_id, platform_run = register_platform_run(thread_id, assistant)
        values = await _wait_assistant(app, assistant, payload, thread_id, request, platform_run)
        return _wait_payload(values, platform_id, thread_id, platform_run)

    @app.get("/threads/{thread_id}/runs", tags=["Thread Runs"], summary="线程下的运行列表")
    async def std_thread_runs(thread_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        from docking_agent.runs import get_run_store

        rows = [r for r in get_run_store().list(limit=500) if r.get("thread_id") == thread_id]
        return rows[:max(1, min(limit, 200))]

    @app.get("/threads/{thread_id}/runs/{run_id}", tags=["Thread Runs"], summary="取单个运行")
    async def std_thread_run_get(thread_id: str, run_id: str) -> Dict[str, Any]:
        record = _resolve_run_id(run_id)
        if record is not None:
            return record
        from docking_agent.runs import get_run_store

        detail = get_run_store().detail(run_id)
        if detail is None or (detail.get("thread_id") not in (None, "", thread_id)):
            raise HTTPException(status_code=404, detail=f"运行不存在: {run_id}")
        return detail

    @app.post("/threads/{thread_id}/runs/{run_id}/cancel", tags=["Thread Runs"], summary="取消运行")
    async def std_thread_run_cancel(thread_id: str, run_id: str) -> Dict[str, Any]:
        from docking_agent.cancellation import request_cancel

        record = _resolve_run_id(run_id)
        business = (record or {}).get("business_run_id") or run_id
        request_cancel(str(business))
        if record is not None:
            record["status"] = "cancelling"
        return {"run_id": run_id, "thread_id": thread_id, "business_run_id": business,
                "status": "cancelling"}

    # ---------------- Stateless Runs ----------------
    @app.post("/runs/stream", tags=["Stateless Runs"], summary="无状态执行并流式返回")
    async def std_run_stream(request: Request,
                             payload: Dict[str, Any] = Body(default={})) -> StreamingResponse:
        assistant = _require_assistant(payload)
        thread_id = str(uuid.uuid4())
        create_thread(thread_id, {"stateless": True})
        _, platform_run = register_platform_run(thread_id, assistant)
        return _sse(_stream_assistant(app, assistant, payload, thread_id, request, platform_run))

    @app.post("/runs/wait", tags=["Stateless Runs"], summary="无状态执行并等待结果")
    async def std_run_wait(request: Request,
                           payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        assistant = _require_assistant(payload)
        thread_id = str(uuid.uuid4())
        create_thread(thread_id, {"stateless": True})
        platform_id, platform_run = register_platform_run(thread_id, assistant)
        values = await _wait_assistant(app, assistant, payload, thread_id, request, platform_run)
        return _wait_payload(values, platform_id, thread_id, platform_run)

    logger.info("标准 Agent Protocol 面已注册：%s（助手 %d 个）",
                "/ok /info /assistants /threads /runs", len(ASSISTANTS))


def _wait_payload(values: Dict[str, Any], platform_id: str, thread_id: str,
                  platform_run: Dict[str, Any]) -> Dict[str, Any]:
    """`/runs/wait` 的返回体：**以最终 values 为准**（标准语义），平台句柄用独立字段。

    注意不能写成 `{"run_id": platform_id, **values}` —— values 里已经有**业务 run id**
    （产物目录名，前端与报告都按它取产物），那样会被覆盖掉（真实踩到过）。
    """
    business = platform_run.get("business_run_id") or values.get("run_id")
    return {**values, "business_run_id": business, "thread_id": thread_id,
            "platform_run_id": platform_id}


def _require_assistant(payload: Dict[str, Any]) -> str:
    name = str((payload or {}).get("assistant_id") or "").strip()
    resolved = _assistant_by_id(name) if name else None
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"助手不存在: {name or '（缺 assistant_id）'}")
    return resolved


def _sse(gen: AsyncGenerator[str, None]) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _snapshot_dict(snapshot: Any) -> Dict[str, Any]:
    return {"values": getattr(snapshot, "values", {}) or {},
            "next": list(getattr(snapshot, "next", ()) or ()),
            "metadata": getattr(snapshot, "metadata", {}) or {},
            "created_at": getattr(snapshot, "created_at", None),
            "checkpoint": {"thread_id": (getattr(snapshot, "config", {}) or {}).get("configurable", {}).get("thread_id"),
                           "checkpoint_id": (getattr(snapshot, "config", {}) or {}).get("configurable", {}).get("checkpoint_id")}}


async def _thread_state(app: FastAPI, thread_id: str) -> Dict[str, Any]:
    graph = app.state.get_graph()  # type: ignore[attr-defined]
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = await graph.aget_state(config)
    except Exception:  # noqa: BLE001 - 无 checkpointer 时如实返回空状态
        return {"values": {}, "next": [], "tasks": [], "metadata": {}, "created_at": None,
                "checkpoint": {"thread_id": thread_id, "checkpoint_id": None}}
    data = _snapshot_dict(snapshot)
    data["tasks"] = []
    return data


def _input_schemas(name: str) -> Dict[str, Any]:
    """助手的输入 schema（标准面 `/assistants/{id}/schemas`）。

    直接复用 pydantic 请求模型 → **schema 与真实校验同一处定义**，不会再漂移。
    """
    if name == "coordinator":
        schema = AgentRequest.model_json_schema()
        props = dict(schema.get("properties") or {})
        props["messages"] = {"type": "array", "items": {"type": "object"},
                             "title": "Messages",
                             "description": "标准对话输入；其中最后一条人类消息即本次指令"}
        schema["properties"] = props
        return {"input_schema": schema, "output_schema": {"type": "object"},
                "state_schema": {"type": "object"}, "config_schema": {"type": "object"}}
    if name == "intake":
        return {"input_schema": {"type": "object", "properties": {
            "message": {"type": "string"}, "mode": {"type": "string", "default": "chat"},
            "advanced": {"type": "boolean", "default": False},
            "use_llm": {"type": "boolean", "default": True}}},
            "output_schema": {"type": "object"}, "state_schema": {"type": "object"},
            "config_schema": {"type": "object"}}
    return {"input_schema": {"type": "object", "properties": {
        "messages": {"type": "array", "items": {"type": "object"}}}, "required": ["messages"]},
        "output_schema": {"type": "object"}, "state_schema": {"type": "object"},
        "config_schema": {"type": "object"}}
