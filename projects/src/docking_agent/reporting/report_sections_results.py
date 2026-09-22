"""报告骨架的后半部分：第 3–9 节（推荐分子、理化性质、结合模式、方法与局限、失败与跳过、结论、产物）。

与 `report_sections_setup.py` 一样：每个 `section_*` 函数是「上下文 → 行列表」，
由 `report.py:build_markdown_report()` 按固定顺序调用。
"""
from __future__ import annotations

from typing import Any, Dict, List

from docking_agent.reporting.content import (
    CONSISTENCY_ZH,
    _artifact_size,
    _demote_headings,
    _num,
    _pct,
    _strip_urls,
    extract_agent_conclusions,
)
from docking_agent.reporting.fields import REPORT_FIELD_LABELS
from docking_agent.reporting.recommend import WEIGHT_LABELS
from docking_agent.reporting.report_context import (
    ReportContext,
    _brief,
    _ph_policy_text,
    _pocket_section_lines,
)


def _section_3_1_recommendations(ctx: ReportContext) -> List[str]:
    """3.1 推荐化合物排行（对接亲和力 × 理化性质 / 类药性 综合分）。"""
    L: List[str] = []
    rec = ctx.rec
    L.append("### 3.1 推荐化合物排行（对接亲和力 × 理化性质 / 类药性）")
    L.append("")
    pose_by_name = ctx.pose_by_name
    if not rec.get("rows"):
        L.append("本次没有可用于综合排行的分子（没有分子获得有效对接分数），"
                 "因此不给出推荐排行；失败明细见第 7 节。")
        L.append("")
        return L
    w = rec["weights"]
    crit = rec["criteria"]
    L.append("综合分 = 四个分量按权重加权；权重与满分口径如下（绝对尺度，不随本批分子库归一化，"
             "因此不同运行之间可比）：")
    L.append("")
    L += ctx.table_block(
        "推荐综合分的分量与权重",
        ["分量", "权重", "满分口径（该分量记为 1.0）"],
        [[f"{WEIGHT_LABELS[k]}（`{k}`）", f"{w[k]:.2f}",
          {
              "affinity": f"对接亲和力 ≤ −{crit['affinity_full_kcal']:g} kcal/mol",
              "ligand_efficiency": f"配体效率 LE = −ΔG/重原子数 ≥ {crit['le_full']:g} kcal·mol⁻¹·重原子⁻¹",
              "drug_likeness": f"Lipinski 违例 0 条（每条扣 {crit['lipinski_step']:g}）",
              "physchem": (f"logP ∈ [{crit['logp_window'][0]:g}, {crit['logp_window'][1]:g}] 且 "
                           f"TPSA ≤ {crit['tpsa_full']:g} Å²（超出线性衰减）"),
          }[k]]
         for k in ("affinity", "ligand_efficiency", "drug_likeness", "physchem")],
        aligns=["l", "r", "l"])
    L.append(f"> 等级：综合分 ≥ {crit['grade_a']:g} 为 **A**、≥ {crit['grade_b']:g} 为 **B**、"
             f"其余为 **C**；亲和力弱于 {crit['affinity_gate_kcal']:g} kcal/mol 者等级封顶为 C。"
             "缺数据的分量按 0 计入，表中以 `—` 标注。")
    L.append("")
    # 排行表只放「决定名次」的 6 列：宽表在 PDF 里会被降级成逐行竖排（8 列以上），
    # 反而不整洁；其余逐分子指标（分子量/logP/TPSA/违例/相似度）在下面的结构卡里，
    # 完整字段仍在 ranking.csv / 分子详情页。
    # 协调 Agent 按用户要求附加的列（customize_report → extra_columns）：只加真实有值的字段，
    # 列名来自共享字段表，取数仍是本次真实计算结果 —— 骨架不变，内容能按用户要求变化。
    extra_columns = ctx.extra_columns
    extra_headers = [REPORT_FIELD_LABELS.get(c, c) for c in extra_columns]

    def _extra_cells(row: Dict[str, Any]) -> List[str]:
        out = []
        for col in extra_columns:
            value = row.get(col)
            if isinstance(value, bool):
                out.append("是" if value else "否")
            elif isinstance(value, float):
                out.append(_num(value))
            elif value in (None, ""):
                out.append("—")
            else:
                out.append(str(value))
        return out

    rank_rows = []
    for r in rec["rows"]:
        rank_rows.append([
            r.get("rank"), r.get("name") or r.get("smiles"),
            f"{r['composite']:.3f}" if isinstance(r.get("composite"), (int, float)) else "—",
            r.get("grade") or "—",
            _num(r.get("affinity_kcal_mol")),
            f"{r['ligand_efficiency']:.3f}" if isinstance(r.get("ligand_efficiency"), (int, float)) else "—",
        ] + _extra_cells(r))
    L += ctx.table_block(
        f"推荐化合物排行（前 {len(rec['rows'])} 名，综合分降序）",
        ["#", "分子", "综合分", "等级", "亲和力 (kcal/mol)", "配体效率 LE"] + extra_headers,
        rank_rows, aligns=["r", "l", "r", "c", "r", "r"] + ["l"] * len(extra_headers))
    L.append("> 逐分子的分子量 / logP / TPSA / Lipinski 违例 / 与对照相似度见下方结构卡与 "
             "`ranking.csv`。")
    L.append("> 顺序 = 先按等级（A → B → C），同级内按综合分降序；等级经门槛/封顶修正，"
             "与综合分不一致时以等级为准。")
    for note in (rec.get("notes") or [])[:2]:
        L.append(f"> {_brief(note, 90)}")
    L.append("")
    # 逐分子卡片：**左边 2D 结构、右边指标表**（同一张图，网页/PDF/离线包一致），
    # 下面再跟两行精简过的理由与建议。完整理由在产物与结果 JSON 里。
    L.append("#### 逐个分子：2D 结构 + 关键指标")
    L.append("")
    reasons_txt = 0
    for index, r in enumerate(rec["rows"], start=1):
        name = r.get("name") or r.get("smiles")
        # 标题只写名次与名字：综合分/等级已经在卡片图里，重复两遍反而乱
        L.append(f"##### #{r.get('rank') or index} {name}")
        L.append("")
        L += ctx.figure_by_rel(f"charts/recommend_card_{index:02d}.png",
                               f"#{r.get('rank') or index} {name}：2D 结构 + 关键指标")
        if r.get("agent_reason"):
            reasons_txt += 1
            L.append(f"- **推荐理由**：{_brief(_strip_urls(str(r['agent_reason'])), 120)}")
        if r.get("agent_suggestion"):
            L.append(f"- **推进建议**：{_brief(_strip_urls(str(r['agent_suggestion'])), 120)}")
        pose_info = pose_by_name.get(str(r.get("name") or ""))
        if pose_info:
            summary = pose_info.get("summary") or {}
            counts = summary.get("type_counts") or {}
            kinds = "、".join(f"{k}×{v}" for k, v in counts.items()) or "无达标接触"
            L.append(f"- **姿态–口袋**：与 {summary.get('contact_residues', 0)} 个残基接触"
                     f"（{kinds}），明细见第 5.2 节。")
        for tip in (r.get("suggestions") or [])[:1]:
            L.append(f"- **说明**：{_brief(tip, 80)}")
        L.append("")
    if not reasons_txt:
        L.append("> 本次未提交逐分子推荐理由，排行与建议来自系统的确定性计算。")
        L.append("")
    if rec.get("unmatched_reasons"):
        L.append(f"> 有 {len(rec['unmatched_reasons'])} 条 Agent 理由未匹配到本次排行的分子，"
                 "已按原样回传、未写入报告（避免把不存在或不在榜的分子混进结论）。")
        L.append("")
    L.append("#### 筛选建议（整体）")
    L.append("")
    g = rec["grades"]
    L.append(f"- 等级分布：**A 级 {g.get('A', 0)} 个 / B 级 {g.get('B', 0)} 个 / "
             f"C 级 {g.get('C', 0)} 个**（共 {rec['scored_total']} 个有有效对接分数的分子）。")
    if rec.get("excluded_total"):
        L.append(f"- 另有 **{rec['excluded_total']} 个分子没有有效对接分数**，未进入排行"
                 "（明细见第 7 节与排序 CSV 的 `error` 列）——评估覆盖率时需计入。")
    L.append("- 推进顺序：先做 A 级中「亲和力 ≤ −8 kcal/mol 且 Lipinski 违例 ≤ 1」的分子"
             "（必要时提高 exhaustiveness 复算），B 级备选，C 级仅在无更好选择或作阴性参考时使用。")
    if rec.get("rows") and any(r.get("box_group") == "large" for r in rec["rows"]):
        L.append("- 排行中含使用**大配体专用盒**的分子（见第 2.2 节）：其分数与主组不可直接比较，"
                 "跨组比较前请先统一盒子重跑。")
    L.append("")
    return L


