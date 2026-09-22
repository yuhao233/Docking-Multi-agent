"""分子筛选与优化协作系统 —— 整体协调 Agent 入口。

整体协调 Agent 统筹 3 个子 Agent（分子属性评估 / Docking 执行 / 结合模式检测），
完成「分子库导入 → 任务分发 → 并行处理 → 结果汇总与优化建议」的完整闭环。

LLM 由 docking_agent.runtime.llm 依据 .env 构建（任意 OpenAI 兼容端点）。
不再依赖 Coze 工作负载身份与平台网关。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from langchain.agents import create_agent

from docking_agent.runtime.context import AgentContext, Context
from docking_agent.runtime.llm import build_chat_llm, load_llm_config
from docking_agent.runtime.checkpoints import get_memory_saver

from docking_agent.runtime.blackboard import shared_store
from docking_agent.agents.middleware import build_agent_middleware
from docking_agent.agents.prompt_blocks import coordinator_prompt_middleware
from docking_agent.agents.prompts import COORDINATOR_SP
from docking_agent.agents.state import AgentState  # noqa: F401 - 再导出，保持既有 import 路径
from docking_agent.agents.workers import init_workers
from docking_agent.agents.dispatch import (
    import_molecule_library,
    run_property_assessment,
    run_pocket_analysis,
    run_docking,
    run_binding_mode_analysis,
)
from docking_agent.tools.pose import analyze_pose_pocket
from docking_agent.tools.recommend import customize_report, recommend_compounds, submit_recommendations
from docking_agent.tools.report import generate_screening_report
from docking_agent.tools.online import (
    fetch_protein_structure,
    fetch_molecule_record,
)

logger = logging.getLogger(__name__)

# 状态与上下文窗口定义在 agents/state.py（避免与 middleware 循环导入），这里再导出：
#   AgentState / MAX_MESSAGES / _windowed_messages（后者是 windowed_messages 的兼容名）
from docking_agent.agents.state import MAX_MESSAGES, _windowed_messages  # noqa: E402,F401


def build_agent(ctx: Optional[Context] = None) -> Any:
    """构建整体协调 Agent（内含 4 个子 Agent 的调度工具）。"""
    # 先初始化 4 个子 Agent（幂等），供分发工具调用
    init_workers(ctx)

    cfg = load_llm_config()
    llm = build_chat_llm(ctx, role="coordinator")
    logger.info("整体协调 Agent 构建完成，model=%s", cfg["config"].get("model"))

    # 系统提示词：配置里的 `sp` 是**全文**（含条件段标记）。动态提示词中间件按本次运行的
    # 事实抽掉用不到的纪律段（省固定开销），并把注入清单记进 run.data["prompt_blocks"]。
    base_sp = cfg.get("sp") or COORDINATOR_SP
    middleware = list(build_agent_middleware(llm, role="coordinator"))
    middleware.append(coordinator_prompt_middleware(base_sp))

    return create_agent(
        model=llm,
        system_prompt=base_sp,
        tools=[
            import_molecule_library,
            run_property_assessment,
            run_pocket_analysis,
            run_docking,
            run_binding_mode_analysis,
            recommend_compounds,
            submit_recommendations,
            customize_report,          # 按用户对输出内容/形式的要求定制报告（骨架不变）
            analyze_pose_pocket,
            generate_screening_report,
            fetch_protein_structure,
            fetch_molecule_record,
        ],
        middleware=middleware,
        checkpointer=get_memory_saver(),
        state_schema=AgentState,
        context_schema=AgentContext,
        # P2-c：父图与 4 个子 Agent 共享同一个 store → 黑板在 store 后端下互相可见
        store=shared_store(),
        name="coordinator",
    )
