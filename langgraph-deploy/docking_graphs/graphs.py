"""LangGraph Studio / LangGraph CLI 暴露的图（langgraph.json → graphs）。

| 图名 | 说明 | 是否需要 LLM |
| --- | --- | --- |
| `coordinator` | 整体协调 Agent：把自然语言任务拆给 4 个子 Agent（属性 / 口袋 / 对接 / 结合模式） | 需要 |
| `pipeline` | 确定性流水线：属性 → 定盒 → 对接 → 排序 → 报告（无 LLM，结果可复算） | 不需要 |
| `intake` | 任务受理层：自然语言 + 表单 → 结构化任务规约（含缺失项 / 假设 / 权威判定） | 可选 |
| `property` / `pocket` / `docking` / `binding` | 4 个子 Agent 本身（逐步调试单个子 Agent 用） | 需要 |

**为什么不直接导出 `build_agent()`**：项目里的协调 Agent 编译时挂了 `MemorySaver`
（网页端多轮会话靠它）。LangGraph Platform / Studio 会**自己托管持久化**，图不能自带
checkpointer。这里在构建瞬间把 `get_memory_saver` / `workers._checkpointer` 替换成
「返回 None」，其余完全复用项目源码（同一份工具列表、同一份提示词、同一份 LLM 配置），
因此**不存在两处维护导致漂移**的问题。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Dict, List, Optional, TypedDict
from unittest import mock

from langchain_core.messages import AnyMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph

from docking_agent.config import env_int

# 重量级/会碰文件系统的导入一律放**模块顶层**：图加载发生在事件循环之外，
# 而首次 `import matplotlib` 会创建缓存目录（os.mkdir）——若留在节点函数里懒导入，
# `langgraph dev` 的 blockbuster 会直接判为 BlockingError 让整次运行失败。
from docking_agent import intake as intake_layer
from docking_agent.api.schemas import AgentRequest
from docking_agent.agents.persistence import persist_agent_run

# 绝对导入（不能用相对导入）：LangGraph CLI 是按**文件路径**加载 graphs.py 的，
# 此时模块没有 __package__，`from .runtime import ...` 会报
# "attempted relative import with no known parent package"。
from docking_graphs.runtime import (
    bind_run,
    call_meta,
    detached_messages,
    last_user_text,
    open_run,
    save_run,
)

logger = logging.getLogger(__name__)

RECURSION_LIMIT = env_int("RECURSION_LIMIT", 60)


class AgentState(TypedDict, total=False):
    """Studio 对话入口的输入/输出契约（与网页端一致：给 messages 即可）。"""

    messages: Annotated[List[AnyMessage], add_messages]
    run_id: str
    run_dir: str


def last_ai_text(messages: List[Any]) -> str:
    """取最后一条有内容的 AI 消息（与网页端 `_last_ai_text` 同义）。"""
    for m in reversed(messages or []):
        if type(m).__name__ not in ("AIMessage", "AIMessageChunk"):
            continue
        content = getattr(m, "content", "")
        if isinstance(content, list):
            content = "".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        text = str(content).strip()
        if text:
            return text
    return ""


def _agent_model_map() -> Dict[str, Any]:
    """各角色实际用的模型/调用次数（与网页端运行记录同结构）。"""
    from docking_agent.runtime.llm import llm_registry

    out: Dict[str, Any] = {}
    for role, meta in llm_registry().items():
        entry = {"model": meta.get("model"), "temperature": meta.get("temperature"),
                 "instance_id": meta.get("instance_id"), "calls": meta.get("calls", 0)}
        if meta.get("actual_model"):
            entry["actual_model"] = meta["actual_model"]
        out[role] = entry
    return out


def _toolset_summary(graph: Any) -> str:
    """列出 create_agent 图里实际绑定的工具名（自检/日志用）。"""
    node = getattr(graph, "nodes", {}).get("tools")
    tools = getattr(getattr(node, "bound", None), "tools_by_name", None)
    if isinstance(tools, dict):
        return ",".join(sorted(tools))
    return ""


def _wrap_agent(name: str, inner: Any) -> CompiledStateGraph:
    """把**已构建好的**内层 Agent 图包成「一层运行作用域 + 直接把消息交给它」的图。

    单节点是刻意的：ContextVar 只在**同一个协程**里可靠传播；若拆成
    `prepare` → `agent` 两个节点，两个节点分属不同 task，`current_run` 会丢。

    `inner` 必须由调用方在**图构建阶段**（事件循环之外）建好并传入：
      - `langgraph dev` 的 blockbuster 会把事件循环里的同步文件 I/O 判为
        `BlockingError` —— 构建 Agent 会读 `config/agent_llm_config.json`
        （`Path.exists()` → `os.stat`），留在节点里则 coordinator 与 4 个子 Agent
        首次运行必然失败（真实缺陷，已由 `tests/test_blockbuster.py` 回归保护）；
      - 顺带避免每个请求都重建 5 个 LLM 实例。
    """
    if inner is None:
        raise RuntimeError(f"内层 Agent 图未构建成功：{name}（检查 LLM 配置与子 Agent 初始化）")

    async def _run(state: AgentState) -> Dict[str, Any]:
        messages = list(state.get("messages") or [])
        request = {"message": last_user_text(messages)}
        # 建运行 = 同步文件 I/O → 必须离开事件循环（langgraph dev 开着 blockbuster）
        run = await open_run(name, request)
        with bind_run(run):
            try:
                # P1：把运行上下文作为 **LangGraph context** 传入（工具侧的权威来源），
                # ContextVar 仍保留为兜底（双读期两条路径等价）
                from docking_agent.runtime.context import AgentContext
                from docking_agent.agents.blackboard import current_blackboard

                out = await inner.ainvoke(
                    {"messages": messages},
                    config={"recursion_limit": RECURSION_LIMIT},
                    context=AgentContext(run=run, blackboard=current_blackboard.get()),
                )
            except Exception as e:  # noqa: BLE001 - 失败也要如实落盘，便于事后定位
                await asyncio.to_thread(run.finish, "error", str(e))
                await save_run(run)
                raise
            after = list((out or {}).get("messages") or []) if isinstance(out, dict) else []
            # 与网页端同一套收尾：把真实工具产物（docking.json / ranking.csv / report.md …）
            # 写进运行目录并标记完成 —— 否则 Studio 里只剩 *_tool.json 中间文件。
            await asyncio.to_thread(persist_agent_run, run, after, last_ai_text(after))
            run.data["agent_models"] = _agent_model_map()
            await asyncio.to_thread(run.finish, "ok")
            meta = call_meta(run)
            await save_run(run)
            return {**meta, "messages": detached_messages(messages, after)}

    builder = StateGraph(AgentState)
    builder.add_node("agent", _run)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    compiled = builder.compile(name=name)
    logger.info("Studio 图 %s 构建完成（工具：%s）", name, _toolset_summary(compiled) or "n/a")
    return compiled


# --------------------------------------------------------------------------- #
# 1) 协调 Agent（多 Agent 协作主入口）
# --------------------------------------------------------------------------- #
_coordinator: Optional[CompiledStateGraph] = None


def _build_coordinator_inner() -> Any:
    """复用项目的 `build_agent()`，但让它在**无 checkpointer** 下构建（平台自己托管）。"""
    from docking_agent.agents import coordinator as C
    from docking_agent.agents import workers as W

    with mock.patch.object(C, "get_memory_saver", lambda: None), \
            mock.patch.object(W, "_checkpointer", lambda role: None):
        return C.build_agent(None)


def coordinator() -> CompiledStateGraph:
    """整体协调 Agent（Studio 主入口）。"""
    global _coordinator
    if _coordinator is None:
        # 在**图构建阶段**构建内层图（此时不在事件循环里，blockbuster 不会拦）
        _coordinator = _wrap_agent("coordinator", _build_coordinator_inner())
    return _coordinator


# --------------------------------------------------------------------------- #
# 2) 4 个子 Agent（逐步调试用；与协调 Agent 内部用的是同一批对象）
# --------------------------------------------------------------------------- #
_workers: Dict[str, CompiledStateGraph] = {}

_WORKER_GETTERS = {
    "property": "get_property_agent",
    "pocket": "get_pocket_agent",
    "docking": "get_docking_agent",
    "binding": "get_binding_agent",
}


def _worker(name: str) -> CompiledStateGraph:
    if name not in _workers:
        from docking_agent.agents import workers as W

        # 子 Agent 由 init_workers 构建；这里沿用协调 Agent 的构建路径（同样去掉 checkpointer），
        # 保证 Studio 里单独跑的子 Agent 与协调 Agent 内部调用的是**同一个**实例/提示词/工具集。
        # 同样是构建期完成（不在事件循环里），并使用同一个缓存实例。
        _build_coordinator_inner()
        getter = getattr(W, _WORKER_GETTERS[name])
        _workers[name] = _wrap_agent(name, getter())
    return _workers[name]


def property_agent() -> CompiledStateGraph:
    """分子属性评估 Agent（RDKit 理化性质 / 类药性）。"""
    return _worker("property")


def pocket_agent() -> CompiledStateGraph:
    """口袋分析 Agent（P2Rank / 几何法预测口袋并提交对接盒）。"""
    return _worker("pocket")


def docking_agent() -> CompiledStateGraph:
    """Docking 执行 Agent（真实 AutoDock Vina / AutoDock4 对接）。"""
    return _worker("docking")


def binding_agent() -> CompiledStateGraph:
    """结合模式检测 Agent（相互作用指纹 / 阳性对照相似度）。"""
    return _worker("binding")


# --------------------------------------------------------------------------- #
# 4) 任务受理层（可观测：自然语言到底被理解成了什么）
# --------------------------------------------------------------------------- #
class IntakeState(TypedDict, total=False):
    """受理层输入（字段与 `AgentRequest` 对齐，只列常用的）。"""

    message: str
    mode: str
    advanced: bool
    receptor: str
    receptor_file: str
    ligands_text: str
    molecule_file: str
    positive_control: str
    exhaustiveness: Optional[int]
    n_poses: Optional[int]
    engine: str
    pocket_engine: str
    site_center: Optional[List[float]]
    site_size: Optional[List[float]]
    save_poses: Optional[bool]
    max_ligands: Optional[int]
    protonation: str
    protonation_ph: float
    skip_positive_control: bool
    use_llm: bool
    # ---- 输出 ----
    task_spec: Dict[str, Any]
    agent_message: str
    run_id: str
    run_dir: str


async def _run_intake(state: IntakeState) -> Dict[str, Any]:
    fields = {k: v for k, v in state.items()
              if k in AgentRequest.model_fields and v is not None}
    fields["mode"] = fields.get("mode") or "chat"
    req = AgentRequest(**fields)
    request = {"message": req.message, "mode": req.mode, "advanced": req.advanced}

    run = await open_run("intake", request)
    with bind_run(run):
        message, spec = await asyncio.to_thread(
            intake_layer.build_message, req, allow_llm=bool(state.get("use_llm", True)),
            run=run, prior_turns=None)
        run.data["task_spec"] = spec
        meta = call_meta(run)
        await save_run(run)
        return {**meta, "task_spec": spec, "agent_message": message}


def intake() -> CompiledStateGraph:
    """任务受理层：自然语言 + 表单 → 结构化任务规约 + 下发给协调 Agent 的指令。"""
    builder = StateGraph(IntakeState)
    builder.add_node("intake", _run_intake)
    builder.add_edge(START, "intake")
    builder.add_edge("intake", END)
    return builder.compile(name="intake")