def _section_3_2_beats_control(ctx: ReportContext) -> List[str]:
    """3.2 优于阳性对照的分子（与综合分排行互补，不是同一件事）。"""
    L: List[str] = ["### 3.2 优于阳性对照的分子", ""]
    if not ctx.pc:
        L.append("未提供阳性对照，无法做以对照为基准的富集判断；可参考第 2 节排序，"
                 "或提供阳性对照 SMILES 后重跑。")
    elif ctx.hits:
        L.append("以下分子在本次流程条件下打分优于阳性对照（`Δ < 0`）：")
        L.append("")
        for m in ctx.hits[:20]:
            L.append(f"- **{m.get('name') or m.get('smiles')}**：{_num(m.get('affinity_kcal_mol'))} kcal/mol"
                     f"（Δ = {_num(round(m['affinity_kcal_mol'] - ctx.pc_aff, 2))} kcal/mol）；"
                     f"分子量 {_num(m.get('molecular_weight'), 2)} Da，logP {_num(m.get('logP'))}，"
                     f"TPSA {_num(m.get('tpsa'))} Å²，"
                     f"类药性{'通过' if m.get('drug_likeness_pass') else '不通过'}，"
                     f"与对照相似度 {_num(m.get('similarity_to_positive_control'), 3)}")
        L.append("")
        L.append(f"> 共 {len(ctx.hits)} 个分子优于阳性对照（占成功对接分子的 "
                 f"{_pct(len(ctx.hits), len(ctx.full_ranking))}）；为打分层面的相对结论，"
                 "需实验验证（见第 6 节）。")
    else:
        L.append("本次没有分子在打分上优于阳性对照。可考虑扩大分子库、调整位点/盒子或提高搜索"
                 "强度后重试；该结论不排除这些分子具有实验活性。")
    L.append("")
    return L


