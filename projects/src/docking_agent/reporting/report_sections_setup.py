"""报告骨架的前半部分：抬头（表 1）、第 0 节「本次要求与响应」、第 1 节「任务与参数」、第 2 节「结果排序」。

每个 `section_*` 函数都是「上下文 → 行列表」，由 `report.py:build_markdown_report()`
**按固定顺序**调用；图/表编号计数器挂在上下文上，因此跨章节仍连续。
"""
from __future__ import annotations

from typing import Any, Dict, List

from docking_agent.core.protonation import PKA_TABLE_VERSION
from docking_agent.reporting.content import (
    _AUTHORITY_ZH,
    _DECISION_ZH,
    _LIGAND_SOURCE_ZH,
    _RECEPTOR_SOURCE_ZH,
    _SITE_SOURCE_ZH,
    _SOURCE_ZH,
    _box_size_text,
    _num,
    tool_versions_markdown as _tool_versions_markdown,
)
from docking_agent.reporting.fields import REPORT_FIELD_LABELS
from docking_agent.reporting.report_context import (
    _ROLE_ZH,
    ReportContext,
    _brief,
    _cocrystal_control_line,
    _ph_policy_text,
    _receptor_provenance_text,
    _receptor_protonation_lines,
)


def section_header(ctx: ReportContext) -> List[str]:
    """抬头信息（表 1）：标题 + 运行元数据 + 各 Agent 模型。"""
    L: List[str] = []
    L.append("# " + (str(ctx.custom.get("title") or "").strip() or "分子对接筛选报告"))
    L.append("")
    if ctx.custom.get("title"):
        L.append("")
    center_txt, size_txt = ctx.center_txt, ctx.size_txt
    # 抬头表只放摘要：盒子来源与库级下限的完整说明在第 1.2 节，避免 2 列表格被压到截断
    box_cell = f"中心 {center_txt} Å；尺寸 {size_txt}"
    if ctx.spec:
        meta_spec_cell = (f"`{ctx.spec.get('task_type')}` · 权威={ctx.spec.get('authority')} · "
                          f"决策=`{ctx.spec.get('decision')}` · 来源={ctx.spec.get('source', 'rules')}")
    else:
        meta_spec_cell = "表单直接提交（无 `task_spec`）"
    meta_rows: List[List[Any]] = [
        ["运行编号", f"`{ctx.run_id or '—'}`"],
        ["运行模式", ctx.mode_zh],
        ["生成时间", ctx.created_at or "—"],
        ["受体", ctx.receptor_label or "—"],
        ["对接盒", box_cell],
        ["对接引擎", str((ctx.ranking[0].get("engine") if ctx.ranking else "") or "—")],
        ["搜索强度", str((ctx.ranking[0].get("exhaustiveness") if ctx.ranking else "") or "—")],
        ["随机种子", f"`seed={ctx.seed_value}` / `seed_policy={ctx.seed_policy}`"],
        ["候选分子数", len(ctx.molecules)],
        ["阳性对照", (f"{ctx.pc.get('name') or '—'}（{_num(ctx.pc_aff)} kcal/mol）")
         if ctx.pc else "未提供"],
        ["受体共晶配体", _cocrystal_control_line(ctx.result)],
        ["任务受理", meta_spec_cell],
    ]
    models_cell = ctx.models_cell()
    if models_cell:
        meta_rows.append(["各 Agent 模型", models_cell])
    L += ctx.table_block("报告信息", ["项目", "内容"], meta_rows, aligns=["l", "l"])
    return L


