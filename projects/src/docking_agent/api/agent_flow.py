"""Agent 编排的流式支撑：SSE 心跳、线程自愈、受理层需要的多轮上下文。

这些都是「HTTP 层与 LangGraph 之间」的胶水，原先挤在 `api/app.py` 的 `create_app()` 闭包外层；
拆到本模块后，`routers/agent.py` 与 `routers/legacy.py` 共用同一份实现。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator, Dict, List

from docking_agent.runs import Run
from docking_agent.runtime.payload import as_text, normalize_agent_input
from docking_agent.runtime.streaming import sse_event

logger = logging.getLogger(__name__)


def _attach_pose_url(item: Dict[str, Any], run_id: str) -> None:
    """把分子事件里的位姿产物名转成可下载 URL。"""
    if not isinstance(item, dict):
        return
    artifact = item.pop("pose_artifact", None)
    if artifact:
        item["pose_url"] = f"/api/runs/{run_id}/artifacts/{artifact}"


def _drain_live_molecules(run: Run) -> List[str]:
    """把子 Agent 上报的逐分子结果转成 `molecules` SSE 事件（心跳调用）。

    多 Agent 模式下 `run_docking` 工具在子 Agent 里跑，父流程阻塞，工具通过
    `run.data["live_molecules"]` 逐条上报；这里按已消费下标取增量，既不丢也不重复
    （用下标而不是 `del`，避免与工具线程的 append 竞争）。同时补上位姿下载地址。
    """
    rows = run.data.get("live_molecules")
    if not rows:
        return []
    seen = int(run.data.get("live_molecules_seen") or 0)
    items = list(rows)[seen:]
    if not items:
        return []
    run.data["live_molecules_seen"] = seen + len(items)
    for item in items:
        _attach_pose_url(item, run.id)
    return [sse_event({"type": "molecules", "items": items})]


def _last_ai_text(messages: List[Any]) -> str:
    for m in reversed(messages or []):
        if type(m).__name__ in ("AIMessage", "AIMessageChunk"):
            content = getattr(m, "content", "")
            if isinstance(content, list):
                content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
            if str(content).strip():
                return str(content).strip()
    return ""


async def _interleave(stream: Any, interval: float,
                      tick: Any) -> AsyncGenerator[str, None]:
    """把「周期心跳」插进 SSE 事件流。

    多 Agent 模式下父流程在 graph.invoke 上阻塞，子 Agent 内部的实时进度只能靠心跳
    （`tick()` 返回的进度事件）推给前端；否则要等整轮工具调用结束才看到任何动静。
    """
    queue: "asyncio.Queue[Any]" = asyncio.Queue()
    DONE = object()

    async def _pump() -> None:
        try:
            async for item in stream:
                await queue.put(item)
        except Exception as e:  # noqa: BLE001
            logger.warning("事件流中断：%s", e)
        finally:
            await queue.put(DONE)

    task = asyncio.create_task(_pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                for extra in (tick() or []):
                    yield extra
                continue
            if item is DONE:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()


async def _final_state(graph: Any, config: Dict[str, Any]) -> tuple[str, List[Any]]:
    try:
        snapshot = await graph.aget_state(config)
        values = getattr(snapshot, "values", {}) or {}
        messages = values.get("messages", []) or []
        return _last_ai_text(messages), messages
    except Exception as e:  # noqa: BLE001
        logger.warning("读取最终状态失败: %s", e)
        return "", []


# 单条消息类型 → 受理层用的角色（tool/system 不参与多轮上下文）
_MESSAGE_ROLES = {"HumanMessage": "user", "AIMessage": "assistant",
                  "AIMessageChunk": "assistant"}


async def _heal_thread(graph: Any, config: Dict[str, Any], run: Any) -> int:
    """续聊前自愈线程状态：补齐上一轮中断留下的未回填工具调用（返回补了几条）。

    为什么必须做：模型已经发出 `tool_calls` 而工具被中止（用户取消/运行失败）时，
    checkpointer 里会留下悬空调用；下一轮把这段历史发给 OpenAI 会直接 400
    （`An assistant message with 'tool_calls' must be followed by tool messages…`），
    整段对话无法继续。这里补的是**占位回执（事实说明：该调用被中断、没有结果）**，不是编造数据。
    """
    from docking_agent.agents.threads import repair_thread_state

    try:
        healed = await repair_thread_state(graph, config)
    except Exception as e:  # noqa: BLE001 - 自愈失败不该让运行起不来
        logger.warning("线程状态自愈失败（继续运行）：%s", e)
        return 0
    if healed.get("repaired"):
        run.log(f"已修复上一轮中断留下的 {healed['repaired']} 个未回填工具调用"
                f"（补占位回执，避免模型报 tool_calls 校验错误）")
    return int(healed.get("repaired") or 0)


async def _recent_prior_turns(graph: Any, config: Dict[str, Any],
                              limit: int = 8) -> List[Dict[str, str]]:
    """从 checkpointer 读取该 thread 的历史消息，取最近 limit 条 human/ai（忽略 tool/system）。

    用途仅限「让受理层看见上一轮」：用户回答追问时继承上一轮的分子/受体，不再重复判 ask。
    真正的上下文延续由 checkpointer + thread_id 完成，本函数**不修改**任何状态。
    """
    snapshot = await graph.aget_state(config)
    values = getattr(snapshot, "values", None) or {}
    messages = values.get("messages") or []
    turns: List[Dict[str, str]] = []
    for m in messages:
        role = _MESSAGE_ROLES.get(type(m).__name__, "")
        if not role:
            continue
        text = as_text(getattr(m, "content", "")).strip()
        if not text:
            continue
        turns.append({"role": role, "content": text})
    return turns[-max(0, int(limit)):]


def _payload_conversation_id(payload: Any) -> str:
    """从任意请求体里取会话 id（兼容 legacy /run 与 /v1/chat/completions 的原始 JSON）。"""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("conversation_id") or "").strip()


def _extract_text(body: Any) -> str:
    try:
        return normalize_agent_input(body)["messages"][0].content
    except Exception:  # noqa: BLE001
        return ""


def _agent_fields(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    keep = ("mode", "advanced", "receptor", "receptor_file", "ligands_text", "molecule_file",
            "positive_control", "exhaustiveness", "n_poses", "engine", "site_center", "site_size",
            "max_ligands", "save_poses", "skip_positive_control", "conversation_id")
    return {k: body[k] for k in keep if k in body and body[k] is not None}