def section_3_recommendations(ctx: ReportContext) -> List[str]:
    """第 3 节：推荐分子（3.1 综合排行 / 3.2 优于对照）。"""
    L: List[str] = ["## 3. 推荐分子", ""]
    L += _section_3_1_recommendations(ctx)
    L += _section_3_2_beats_control(ctx)
    return L


def section_4_properties(ctx: ReportContext) -> List[str]:
    """第 4 节：理化性质（RDKit 计算）。"""
    L: List[str] = ["## 4. 理化性质", ""]
    prot = ctx.prot
    if prot["policy"]:
        policy_note = (_ph_policy_text(prot) if prot["policy"] == "ph"
                       else prot["policy"])
        L.append(f"> 理化性质按与对接相同的质子化态计算（策略 `{prot['policy']}`＝{policy_note}；"
                 f"{len(prot['applied'])} 个分子被调整）。被调整分子在产物里同时保留 `smiles`"
                 "（原始输入，主键）与 `protonated_smiles`（实际计算形式）。")
        L.append("")
    L += ctx.table_block(
        "候选分子理化性质（RDKit 计算）",
        ["分子", "分子式", "分子量 (Da)", "logP", "TPSA (Å²)", "HBD", "HBA", "可旋转键", "芳香环",
         "Lipinski 违例"],
        [[m.get("name") or m.get("smiles"), m.get("formula"), _num(m.get("molecular_weight"), 2),
          _num(m.get("logP"), 2), _num(m.get("tpsa"), 2), m.get("hbd"), m.get("hba"),
          m.get("rotatable_bonds"), m.get("aromatic_rings"), m.get("lipinski_violations")]
         for m in ctx.ranking],
        aligns=["l", "l", "r", "r", "r", "r", "r", "r", "r", "r"])
    L += ctx.figure_block("property_chart", "理化性质空间（横轴分子量，纵轴 logP，点大小表示对接强度）")
    return L