def section_0_requirements(ctx: ReportContext) -> List[str]:
    """第 0 节：协调 Agent 按本次要求组织的部分（骨架不变，内容按用户要求变化）。"""
    L: List[str] = []
    custom = ctx.custom
    requirements = [r for r in (custom.get("requirements") or []) if isinstance(r, dict)]
    highlights = [str(h) for h in (custom.get("highlights") or []) if str(h).strip()]
    if requirements or highlights or custom.get("notes") or ctx.requested_columns or ctx.agent_models:
        L.append("## 0. 本次要求与响应（协调 Agent）")
        L.append("")
        L.append("")
        if requirements:
            L += ctx.table_block(
                "用户要求 → 实际处理",
                ["用户要求", "实际处理（含真实数据）"],
                [[_brief(r.get("ask"), 120) or "—", _brief(r.get("response"), 220) or "—"]
                 for r in requirements],
                aligns=["l", "l"])
        if ctx.requested_columns:
            labels = "、".join(REPORT_FIELD_LABELS.get(c, c) for c in ctx.requested_columns)
            L.append(f"- **按用户要求附加的列**：{labels}（见第 3.1 节排行表与排序 CSV）。")
        if highlights:
            L.append("- **协调 Agent 本次要点**：")
            for h in highlights:
                L.append(f"  - {_brief(h, 200)}")
        if custom.get("notes"):
            L.append("")
            L.append(_brief(str(custom["notes"]), 600))
        if ctx.agent_models:
            rows = []
            for role, info in sorted((ctx.agent_models or {}).items()):
                if not isinstance(info, dict):
                    continue
                rows.append([_ROLE_ZH.get(role, role),
                             str(info.get("actual_model") or info.get("model") or "—"),
                             str(info.get("calls") if info.get("calls") is not None else "—")])
            if rows:
                L.append("")
                L += ctx.table_block("各角色模型调用统计",
                                     ["角色", "实际模型", "调用次数"], rows, aligns=["l", "l", "r"])
        L.append("")
    return L


#: 运行笔记去重用的「可变片段」：受体 key/哈希、路径、括号里的数字都不该影响「是不是同一件事」
_NOTE_NOISE_RE = None


def _dedupe_notes(notes: List[Any]) -> List[str]:
    """按签名去重运行笔记（同一件事在多个受体准备里会重复出现）。

    签名 = 去掉受体标识（`7YHP_f7f8da9b58da39a3_ph7.4` 这类）后的文本。
    先到先得，保持原始顺序；只去重、不改写内容。
    """
    global _NOTE_NOISE_RE
    if _NOTE_NOISE_RE is None:
        import re as _re

        _NOTE_NOISE_RE = _re.compile(r"\b[0-9A-Za-z_]{6,}\b")
    seen = set()
    out: List[str] = []
    for note in notes:
        text = str(note or "").strip()
        if not text:
            continue
        signature = _NOTE_NOISE_RE.sub("#", text)
        if signature in seen:
            continue
        seen.add(signature)
        out.append(text)
    return out


def _section_1_1_task_source(ctx: ReportContext) -> List[str]:
    """1.1 任务来源与受理（含运行笔记）。"""
    L: List[str] = ["### 1.1 任务来源与受理", ""]
    spec = ctx.spec
    if spec:
        ligand_src = ((spec.get("ligands") or {}).get("source")
                      if isinstance(spec.get("ligands"), dict) else "")
        rec_src = ((spec.get("receptor") or {}).get("source")
                   if isinstance(spec.get("receptor"), dict) else "")
        site_src = ((spec.get("site") or {}).get("source")
                    if isinstance(spec.get("site"), dict) else "")
        spec_rows = [
            ["任务类型", str(spec.get("task_type") or "—")],
            ["受理权威", _AUTHORITY_ZH.get(str(spec.get("authority")), str(spec.get("authority") or "—"))],
            ["受理决策", _DECISION_ZH.get(str(spec.get("decision")), str(spec.get("decision") or "—"))],
            ["信息来源", _SOURCE_ZH.get(str(spec.get("source")), str(spec.get("source") or "—"))],
            ["配体来源", _LIGAND_SOURCE_ZH.get(str(ligand_src), str(ligand_src or "—"))],
            ["受体来源", _RECEPTOR_SOURCE_ZH.get(str(rec_src), str(rec_src or "—"))],
            ["位点来源", _SITE_SOURCE_ZH.get(str(site_src), str(site_src or "—"))],
        ]
        assumptions = [str(a) for a in (spec.get("assumptions") or []) if str(a).strip()]
        if assumptions:
            spec_rows.append(["关键假设", "；".join(assumptions[:3])])
        L += ctx.table_block("任务受理（来源与决策）", ["项目", "内容"], spec_rows, aligns=["l", "l"])
    else:
        L.append("本次由参数表单直接提交，未经过任务受理模块（无 `task_spec`）；"
                 "受体、位点、配体与参数均以表单/请求为准。")
        L.append("")
    if ctx.notes:
        # 报告只给一句话、最多 6 条：完整原文在 run.json / 结果 JSON 里。
        # 同一次运行往往有多个受体准备（重跑/多种子），同一件事会逐条重复 → 这里按签名去重。
        L.append("**运行笔记**（最多 6 条；完整记录见 `run.json` 的 `notes`）：")
        L.append("")
        for n in _dedupe_notes(ctx.notes)[:6]:
            L.append(f"- {_brief(n, 110)}")
        L.append("")
    else:
        L.append("- 无特别说明。")
        L.append("")
    return L


