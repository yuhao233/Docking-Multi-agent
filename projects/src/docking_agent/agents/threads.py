"""对话线程（checkpointer state）自愈：补齐中断留下的**未回填工具调用**。

## 真实故障（用户报的 400）

    400 - An assistant message with 'tool_calls' must be followed by tool messages
          responding to each 'tool_call_id'. (insufficient tool messages ...)

成因：用户点「停止」或运行中途失败时，模型**已经发出** `tool_calls`，而对应的 `ToolMessage`
永远不会产生（工具被中止/进程被放弃）。LangGraph 的 checkpointer 把那条 AIMessage 记进了 thread
历史 —— 于是**同一会话的下一轮**请求就把非法消息序列发给 OpenAI，直接 400，整段对话卡死。

## 修法

**每一轮开始前**检查 thread 历史：对每个没有回执的 `tool_call_id`，在它所属的 AIMessage
**正后方**插入一条占位 `ToolMessage`，内容如实说明「该工具调用在上一轮被中断、没有产生结果、
不要假设数据存在」。然后把这些消息写回状态（先 RemoveMessage 全部、再按修正后的顺序重新写入，
因为 `add_messages` 只能追加、不能插入中间位置）。

两条纪律：

1. **占位回执是事实说明，不是编造结果** —— 符合「工具只报事实、Agent/用户做判断」的项目准则；
2. **只在下一轮开始前修**（不在取消的那一刻修）：取消时工具可能仍在跑，若它稍后补回真实回执，
   同一条 `tool_call_id` 会出现两条回执，同样会被 OpenAI 拒绝。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

#: 占位回执正文（给模型看的事实说明）
INTERRUPT_NOTE = ("该工具调用在上一轮运行中被中断（用户取消或运行失败），**没有产生结果**。"
                  "不要假设它已经返回数据；如仍需要该结果，请重新调用。")


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


def repair_messages(messages: List[Any], *, note: str = INTERRUPT_NOTE) -> Tuple[List[Any], int]:
    """在缺失回执的 AIMessage 正后方插入占位 `ToolMessage`。

    返回 `(修正后的消息列表, 补了几条)`；不修改入参、不丢任何原有消息（顺序保持）。
    """
    from langchain_core.messages import ToolMessage

    answered = set()
    for m in messages or []:
        if type(m).__name__ == "ToolMessage":
            answered.add(str(getattr(m, "tool_call_id", "") or ""))

    repaired: List[Any] = []
    added = 0
    pending: List[Tuple[str, str]] = []
    for m in messages or []:
        name = type(m).__name__
        # 先把**上一条** AIMessage 欠下的回执补上（必须在 AIMessage 之后、下一条消息之前）
        if pending and name != "ToolMessage":
            for call_id, tool_name in pending:
                repaired.append(ToolMessage(content=note, tool_call_id=call_id,
                                            name=tool_name or "tool"))
                added += 1
            pending = []
        repaired.append(m)
        if name in ("AIMessage", "AIMessageChunk"):
            for call in (getattr(m, "tool_calls", None) or []):
                call_id = str((call or {}).get("id") or "")
                if not call_id or call_id in answered:
                    continue
                pending.append((call_id, str((call or {}).get("name") or "")))
        elif name == "ToolMessage":
            pending = [(cid, tname) for cid, tname in pending
                       if cid != str(getattr(m, "tool_call_id", "") or "")]
    for call_id, tool_name in pending:              # 结尾处仍欠着的（最常见：中断在尾部）
        repaired.append(ToolMessage(content=note, tool_call_id=call_id,
                                    name=tool_name or "tool"))
        added += 1
    return repaired, added


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


async def repair_thread_state(graph: Any, config: Dict[str, Any], *,
                              note: str = INTERRUPT_NOTE) -> Dict[str, Any]:
    """检查并修复该 thread 的悬空工具调用；返回 `{"repaired": n, "ids": [...]}`。

    `graph` 必须是带 checkpointer 的已编译图（LangGraph `aget_state` / `aupdate_state`）。
    修复方式：先移除全部历史消息，再按修正后的顺序整体写回（`add_messages` 只能追加）。
    任何异常都只记日志并返回 `repaired=0` —— 自愈失败不该让运行起不来。
    """
    from langchain_core.messages import RemoveMessage

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
    if not missing:
        return {"repaired": 0, "ids": []}

    repaired, added = repair_messages(messages, note=note)
    update = {"messages": [RemoveMessage(id=getattr(m, "id", "") or "") for m in messages] + repaired}
    as_node = _tools_node_name(graph)
    try:
        if as_node:
            await graph.aupdate_state(config, update, as_node=as_node)
        else:
            await graph.aupdate_state(config, update)
    except Exception as e:  # noqa: BLE001
        logger.warning("线程状态自愈写入失败（本轮仍会尝试继续）：%s", e)
        return {"repaired": 0, "ids": [cid for cid, _ in missing]}
    logger.warning("线程自愈：为 %s 个中断的 tool_call 补了占位回执（%s）",
                   added, "、".join(cid for cid, _ in missing[:5]))
    return {"repaired": added, "ids": [cid for cid, _ in missing]}