def _section_5_2_poses(ctx: ReportContext) -> List[str]:
    """5.2 推荐分子的姿态–口袋相互作用（2D / 3D）。"""
    L: List[str] = ["### 5.2 推荐分子的姿态–口袋相互作用（2D / 3D）", ""]
    pose_rows = ctx.result.get("pose_analysis") or []
    if ctx.result.get("pose_analysis_note"):
        L.append(f"> {ctx.result['pose_analysis_note']}")
        L.append("")
    if not pose_rows:
        if not ctx.result.get("pose_analysis_note"):
            L.append("没有可分析的位姿（未保存位姿或全部对接失败），本节不给出结合分析。")
            L.append("")
        return L
    L.append("> 判定口径（几何启发式，基于真实坐标）：氢键 = 配体 N/O 与受体 N/O ≤ 3.5 Å；"
             "盐桥 = 双方部分电荷 |q| ≥ 0.3 且符号相反、距离 ≤ 4.0 Å；疏水接触 = 双方碳 ≤ 4.2 Å；"
             "π–π 堆叠 = 芳香碳对数 ≥ 3 且距离 ≤ 5.5 Å；金属配位 = 金属离子与配体 N/O ≤ 3.0 Å。"
             "未做氢键角度校正与能量分解，不替代 MD/MM-GBSA（见第 6 节）。")
    L.append("")
    for item in pose_rows:
        summary = item.get("summary") or {}
        counts = summary.get("type_counts") or {}
        detail = "、".join(f"{k}×{v}" for k, v in counts.items()) or "无达标接触"
        L.append(f"**#{item.get('rank')} {item.get('name')}**"
                 f"（{_num(item.get('affinity_kcal_mol'))} kcal/mol；位姿文件 "
                 f"`{item.get('pose_file') or '—'}`）：与 **{summary.get('contact_residues', 0)}** "
                 f"个受体残基有接触（{detail}）。")
        L.append("")
        residues = item.get("residues") or []
        if residues:
            L.append("| 残基 | 相互作用 | 最近距离 (Å) | 接触原子对（示例） |")
            L.append("|:---|:---|---:|:---|")
            for residue in residues[:10]:
                examples = "；".join(
                    f"{d.get('ligand_atom')}–{d.get('receptor_atom')}"
                    + (f" {d['distance']:.2f} Å" if isinstance(d.get("distance"), (int, float)) else "")
                    for d in (residue.get("detail") or [])[:2])
                L.append(f"| {residue.get('residue')} | {'/'.join(residue.get('types') or [])} | "
                         f"{_num(residue.get('min_distance'), 2) if residue.get('min_distance') is not None else '—'} | "
                         f"{examples or '—'} |")
            L.append("")
        if item.get("figure_2d"):
            L += ctx.figure_by_rel(f"charts/{item['figure_2d']}.png",
                                   f"#{item.get('rank')} {item.get('name')} 的 2D 相互作用图")
        if item.get("figure_3d"):
            L += ctx.figure_by_rel(f"charts/{item['figure_3d']}.png",
                                   f"#{item.get('rank')} {item.get('name')} 的 3D 结合姿态（含对接盒）")
        for warning in (item.get("warnings") or [])[:2]:
            L.append(f"> 说明：{warning}")
            L.append("")
    L.append("> 逐原子接触明细见产物 `docking.json` 的 `pose_analysis`；2D/3D 图为本次运行产物。")
    L.append("")
    return L