def _section_1_2_receptor_box(ctx: ReportContext) -> List[str]:
    """1.2 受体、位点与对接盒（含受体质子化与盒子一致性）。"""
    L: List[str] = ["### 1.2 受体、位点与对接盒", ""]
    large_rows = ctx.large_rows
    L.append(f"- 受体：{ctx.receptor_label or '—'}")
    provenance = _receptor_provenance_text(ctx.result)
    if provenance:
        L.append(f"- 受体来源：{provenance}")
    L.append(f"- 位点来源：{ctx.box_source()}")
    L.append(f"- 对接盒：中心 {ctx.center_txt} Å；尺寸 {ctx.size_txt}")
    L.append(f"- 库级下限：{ctx.floor_text()}")
    # 受体质子化：必须与配体同一目标 pH，且要如实写明"做到了没有"
    L += _receptor_protonation_lines(ctx.result, ctx.prot)
    if large_rows:
        L.append(f"- 盒子一致性：主组共用同一对接盒；另有 {len(large_rows)} 个分子因 3D 跨度超出主盒，"
                 "划入 `large` 组、以同一中心更大盒子单独重跑，两组分数不可跨组比较（见第 6 节）。")
    elif (ctx.result.get("docking") or {}).get("receptors"):
        L.append("- 盒子一致性：所有参与排序的分子共用同一对接盒，盒子效应对相对排序无影响。")
    L.append("")
    return L


def _section_1_3_params(ctx: ReportContext) -> List[str]:
    """1.3 对接参数（同一阶段内参数一致）。"""
    L: List[str] = ["### 1.3 对接参数（同一阶段内参数一致）", ""]
    plan = ctx.plan
    prot = ctx.prot
    param_rows = [
        ["对接引擎", f"`{ctx.engine_val}`（AutoDock Vina 打分函数）"],
        ["搜索强度 exhaustiveness",
         (f"主组 `{ctx.exh_val if ctx.exh_val is not None else '—'}`"
          + (f"；两阶段漏斗：粗筛 `{plan.get('coarse_exhaustiveness')}` → 精算 "
             f"`{plan.get('fine_exhaustiveness')}`（前 {plan.get('refine_top_n')} 个）"
             if ctx.two_stage else ""))],
        ["输出构象数 n_poses", str(ctx.nposes if ctx.nposes is not None else "—")],
        ["随机种子", f"`seed={ctx.seed_value}` / `seed_policy={ctx.seed_policy}`"],
        ["对接盒", f"中心 {ctx.center_txt} Å；尺寸 {ctx.size_txt}"],
    ]
    if ctx.concurrency:
        param_rows.append(["并发", str(ctx.concurrency)])
    if prot["policy"]:
        policy_zh = {"neutralize": "中和带净电荷的分子（中性分子不变）",
                     "keep": "保持输入形式（不改变质子化态）",
                     "ph": _ph_policy_text(prot),
                     }.get(prot["policy"], prot["policy"])
        detail = f"`{prot['policy']}`（{policy_zh}）"
        if prot["observed"]:
            detail += f"；{len(prot['applied'])}/{prot['observed']} 个分子的质子化态被调整"
            cannot_neutralize = max(0, prot["charged"] - len(prot["applied"]))
            if cannot_neutralize:
                detail += f"（另有 {cannot_neutralize} 个带净电荷但无法中和，如季铵等永久电荷）"
        if prot["applied"]:
            examples = "、".join(
                f"{a['name'] or a['smiles']}（{a['before']:+.0f}→{a['after']:+.0f}）"
                for a in prot["applied"][:5] if isinstance(a.get("before"), (int, float)))
            if examples:
                detail += f"；例如 {examples}"
        # 质子化引擎溯源：优先专业 pKa 引擎（Dimorphite-DL）；回退内置规则表时如实说明
        engines = prot.get("engine_counts") or {}
        engine_txt = ""
        if engines:
            parts = []
            for name, count in sorted(engines.items(), key=lambda kv: -kv[1])[:3]:
                version = (prot.get("engine_versions") or {}).get(name)
                parts.append(f"{name}{(' ' + version) if version else ''}（{count} 个分子）")
            engine_txt = "；质子化引擎：" + "、".join(parts)
        if prot["policy"] == "ph" and prot.get("rule_counts"):
            top_rules = "、".join(f"{k}×{v}" for k, v in sorted(
                prot["rule_counts"].items(), key=lambda kv: -kv[1])[:6])
            detail += (f"；命中的可电离官能团：{top_rules}")
            if engines.get("内置规则表（回退）"):
                detail += (f"（**内置 pKa 规则表 {PKA_TABLE_VERSION} 回退**："
                           "**规则近似，不是 pKa 预测**；装好专业引擎后重跑可获得 pKa 预测口径）")
        detail += engine_txt
        detail += ("；逐分子溯源见产物 `docking.json` 的 `ligand_facts.protonation` 与排序 CSV 的 "
                   "`charge_input`/`charge_used` 列（原始 SMILES 始终保留）")
        param_rows.append(["质子化态策略", detail])
    L += ctx.table_block("对接参数（同一阶段内一致）", ["参数", "取值"], param_rows, aligns=["l", "l"])
    if prot["policy"] and prot["applied"]:
        if prot["policy"] == "ph":
            engine_names = "、".join(sorted(k for k in (prot.get("engine_counts") or {})
                                            if not k.startswith("内置")))
            basis = (f"由专业 pKa 引擎 {engine_names} 按目标 pH 计算"
                     if engine_names else "由内置官能团 pKa 规则表近似分配")
            L.append(f"> **质子化态说明**：本次{basis}，理化性质与对接使用同一种化学形式；"
                     "被调整分子的分子量/电荷与原始输入不同，属有意的化学处理。"
                     f"若输入已是目标 pH 质子化态，把策略改为 `keep` 可避免二次处理"
                     "（逐分子溯源见 1.3 表）。")
        else:
            L.append("> **质子化态说明**：理化性质与对接使用同一种化学形式（运行级策略）；"
                     "被调整分子的分子量/电荷与原始输入不同，属有意的化学处理。")
    L.append("> 同一阶段内所有分子共用同一组参数（引擎、exhaustiveness、n_poses、seed、盒子），"
             "排序因此可比；排序 CSV 每行都带该阶段的参数留痕。")
    L.append("")
    return L


