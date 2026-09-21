"""子 Agent 工作器：构建并托管 3 个协作子 Agent。

- 分子属性评估 Agent：调用 molecular_property_assessment
- 口袋分析 Agent：调用 predict_binding_pockets（P2Rank / 内置几何法）并提交对接盒
- Docking 执行 Agent：调用 molecular_docking
- 结合模式检测 Agent：调用 binding_mode_analysis / positive_control_similarity

被整体协调 Agent 通过样本分发工具调用，实现多 Agent 协作。

**每个子 Agent 拥有独立的 LLM 实例**：`init_workers` 会为 property / docking / binding
三个角色分别调用 `build_chat_llm(role=...)`（见 runtime/llm 的按角色配置说明），
因此三者可以使用**不同模型/端点/采样参数**，且不共享模型对象；
同时每个子 Agent 使用**独立的 checkpointer**，短期记忆互不干扰。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, Optional

from langchain.agents import create_agent
from langgraph.checkpoint.memory import InMemorySaver
from docking_agent.runtime.context import (AgentContext, Context, active_run,
                                          current_agent_context)
from docking_agent.runtime.llm import build_chat_llm

from docking_agent.agents.blackboard import shared_store
from docking_agent.agents.middleware import build_agent_middleware
from docking_agent.agents.reports import (is_structured_rejection, report_model,
                                          uses_structured_output)
from docking_agent.agents.prompts import BINDING_SP, DOCKING_SP, POCKET_SP, PROPERTY_SP
from docking_agent.tools.binding import (
    binding_mode_analysis,
    check_binding_consistency,
    positive_control_similarity,
)
from docking_agent.tools.docking import available_receptors, molecular_docking
from docking_agent.tools.online import fetch_protein_structure
from docking_agent.tools.pockets import (
    compare_pocket_with_experiment,
    list_pocket_engines,
    predict_binding_pockets,
    set_docking_site,
)
from docking_agent.tools.properties import molecular_property_assessment, normalize_molecule_library

logger = logging.getLogger(__name__)

# 子 Agent 角色名（与 runtime/llm.ROLES 一致，用于按角色构建独立模型实例）
WORKER_ROLES = ("property", "pocket", "docking", "binding")


def _build_llm(ctx: Optional[Context] = None, role: str = "worker"):
    """构建该子 Agent 专属的 LLM 实例（本地 .env / agent_llm_config.json 按角色配置）。"""
    return build_chat_llm(ctx, role=role)


_property_agent = None
_pocket_agent = None
_docking_agent = None
_binding_agent = None

# 每个子 Agent 一个独立模型实例 / 独立 checkpointer（role → 对象）
_worker_llms: Dict[str, Any] = {}
_worker_checkpointers: Dict[str, Any] = {}


def _checkpointer(role: str) -> InMemorySaver:
    """为子 Agent 单独创建 checkpointer：三者的会话记忆彼此隔离。"""
    if role not in _worker_checkpointers:
        _worker_checkpointers[role] = InMemorySaver()
    return _worker_checkpointers[role]


def _make_agent(llm, sp, tools, mem, name: str = "worker", structured: bool = True):
    """构建子 Agent 图。

    `name` 必须给：默认名会退化成 `'LangGraph'`，四个子 Agent 在 Studio / 回调归属 /
    子图编排里将无法区分（可观测性规范）。

    **结构化输出**（P2）：按角色挂 `response_format=ToolStrategy(<Role>Report)`，
    由框架强制模型调用结构化输出工具 —— 取代「靠提示词要求模型自己吐 JSON + 服务端解析重试」
    这条会真实产生 `agent_output_invalid` 的路径（见 `agents/reports.py`）。
    """
    model_cls = report_model(name) if structured else None
    structured_kwargs: Dict[str, Any] = {}
    if model_cls is not None and uses_structured_output():
        from langchain.agents.structured_output import ToolStrategy  # 延迟导入，构建期开销小

        structured_kwargs["response_format"] = ToolStrategy(model_cls)
    return create_agent(model=llm, system_prompt=sp, tools=tools,
                        middleware=build_agent_middleware(llm, role=name or "worker"),
                        checkpointer=mem, state_schema=None,
                        context_schema=AgentContext, store=shared_store(),
                        name=name, **structured_kwargs)


#: 角色 → 系统提示词与工具集（构建/重建共用，避免两处漂移）
_WORKER_SPECS: Dict[str, Any] = {
    "property": (lambda: PROPERTY_SP,
                 lambda: [normalize_molecule_library, molecular_property_assessment]),
    "pocket": (lambda: POCKET_SP,
               lambda: [predict_binding_pockets, compare_pocket_with_experiment,
                        set_docking_site, list_pocket_engines, available_receptors]),
    "docking": (lambda: DOCKING_SP,
                lambda: [available_receptors, molecular_docking, fetch_protein_structure]),
    "binding": (lambda: BINDING_SP,
                lambda: [binding_mode_analysis, positive_control_similarity,
                         check_binding_consistency]),
}

#: id(子 Agent 图) → 角色名；以及 角色 → 「重建一个文本契约版」的构建器
_agent_roles: Dict[int, str] = {}
_WORKER_BUILDERS: Dict[str, Any] = {}


def _build_role_agent(role: str, ctx: Optional[Context] = None, *,
                      structured: bool = True) -> Any:
    """构建某个角色的子 Agent 图（同一角色复用同一个 LLM 实例与 checkpointer）。"""
    sp_factory, tools_factory = _WORKER_SPECS[role]
    if role not in _worker_llms:
        # 关键：每个角色一个独立模型实例，而不是共用一个 llm 对象
        _worker_llms[role] = _build_llm(ctx, role=role)
    return _make_agent(_worker_llms[role], sp_factory(), tools_factory(),
                       _checkpointer(role), name=role, structured=structured)


def init_workers(ctx: Optional[Context] = None) -> None:
    """构建 4 个子 Agent（幂等，仅构建一次）；每个子 Agent 使用独立 LLM 实例。"""
    global _property_agent, _pocket_agent, _docking_agent, _binding_agent
    if _property_agent is not None:
        return

    agents = {role: _build_role_agent(role, ctx) for role in WORKER_ROLES}
    _property_agent = agents["property"]
    _pocket_agent = agents["pocket"]
    _docking_agent = agents["docking"]
    _binding_agent = agents["binding"]

    # 登记「图 → 角色」与「角色 → 文本契约重建器」：供应商拒绝结构化输出时用来降级
    for role, agent in agents.items():
        _agent_roles[id(agent)] = role
        _WORKER_BUILDERS[role] = (lambda role=role, **kw: _build_role_agent(
            role, structured=bool(kw.get("structured", False))))
    logger.info("子 Agent 已初始化：%s（各自独立 LLM 实例与 checkpointer；结构化输出=%s）",
                worker_llm_info() or {r: "?" for r in WORKER_ROLES}, uses_structured_output())


def reset_workers() -> None:
    """清空已构建的子 Agent（配置热更新/测试用）。"""
    global _property_agent, _pocket_agent, _docking_agent, _binding_agent
    _property_agent = _pocket_agent = _docking_agent = _binding_agent = None
    _worker_llms.clear()
    _worker_checkpointers.clear()
    _agent_roles.clear()
    _WORKER_BUILDERS.clear()
    _worker_text_fallbacks.clear()


def get_property_agent():
    return _property_agent


def get_pocket_agent():
    return _pocket_agent


def get_docking_agent():
    return _docking_agent


def get_binding_agent():
    return _binding_agent


def get_worker_llm(role: str) -> Any:
    """取某个子 Agent 的模型实例（用于核对实例独立性）。"""
    return _worker_llms.get(role)


def worker_llm_info() -> Dict[str, Any]:
    """各子 Agent 实际使用的模型与实例号。"""
    info: Dict[str, Any] = {}
    for role, llm in _worker_llms.items():
        info[role] = {
            "model": getattr(llm, "model_name", None) or getattr(llm, "model", None),
            "instance_id": id(llm),
        }
    return info


def worker_llm_models() -> Dict[str, Any]:
    """各子 Agent 实际使用的模型名（用于运行记录/报告展示）。"""
    return {role: meta.get("model") for role, meta in worker_llm_info().items()}


#: 结构化输出被供应商拒绝后，按 `id(名字图)` 缓存对应的「文本契约」图（懒构建，只建一次）
_worker_text_fallbacks: Dict[int, Any] = {}


def _text_fallback_agent(agent: Any, role: str) -> Any:
    """为某个结构化子 Agent 构建/取出「不带 response_format」的同款图（同一 LLM 实例）。"""
    key = id(agent)
    if key in _worker_text_fallbacks:
        return _worker_text_fallbacks[key]
    builder = _WORKER_BUILDERS.get(role)
    if builder is None:
        return None
    fallback = builder(structured=False)
    _worker_text_fallbacks[key] = fallback
    return fallback


def invoke_worker(agent: Any, content: str, thread_id: str) -> str:
    """调用子 Agent 并提取最终文本结果（优先解析为 JSON 原文）。

    子 Agent 是「只调用工具并原样返回结果」的**无状态执行器**（既定设计，P1 明确保留）：
      - 每次调用使用**独立 thread_id**（`{thread_id}-{agent}-{随机}`），因此同一轮对话里
        反复调用同一子 Agent 时，它**看不到自己上一次的输出** —— 这是刻意的：
        防止两次筛选任务之间上下文串扰（本地长驻服务/多会话共享一个进程）；
      - 需要跨步骤共享的信息一律走**运行级**通道：运行产物文件（`tool_io.artifact_path`）
        与共享黑板（受体/位点盒/分子库等小状态），而不是子 Agent 的会话记忆。
    回归保护：`tests/test_agent_conventions.py::test_sub_agent_calls_are_stateless_by_design`。
    """
    from langchain_core.messages import HumanMessage
    if agent is None:
        raise RuntimeError("sub-agent 未初始化，请先调用 init_workers(ctx)")
    call_thread = f"{thread_id}-{id(agent)}-{uuid.uuid4().hex[:8]}"
    payload = {"messages": [HumanMessage(content=content)]}
    config = {"configurable": {"thread_id": call_thread}}
    # 该角色的结构化输出此前已被供应商拒绝过 → 直接用文本契约图，不再重复付一次 400 的代价
    if isinstance(agent, object) and id(agent) in _worker_text_fallbacks:
        agent = _worker_text_fallbacks[id(agent)]
    try:
        result = agent.invoke(payload, config=config,
                              # 双读期：把当前运行上下文作为**权威来源**传入
                              context=current_agent_context())
    except Exception as exc:  # noqa: BLE001 - 只拦「供应商不支持结构化输出」这一类
        role = _agent_roles.get(id(agent), "")
        if not is_structured_rejection(exc) or not role:
            raise
        fallback = _text_fallback_agent(agent, role)
        if fallback is None:
            raise
        logger.warning("%s 子 Agent：供应商拒绝结构化输出（%s），已降级为文本 JSON 契约"
                       "（AGENT_STRUCTURED_OUTPUT=off 可彻底关闭结构化输出）",
                       role, getattr(exc, "message", "") or exc)
        # 如实写进运行记录：报告/运行详情里能看到"本次为什么没用结构化输出"
        run = active_run()
        if run is not None and hasattr(run, "log"):
            try:
                run.log(f"{role} 子 Agent：供应商拒绝结构化输出（thinking 模式不支持强制 "
                        f"tool_choice），已降级为文本 JSON 契约（结果仍经必需字段校验）")
            except Exception:  # noqa: BLE001 - 记录失败不影响本次调用
                logger.debug("降级说明写入运行记录失败（忽略）", exc_info=True)
        result = fallback.invoke(payload, config=config, context=current_agent_context())
    # 结构化输出优先（P2）：框架已用 pydantic 校验过，直接序列化成 JSON 字符串返回，
    # 协调 Agent / persistence 侧的输入契约保持不变（仍是 JSON 原文）
    structured = result.get("structured_response") if isinstance(result, dict) else None
    if structured is not None:
        if hasattr(structured, "model_dump"):
            payload = structured.model_dump()
        elif isinstance(structured, dict):
            payload = structured
        else:  # 理论上不会发生；如实包装，绝不静默丢数据
            payload = {"status": "ok", "value": str(structured)}
        logger.info("子 Agent 返回结构化输出：%s 个字段（%s）", len(payload),
                    ", ".join(sorted(payload)[:6]))
        return json.dumps(payload, ensure_ascii=False, default=str)

    messages = result.get("messages", []) if isinstance(result, dict) else []
    if not messages:
        return str(result)
    last = messages[-1]
    text = last.content if hasattr(last, "content") else str(last)
    if isinstance(text, list):
        text = "".join(str(p.get("text", "")) for p in text if isinstance(p, dict))
    return str(text).strip()