def _section_5_3_binding_rows(ctx: ReportContext) -> List[str]:
    """5.2 后半：与阳性对照的结合模式比较（有对照数据才输出）。"""
    L: List[str] = []
    if not ctx.binding_rows:
        L.append("未提供阳性对照或未执行结合模式分析，无对照比较数据。")
        L.append("")
        return L
    L.append("> 方法学：Morgan(radius=2, 2048 bit) 与 MACCS 双指纹 Tanimoto 相似度 + SMARTS "
             "药效团锚定基团匹配 + 理化性质差异；结构一致性由双指纹综合判定。"
             "相似度高仅说明结构相近，不等同于结合模式一致。")
    L.append("")
    L += ctx.figure_block("similarity_chart", "与阳性对照的指纹相似度（Morgan Tanimoto）")
    L += ctx.figure_block("binding_scatter", "结合模式：相似度 vs 亲和力（含对照亲和力参考线）")
    L += ctx.figure_block("structure_grid", "候选分子结构对比（含阳性对照，按亲和力排序）")
    L += ctx.table_block(
        "结合模式与阳性对照比较",
        ["分子", "Morgan 相似度", "MACCS 相似度", "结构一致性", "锚定基团匹配", "结合模式提示"],
        [[m.get("name") or m.get("smiles"),
          _num(ctx.binding_row(m).get("morgan_tanimoto",
                                   ctx.binding_row(m).get("similarity_to_positive_control")), 3),
          _num(ctx.binding_row(m).get("maccs_tanimoto"), 3),
          CONSISTENCY_ZH.get(str(ctx.binding_row(m).get("structural_consistency")),
                             ctx.binding_row(m).get("structural_consistency") or "—"),
          "是" if ctx.binding_row(m).get("anchor_match") else
          ("否" if ctx.binding_row(m).get("anchor_match") is False else "—"),
          ctx.binding_row(m).get("binding_mode_hint", "—")]
         for m in ctx.ranking],
        aligns=["l", "r", "r", "c", "c", "l"])
    return L


def section_5_binding(ctx: ReportContext) -> List[str]:
    """第 5 节：结合模式与阳性对照比较（5.1 口袋 / 5.2 姿态 / 对照比较）。"""
    L: List[str] = ["## 5. 结合模式与阳性对照比较", ""]
    L += _pocket_section_lines(ctx.result, ctx.table_block, ctx.figure_block, ctx.prot)
    L += _section_5_2_poses(ctx)
    L += _section_5_3_binding_rows(ctx)
    return L


