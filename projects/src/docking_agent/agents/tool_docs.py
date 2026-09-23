"""工具用法文档（**给 Agent 查的**，不是塞进系统提示词的纪律）。

为什么要这个模块：用户明确要求"不要用超长提示词实现功能，让 Agent 合理调用文档和工具"。
于是把"怎么用、有什么约束、什么情况下会被拒绝"写成**可调用的文档**：
主管 Agent 不确定时调用 `tool_guide()`（或 `tool_guide("docking")`）取用，而不是把几十行
规则常驻在 SP 里（SP 只留角色与产出契约）。
"""
from __future__ import annotations

from typing import Any, Dict

from langchain.tools import tool

#: 主题 → 用法说明。写"工具会做什么/会拒绝什么/产出在哪"，不写流程口号。
TOOL_GUIDE: Dict[str, str] = {
    "docking": (
        "molecular_docking / run_docking —— 真实对接。\n"
        "参数：molecules_json 或 molecule_file（大库用文件，别把清单塞进上下文）；"
        "receptor_file（上传结构）或 receptor_sources（PDB/UniProt/名称）；"
        "site_center+site_size（不给则由口袋分析定盒）；exhaustiveness（0/留空=用运行级规划值）、"
        "n_poses、engine（留空=设置页默认）、save_poses、max_ligands、keep_hetatm。\n"
        "**前置条件（工具自己检查，不满足直接返回、不做任何计算）**：受体已确定、位点已确定、"
        "配体库非空、受体自带共晶配体时用户已决定是否作阳性对照。缺项时返回里会写清缺什么。\n"
        "大库（≥500）会自动走两阶段：粗筛全库 → 头部精算（精算行 pass=fine，保留 affinity_coarse）。"
    ),
    "report": (
        "generate_screening_report（生成产物与报告）+ customize_report（按用户要求定制，骨架不变）。\n"
        "customize_report 可给：title / extra_columns（白名单列，含 id/cas/remark/formula/…）/ "
        "highlights / requirements[{ask,response}] / notes（≤600 字）。\n"
        "返回 coverage 告诉你每列真实覆盖率：**输入文件里没有的字段不要假装有**，如实说明。\n"
        "报告的表格由脚本排版（口径与可复现性），文字结论由你写（见第 8 节）。"
    ),
    "choices": (
        "需要用户决定时（配体多组分、受体歧义、共晶配体是否作阳性对照）工具会下发**可点选项**：\n"
        "给模型的返回只有原因与数量，选项在界面上；你不要复述选项内容。\n"
        "阻断式问题未回答前，相关工具会拒绝开跑；用户点选后系统**续跑同一运行**，不需要你另起一轮。"
    ),
    "parameters": (
        "exhaustiveness/n_poses 留空 = 用受理层自动规划值（运行级一致）；显式给值即以你为准，"
        "但同一阶段必须一致。参数来源与决策链会写进报告 1.5 节。"
    ),
    "receptor": (
        "受体只能来自任务规约：上传文件 / PDB 编号 / UniProt / 基因或蛋白名。"
        "**系统没有默认受体**，也不要替用户挑靶点；没给就让用户补。"
    ),
}


@tool
def tool_guide(topic: str = "") -> str:
    """查工具用法与约束（不确定就先查，不要凭记忆猜）。

    topic 留空 = 目录（可用主题）；给出主题（docking / report / choices / parameters / receptor）
    返回该主题的用法、参数、会被拒绝的情况与产出位置。
    """
    key = str(topic or "").strip().lower()
    if not key:
        return ("可用主题：" + "、".join(sorted(TOOL_GUIDE))
                + "。每个主题说明参数、前置条件、被拒绝的情形与产出位置。")
    if key in TOOL_GUIDE:
        return TOOL_GUIDE[key]
    for name, text in TOOL_GUIDE.items():      # 关键词模糊命中（如 "对接" / "报告"）
        if key in name or key in text.lower():
            return text
    return f"没有主题「{topic}」。可用主题：" + "、".join(sorted(TOOL_GUIDE))


TOOL_GUIDE_TOOL: Any = tool_guide
