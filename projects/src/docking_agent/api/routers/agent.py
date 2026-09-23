"""路由：多 Agent 流式执行（SSE）与取消。

`api_agent_stream` 同时被标准 Agent Protocol 面复用（`app.state.legacy_agent_stream`），
因此它必须是模块级函数（原先定义在 `create_app()` 闭包内）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncGenerator, Dict, List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from docking_agent.runtime.blackboard import (current_blackboard, forget_store_blackboard,
                                              shared_store, store_blackboard)
from docking_agent.api.agent_flow import (
    _drain_live_molecules,
    _final_state,
    _heal_thread,
    _interleave,
    _recent_prior_turns,
)
from docking_agent.api.schemas import AgentRequest
from docking_agent.api.support import _agent_model_map, _completeness, state
from docking_agent.cancellation import cancel_flag, clear_cancel, request_cancel
from docking_agent.config import DEFAULT_RECURSION_LIMIT, env_int
from docking_agent.runs import current_run, get_run_store, safe_run_component
from docking_agent.runtime.context import current_agent_context
from docking_agent.runtime.errors import error_payload
from docking_agent.runtime.streaming import parse_sse_data, sse_event, stream_agent_sse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/agent/stream", deprecated=True,
             summary="[已废弃] 多 Agent 协作流式执行（请改用标准 Agent Protocol）")
async def api_agent_stream(req: AgentRequest, request: Request) -> Any:
    payload = dict(req.model_dump())
    store = get_run_store()
    # 点选候选 = **续跑同一个运行**（用户 2026-09-24 拍板）：不新建 run、不重新受理，
    # 把答案注入线程并从 checkpoint 继续；这样运行的参数/产物/报告都留在同一条记录里。
    resumed = False
    resume_id = str(getattr(req, "resume_run_id", "") or "").strip()
    if resume_id:
        try:
            existing = store.load(resume_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if existing is not None and (existing.data.get("choices") or []):
            run, resumed = existing, True
    if not resumed:
        run = store.new("agent", payload)
        # 本次运行的调用计数从零开始（否则 calls 会累计上一次运行，无法判断本次调用量）
        from docking_agent.runtime.llm import reset_registry_counters

        reset_registry_counters()
    # 会话 id：同一 id = 同一段对话。LangGraph checkpointer 按 thread_id 记忆，
    # 因此**不能**再用每次都会变的 run.id 当 thread_id，否则永远命中不到上一轮。
    conversation_id = (req.conversation_id or "").strip()
    if conversation_id:
        # 会话 id 会被用作 checkpointer 的 thread_id，并透传到标准面的线程文件路径，
        # 因此与 run_id 用同一套校验（防路径穿越 / 防控制字符）。
        try:
            conversation_id = safe_run_component(conversation_id, field="conversation_id")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    thread_id = conversation_id or run.id
    run.data["conversation_id"] = conversation_id
    run.data["thread_id"] = thread_id
    if resumed:
        run.data["status"] = "running"
        run.data["finished_at"] = None
        run.data["error"] = None
        run.log("用户在界面上作答，继续同一运行（不新建运行）")
    run_config: Dict[str, Any] = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": env_int("RECURSION_LIMIT", DEFAULT_RECURSION_LIMIT),
    }
    # 受理层要「看见上一轮」：从 checkpointer 读该 thread 的历史消息（只读，用于继承分子/受体）
    prior_turns: List[Dict[str, str]] = []
    try:
        graph = state.get_graph()
        # 先自愈：上一轮被取消/失败时可能留下「模型发了 tool_calls 但工具没回执」的历史，
        # 直接续聊会被 OpenAI 以 400 拒绝（insufficient tool messages）——补占位回执后再读历史。
        await _heal_thread(graph, run_config, run)
        prior_turns = await _recent_prior_turns(graph, run_config)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取会话历史失败（按新会话处理）：%s", e)
    if prior_turns:
        run.data["conversation_turns"] = len(prior_turns)
    if resumed:
        # 续跑：答案本身就是本轮指令；**不重新受理**（task_spec / param_plan 沿用第一次受理的），
        # 但用户这次带上的字段要并入 —— 且**只覆盖真正给了值的字段**：
        # 续跑请求里没出现的字段在 AgentRequest 里是 None/""（表单参数在对话模式本就不下发），
        # 直接 `**payload` 会把用户第一次设置的参数（搜索强度/盒子/引擎…）清成 None。
        message = str(req.message or "").strip()
        merged = dict(run.data.get("request") or {})
        for key, value in payload.items():
            if value is None or value == "" or key in ("message", "conversation_id"):
                continue
            merged[key] = value
        merged["message"] = message
        run.data["request"] = merged
    else:
        # 受理层（intake）：理解用户要什么 → 任务规约（确定性优先，只在对话模式下才调模型）。
        # 结构化字段（分子库/受体/位点/参数）始终随指令一起下发，避免表单与自然语言互相覆盖。
        from docking_agent import intake

        message, task_spec = await asyncio.to_thread(
            intake.build_message, req, run=run, prior_turns=prior_turns or None)
        run.data["request"] = {**payload, "message": message}
        run.data["task_spec"] = task_spec      # 可观测：本次任务到底被理解成了什么
    if not resumed:
        run.log(f"任务受理：task_type={task_spec.get('task_type')} "
                f"authority={task_spec.get('authority')} decision={task_spec.get('decision')} "
                f"来源={task_spec.get('source')}"
                + (f"（延续会话 {conversation_id[:8]}，历史 {len(prior_turns)} 条）"
                   if conversation_id and prior_turns else ""))
        run.save()
        if task_spec.get("assumptions"):
            for note in task_spec["assumptions"][:4]:
                run.log(f"受理假设：{note}")
    else:
        run.save()

    async def gen() -> AsyncGenerator[str, None]:
        yield sse_event({"type": "start", "run_id": run.id,
                         "conversation_id": conversation_id,
                         "thread_id": thread_id,
                         "request": run.data.get("request"),
                         "task_spec": run.data.get("task_spec")})
        token = current_run.set(run)
        board = store_blackboard(shared_store(), run.id)
        board_token = current_blackboard.set(board)
        cancel_event = cancel_flag(run.id)
        if resumed:
            # 用户已作答：清掉这一问（及其阻断标记），否则工具护栏会继续拒绝开跑
            from docking_agent.tools.choices import clear_choices  # noqa: PLC0415

            clear_choices(str(getattr(req, "resume_choice_kind", "") or ""))
        try:
            graph = state.get_graph()
            # 记录各 Agent 角色实际使用的模型（每个角色独立实例）
            run.data["agent_models"] = _agent_model_map()
            run.save()
            config = run_config
            started = time.time()
            last_progress: Dict[str, Any] = {}

            def _choices_event() -> Any:
                """结构化「候选选择项」事件（受体/分子解析不确定时下发，供前端点选）。

                去重：同一份 choices 只发一次（心跳 `_tick` 与 final 前各有一条触发路径）。
                """
                choices = run.data.get("choices")
                if not choices or last_progress.get("choices_sent") == list(choices):
                    return None
                last_progress["choices_sent"] = list(choices)
                return sse_event({"type": "choices", "run_id": run.id,
                                  "choices": choices,
                                  "blocking": bool(run.data.get("choices_blocking")),
                                  "note": run.data.get("choices_note") or ""})

            def _tick() -> List[str]:
                """心跳：把子 Agent 上报的实时对接进度与逐分子结果转成 SSE 事件。"""
                out = _drain_live_molecules(run)
                choice_event = _choices_event()
                if choice_event:
                    out.append(choice_event)
                    if run.data.get("choices_blocking"):
                        # 如实记录"本轮会等用户决定"（不打断流：见下方 async for 的说明）
                        run.log("已就阻断式问题征询用户：本轮不再开始对接，等界面点选后续跑同一运行")
                info = run.data.get("live_progress")
                if info and info != last_progress.get("info"):
                    last_progress["info"] = dict(info)
                    out.append(sse_event({"type": "progress", **info,
                                          "elapsed_sec": round(time.time() - started, 1)}))
                return out

            # 续跑与首次执行**同一条入参形态**：把（用户的）消息作为图输入跑一轮。
            # 真实故障（运行 20260924-013514-2617）：暂停是"本轮自然结束"，图的 next 已空 ——
            # 此时 `aupdate_state(答案) + astream(None)` 不会触发任何节点，运行 1 秒就"完成"，
            # 对接根本没跑。同一 thread 的输入会追加到历史之后，等于续聊同一段对话。
            stream = stream_agent_sse(graph, {"messages": [{"role": "user", "content": message}]},
                                      config, run.id, context=current_agent_context())
            async for chunk in _interleave(stream, 1.0, _tick):
                # 阻断式候选**不在这里中断流**：实战踩坑（运行 20260924-011325-0913）——
                # 同一模型步里还并行跑着 `run_property_assessment`，流被中途掐断后它的 ToolMessage
                # 永远没进 checkpoint，续跑时只能补"该调用被中断、没有结果"的占位回执，
                # 模型据此认为两个工具都没结果、直接收尾，对接一次都没跑。
                # 正确做法：让当前步自然结束（对接工具本身会拒绝开跑，见 blocking_choice_pending），
                # 由持久化按"有待回答的阻断式问题"把运行标成 needs_user_input 并落盘。
                if cancel_event.is_set():
                    run.log("已被用户取消")
                    run.finish("cancelled", error="用户取消")
                    yield sse_event({"type": "cancelled", "run_id": run.id,
                                     "message": "运行已被用户取消",
                                     "summary": run.to_dict()})
                    yield sse_event({"type": "done", "run_id": run.id, "summary": run.to_dict()})
                    return
                data = parse_sse_data(chunk)
                # start/done 由本函数统一发送（done 需带 summary）
                if data and data.get("type") in ("start", "done"):
                    continue
                # 步数预算：跑满上限自动放宽/收尾时，如实写进运行日志（用户可见但不打扰）
                if data and data.get("type") == "limit":
                    run.log(str(data.get("message") or "已达步数上限"))
                # final 之前补发一次 choices（若心跳还没发过），确保前端一定拿得到可选项
                if data and data.get("type") == "final":
                    choice_event = _choices_event()
                    if choice_event:
                        yield choice_event
                yield chunk

            # 图执行完毕：把最后一个心跳窗口内产生的逐分子结果补齐再做持久化，
            # 否则对接最后一秒的分子只出现在结果里、不出现「实时」流。
            for extra in _drain_live_molecules(run):
                yield extra
            run.data.pop("live_molecules", None)
            run.data.pop("live_molecules_seen", None)

            final_text, messages = await _final_state(graph, config)
            from docking_agent.agents.persistence import persist_agent_run

            # 图执行完毕：此刻各角色已真实调用过模型，
            # 刷新为「服务端确认的实际模型 + 调用次数」，报告与运行记录都用这份
            run.data["agent_models"] = _agent_model_map()
            run.save()

            result = await asyncio.to_thread(persist_agent_run, run, messages, final_text)

            # 流程控制权完全在主管 Agent 手里：服务端**不再**接管补齐或复核，
            # 只把「实际完成了什么」记录成 completeness 供查看（信息，不是控制）。
            run.set(completeness=_completeness(run, result, False))
            # 没算任何东西（例如用户只说了句「你好」，受理层 reject）→ 状态 no_op，
            # 历史列表显示 [ SKIP ]，页面也不会把用户甩到空的结果总览；
            # 「停下来等用户点选」则是 needs_user_input（工具跑过、问题已下发），两者不能混。
            if result.get("no_op"):
                run.finish("no_op")
            elif result.get("needs_user_input"):
                run.finish("needs_user_input")
            else:
                run.finish("ok")
            yield sse_event({"type": "done", "run_id": run.id, "summary": run.to_dict(),
                             "ranking_size": len(result.get("ranking") or [])})
        except Exception as e:  # noqa: BLE001
            logger.exception("多 Agent 执行失败")
            run.finish("error", error=str(e))
            yield sse_event({**error_payload(e, {"node_name": "agent", "run_id": run.id}),
                             "type": "error"}, event="error")
            yield sse_event({"type": "done", "run_id": run.id, "summary": run.to_dict()})
        finally:
            # 共享黑板快照落盘：可以看到各 Agent 在协作区里留下了什么
            try:
                run.write_json("blackboard", board.snapshot(), label="共享黑板快照")
                run.set(blackboard_stats=board.stats())
            except Exception:  # noqa: BLE001
                logger.debug("黑板快照落盘失败", exc_info=True)
            # 逐分子实时缓冲只在运行期存在：异常/取消路径也不能把它写进运行元数据
            run.data.pop("live_molecules", None)
            run.data.pop("live_molecules_seen", None)
            # 收尾刷新：此刻协调/子 Agent 都已真实调用过模型，
            # 把各角色「服务端确认的模型 + 调用次数」写入运行记录（与报告口径一致）
            run.data["agent_models"] = _agent_model_map()
            run.save()   # 黑板快照与统计在上一步写入内存，这里必须落盘
            current_blackboard.reset(board_token)
            current_run.reset(token)
            forget_store_blackboard(run.id)   # 运行结束即丢弃该 run 的黑板视图（防进程内泄漏）
            clear_cancel(run.id)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/runs/{run_id}/cancel")
async def api_run_cancel(run_id: str) -> Dict[str, Any]:
    """请求取消运行。

    对接跑在工作线程 + 子进程里，`asyncio.Task.cancel()` 无法让它停下，
    因此真正的机制是**协作式取消标志**：置位后对接会在收集结果时终止进程池并停止。
    """
    store = get_run_store()
    meta = store.meta(run_id)
    task = state.tasks.get(run_id)
    running = bool(task and not task.done())
    if meta is None and not running:
        raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
    if meta and meta.get("status") not in ("running", None):
        return {"status": "already_finished", "run_id": run_id,
                "message": f"该运行已结束（状态：{meta.get('status')}）"}
    first = request_cancel(run_id)
    if task is not None and not task.done():
        try:
            task.cancel()  # 协程层面（多 Agent 的模型调用）立即收到取消
        except Exception:  # noqa: BLE001
            logger.debug("取消协程任务失败", exc_info=True)
    return {"status": "cancelling" if first else "already_cancelling",
            "run_id": run_id,
            "message": "已发送取消请求，正在停止对接与后续步骤"}


@router.post("/cancel/{run_id}", deprecated=True,
             summary="[已废弃] 请改用 /threads/{tid}/runs/{rid}/cancel 或 /api/runs/{run_id}/cancel")
async def api_cancel(run_id: str) -> Dict[str, Any]:
    """兼容旧接口。"""
    try:
        return await api_run_cancel(run_id)
    except HTTPException:
        task = state.tasks.get(run_id)
        if task is None or task.done():
            return {"status": "not_found", "run_id": run_id, "message": "未找到运行中的任务"}
        task.cancel()
        return {"status": "success", "run_id": run_id, "message": "已发送取消信号"}