def section_6_methods(ctx: ReportContext) -> List[str]:
    """第 6 节：方法与局限（能回答什么、不能回答什么）。"""
    L: List[str] = ["## 6. 方法与局限", ""]
    prot = ctx.prot
    exh = ctx.exh_val if ctx.exh_val is not None else "—"
    L.append(f"**（1）打分函数**：AutoDock Vina 经验打分（exhaustiveness={exh}，"
             f"seed={ctx.seed_value}）。分数用于同一流程内的相对排序，不是结合自由能，"
             "不能换算 Ki/Kd/IC50；取单点最优构象，不含熵贡献。")
    L.append("")
    if ctx.large_rows:
        L.append(f"**（2）可比性**：主组共用同一对接盒；`large` 组（{len(ctx.large_rows)} 个）以同一"
                 "中心、更大盒子单独重跑，两组分数不可跨组比较。实测同一配体仅改变盒边长"
                 "（18/22/28/34 Å）分数最大相差 1.34 kcal/mol 且非单调，因此不做「每分子自适应盒子」。")
    else:
        L.append("**（2）可比性**：全部候选共用同一对接盒，排序不受盒子差异影响。实测同一配体仅改变"
                 "盒边长（18/22/28/34 Å）分数最大相差 1.34 kcal/mol 且非单调，因此不做"
                 "「每分子自适应盒子」。")
    L.append("")
    L.append("**（3）搜索强度与盒子**：exhaustiveness 越低，构象采样越不充分，分数不确定度越大；"
             "提高强度可降低采样噪声，但不能修正打分函数的系统偏差。盒子由口袋范围与库级下限共同"
             "确定并全局统一。")
    L.append("")
    L.append("**（4）未做的验证**：未做重打分、共识打分、MM/GBSA、分子动力学与多引擎交叉打分；"
             "如需更高置信度，建议对优选分子补充上述计算。")
    L.append("")
    if prot["policy"]:
        engines = prot.get("engine_counts") or {}
        pro_engines = {k: v for k, v in engines.items() if not k.startswith("内置")}
        if pro_engines:
            name = sorted(pro_engines, key=lambda k: -pro_engines[k])[0]
            version = (prot.get("engine_versions") or {}).get(name)
            ph_suffix = f"由专业 pKa 引擎 {name}{(' ' + version) if version else ''} 判定"
        else:
            ph_suffix = "用内置 pKa 规则表分配"
        policy_zh = {"neutralize": "中和带净电荷的分子", "keep": "保持输入形式",
                     "ph": _ph_policy_text(prot, suffix=ph_suffix)}.get(
            prot["policy"], prot["policy"])
        if prot["policy"] == "ph" and pro_engines:
            ph_note = ("未做微观态布居与打分对比：同一分子在 pH 窗口内存在多个微观态时只取一个"
                       "形式对接（选定规则见产物溯源）；金属配位、互变异构与显式水/辅因子效应"
                       "不在本次范围。")
        elif prot["policy"] == "ph":
            ph_note = ("pH 处理为官能团 pKa 规则近似，不校正酰胺/杂环等特殊环境的 pKa 偏移，"
                       "也未做微观态分布采样；装好专业 pKa 引擎后重跑可改为 pKa 预测口径，"
                       "或用专业工具生成质子化态以 SDF 提供并选择 `keep`。")
        else:
            ph_note = "如需按目标 pH 处理离子态，把质子化态策略改为 `ph` 后重跑。"
        L.append(f"**（5）质子化态**：按运行级策略 `{prot['policy']}`（{policy_zh}）统一处理，"
                 f"本次 {len(prot['applied'])} 个分子被调整。{ph_note}")
    else:
        L.append("**（5）质子化态**：分子按输入 SMILES 的默认质子化态与给定立体化学对接，"
                 "未枚举生理 pH 下的微观态、互变异构与金属配位；输入缺少立体信息时，"
                 "配体准备阶段会在运行笔记中给出告警。")
    L.append("")
    receptor_state = ("去水、保持刚性（未做柔性受体/诱导契合）；"
                      + ("质子化态按**目标 pH 重算**（与配体同一口径，见 1.2）；"
                         if any((b.get("receptor_protonation") or {}).get("applied")
                                for b in (ctx.result.get("receptors") or []))
                         else "质子化态未按目标 pH 重算（来自受体文件/模板默认态，见 1.2）；")
                      + "辅因子处理以 1.1 节运行笔记为准，未做「保留 vs 去除辅因子」的对照实验。")
    L.append(f"**（6）受体状态**：{receptor_state}")
    L.append("")
    stats = ctx.stats
    if stats["failed"] or stats["skipped"]:
        L.append(f"**（7）覆盖范围**：输入 {stats['total']} 个分子，成功对接 {stats['success']} 个，"
                 f"失败/跳过 {stats['failed'] + stats['skipped']} 个；第 2–5 节与结论只覆盖成功对接的"
                 "分子，失败分子不代表无活性（见第 7 节）。")
    else:
        L.append(f"**（7）覆盖范围**：输入 {stats['total']} 个分子全部成功对接，第 2–5 节与结论"
                 "覆盖全部输入分子。")
    L.append("")
    L.append("**（8）结论口径**：本报告给出的是本流程条件下的相对优先级；排序靠前不等于实验有效，"
             "推进决策需结合实验验证。")
    L.append("")
    return L


def section_7_failures(ctx: ReportContext) -> List[str]:
    """第 7 节：失败与跳过（按原因分组）。"""
    L: List[str] = ["## 7. 失败与跳过", ""]
    stats = ctx.stats
    if not stats["failed"] and not stats["skipped"]:
        L.append(f"输入 {stats['total']} 个分子全部成功对接，无失败或跳过；"
                 "第 2–3 节的排序与推荐覆盖全部输入分子。")
        L.append("")
        return L
    L.append("> 按原因分组列出未获得有效分数的分子。第 2–5 节的排序、推荐与结论只覆盖成功对接"
             "的分子；失败/跳过分子未参与排序与命中判断，也不代表它们没有活性。")
    L.append("")
    fail_total = stats["failed"] + stats["skipped"]
    L.append(f"- 输入分子：{stats['total']} 个")
    L.append(f"- 成功对接：{stats['success']} 个（{_pct(stats['success'], stats['total'])}）")
    L.append(f"- 失败 / 跳过：{fail_total} 个（{_pct(fail_total, stats['total'])}）")
    if stats["skipped"]:
        L.append(f"- 其中未进入对接（解析/准备阶段跳过）：{stats['skipped']} 个")
    L.append("")
    L += ctx.table_block(
        "失败与跳过清单（按原因分组）",
        ["原因", "分子数", "占比", "示例分子"],
        [[g["reason"], g["count"], _pct(g["count"], stats["total"]),
          "、".join(g["names"][:3]) + ("…" if len(g["names"]) > 3 else "")]
         for g in stats["groups"]],
        aligns=["l", "r", "r", "l"])
    L.append("> 逐分子错误信息（`status` / `error`）见产物 `docking.json` 与 `ranking.csv`。")
    L.append("")
    return L


