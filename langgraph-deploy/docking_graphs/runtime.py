"""LangGraph Studio / Platform 运行上下文适配层。

项目的工具层通过 **ContextVar** 获取「当前运行」，Web API 在 `api/app.py` 的每个
SSE 请求里注入它们：

    docking_agent.runs.current_run                     -> 运行目录（中间数据 / 产物落盘）
    docking_agent.agents.blackboard.current_blackboard -> 子 Agent 之间的黑板交接
    docking_agent.runtime.context.request_context      -> 调用上下文（run_id / method）

LangGraph Studio 与 LangGraph Platform 没有这一层，因此本模块补上等价的
「运行作用域」：在**图节点内部**建一次 run、设置三个 ContextVar，然后**在同一个协程里**
调用内层 Agent —— ContextVar 随协程/线程上下文传播，子 Agent 与工具都能拿到，
行为与网页端一致（产物照样落在 `var/runs/<run_id>/`）。

**为什么要拆成 open/close**：`langgraph dev` 默认开着 blockbuster，一旦在事件循环里
出现同步阻塞调用（哪怕只是 `os.mkdir`）整次运行就会失败并报 `BlockingError`。
而「建运行目录 / 写 run.json」必然是同步文件 I/O，所以这里把它们显式放进
`asyncio.to_thread`，事件循环里只留 ContextVar 的 set/reset。

设计约束：本模块**不修改** `projects/` 的任何文件，也不改它的默认行为。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Dict, Iterator, List, Optional

# 顶层导入：这些模块在事件循环里被首次导入时同样会触发文件 I/O（blockbuster 会拦）
from docking_agent.agents.blackboard import (Blackboard, current_blackboard, shared_store,
                                              store_blackboard)
from docking_agent.paths import project_root, workspace_dir
from docking_agent.runs import current_run, get_run_store
from docking_agent.runtime.context import new_context, request_context

logger = logging.getLogger(__name__)

# 部署标识：写进运行记录的 kind / request，便于与网页运行区分
STUDIO_KIND = "studio"


def workspace_path() -> str:
    """当前生效的工作区（决定 assets / config / var 的位置）。"""
    return str(workspace_dir())


def project_path() -> str:
    """被部署的项目源码根目录（`projects/`）。"""
    return str(project_root())


def _reset_llm_counters() -> None:
    """每次 Studio 运行从零计数（与 API 行为一致：调用量只统计本次运行）。"""
    try:
        from docking_agent.runtime.llm import reset_registry_counters

        reset_registry_counters()
    except Exception:  # noqa: BLE001 - 计数是观测能力，失败不影响运行
        logger.debug("LLM 计数重置失败（忽略）", exc_info=True)


def create_run(graph: str, request: Optional[Dict[str, Any]] = None,
               thread_id: str = "") -> Any:
    """建一次运行记录（**阻塞**：mkdir + 写 run.json）。

    `langgraph dev` 下必须通过 `open_run()`（内部 to_thread）调用，否则触发 BlockingError。
    """
    payload: Dict[str, Any] = {"mode": STUDIO_KIND, "graph": graph}
    if request:
        payload.update({k: v for k, v in request.items() if v not in (None, "")})
    run = get_run_store().new(STUDIO_KIND, payload)
    run.data["request"] = payload
    if thread_id:
        run.data["thread_id"] = thread_id
    run.log(f"LangGraph Studio 运行开始：graph={graph}")
    run.save()
    _reset_llm_counters()
    return run


@contextlib.contextmanager
def bind_run(run: Any) -> Iterator[Any]:
    """把 run 绑到当前上下文（**不做 I/O**，可在事件循环里安全使用）。"""
    graph = str((run.data.get("request") or {}).get("graph") or STUDIO_KIND)
    token_run = current_run.set(run)
    token_ctx = request_context.set(new_context(method=f"studio:{graph}"))
    token_board = current_blackboard.set(store_blackboard(shared_store(), run.id))
    try:
        yield run
    finally:
        current_blackboard.reset(token_board)
        request_context.reset(token_ctx)
        current_run.reset(token_run)


async def open_run(graph: str, request: Optional[Dict[str, Any]] = None,
                   thread_id: str = "") -> Any:
    """异步建运行（阻塞部分在线程里跑）—— Studio / Platform 节点用这个。"""
    return await asyncio.to_thread(create_run, graph, request, thread_id)


async def save_run(run: Any) -> None:
    """异步落盘 run.json（同样是阻塞 I/O）。"""
    try:
        await asyncio.to_thread(run.save)
    except Exception:  # noqa: BLE001 - 落盘失败不影响图返回
        logger.warning("run.json 落盘失败", exc_info=True)


@contextlib.contextmanager
def studio_run(graph: str, request: Optional[Dict[str, Any]] = None,
               thread_id: str = "") -> Iterator[Any]:
    """同步版运行作用域（给 `graph.invoke()` 这类**非异步**调用方用）。

    在 `langgraph dev` / Platform 的异步节点里请改用
    `run = await open_run(...)` + `with bind_run(run):` + `await save_run(run)`。
    """
    run = create_run(graph, request, thread_id)
    try:
        with bind_run(run):
            yield run
    finally:
        try:
            run.save()
        except Exception:  # noqa: BLE001
            logger.warning("run.json 落盘失败", exc_info=True)


def detached_messages(before: List[Any], after: Optional[List[Any]]) -> List[Any]:
    """从内层 Agent 的完整消息列表里挑出「新增/被更新」的那些。

    为什么要挑：外层图的 `messages` 用 `add_messages` 归并，若把内层完整历史整段返回，
    会与已有消息重复；而内层状态带滑动窗口（只留最近 40 条），按下标切片也不可靠。
    这里按「对象身份 + 消息 id」判断，窗口把旧消息丢掉时同样正确。
    """
    seen_ids = set()
    seen_obj = set()
    for m in before or []:
        seen_obj.add(id(m))
        mid = getattr(m, "id", None)
        if mid:
            seen_ids.add(mid)
    out: List[Any] = []
    for m in after or []:
        mid = getattr(m, "id", None)
        if id(m) in seen_obj or (mid and mid in seen_ids):
            continue
        out.append(m)
    return out


def last_user_text(messages: Optional[List[Any]]) -> str:
    """取最后一条人类消息的纯文本（写进运行记录，便于事后检索）。"""
    for m in reversed(messages or []):
        if type(m).__name__ not in ("HumanMessage",):
            continue
        content = getattr(m, "content", "")
        if isinstance(content, list):
            content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        text = str(content).strip()
        if text:
            return text
    return ""


def call_meta(run: Any) -> Dict[str, str]:
    """运行记录的路径信息（图返回值里带上，Studio 里一眼能看到产物在哪）。"""
    return {"run_id": str(run.id), "run_dir": str(run.dir)}