def _section_1_4_to_1_6_tools(ctx: ReportContext) -> List[str]:
    """1.4 工具与版本；1.5 参数自动规划；1.6 结合口袋预测。"""
    L: List[str] = ["### 1.4 工具与版本", ""]
    L += ctx.table_block("本次运行实际使用的工具与版本", ["工具", "版本"],
                         _tool_versions_markdown(), aligns=["l", "r"])
    L += ctx.param_plan_lines()
    L += ctx.pocket_table()
    return L


def section_1_task_and_params(ctx: ReportContext) -> List[str]:
    """第 1 节：任务与参数（1.1–1.6，顺序固定）。"""
    L: List[str] = ["## 1. 任务与参数", ""]
    L += _section_1_1_task_source(ctx)
    L += _section_1_2_receptor_box(ctx)
    L += _section_1_3_params(ctx)
    L += _section_1_4_to_1_6_tools(ctx)
    return L


def section_2_ranking(ctx: ReportContext) -> List[str]:
    """第 2 节：结果排序（2.1 主组 / 2.2 大配体组）。"""
    L: List[str] = ["## 2. 结果排序", ""]
    L.append("> 按 AutoDock Vina 亲和力升序（越负越强）。分数仅用于本次流程内的相对比较，"
             "不是自由能，不能换算实验活性。")
    L.append("")
    if isinstance(ctx.pc_aff, (int, float)):
        L.append(f"> **命中判据**：`Δ = 亲和力 − 阳性对照亲和力`，`Δ < 0` 记为优于对照；"
                 f"阳性对照亲和力 {_num(ctx.pc_aff)} kcal/mol。")
    else:
        L.append("> **命中判据**：未提供阳性对照，无基准，不判定「命中」；排序仅表示同批分子"
                 "在同一套参数下的相对打分。")
    L.append("")
    if ctx.large_rows:
        L.append(f"> **可比性说明**：`large` 组（{len(ctx.large_rows)} 个）因 3D 跨度超出主盒，"
                 "以同一中心更大盒子单独重跑；换盒会改变 Vina 分数，故该组不并入主表、不跨组比较"
                 "（见 2.2）。")
        L.append("")
    if ctx.truncated:
        L.append(f"> 共 {len(ctx.full_ranking)} 个成功对接的分子，下表列出前 {len(ctx.ranking)} 个；"
                 "完整排序见 `ranking.csv` / `ranking.json`（第 9 节）。")
        L.append("")

    L.append("### 2.1 主组排序")
    L.append("")
    # 输入文件里的附加信息（ID / CAS / 自定义字段）：只有**真的存在**才加列，
    # 避免给没有这些信息的库排出空列（用户要求"输出带上 ID/CAS"，没有就如实留空）。
    def _remark(m: Dict[str, Any]) -> str:
        fields = m.get("fields") or {}
        return "; ".join(f"{k}={v}" for k, v in fields.items()
                         if str(k).upper() not in ("ID", "CAS", "MOLENAME", "NAME", "名称"))

    show_id = any(str(m.get("id") or "").strip() and str(m.get("id")) != str(m.get("name"))
                  for m in ctx.ranking)
    show_cas = any(str(m.get("cas") or "").strip() for m in ctx.ranking)
    show_remark = any(_remark(m) for m in ctx.ranking)
    header = ["排名", "分子"]
    for label, shown in (("ID", show_id), ("CAS", show_cas), ("备注", show_remark)):
        if shown:
            header.append(label)
    header += ["亲和力 (kcal/mol)", "Δ vs 对照 (kcal/mol)", "引擎", "搜索强度",
               "分子量 (Da)", "logP", "TPSA (Å²)", "类药性", "相似度"]
    rows: List[List[Any]] = []
    for i, m in enumerate(ctx.ranking, 1):
        aff = m.get("affinity_kcal_mol")
        delta = (round(aff - ctx.pc_aff, 2) if isinstance(aff, (int, float))
                 and isinstance(ctx.pc_aff, (int, float)) else None)
        row = [i, m.get("name") or m.get("smiles")]
        for value, shown in ((m.get("id"), show_id), (m.get("cas"), show_cas),
                             (_remark(m), show_remark)):
            if shown:
                row.append(value or "—")
        rows.append(row + [_num(aff), _num(delta),
                     m.get("engine") or "—", m.get("exhaustiveness") or "—",
                     _num(m.get("molecular_weight"), 2), _num(m.get("logP"), 2),
                     _num(m.get("tpsa"), 2),
                     "通过" if m.get("drug_likeness_pass") else
                     ("违例" if m.get("lipinski_violations") else "—"),
                     _num(m.get("similarity_to_positive_control"), 3)])
    if ctx.pc:
        pad = ["" for label, shown in (("ID", show_id), ("CAS", show_cas), ("备注", show_remark))
               if shown]
        rows.append(["对照", ctx.pc.get("name") or "阳性对照"] + pad
                    + [_num(ctx.pc_aff), "0.00",
                       ctx.pc.get("engine") or "—", ctx.pc.get("exhaustiveness") or "—",
                       "—", "—", "—", "—", "1.000"])
    L += ctx.table_block("候选分子对接结果（主组，按亲和力升序；对照行不参与排名）",
                         header, rows,
                         aligns=["r", "l", "r", "r", "c", "r", "r", "r", "r", "c", "r"])
    L += ctx.figure_block("docking_chart", "主组对接亲和力对比（含阳性对照参考线）")
    L += ctx.figure_block("affinity_histogram", "亲和力分布（含均值 / 中位数 / 对照参考线）")

    if ctx.large_rows:
        L.append("### 2.2 大配体组（盒子不同，不跨组比较）")
        L.append("")
        L.append("> 这些分子的 3D 跨度超出主盒，以同一中心更大盒子单独重跑，分数与主组不可直接"
                 "比较（见第 6 节）；分组标记（`box_group` / `box_size`）保留在产物里。")
        L.append("")
        L += ctx.table_block(
            "大配体组结果（独立盒子）",
            ["分子", "亲和力 (kcal/mol)", "盒子", "分组说明"],
            [[m.get("name") or m.get("smiles"), _num(m.get("affinity_kcal_mol")),
              _box_size_text(m.get("box_size")),
              m.get("box_fit_warning") or "跨度超出主盒，独立重跑"]
             for m in ctx.large_rows],
            aligns=["l", "r", "l", "l"])
    return L


#: 第 2 波结构拆分后仍需从本模块可见的公开符号（供静态检查与文档核对）
SECTIONS: Dict[str, Any] = {
    "header": section_header,
    "requirements": section_0_requirements,
    "task_and_params": section_1_task_and_params,
    "ranking": section_2_ranking,
}