def section_8_conclusions(ctx: ReportContext) -> List[str]:
    """第 8 节：结论与建议（协调 Agent 有结论则用其结论，否则给确定性建议）。"""
    L: List[str] = ["## 8. 结论与建议", ""]
    L.append("> 结论限定在本流程条件下（见第 6 节），用于候选优先级排序，需经实验验证。")
    L.append("")
    # 只取协调 Agent 的**结论/建议**类小节：整段报告贴进来会让同一批数字出现两遍
    # （第 1–7 节已用真实表格写过），用户明确要求"报告中不要重复内容"。
    narrative = extract_agent_conclusions(ctx.agent_narrative)
    if narrative:
        L.append("> 以下为协调 Agent 的结论/建议（数据与参数见第 1–7 节；完整原文见产物 "
                 "`agent_report.md`）。")
        L.append("")
        L.append(_strip_urls(_demote_headings(narrative)))
    else:
        if ctx.hits:
            best = ctx.hits[0]
            best_delta = round(best["affinity_kcal_mol"] - ctx.pc_aff, 2)
            L.append(f"1. **优先推进 {best.get('name') or best.get('smiles')}**：在本流程条件下，"
                     f"其对接亲和力 {_num(best.get('affinity_kcal_mol'))} kcal/mol，"
                     f"较阳性对照强 {_num(abs(best_delta))} kcal/mol"
                     f"（Δ = {_num(best_delta)} kcal/mol）。")
        L.append("2. 结合类药性、与对照的结合模式相似度与锚定基团匹配综合选择；"
                 "排序靠前但相似度低、类药性差者应谨慎。")
        L.append("3. 如需进一步验证，可对优选分子提高 exhaustiveness / n_poses 复算，"
                 "或引入其他受体/引擎交叉筛选（本报告未做共识打分，见第 6 节）。")
        if ctx.stats["failed"] or ctx.stats["skipped"]:
            L.append(f"4. 本次有 {ctx.stats['failed'] + ctx.stats['skipped']} 个分子未获得有效分数"
                     "（见第 7 节），在评估覆盖度时需计入，必要时修正输入或重跑。")
    L.append("")
    return L


def section_9_artifacts(ctx: ReportContext) -> List[str]:
    """第 9 节：数据与产物清单 + 固定尾部声明。"""
    L: List[str] = ["## 9. 数据与产物", ""]
    L.append("本次运行的全部中间数据已落盘，可在报告页「中间数据」页签单独下载或整包获取；"
             "报告内图片来自 `charts/` 下的同名 PNG。")
    L.append("")
    if ctx.artifacts:
        L += ctx.table_block(
            "本次运行产物清单",
            ["产物文件", "说明", "大小"],
            [[f"`{a.get('name')}`", a.get("label") or "—",
              _artifact_size(ctx.artifacts, str(a.get("name") or ""))]
             for a in ctx.artifacts],
            aligns=["l", "l", "r"])
    L.append("---")
    L.append("")
    L.append("*本报告数值均由真实计算产生（RDKit / AutoDock Vina），图表为本次运行产物；"
             "完整溯源见 `result.json` 与上表产物。*")
    return L


#: 报告骨架后半部分的章节函数（顺序即渲染顺序）
SECTIONS: Dict[str, Any] = {
    "recommendations": section_3_recommendations,
    "properties": section_4_properties,
    "binding": section_5_binding,
    "methods": section_6_methods,
    "failures": section_7_failures,
    "conclusions": section_8_conclusions,
    "artifacts": section_9_artifacts,
}
