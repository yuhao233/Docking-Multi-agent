"""固定格式的 Markdown 报告生成（两种运行模式共用同一模板）。

报告章节顺序与编号**固定**，便于人工阅读与机器解析：

    表 1  报告信息（运行编号 / 模式 / 生成时间 / 受体 / 对接盒 / 参数 / 计数）
    ## 1. 任务与参数
        1.1 任务来源与受理
        1.2 受体、位点与对接盒
        1.3 对接参数（同一阶段内参数一致）
        1.4 工具与版本
        1.5 参数自动规划
        1.6 结合口袋预测
    ## 2. 结果排序
        2.1 主组排序（含命中判据）
        2.2 大配体组（盒子不同，不跨组比较）
    ## 3. 推荐分子
    ## 4. 理化性质
    ## 5. 结合模式与阳性对照比较
    ## 6. 方法与局限
    ## 7. 失败与跳过
    ## 8. 结论与建议（多 Agent 模式下由协调 Agent 撰写）
    ## 9. 数据与产物

排版约定（Markdown 与 PDF 共用同一套骨架）：

* 图、表都有**连续编号与题注**（表题在表上方，图题在图下方）；
* 图片以**相对路径**内嵌（`![图 N …](charts/xxx.png)`），任何 Markdown 阅读器都能直接显示；
  网页报告页会把相对路径解析成产物接口的真实 `<img src>`，界面不出现任何地址文本；
* 正文**不出现 URL**：产物引用统一写成「文件名 + 章节位置」，不使用裸链接；
* 数字格式统一：亲和力 2 位小数 + 单位 `kcal/mol`、分子量 2 位、logP/TPSA 2 位、百分比 1 位。

**结构**（第 2 波拆分）：原先这里是一个 900+ 行的 `build_markdown_report()`。现在它只做两件事：
构造 `ReportContext`（见 `report_context.py`，集中所有「算一次」的派生数据与图/表编号），
再按下面的固定顺序调用各章节函数（见 `report_sections_setup.py` / `report_sections_results.py`）。
每个章节函数都是「上下文 → 行列表」的纯函数，`tests/test_report_golden.py` 用逐字节快照兜底。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from docking_agent.reporting.content import _strip_urls, tool_versions as tool_versions
from docking_agent.reporting.report_context import (
    CHART_FILES as CHART_FILES,
    ReportContext,
    _brief as _brief,
)
from docking_agent.reporting.report_sections_results import (
    section_3_recommendations,
    section_4_properties,
    section_5_binding,
    section_6_methods,
    section_7_failures,
    section_8_conclusions,
    section_9_artifacts,
)
from docking_agent.reporting.report_sections_setup import (
    section_0_requirements,
    section_1_task_and_params,
    section_2_ranking,
    section_header,
)

# 仍从本模块导出：`artifacts.py` 与 `pdf.py` 通过 `report.CHART_FILES` / `report.tool_versions` 复用
__all__ = ["CHART_FILES", "build_markdown_report", "tool_versions"]

#: 报告骨架的**固定渲染顺序**（章节编号与顺序是报告契约的一部分 —— 不要重排）。
#: 图表编号计数器在上下文里递增，因此顺序也决定了图/表编号。
SECTIONS: List[Callable[[ReportContext], List[str]]] = [
    section_header,
    section_0_requirements,
    section_1_task_and_params,
    section_2_ranking,
    section_3_recommendations,
    section_4_properties,
    section_5_binding,
    section_6_methods,
    section_7_failures,
    section_8_conclusions,
    section_9_artifacts,
]


def build_markdown_report(result: Dict[str, Any], *, kind: str = "agent",
                          run_id: str = "", receptor_label: str = "",
                          site: Optional[Dict[str, Any]] = None,
                          artifacts: Optional[List[Dict[str, Any]]] = None,
                          agent_narrative: str = "",
                          agent_models: Optional[Dict[str, Any]] = None,
                          created_at: str = "", top_n: int = 0) -> str:
    """按固定模板生成报告。

    agent_narrative：多 Agent 模式下协调 Agent 的结论文本，放入固定的第 8 节。
    agent_models：多 Agent 模式下各角色 Agent 实际使用的模型（每个角色独立实例）。
    """
    ctx = ReportContext(result, kind=kind, run_id=run_id, receptor_label=receptor_label,
                        site=site, artifacts=artifacts, agent_narrative=agent_narrative,
                        agent_models=agent_models, created_at=created_at, top_n=top_n)
    lines: List[str] = []
    for section in SECTIONS:
        lines += section(ctx)
    return _strip_urls("\n".join(lines))
