"""路由：Coze 时代遗留的兼容接口（/health、/run、/stream_run、/files、/graph_parameter、
/v1/chat/completions）。新客户端请走标准 Agent Protocol。"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from docking_agent.runtime.blackboard import (current_blackboard, forget_store_blackboard,
                                              shared_store, store_blackboard)
from docking_agent.config import DEFAULT_RECURSION_LIMIT, env_int
from docking_agent.runtime.limits import (base_limit, escalate, is_recursion_error,
                                          log_escalation)
from docking_agent.api.agent_flow import (
    _agent_fields,
    _extract_text,
    _heal_thread,
    _last_ai_text,
    _payload_conversation_id,
)
from docking_agent.api.routers.agent import api_agent_stream
from docking_agent.api.routers.meta import api_health
from docking_agent.api.schemas import AgentRequest


async def _invoke_with_budget(graph: Any, agent_input: Any, config: Dict[str, Any], run: Any,
                              *, context: Any = None, timeout: Optional[float] = None) -> Any:
    """调用（非流式）图；**跑满步数自动放宽上限并继续**，到顶返回 None。

    与流式路径同一套预算策略（`runtime/limits.py`）：步数是执行细节，不该变成用户可见的报错。
    """
    limit = int(config.get("recursion_limit") or base_limit())
    payload: Any = agent_input
    while True:
        call = graph.ainvoke(payload, config={**config, "recursion_limit": limit}, context=context)
        try:
            return await (asyncio.wait_for(call, timeout=timeout) if timeout else call)
        except Exception as exc:  # noqa: BLE001 - 只吸收「跑满步数」
            if not is_recursion_error(exc):
                raise
            escalated = escalate(limit)
            log_escalation(run, limit, escalated)
            if escalated is None:
                return None
            limit = escalated
            payload = None                     # 从 checkpoint 继续，不重放输入
from docking_agent.api.support import ERRORS, TIMEOUT_SECONDS, _serialize, state
from docking_agent.reporting.store import resolve_output
from docking_agent.runs import current_run, get_run_store, safe_run_component
from docking_agent.runtime.context import (AgentContext, current_agent_context, new_context,
                                          request_context)
from docking_agent.runtime.llm import load_llm_config
from docking_agent.runtime.payload import PayloadError, normalize_agent_input

router = APIRouter()


@router.get("/health", deprecated=True, summary="[已废弃] 请改用 /ok 或 /api/health")
async def legacy_health() -> Dict[str, Any]:
    return await api_health()


@router.post("/run", deprecated=True,
             summary="[已废弃] Coze 遗留执行入口（请改用 /threads/{tid}/runs/wait）")
async def legacy_run(request: Request) -> Dict[str, Any]:
    ctx = new_context(method="run", headers=request.headers)
    # 请求头里的 run id 会被当成**目录名**（`runs/<id>/`），必须先校验：
    # 未校验时 `x-run-id: ../x` 能逃出运行目录并写入攻击者可控内容（真实漏洞）。
    upstream = (request.headers.get("x-run-id") or "").strip()
    if upstream:
        try:
            ctx.run_id = safe_run_component(upstream, field="x-run-id")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    request_context.set(ctx)
    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    run = get_run_store().new("agent", {"mode": "run", "payload": payload}, run_id=ctx.run_id)
    token = current_run.set(run)
    board_token = current_blackboard.set(store_blackboard(shared_store(), run.id))
    try:
        agent_input = normalize_agent_input(payload)
        graph = state.get_graph()
        conversation_id = _payload_conversation_id(payload)
        thread_id = conversation_id or run.id
        run.data["conversation_id"] = conversation_id
        run.data["thread_id"] = thread_id
        config = {"configurable": {"thread_id": thread_id},
                  "recursion_limit": env_int("RECURSION_LIMIT", DEFAULT_RECURSION_LIMIT)}
        await _heal_thread(graph, config, run)
        result = await _invoke_with_budget(
            graph, agent_input, config, run, context=current_agent_context(),
            timeout=float(TIMEOUT_SECONDS))
        if result is None:                     # 到步数上限：用已有结果收尾，不报错
            snapshot = await graph.aget_state(config)
            result = {"messages": list((getattr(snapshot, "values", None) or {}).get("messages") or [])}
        from docking_agent.agents.persistence import persist_agent_run

        await asyncio.to_thread(persist_agent_run, run, result.get("messages", []),
                                _last_ai_text(result.get("messages", [])))
        run.finish("ok")
        out = _serialize(result)
        out["run_id"] = run.id
        return out
    except PayloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except asyncio.TimeoutError:
        run.finish("error", error="timeout")
        raise HTTPException(status_code=504, detail=f"执行超时（>{TIMEOUT_SECONDS}s）")
    except Exception as e:  # noqa: BLE001
        run.finish("error", error=str(e))
        raise HTTPException(status_code=500, detail=ERRORS.get_error_response(e, {"node_name": "run"}))
    finally:
        current_blackboard.reset(board_token)
        current_run.reset(token)
        forget_store_blackboard(run.id)   # 运行结束即丢弃该 run 的黑板视图（防进程内泄漏）


@router.post("/stream_run", deprecated=True,
             summary="[已废弃] 与 /api/agent/stream 等价（请改用标准 Agent Protocol）")
async def legacy_stream_run(request: Request) -> Any:
    """兼容旧接口：与 /api/agent/stream 行为一致。"""
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    req = AgentRequest(message=_extract_text(body), **_agent_fields(body))
    return await api_agent_stream(req, request)


@router.get("/files/{key}")
async def legacy_files(key: str) -> FileResponse:
    path = resolve_output(key)
    if path is None:
        raise HTTPException(status_code=404, detail=f"artifact not found: {key}")
    return FileResponse(str(path))


@router.get("/graph_parameter", deprecated=True,
            summary="[已废弃] Coze 遗留 graph 参数描述（请改用 /assistants/{id}/schemas）")
async def graph_parameter() -> Dict[str, Any]:
    msg = {"type": "array", "items": {"type": "object",
                                      "properties": {"role": {"type": "string"},
                                                     "content": {"type": "string"}}}}
    return {"input_schema": {"type": "object", "properties": {"messages": msg}},
            "output_schema": {"type": "object", "properties": {"messages": msg}},
            "code": 0, "msg": ""}


@router.post("/v1/chat/completions")
async def openai_chat(request: Request) -> Any:
    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    try:
        agent_input = normalize_agent_input(payload)
    except PayloadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    cfg = load_llm_config()["config"]
    model = str(payload.get("model") or cfg.get("model") or "docking-agent")
    run = get_run_store().new("agent", {"mode": "openai", "model": model})
    graph = state.get_graph()
    conversation_id = _payload_conversation_id(payload)
    thread_id = conversation_id or run.id
    run.data["conversation_id"] = conversation_id
    run.data["thread_id"] = thread_id
    config = {"configurable": {"thread_id": thread_id},
                  "recursion_limit": env_int("RECURSION_LIMIT", DEFAULT_RECURSION_LIMIT)}
    await _heal_thread(graph, config, run)
    try:
        # 这个兼容端点没有设置 ContextVar：显式把 run 作为 context 传下去
        # （否则工具层的 active_run() 拿不到运行目录，产物登记会缺失）
        result = await _invoke_with_budget(graph, agent_input, config, run,
                                          context=AgentContext(run=run))
        if result is None:                     # 到步数上限：用已有结果收尾，不报错
            snapshot = await graph.aget_state(config)
            result = {"messages": list((getattr(snapshot, "values", None) or {}).get("messages") or [])}
    except Exception as e:  # noqa: BLE001
        run.finish("error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    text = _last_ai_text(result.get("messages", []))
    run.finish("ok", report=bool(text))
    return {
        "id": f"chatcmpl-{run.id}", "object": "chat.completion", "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
