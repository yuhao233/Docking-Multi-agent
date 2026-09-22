"""对话线程（checkpointer state）自愈：把**非法的工具调用序列**修成模型端能接受的形态。

## 真实故障（用户报的 400）

    400 - An assistant message with 'tool_calls' must be followed by tool messages
          responding to each 'tool_call_id'. (insufficient tool messages …)

成因：用户点「停止」或运行中途失败 / 触发限额时，模型**已经发出** `tool_calls`，而对应的
`ToolMessage` 永远不会产生。checkpointer 把那条 AIMessage 记进了 thread 历史 —— 于是同一会话的
下一轮请求就把非法消息序列发给模型端，直接 400，整段对话卡死。

两种非法形态都要修：

| 形态 | 触发 | 模型端报错 |
| --- | --- | --- |
| 悬空 `tool_calls` | 取消 / 限额（`ToolCallLimitMiddleware(exit_behavior="end")`）/ 工具异常 | `An assistant message with 'tool_calls' must be followed by tool messages…` |
| 孤儿 `ToolMessage` | 摘要 / 裁剪把配对的 AIMessage 切掉 | `messages with role 'tool' must be a response to a preceding message with 'tool_calls'` |

## 修法（以及一个踩过的坑）

**改写那条 AIMessage**（同 `id` → LangGraph 的 `add_messages` 会**原地替换**）：只保留有回执的
`tool_calls`，并在正文追加一句事实说明（「上一轮有工具调用被中断、没有结果，如仍需要请重新调用」）；
孤儿回执直接 `RemoveMessage` 掉。

为什么**不是**「在 AIMessage 正后方插入一条占位 ToolMessage」——第一版就是这么写的，结果踩坑：
`add_messages` 只把**新 id** 追加到列表**末尾**（同 id 才原地替换），所以占位回执落到了最新一条
人类消息**之后**，序列依旧非法（实测模型调用拿到的顺序是
`[Human, AI(tool_calls), Human, ToolMessage]`）。改写 AIMessage 是原地生效的，顺序天然合法。

两条纪律：

1. 追加的是**事实说明**，不是编造结果 —— 符合「工具只报事实、Agent/用户做判断」的项目准则；
2. 修在**模型调用前**（中间件）而不是取消的那一刻：取消时工具可能仍在跑，若它稍后补回真实回执，
   同一 `tool_call_id` 会出现两条回执，同样会被模型端拒绝。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, RemoveMessage

logger = logging.getLogger(__name__)

#: 中断说明正文（给模型看的事实说明；拼在被打断的那条 AIMessage 后面）
INTERRUPT_NOTE = ("上一轮的工具调用在运行中被中断（用户取消 / 触发限额 / 运行失败），"
                  "**没有产生结果**；不要假设它已经返回数据，如仍需要该结果请重新调用。")


def dangling_tool_calls(messages: List[Any]) -> List[Tuple[str, str]]:
    """返回 `[(tool_call_id, tool_name)]`：AIMessage 已发出、但没有任何 ToolMessage 回应的调用。

    按消息顺序检查每一条 AIMessage（OpenAI 校验的是**整段**历史，不只是最后一条）。
    """
    answered = set()
    for m in messages or []:
        if type(m).__name__ == "ToolMessage":
            answered.add(str(getattr(m, "tool_call_id", "") or ""))
    missing: List[Tuple[str, str]] = []
    for m in messages or []:
        name = type(m).__name__
        if name not in ("AIMessage", "AIMessageChunk"):
            continue
        for call in (getattr(m, "tool_calls", None) or []):
            call_id = str((call or {}).get("id") or "")
            if not call_id or call_id in answered:
                continue
            answered.add(call_id)
            missing.append((call_id, str((call or {}).get("name") or "")))
    return missing


def orphan_tool_call_ids(messages: List[Any]) -> List[str]:
    """返回**没有对应 AI(tool_calls)** 的 ToolMessage 的 `tool_call_id`（另一种非法形态）。"""
    wanted = set()
    for m in messages or []:
        if type(m).__name__ not in ("AIMessage", "AIMessageChunk"):
            continue
        for call in (getattr(m, "tool_calls", None) or []):
            call_id = str((call or {}).get("id") or "")
            if call_id:
                wanted.add(call_id)
    orphans: List[str] = []
    for m in messages or []:
        if type(m).__name__ != "ToolMessage":
            continue
        call_id = str(getattr(m, "tool_call_id", "") or "")
        if call_id and call_id not in wanted:
            orphans.append(call_id)
    return orphans


def _text_of(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # 块式 content：只取文本块，避免把结构塞进新消息
        return "".join(str(b.get("text") or "") for b in content
                       if isinstance(b, dict) and b.get("type") in (None, "text"))
    return str(content or "")


def pairing_updates(messages: List[Any]) -> Tuple[List[Any], Dict[str, int]]:
    """算出「让这段历史对模型端合法」的最小更新（直接喂给 `messages` reducer）。

    返回 `(updates, stats)`：

    * 悬空 `tool_calls` 的 AIMessage → **同 id 的改写版**（只留已回执的调用 + 追加事实说明）；
    * 孤儿 `ToolMessage` → `RemoveMessage`；
    * 其余消息**不动**（不重排、不丢内容）。

    `stats` 用于日志与测试：`{"rewritten", "dropped_calls", "removed_orphans", "skipped"}`。
    """
    answered = {str(getattr(m, "tool_call_id", "") or "") for m in messages or []
                if type(m).__name__ == "ToolMessage"}
    orphan_calls = set(orphan_tool_call_ids(messages))
    updates: List[Any] = []
    stats = {"rewritten": 0, "dropped_calls": 0, "removed_orphans": 0, "skipped": 0}

    for m in messages or []:
        name = type(m).__name__
        if name == "ToolMessage":
            if str(getattr(m, "tool_call_id", "") or "") in orphan_calls:
                message_id = str(getattr(m, "id", "") or "")
                if message_id:
                    updates.append(RemoveMessage(id=message_id))
                    stats["removed_orphans"] += 1
                else:  # 没有 id 就没法定位，只能放弃这条（记录以便排查）
                    stats["skipped"] += 1
            continue
        if name not in ("AIMessage", "AIMessageChunk"):
            continue
        calls = list(getattr(m, "tool_calls", None) or [])
        missing = [c for c in calls if str((c or {}).get("id") or "") not in answered]
        if not missing:
            continue
        message_id = str(getattr(m, "id", "") or "")
        if not message_id:  # 没有 id 就无法原地替换（正常状态里不会发生）
            stats["skipped"] += 1
            continue
        kept = [c for c in calls if str((c or {}).get("id") or "") in answered]
        names = "、".join(str((c or {}).get("name") or "tool") for c in missing)
        text = _text_of(m).rstrip()
        note = f"（{INTERRUPT_NOTE} 涉及：{names}。）"
        updates.append(AIMessage(content=(text + "\n\n" + note).strip() if text else note,
                                 id=message_id, tool_calls=kept,
                                 additional_kwargs=dict(getattr(m, "additional_kwargs", None) or {})))
        stats["rewritten"] += 1
        stats["dropped_calls"] += len(missing)
    return updates, stats


def repaired_messages(messages: List[Any]) -> "tuple[List[Any], Dict[str, int]]":
    """返回 `(可用于模型调用的消息列表, stats)`：已经是合法序列时原样返回。"""
    if not dangling_tool_calls(messages) and not orphan_tool_call_ids(messages):
        return list(messages), {"rewritten": 0, "dropped_calls": 0,
                                "removed_orphans": 0, "skipped": 0}
    updates, stats = pairing_updates(messages)
    if not updates:
        return list(messages), stats
    from langgraph.graph.message import add_messages

    return list(add_messages(list(messages), updates)), stats


class ToolCallPairingMiddleware(AgentMiddleware):
    """模型调用前的最后一道保险：**任何**进入模型的 messages 都必须是合法的工具调用序列。

    为什么光有 `repair_thread_state()` 不够（真实故障，用户报的 400）：
    那只在**每轮开始前**修**协调 Agent** 的 thread；而 4 个子 Agent 用的是**固定角色线程**
    （`dispatch.py` 里 `thread_id="property"/"pocket"/"docking"/"binding"`）+ 各自的
    `InMemorySaver`。一次取消、一次限额或一次工具异常，都会把「AI(tool_calls) 而没有回执」
    留在**子 Agent** 的线程里 —— 下一次调用同一个子 Agent 时，这段非法历史直接发给模型 →
    400 `insufficient tool messages`，整轮对话卡死，且当时没有任何补丁路径。

    实现选 `wrap_model_call`（**包裹模型调用**）而不是 `before_model`（**图里的一个节点**）：
    后者会为每次模型调用多消耗一个 super-step —— 协调 Agent 的 `recursion_limit` 是 60，
    子 Agent 更是用默认值，多出来的步数会实打实地把长任务顶到
    `GRAPH_RECURSION_LIMIT`（真实故障）。包裹式钩子不改变图结构，只改这一次请求的消息。
    """

    name = "ToolCallPairingMiddleware"

    @staticmethod
    def _repair(request: Any) -> Any:
        """把请求里的消息修成合法序列（需要修才 override，否则原样返回）。"""
        messages = list(getattr(request, "messages", None) or [])
        if not messages:
            return request
        repaired, stats = repaired_messages(messages)
        if stats["rewritten"] or stats["removed_orphans"]:
            logger.warning("模型调用前修正非法工具调用序列：改写 %d 条消息（去掉 %d 个无回执调用），"
                           "移除 %d 条孤儿回执", stats["rewritten"], stats["dropped_calls"],
                           stats["removed_orphans"])
            return request.override(messages=repaired)
        return request

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(self._repair(request))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(self._repair(request))


def _tools_node_name(graph: Any) -> Any:
    """找工具节点的名字（`create_agent` 生成的图里通常叫 `tools`），用于 as_node。"""
    try:
        names = set((graph.get_graph().nodes or {}).keys())
    except Exception:  # noqa: BLE001 - 取不到就退回默认推断
        return None
    for candidate in ("tools", "tool", "ToolNode"):
        if candidate in names:
            return candidate
    return None


async def repair_thread_state(graph: Any, config: Dict[str, Any]) -> Dict[str, Any]:
    """检查并修复该 thread 的非法工具调用序列；返回 `{"repaired": n, "ids": [...]}`。

    `graph` 必须是带 checkpointer 的已编译图（LangGraph `aget_state` / `aupdate_state`）。
    任何异常都只记日志并返回 `repaired=0` —— 自愈失败不该让运行起不来。
    """
    try:
        snapshot = await graph.aget_state(config)
        values = getattr(snapshot, "values", None) or {}
        messages = list(values.get("messages") or [])
    except Exception as e:  # noqa: BLE001
        logger.debug("读取线程状态失败（跳过自愈）：%s", e)
        return {"repaired": 0, "ids": []}
    if not messages:
        return {"repaired": 0, "ids": []}

    missing = dangling_tool_calls(messages)
    if not missing and not orphan_tool_call_ids(messages):
        return {"repaired": 0, "ids": []}

    updates, stats = pairing_updates(messages)
    if not updates:
        return {"repaired": 0, "ids": [cid for cid, _ in missing]}
    as_node = _tools_node_name(graph)
    try:
        if as_node:
            await graph.aupdate_state(config, {"messages": updates}, as_node=as_node)
        else:
            await graph.aupdate_state(config, {"messages": updates})
    except Exception as e:  # noqa: BLE001
        logger.warning("线程状态自愈写入失败（本轮仍会尝试继续）：%s", e)
        return {"repaired": 0, "ids": [cid for cid, _ in missing]}
    logger.warning("线程自愈：改写 %d 条消息（去掉 %d 个无回执调用）、移除 %d 条孤儿回执（%s）",
                   stats["rewritten"], stats["dropped_calls"], stats["removed_orphans"],
                   "、".join(cid for cid, _ in missing[:5]) or "-")
    return {"repaired": stats["rewritten"] + stats["removed_orphans"],
            "ids": [cid for cid, _ in missing]}
