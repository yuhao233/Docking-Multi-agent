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
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from docking_agent.config import env_int
from docking_agent.core.docking import POSITIVE_CONTROL_NAME
from docking_agent.core.ranking import sort_by_affinity, split_box_groups
from docking_agent.reporting.content import (
    CONSISTENCY_ZH,
    _AUTHORITY_ZH,
    _DECISION_ZH,
    _LIGAND_SOURCE_ZH,
    _RECEPTOR_SOURCE_ZH,
    _SITE_SOURCE_ZH,
    _SOURCE_ZH,
    _artifact_size,
    _box_size_text,
    _demote_headings,
    _num,
    _param_plan,
    _pct,
    _seed_info,
    _strip_urls,
    _table,
    _vec,
    extract_agent_conclusions,
    failure_stats as _failure_stats,
    protonation_summary,
    param_plan_lines as _param_plan_lines,
    tool_versions,
    tool_versions_markdown as _tool_versions_markdown,
)
from docking_agent.core.protonation import PKA_TABLE_VERSION
from docking_agent.reporting.recommend import WEIGHT_LABELS, build_recommendations
from docking_agent.reporting.tables import rank_molecules

# 图表产物名 → 本次运行目录内的相对路径（报告内嵌用相对路径；PDF 依此找回 PNG）。
# 这份映射是唯一事实源：`reporting/artifacts.py` 的 CHART_SPECS 直接复用它。
# 文件名与产物名一致（`charts/<name>.png`），网页报告页据此把相对路径解析成图片接口。
logger = logging.getLogger(__name__)

from docking_agent.reporting.fields import REPORT_FIELD_LABELS  # noqa: E402

CHART_FILES: Dict[str, str] = {
    "docking_chart": "charts/docking_chart.png",
    "similarity_chart": "charts/similarity_chart.png",
    "affinity_histogram": "charts/affinity_histogram.png",
    "property_chart": "charts/property_chart.png",
    "structure_grid": "charts/structure_grid.png",
    "binding_scatter": "charts/binding_scatter.png",
    # 文件名与产物名保持一致（见本字典上方约定）：网页/PDF 都按它反查图片接口
    "recommend_chart": "charts/recommend_chart.png",
}

# 仍从本模块导出：`artifacts.py` 与 `pdf.py` 通过 `report.CHART_FILES` / `report.tool_versions` 复用
__all__ = ["CHART_FILES", "build_markdown_report", "tool_versions"]


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

#: Agent 角色 → 中文名（抬头「各 Agent 模型」与第 0 节调度记录共用）
_ROLE_ZH: Dict[str, str] = {"coordinator": "协调", "property": "属性评估",
                            "docking": "对接执行", "binding": "结合模式",
                            "pocket": "口袋分析", "intake": "任务受理"}


def _brief(text: Any, limit: int = 110) -> str:
    """把「运行笔记 / 推荐理由」这类长文本压成一句话（报告要整洁，PDF 尤其不能整页备注）。

    规则：去掉行内加粗标记 → 只取第一个分句（；/。 分句）→ 超长再截断加省略号。
    完整原文始终保留在 run.json / 结果 JSON 与产物里，报告只呈现核心。
    """
    import re as _re

    value = str(text or "").strip()
    if not value:
        return ""
    value = _re.sub(r"\*\*(.+?)\*\*", r"\1", value)
    value = _re.sub(r"\s+", " ", value)
    if len(value) > limit:
        parts = _re.split(r"[；。](?![^（(]*[）)])", value)
        head = parts[0].strip() if parts and parts[0].strip() else value
        if len(head) > limit:
            head = head[:limit].rstrip("，,、 ") + "…"
        value = head if len(head) < len(value) else value[:limit].rstrip("，,、 ") + "…"
    return value

def _receptor_protonation_lines(result: Dict[str, Any],
                                ligand_prot: Dict[str, Any]) -> List[str]:
    """「1.2 受体」里的受体质子化说明（做到了/没做到都要写清，并给出可执行建议）。"""
    from docking_agent.core import receptor_ph

    blocks = [b for b in (result.get("receptors") or []) if isinstance(b, dict)]
    if not blocks:
        return []
    out: List[str] = []
    infos = []
    for b in blocks:
        info = dict(b.get("receptor_protonation") or {})
        # 标准（模板态）准备也会丢残基：spec 顶层记录，这里合并进来一起呈现
        if b.get("dropped_bad_residues") and not info.get("dropped_bad_residues"):
            info["dropped_bad_residues"] = b.get("dropped_bad_residues")
        infos.append((b.get("receptor") or b.get("receptor_key") or "受体", info))
    applied = [(name, info) for name, info in infos if (info or {}).get("applied")]
    for name, info in applied:
        out.append(f"- 受体质子化（{name}）：{receptor_ph.describe(info)}")
    # 模板不匹配而被丢弃的残基（pH 路径与标准路径都会记录）：必须单列，不能只藏在 detail 里
    for name, info in infos:
        dropped_bad = (info or {}).get("dropped_bad_residues") or []
        if dropped_bad:
            out.append(f"  > **受体准备丢弃了模板不匹配的残基（{name}）**："
                       + "、".join(str(x) for x in dropped_bad[:12])
                       + ("…" if len(dropped_bad) > 12 else "")
                       + "。这些残基**没有进入对接**：若其中任何残基参与结合位点，"
                         "请修结构（补全/修正该段几何）后重跑。")
    not_applied = [(name, info) for name, info in infos if not (info or {}).get("applied")]
    for name, info in not_applied:
        reason = (info or {}).get("reason") or "未记录原因"
        out.append(f"- 受体质子化（{name}）：**未按目标 pH 重新准备**（{reason}）；"
                   "其质子化态来自受体文件本身或 meeko 残基模板默认态（≈pH 7 标准态）")
    if not_applied and str(ligand_prot.get("policy") or "") == "ph":
        ph = ligand_prot.get("ph")
        ph_txt = f"{ph:g}" if isinstance(ph, (int, float)) else "目标"
        out.append(f"  > **口径提示**：配体按目标 pH {ph_txt} 处理，但上述受体未做同样的 pH 处理，"
                   "两侧质子化条件**不完全一致**。对 His/Asp/Glu 敏感的体系（如凝血酶 S1 的 Asp189）"
                   "建议：① 直接提供已按目标 pH 准备好的受体 PDBQT/PQR；"
                   "② 用环境变量 `PDB2PQR_BIN` 指向可用的 pdb2pqr 让系统自动准备；"
                   "③ 对结论保留相应不确定性（本报告第 6 节已计入该局限）。")
    return out


def _pocket_section_lines(result: Dict[str, Any], table_block: Any, figure_block: Any,
                          prot: Dict[str, Any]) -> List[str]:
    """`### 5.1 结合口袋分析`：说明本次用的是哪个口袋、依据是什么、里面有哪些残基。"""
    from docking_agent.core import interactions as I

    L: List[str] = ["### 5.1 结合口袋分析", ""]
    blocks = [b for b in (result.get("receptors") or []) if isinstance(b, dict)]
    if not blocks:
        L.append("本次没有受体信息，无法给出结合口袋分析。")
        L.append("")
        return L
    analysis = result.get("pocket_analysis") or {}
    pockets = result.get("pockets") or []
    for block in blocks:
        label = block.get("receptor") or block.get("receptor_key") or "受体"
        source = str(block.get("box_source") or (block.get("site") or {}).get("source") or "—")
        center = block.get("box_center") or []
        size = block.get("box_size") or []
        L.append(f"**受体 {label}**")
        L.append("")
        L.append(f"- 盒子来源：{source}"
                 + (f"（由 {block.get('box_chosen_by')} 决定）" if block.get("box_chosen_by") else ""))
        if center and size:
            L.append(f"- 对接盒：中心 [{', '.join(_num(v, 2) for v in center)}] Å；"
                     f"尺寸 [{', '.join(_num(v, 2) for v in size)}] Å")
        stats = block.get("box_atom_stats") or {}
        if stats.get("atoms") is not None:
            L.append(f"- 盒内受体原子数：{stats.get('atoms')}"
                     + (f"；最近原子 {stats.get('nearest_atom') or '—'}"
                        f"（{_num(stats.get('nearest_angstrom'), 2)} Å）"
                        if stats.get("nearest_angstrom") is not None else ""))
        # 口袋组成：直接按对接盒统计残基（不依赖口袋引擎，实验位点/手填盒也能说清）
        receptor_pdbqt = str(block.get("pdbqt") or "")
        residues: List[Dict[str, Any]] = []
        try:
            if receptor_pdbqt and os.path.isfile(receptor_pdbqt) and center and size:
                residues = I.pocket_residues(receptor_pdbqt, center, size, limit=24)
        except Exception as e:  # noqa: BLE001 - 口袋描述失败不影响报告其它部分
            logger.debug("统计盒内残基失败：%s", e)
        if residues:
            L.append(f"- 盒内残基（{len(residues)} 个，按盒内原子数排序）："
                     + "、".join(f"{r['residue']}" for r in residues))
            hydrophobic = [r for r in residues if r.get("resname") in
                           ("ALA", "VAL", "LEU", "ILE", "MET", "PHE", "TRP", "PRO", "TYR", "CYS")]
            aromatic = [r for r in residues if r.get("resname") in ("PHE", "TYR", "TRP", "HIS")]
            charged = [r for r in residues if r.get("resname") in ("ASP", "GLU", "LYS", "ARG", "HIS")]
            polar = [r for r in residues if r.get("resname") in
                     ("ASN", "GLN", "SER", "THR", "TYR", "HIS", "CYS", "TRP")]
            L.append(f"  - 组成特征：疏水/芳香残基 {len(hydrophobic)} 个"
                     + (f"（芳香 {len(aromatic)} 个：{'、'.join(r['residue'] for r in aromatic[:6])}）"
                        if aromatic else "")
                     + f"；可质子化/带电残基 {len(charged)} 个"
                     + (f"（{'、'.join(r['residue'] for r in charged[:6])}）" if charged else "")
                     + f"；极性残基 {len(polar)} 个。")
            if aromatic:
                L.append("  - **提示**：芳香残基较多时，说明该口袋可能偏好 π–π 堆叠/疏水作用；"
                         "具体到每个分子是否真的形成 π 堆积，以第 5.2 节的几何分析为准。")
            if charged:
                L.append("  - **提示**：存在带电残基时，配体的质子化态（第 1.3 节策略）与盐桥/氢键"
                         "互补性会显著影响结合；请结合第 5.2 节的盐桥/氢键明细判断。")
        # 口袋预测结果（有则给表格：多个候选口袋的评分与残基）
        receptor_key = str(block.get("receptor_key") or "")
        pocket_rows = [p for p in pockets
                       if not receptor_key or str(p.get("receptor_key") or receptor_key) == receptor_key]
        if pocket_rows:
            L += table_block(
                f"口袋预测候选（{label}，按工具评分排序）",
                ["#", "口袋", "评分", "来源", "中心 (Å)", "口袋残基（工具给出）"],
                [[p.get("rank"), p.get("name") or "—", _num(p.get("score"), 2),
                  p.get("engine") or p.get("source") or "—",
                  "[" + ", ".join(_num(v, 1) for v in (p.get("center") or [])) + "]",
                  "、".join((p.get("residues") or [])[:8]) or "—"]
                 for p in pocket_rows[:6]],
                aligns=["r", "l", "r", "l", "l", "l"])
        chosen = block.get("site") or {}
        if chosen.get("source"):
            L.append(f"- 本次采用：{chosen.get('source')}")
        validation = block.get("box_validation") or {}
        if validation:
            verdict = validation.get("verdict") or validation.get("agreement") or ""
            distance = validation.get("distance_angstrom") or validation.get("distance")
            L.append("- 工具预测与实验位点的一致性："
                     + (f"{verdict}" if verdict else "已做一致性检查")
                     + (f"（相距 {_num(distance, 2)} Å）" if isinstance(distance, (int, float)) else ""))
        for warning in (block.get("box_warnings") or [])[:3]:
            L.append(f"- 口袋/盒子提示：{warning}")
        dropped_bad = block.get("dropped_bad_residues") or []
        if dropped_bad:
            L.append(f"- 受体准备丢弃的模板不匹配残基：{'、'.join(str(x) for x in dropped_bad[:10])}"
                     "（这些残基不在口袋/对接范围内）")
        L.append("")
    if analysis.get("engine"):
        L.append(f"> 口袋引擎：`{analysis.get('engine')}`"
                 + ("（真实预测工具）" if not str(analysis.get("engine")).startswith("geometric")
                    else "（内置几何法：无需外部依赖的 alpha-sphere 简化实现）")
                 + "。口袋坐标与残基清单是**工具输出**，不是按序列推测的。")
        L.append("")
    return L


def _ph_policy_text(prot: Dict[str, Any], suffix: str = "重新分配质子化态") -> str:
    """ph 策略的中文说明（pH 可能缺失 —— 例如旧运行产物里没有该字段）。"""
    ph = prot.get("ph")
    if isinstance(ph, (int, float)):
        return f"按目标 pH {ph:g} {suffix}"
    return f"按目标 pH {suffix}"



def _cocrystal_control_line(result: Dict[str, Any]) -> str:
    """报告正文中的一行：受体是否自带共晶配体、是否询问、用户最终怎么选。

    用户要求：该决定要能**单独追溯**（不必回看对话）。因此这里把三件事写全：
    检测到的配体（残基名/链:残基号/原子数）、是否询问过、用户的决定与据此采取的动作。
    """
    offer = result.get("cocrystal_control_offer") or {}
    decision = str(result.get("positive_control_decision") or "").strip().lower()
    if not offer:
        if decision == "skip":
            return "用户选择不使用阳性对照（报告未做对照分析）"
        return "未检测到（受体结构未带可用的共晶配体）"
    label = str(offer.get("label") or offer.get("resname") or "")
    smiles = str(offer.get("smiles") or "")
    if decision == "use":
        return (f"检测到 {label}，已询问用户 → 用户选择**用作阳性对照**"
                + (f"（SMILES `{smiles}`）" if smiles else ""))
    if decision == "skip":
        return f"检测到 {label}，已询问用户 → 用户选择**不使用**（本轮未做对照分析）"
    return f"检测到 {label}，已询问用户是否用作阳性对照（本次运行未见用户选择）"



def build_markdown_report(result: Dict[str, Any], *, kind: str = "agent",
                          run_id: str = "", receptor_label: str = "",
                          site: Optional[Dict[str, Any]] = None,
                          artifacts: Optional[List[Dict[str, Any]]] = None,
                          agent_narrative: str = "",
                          agent_models: Optional[Dict[str, Any]] = None,
                          created_at: str = "",
                          top_n: int = 0) -> str:
    """按固定模板生成报告。

    agent_narrative：多 Agent 模式下协调 Agent 的结论文本，放入固定的第 8 节。
    agent_models：多 Agent 模式下各角色 Agent 实际使用的模型（每个角色独立实例）。
    """
    full_ranking = rank_molecules(result.get("ranking") or [])
    limit = top_n or env_int("REPORT_TOP_N", 50)
    ranking = full_ranking[:max(1, limit)] if limit > 0 else full_ranking
    truncated = len(full_ranking) > len(ranking)
    binding_rows = {r.get("smiles"): r for r in (result.get("binding") or {}).get("rows", [])}
    pc = result.get("positive_control") or {}
    notes = result.get("notes") or []
    molecules = result.get("molecules") or []
    pc_aff = pc.get("affinity_kcal_mol")
    spec = result.get("task_spec") or {}
    hits = [m for m in full_ranking
            if isinstance(m.get("affinity_kcal_mol"), (int, float))
            and isinstance(pc_aff, (int, float)) and m["affinity_kcal_mol"] < pc_aff]
    # 运行模式文案：对接统一由 Agent 驱动（记录 60 移除流水线模式），
    # 历史运行目录里可能仍是旧 kind，保留映射以便旧报告照原样复看。
    mode_zh = {"agent": "多 Agent 协作", "studio": "多 Agent 协作（Studio）",
               "pipeline": "确定性流水线（历史记录）"}.get(str(kind or "agent"), "多 Agent 协作")
    available = {str(a.get("name")) for a in (artifacts or []) if isinstance(a, dict)}
    # 协调 Agent 通过 customize_report 记录的「按用户要求定制」（标题 / 附加列 / 要点 / 要求与响应）。
    # 骨架（§1–§9）保持不变，变的只是标题、排行表附加列与最前面的「要求与响应」一节。
    custom = result.get("report_customization") or {}
    if not isinstance(custom, dict):
        custom = {}
    requested_columns = [str(c) for c in (custom.get("extra_columns") or [])
                         if str(c) in REPORT_FIELD_LABELS]
    # 数据自带分子 ID（来自输入文件、且与名称不同）→ 默认也带上 ID 列：
    # 报告应当跟着**本次数据**走，而不是永远只印固定的那几列。
    _id_rows = [r for r in full_ranking if str(r.get("id") or "").strip()]
    _id_distinct = any(str(r.get("id")) != str(r.get("name")) for r in _id_rows)
    extra_columns = list(requested_columns)
    if "id" not in extra_columns and _id_rows and _id_distinct:
        extra_columns = ["id"] + extra_columns

    # 图/表编号：按出现顺序连续编号（缺图/缺表时不占号，编号始终连续）
    counters = {"fig": 0, "tbl": 0}

    def fig_caption(title: str) -> str:
        counters["fig"] += 1
        return f"图 {counters['fig']} {title}"

    def tbl_caption(title: str) -> str:
        counters["tbl"] += 1
        return f"表 {counters['tbl']} {title}"

    def figure_block(name: str, title: str, note: str = "") -> List[str]:
        """图：图片 + 图题（缺产物时不嵌图，也不占用图号）。"""
        rel = CHART_FILES.get(name)
        if not rel or (available and name not in available):
            return []
        caption = fig_caption(title)
        out = [f"![{caption}]({rel})", ""]
        if note:
            out += [note, ""]
        out += [f"**{caption}**", ""]
        return out

    def figure_by_rel(rel: str, title: str) -> List[str]:
        """按**相对路径**内嵌图片（用于按名次动态生成的 2D/3D 姿态图）。

        与 `figure_block` 的区别：这些图的产物名在编译期未知（`interaction_2d_01` 等），
        因此按「文件是否真的在产物清单里」判断，避免引用不存在的图。
        """
        name = rel.rsplit("/", 1)[-1][:-4]
        if available and name not in available:
            return []
        caption = fig_caption(title)
        return [f"![{caption}]({rel})", "", f"**{caption}**", ""]

    def table_block(title: str, headers: List[str], rows: List[List[Any]],
                    aligns: Optional[List[str]] = None) -> List[str]:
        return [f"**{tbl_caption(title)}**", "", _table(headers, rows, aligns), ""]

    def _b(m: Dict[str, Any]) -> Dict[str, Any]:
        return binding_rows.get(m.get("smiles")) or m

    # ---- 对接盒与位点溯源 ----
    def _box_source() -> str:
        for block in ((result.get("docking") or {}).get("receptors") or []):
            if isinstance(block, dict) and (block.get("box_source") or (block.get("site") or {})):
                return str(block.get("box_source") or (block.get("site") or {}).get("source") or "—")
        if site:
            return str(site.get("source") or "—")
        return "—"

    def _box_center_size() -> Tuple[str, str]:
        for block in ((result.get("docking") or {}).get("receptors") or []):
            if isinstance(block, dict) and (block.get("box_center") or block.get("box_size")):
                return _vec(block.get("box_center")), _box_size_text(block.get("box_size"))
        if site:
            return _vec(site.get("center")), _box_size_text(site.get("size"))
        return "—", "—"

    def _floor() -> Dict[str, Any]:
        for block in ((result.get("docking") or {}).get("receptors") or []):
            if isinstance(block, dict) and block.get("box_library_floor"):
                return dict(block.get("box_library_floor") or {})
        return {}

    def _floor_text() -> str:
        floor = _floor()
        if not floor:
            return "未记录（旧数据或未启用库级下限）"
        if floor.get("enabled") is False:
            return "已关闭（`BOX_SPAN_ENABLED=off`），盒子为口袋驱动值"
        bound = _num(floor.get("bound"), 1)
        if floor.get("p95_span") is not None:
            return (f"**{bound} Å**（库内 P95 跨度 {_num(floor.get('p95_span'), 1)} Å，抽样 "
                    f"{floor.get('sample_n')}/{floor.get('library_n')} 个）；"
                    + ("**盒子因此扩大**" if floor.get("raised") else "盒子未因此改变"))
        return f"{bound} Å（未取得跨度样本，退回口袋驱动值）"

    def _large_box_rows() -> List[Dict[str, Any]]:
        """大配体组的结果行（盒子不同，单独一节展示，不并入主排序榜）。"""
        rows: List[Dict[str, Any]] = []
        for block in ((result.get("docking") or {}).get("receptors") or []):
            _main, large = split_box_groups(block.get("results") or [])
            rows.extend(large)
        return sort_by_affinity(rows)

    def _pocket_table() -> List[str]:
        """口袋预测结果表（工具输出，含被采用的那个）。"""
        blocks = (result.get("docking") or {}).get("receptors") or []
        pockets: List[Dict[str, Any]] = []
        engine = ""
        chosen = ""
        for block in blocks:
            site_info = block.get("site") or {}
            if block.get("pockets"):
                pockets = block["pockets"]
                engine = str(site_info.get("engine") or "")
                pocket = site_info.get("pocket") or {}
                chosen = str(pocket.get("name") or "")
                break
        if not pockets:
            return []
        rows = []
        for pocket in pockets[:8]:
            mark = "← 采用" if chosen and pocket.get("name") == chosen else ""
            residues = " ".join((pocket.get("residues") or [])[:6])
            rows.append([
                str(pocket.get("rank") or ""), pocket.get("name") or "",
                _num(pocket.get("score")), _vec(pocket.get("center")),
                _vec(pocket.get("extent")), residues, mark,
            ])
        out = [f"### 1.6 结合口袋预测", ""]
        out += table_block(
            "结合口袋预测（真实工具输出，工具为预测结果，不代替实验定位）",
            ["#", "口袋", "score", "中心 (Å)", "范围 (Å)", "附近残基", "选择"],
            rows, aligns=["r", "l", "r", "l", "l", "l", "c"])
        out += [f"> 引擎：`{engine or '—'}`；实际使用的对接盒见第 1.2 节。", ""]
        return out

    def _models_cell() -> Optional[str]:
        """各 Agent 角色实际使用的模型（同一角色可能用了不同模型端点）。"""
        if not agent_models:
            return None
        role_zh = _ROLE_ZH
        parts = []
        for role, meta in agent_models.items():
            if isinstance(meta, dict):
                model = meta.get("actual_model") or meta.get("model")
                calls = meta.get("calls")
            else:
                model, calls = meta, None
            if not model:
                continue
            tag = f"{role_zh.get(role, role)} `{model}`"
            if calls == 0:
                tag += "（本次未调用）"
            parts.append(tag)
        return " · ".join(parts) or None

    # ===================================================================== #
    # 抬头信息（表 1）
    # ===================================================================== #
    L: List[str] = []
    L.append("# " + (str(custom.get("title") or "").strip() or "分子对接筛选报告"))
    L.append("")
    if custom.get("title"):
        L.append("> 报告标题由协调 Agent 按本次要求设置；§1–§9 仍是系统固定骨架，可跨运行对照。")
        L.append("")
    center_txt, size_txt = _box_center_size()
    # 抬头表只放摘要：盒子来源与库级下限的完整说明在第 1.2 节，避免 2 列表格被压到截断
    box_cell = f"中心 {center_txt} Å；尺寸 {size_txt}"
    if spec:
        meta_spec_cell = (f"`{spec.get('task_type')}` · 权威={spec.get('authority')} · "
                          f"决策=`{spec.get('decision')}` · 来源={spec.get('source', 'rules')}")
    else:
        meta_spec_cell = "表单直接提交（未经过任务受理模块）"
    seed_value, seed_policy = _seed_info(result)
    meta_rows = [
        ["运行编号", f"`{run_id or '—'}`"],
        ["运行模式", mode_zh],
        ["生成时间", created_at or "—"],
        ["受体", receptor_label or "—"],
        ["对接盒", box_cell],
        ["对接引擎", str((ranking[0].get("engine") if ranking else "") or "—")],
        ["搜索强度", str((ranking[0].get("exhaustiveness") if ranking else "") or "—")],
        ["随机种子", f"`seed={seed_value}` / `seed_policy={seed_policy}`"],
        ["候选分子数", len(molecules)],
        ["阳性对照", (f"{pc.get('name') or '—'}（{_num(pc_aff)} kcal/mol）") if pc else "未提供（未做对照分析）"],
        ["受体共晶配体", _cocrystal_control_line(result)],
        ["任务受理", meta_spec_cell],
    ]
    models_cell = _models_cell()
    if models_cell:
        meta_rows.append(["各 Agent 模型", models_cell])
    L += table_block("报告信息", ["项目", "内容"], meta_rows, aligns=["l", "l"])

    # ===================================================================== #
    # 0. 本次要求与响应（协调 Agent 的自主部分：骨架不变，内容按用户要求组织）
    # ===================================================================== #
    requirements = [r for r in (custom.get("requirements") or []) if isinstance(r, dict)]
    highlights = [str(h) for h in (custom.get("highlights") or []) if str(h).strip()]
    if requirements or highlights or custom.get("notes") or requested_columns or agent_models:
        L.append("## 0. 本次要求与响应（协调 Agent）")
        L.append("")
        L.append("> 本节是**协调 Agent 按本次任务的具体要求**组织的部分："
                 "§1–§9 仍是系统固定骨架（便于跨运行对照），本节体现本次的个性要求。")
        L.append("")
        if requirements:
            L += table_block(
                "用户要求 → 实际处理",
                ["用户要求", "实际处理（含真实数据）"],
                [[_brief(r.get("ask"), 120) or "—", _brief(r.get("response"), 220) or "—"]
                 for r in requirements],
                aligns=["l", "l"])
        if requested_columns:
            labels = "、".join(REPORT_FIELD_LABELS.get(c, c) for c in requested_columns)
            L.append(f"- **按用户要求附加的列**：{labels}（见第 3.1 节排行表与排序 CSV）。")
        if highlights:
            L.append("- **协调 Agent 本次要点**：")
            for h in highlights:
                L.append(f"  - {_brief(h, 200)}")
        if custom.get("notes"):
            L.append("")
            L.append(_brief(str(custom["notes"]), 600))
        if agent_models:
            rows = []
            for role, info in sorted((agent_models or {}).items()):
                if not isinstance(info, dict):
                    continue
                rows.append([_ROLE_ZH.get(role, role),
                             str(info.get("actual_model") or info.get("model") or "—"),
                             str(info.get("calls") if info.get("calls") is not None else "—")])
            if rows:
                L.append("")
                L += table_block("协调 Agent 的调度记录（各角色实际调用的模型与次数）",
                                 ["角色", "实际模型", "调用次数"], rows, aligns=["l", "l", "r"])
        L.append("")

    # ===================================================================== #
    # 1. 任务与参数
    # ===================================================================== #
    L.append("## 1. 任务与参数")
    L.append("")
    L.append("本章列出本次运行的输入来源、受体与位点依据、对接盒、参数、工具版本与随机种子，"
             "供复现与核对；结论只在本章条件下成立。")
    L.append("")

    # ---- 1.1 任务来源与受理 ----
    L.append("### 1.1 任务来源与受理")
    L.append("")
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
        L += table_block("任务受理（来源与决策）", ["项目", "内容"], spec_rows, aligns=["l", "l"])
    else:
        L.append("本次由参数表单直接提交，未经过任务受理模块（无 `task_spec`）；"
                 "受体、位点、配体与参数均以表单/请求为准。")
        L.append("")
    if notes:
        # 报告只给核心（每条压成一句话、最多 6 条）：完整原文在 run.json / 结果 JSON 里，
        # 否则一张 A4 全是「备注」，PDF 尤其不可读。
        L.append("**运行笔记**（核心，最多 6 条；完整原文见 `run.json` 的 `notes`）：")
        L.append("")
        for n in notes[:6]:
            L.append(f"- {_brief(n, 110)}")
        L.append("")
    else:
        L.append("- 无特别说明。")
        L.append("")

    # 质子化态策略（运行级；逐分子溯源来自工具真实回传的 ligand_facts.protonation）
    # 注意：§1.2 也要用它判断"受体是否与配体同一 pH"，因此必须在最前面算好。
    prot = protonation_summary(full_ranking)

    # ---- 1.2 受体、位点与对接盒 ----
    L.append("### 1.2 受体、位点与对接盒")
    L.append("")
    large_rows = _large_box_rows()
    L.append(f"- 受体：{receptor_label or '—'}")
    L.append(f"- 位点来源：{_box_source()}")
    L.append(f"- 对接盒：中心 {center_txt} Å；尺寸 {size_txt}")
    L.append(f"- 库级下限：{_floor_text()}")
    # 受体质子化：必须与配体同一目标 pH，且要如实写明"做到了没有"
    protonation_row = _receptor_protonation_lines(result, prot)
    L += protonation_row
    if large_rows:
        L.append(f"- 盒子一致性：主组所有分子共用**同一个对接盒**；另有 {len(large_rows)} 个分子因 3D "
                 "跨度超出主盒，被划入 `large` 组、用**同一中心但更大**的盒子单独重跑。两组"
                 "分数不可跨组比较（依据见第 6 节）。")
    elif (result.get("docking") or {}).get("receptors"):
        L.append("- 盒子一致性：本次所有参与排序的分子共用**同一个对接盒**，盒子效应对排序是共模的。")
    L.append("")

    # ---- 1.3 对接参数 ----
    L.append("### 1.3 对接参数（同一阶段内参数一致）")
    L.append("")
    plan = result.get("param_plan") or {}
    engine_val = (ranking[0].get("engine") if ranking else None) or plan.get("engine") or "—"
    exh_val = (ranking[0].get("exhaustiveness") if ranking else None)
    if exh_val is None:
        exh_val = plan.get("exhaustiveness")
    nposes = (ranking[0].get("n_poses") if ranking else None)
    if nposes is None:
        nposes = plan.get("n_poses")
    two_stage = bool(plan.get("two_stage"))
    param_rows = [
        ["对接引擎", f"`{engine_val}`（AutoDock Vina 打分函数）"],
        ["搜索强度 exhaustiveness",
         (f"主组 `{exh_val if exh_val is not None else '—'}`"
          + (f"；两阶段漏斗：粗筛 `{plan.get('coarse_exhaustiveness')}` → 精算 "
             f"`{plan.get('fine_exhaustiveness')}`（前 {plan.get('refine_top_n')} 个）"
             if two_stage else ""))],
        ["输出构象数 n_poses", str(nposes if nposes is not None else "—")],
        ["随机种子", f"`seed={seed_value}` / `seed_policy={seed_policy}`"],
        ["对接盒", f"中心 {center_txt} Å；尺寸 {size_txt}"],
    ]
    concurrency = (result.get("docking") or {}).get("concurrency")
    if concurrency:
        param_rows.append(["并发", str(concurrency)])
    if prot["policy"]:
        policy_zh = {"neutralize": "中和带净电荷的分子（中性分子不变）",
                     "keep": "保持输入形式（不改变质子化态）",
                     "ph": _ph_policy_text(prot),
                     }.get(prot["policy"], prot["policy"])
        detail = f"`{prot['policy']}`（{policy_zh}）"
        if prot["observed"]:
            detail += (f"；{len(prot['applied'])}/{prot['observed']} 个分子的质子化态被调整"
                       f"（另有 {max(0, prot['charged'] - len(prot['applied']))} 个带净电荷但无法中和，"
                       "如季铵等永久电荷）")
        if prot["applied"]:
            examples = "、".join(
                f"{a['name'] or a['smiles']}（{a['before']:+.0f}→{a['after']:+.0f}）"
                for a in prot["applied"][:5] if isinstance(a.get("before"), (int, float)))
            if examples:
                detail += f"；例如 {examples}"
        if prot["policy"] == "ph" and prot.get("rule_counts"):
            top_rules = "、".join(f"{k}×{v}" for k, v in sorted(
                prot["rule_counts"].items(), key=lambda kv: -kv[1])[:6])
            detail += (f"；命中的可电离官能团：{top_rules}（内置 pKa 规则表 "
                       f"{PKA_TABLE_VERSION}；**规则近似，不是 pKa 预测**，"
                       "需要微观态分布时请用专业工具生成质子化态后以 SDF 提供并选择 keep）")
        detail += "。逐分子净电荷前/后与方法见排序 CSV 的 `protonation_policy`/`charge_input`/`charge_used` 列与产物 `docking.json` 的 `ligand_facts.protonation`；**原始 SMILES 始终保留**"
        param_rows.append(["质子化态策略", detail])
    L += table_block("对接参数（同一阶段内一致）", ["参数", "取值"], param_rows, aligns=["l", "l"])
    if prot["policy"] and prot["applied"]:
        if prot["policy"] == "ph":
            L.append(f"> **质子化态说明**：本次按目标 pH {prot.get('ph')} 分配质子化态"
                     "（羧酸根/铵/脒/胍/咪唑等官能团），理化性质与对接使用**同一种化学形式**；"
                     "被调整的分子的分子量/电荷与原始输入不同，这是**有意的化学处理**而非计算误差。"
                     "该处理基于内置官能团 pKa 规则表（近似，非 pKa 预测）："
                     "逐分子命中的规则见产物 `docking.json` 的 `ligand_facts.protonation.rules`。"
                     "若输入已是专业工具生成的目标 pH 质子化态，请把策略改为 `keep` 以免二次处理。")
        else:
            L.append("> **质子化态说明**：理化性质与对接使用**同一种化学形式**（同一运行级策略）；"
                     "被中和的分子的分子量/电荷与原始输入不同，这是**有意的化学处理**而非计算误差。"
                     "若要按目标 pH 处理离子态，请把质子化态策略改为 `ph` 并给出目标 pH 后重跑。")
    L.append("> **同一阶段（pass）内所有分子共用完全相同的参数集合**（引擎、exhaustiveness、"
             "n_poses、seed 与盒子），绝不逐分子变化；这是排序可比的前提，"
             "因此排序 CSV 中每一行都带有同一阶段的参数留痕。")
    L.append("")

    # ---- 1.4 工具与版本 ----
    L.append("### 1.4 工具与版本")
    L.append("")
    L += table_block("本次运行实际使用的工具与版本", ["工具", "版本"],
                     _tool_versions_markdown(), aligns=["l", "r"])

    # ---- 1.5 参数自动规划 ----
    if _param_plan(result):
        L += _param_plan_lines(result, "### 1.5 参数自动规划", tbl_caption("对接参数自动规划结果"))

    # ---- 1.6 结合口袋预测 ----
    L += _pocket_table()

    # ===================================================================== #
    # 2. 结果排序
    # ===================================================================== #
    L.append("## 2. 结果排序")
    L.append("")
    L.append("> 排序依据为 AutoDock Vina 亲和力升序（数值越负代表打分越强）。"
             "分数只适用于**本次流程内的相对比较**，不是自由能，也不能直接换算实验活性。")
    L.append("")
    if isinstance(pc_aff, (int, float)):
        L.append(f"> **命中判据**：`Δ = 亲和力 − 阳性对照亲和力`；`Δ < 0 kcal/mol` 记为"
                 f"「在本次打分体系下优于对照」。阳性对照亲和力 = {_num(pc_aff)} kcal/mol。")
    else:
        L.append("> **命中判据**：本次未提供阳性对照，无对照基准，不判定「命中」；"
                 "排序仅表示同批分子在同一套参数下的相对打分。")
    L.append("")
    if large_rows:
        L.append(f"> **可比性说明**：主组所有分子共用同一个对接盒；`large` 组（{len(large_rows)} 个）"
                 "因 3D 跨度超出主盒，改用同一中心、更大盒子单独重跑。换盒子会改变 Vina 分数"
                 "（敏感且非单调），因此 large 组**不并入主表、不跨组比较**，见第 2.2 节。")
        L.append("")
    if truncated:
        L.append(f"> 共 {len(full_ranking)} 个成功对接的分子，下表仅列出**前 {len(ranking)} 个**；"
                 "完整排序见产物 `ranking.csv` 与 `ranking.json`（第 9 节）。")
        L.append("")

    L.append("### 2.1 主组排序")
    L.append("")
    header = ["排名", "分子", "亲和力 (kcal/mol)", "Δ vs 对照 (kcal/mol)", "引擎", "搜索强度",
              "分子量 (Da)", "logP", "TPSA (Å²)", "类药性", "相似度"]
    rows: List[List[Any]] = []
    for i, m in enumerate(ranking, 1):
        aff = m.get("affinity_kcal_mol")
        delta = (round(aff - pc_aff, 2) if isinstance(aff, (int, float))
                 and isinstance(pc_aff, (int, float)) else None)
        rows.append([i, m.get("name") or m.get("smiles"), _num(aff), _num(delta),
                     m.get("engine") or "—", m.get("exhaustiveness") or "—",
                     _num(m.get("molecular_weight"), 2), _num(m.get("logP"), 2),
                     _num(m.get("tpsa"), 2),
                     "通过" if m.get("drug_likeness_pass") else
                     ("违例" if m.get("lipinski_violations") else "—"),
                     _num(m.get("similarity_to_positive_control"), 3)])
    if pc:
        rows.append(["对照", pc.get("name") or "阳性对照", _num(pc_aff), "0.00",
                     pc.get("engine") or "—", pc.get("exhaustiveness") or "—",
                     "—", "—", "—", "—", "1.000"])
    L += table_block("候选分子对接结果（主组，按亲和力升序；对照行不参与排名）",
                     header, rows,
                     aligns=["r", "l", "r", "r", "c", "r", "r", "r", "r", "c", "r"])
    L += figure_block("docking_chart", "主组对接亲和力对比（含阳性对照参考线）")
    L += figure_block("affinity_histogram", "亲和力分布（含均值 / 中位数 / 对照参考线）")

    if large_rows:
        L.append("### 2.2 大配体组（盒子不同，不跨组比较）")
        L.append("")
        L.append("> 这些分子的 3D 跨度 + 余量超过主盒，已在**同一中心**用更大盒子单独重跑；"
                 "其分数与主组不可直接比较（盒子效应见第 6 节）。位姿与分组标记"
                 "（`box_group` / `box_size`）保留在产物 `docking.json` 与 `result.json`。")
        L.append("")
        L += table_block(
            "大配体组结果（独立盒子）",
            ["分子", "亲和力 (kcal/mol)", "盒子", "分组说明"],
            [[m.get("name") or m.get("smiles"), _num(m.get("affinity_kcal_mol")),
              _box_size_text(m.get("box_size")),
              m.get("box_fit_warning") or "跨度超出主盒，独立重跑"]
             for m in large_rows],
            aligns=["l", "r", "l", "l"])

    # ===================================================================== #
    # 3. 推荐分子
    # ===================================================================== #
    L.append("## 3. 推荐分子")
    L.append("")
    # ---- 3.1 推荐化合物排行（对接亲和力 × 理化性质/类药性 综合分）----
    top_n_rank = max(1, env_int("RECOMMEND_TOP_N", 10))
    rec = build_recommendations(
        full_ranking, top_n=top_n_rank,
        reasons=result.get("agent_recommendations") or [],
        positive_control_name=POSITIVE_CONTROL_NAME)
    L.append("### 3.1 推荐化合物排行（对接亲和力 × 理化性质 / 类药性）")
    L.append("")
    pose_by_name = {str(item.get("name") or ""): item for item in (result.get("pose_analysis") or [])}
    if not rec.get("rows"):
        L.append("本次没有可用于综合排行的分子（没有分子获得有效对接分数），"
                 "因此不给出推荐排行；失败明细见第 7 节。")
        L.append("")
    else:
        w = rec["weights"]
        crit = rec["criteria"]
        L.append("综合分由四个**各自有明确含义**的分量按权重合成，权重与满分口径如下"
                 "（均为**绝对尺度**，不按本批分子库归一化，因此不同运行之间可比）：")
        L.append("")
        L += table_block(
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
        L.append(f"> 等级：综合分 ≥ {crit['grade_a']:g} 记 **A**，≥ {crit['grade_b']:g} 记 **B**，"
                 f"其余记 **C**；对接亲和力弱于 {crit['affinity_gate_kcal']:g} kcal/mol 的分子"
                 "即使其它分量满分，等级也**封顶为 C**（没有结合强度的分子不应因「小而类药」被推荐）。"
                 "缺少数据的分量按 0 计入并在表中以 `—` 标注，不静默丢弃。")
        L.append("")
        # 排行表只放「决定名次」的 6 列：宽表在 PDF 里会被降级成逐行竖排（8 列以上），
        # 反而不整洁；其余逐分子指标（分子量/logP/TPSA/违例/相似度）在下面的结构卡里，
        # 完整字段仍在 ranking.csv / 分子详情页。
        # 协调 Agent 按用户要求附加的列（customize_report → extra_columns）：只加真实有值的字段，
        # 列名来自共享字段表，取数仍是本次真实计算结果 —— 骨架不变，内容能按用户要求变化。
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
        L += table_block(
            f"推荐化合物排行（前 {len(rec['rows'])} 名，综合分降序）",
            ["#", "分子", "综合分", "等级", "亲和力 (kcal/mol)", "配体效率 LE"] + extra_headers,
            rank_rows, aligns=["r", "l", "r", "c", "r", "r"] + ["l"] * len(extra_headers))
        L.append("> 逐分子的分子量 / logP / TPSA / Lipinski 违例 / 与对照相似度见下方结构卡；"
                 + (f"带 `*` 的列是本次按用户要求附加的；" if requested_columns else "")
                 + "全部字段见 `ranking.csv` 与「分子详情」页。")
        L.append("> 排行顺序 = 先按等级（**A → B → C**），同级内按综合分降序："
                 "等级是被封顶/门槛修正后的**优先级判定**，综合分是原始加权得分，"
                 "两者不一致时以等级为准（否则会出现「排行第 2 名是 C 级」的自相矛盾）。")
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
            L += figure_by_rel(f"charts/recommend_card_{index:02d}.png",
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
            for tip in (r.get("suggestions") or [])[:2]:
                L.append(f"- **提示**：{_brief(tip, 80)}")
            L.append("")
        if not reasons_txt:
            L.append("> 本次协调 Agent 未提交逐分子推荐理由（工具 `submit_recommendations` 未调用），"
                     "以上排行与建议完全来自系统的确定性计算；如需 Agent 的理由，可要求其补充后重跑报告。")
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
        L.append(f"- 建议推进顺序：先做 A 级中「亲和力 ≤ −8 kcal/mol 且 Lipinski 违例 ≤ 1」的分子"
                 "（必要时提高 exhaustiveness 复算确认稳定性），B 级作为备选；"
                 "C 级仅在无更好选择或作为阴性参考时使用。")
        if rec.get("rows") and any(r.get("box_group") == "large" for r in rec["rows"]):
            L.append("- 排行中含使用**大配体专用盒**的分子（见第 2.2 节）：其分数与主组不可直接比较，"
                     "跨组比较前请先统一盒子重跑。")
        L.append("")
        if reasons_txt:
            L.append("> 上表数值全部来自本次运行的**真实计算**（AutoDock Vina 打分 + RDKit 理化性质）；"
                     "Agent 只补充「为什么推荐/如何推进」的文字，不改动任何数值。")
            L.append("")
    # ---- 3.2 优于阳性对照的分子（与综合分排行互补，不是同一件事）----
    L.append("### 3.2 优于阳性对照的分子")
    L.append("")
    if not pc:
        L.append("本次未提供阳性对照，无法以对照为基准做富集判断；建议参考第 2 节排序前几名，"
                 "或提供阳性对照 SMILES 后重跑以获得对照比较。")
    elif hits:
        L.append("以下分子在本次流程条件下打分优于阳性对照（`Δ < 0`）：")
        L.append("")
        for m in hits[:20]:
            L.append(f"- **{m.get('name') or m.get('smiles')}**：{_num(m.get('affinity_kcal_mol'))} kcal/mol"
                     f"（Δ = {_num(round(m['affinity_kcal_mol'] - pc_aff, 2))} kcal/mol）；"
                     f"分子量 {_num(m.get('molecular_weight'), 2)} Da，logP {_num(m.get('logP'))}，"
                     f"TPSA {_num(m.get('tpsa'))} Å²，"
                     f"类药性{'通过' if m.get('drug_likeness_pass') else '不通过'}，"
                     f"与对照相似度 {_num(m.get('similarity_to_positive_control'), 3)}")
        L.append("")
        L.append(f"> 共 {len(hits)} 个分子优于阳性对照（占成功对接分子的 "
                 f"{_pct(len(hits), len(full_ranking))}）。以上为打分层面的相对结论，"
                 "需实验验证，见第 6 节方法与局限。")
    else:
        L.append("本次没有分子在对接打分上优于阳性对照。在本流程条件下建议扩大分子库、"
                 "调整位点/盒子或提高搜索强度后重试；该结论不排除这些分子具有实验活性。")
    L.append("")

    # ===================================================================== #
    # 4. 理化性质
    # ===================================================================== #
    L.append("## 4. 理化性质")
    L.append("")
    if prot["policy"]:
        policy_note = (_ph_policy_text(prot) if prot["policy"] == "ph"
                       else prot["policy"])
        L.append(f"> 理化性质按**与对接相同的质子化态**计算（本次策略 `{prot['policy']}`"
                 f"＝{policy_note}，{len(prot['applied'])} 个分子的质子化态经过调整）；"
                 "被调整过的分子在产物里同时保留 `smiles`（原始输入，主键）与 `protonated_smiles`"
                 "（实际参与计算的形式）。")
        L.append("")
    L += table_block(
        "候选分子理化性质（RDKit 计算）",
        ["分子", "分子式", "分子量 (Da)", "logP", "TPSA (Å²)", "HBD", "HBA", "可旋转键", "芳香环",
         "Lipinski 违例"],
        [[m.get("name") or m.get("smiles"), m.get("formula"), _num(m.get("molecular_weight"), 2),
          _num(m.get("logP"), 2), _num(m.get("tpsa"), 2), m.get("hbd"), m.get("hba"),
          m.get("rotatable_bonds"), m.get("aromatic_rings"), m.get("lipinski_violations")]
         for m in ranking],
        aligns=["l", "l", "r", "r", "r", "r", "r", "r", "r", "r"])
    L += figure_block("property_chart", "理化性质空间（横轴分子量，纵轴 logP，点大小表示对接强度）")

    # ===================================================================== #
    # 5. 结合模式与阳性对照比较
    # ===================================================================== #
    L.append("## 5. 结合模式与阳性对照比较")
    L.append("")
    # ---- 5.1 结合口袋说明（本次选定的口袋：来源、评分、残基组成、与实验位点的一致性）----
    L += _pocket_section_lines(result, table_block, figure_block, prot)
    L.append("### 5.2 推荐分子的姿态–口袋相互作用（2D / 3D）")
    L.append("")
    pose_rows = result.get("pose_analysis") or []
    if result.get("pose_analysis_note"):
        L.append(f"> {result['pose_analysis_note']}")
        L.append("")
    if not pose_rows:
        if not result.get("pose_analysis_note"):
            L.append("本次没有可分析的位姿（未开启「保存位姿」或全部对接失败），"
                     "因此不给出姿态–口袋结合分析。")
            L.append("")
    else:
        L.append("> 判定口径（**几何启发式**，全部来自真实坐标）：氢键 = 配体 N/O 与受体 N/O ≤ 3.5 Å；"
                 "盐桥 = 双方部分电荷 |q| ≥ 0.3 且符号相反、距离 ≤ 4.0 Å；疏水接触 = 双方碳 ≤ 4.2 Å；"
                 "π–π 堆叠 = 芳香碳对数 ≥ 3 且距离 ≤ 5.5 Å；金属配位 = 金属离子与配体 N/O ≤ 3.0 Å。"
                 "未做氢键角度/质子化方向校正与能量分解，用于判断「结合在哪里、以什么方式接触」，"
                 "不替代 MD/MM-GBSA（见第 6 节）。")
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
                L += figure_by_rel(f"charts/{item['figure_2d']}.png",
                                   f"#{item.get('rank')} {item.get('name')} 的 2D 相互作用图")
            if item.get("figure_3d"):
                L += figure_by_rel(f"charts/{item['figure_3d']}.png",
                                   f"#{item.get('rank')} {item.get('name')} 的 3D 结合姿态（含对接盒）")
            for warning in (item.get("warnings") or [])[:2]:
                L.append(f"> 注意：{warning}")
                L.append("")
        L.append("> 逐分子明细（每对接触原子的距离）见产物 `docking.json` 的 `pose_analysis`；"
                 "2D/3D 图同名的 PNG 可在「中间数据」里单独下载。")
        L.append("")
    if not binding_rows:
        L.append("本次未提供阳性对照，或未执行结合模式分析，本节无对照比较数据。")
        L.append("")
    else:
        L.append("> 方法学：Morgan(radius=2, 2048 bit) 与 MACCS keys 双指纹 Tanimoto 相似度 + "
                 "SMARTS 药效团锚定基团匹配（脒基/胍基/羧酸/磺酰胺/芳环）+ 理化性质差异；"
                 "结构一致性由双指纹综合判定。相似度高只说明结构相近，不等同于结合模式一致。")
        L.append("")
        L += figure_block("similarity_chart", "与阳性对照的指纹相似度（Morgan Tanimoto）")
        L += figure_block("binding_scatter", "结合模式：相似度 vs 亲和力（含对照亲和力参考线）")
        L += figure_block("structure_grid", "候选分子结构对比（含阳性对照，按亲和力排序）")
        L += table_block(
            "结合模式与阳性对照比较",
            ["分子", "Morgan 相似度", "MACCS 相似度", "结构一致性", "锚定基团匹配", "结合模式提示"],
            [[m.get("name") or m.get("smiles"),
              _num(_b(m).get("morgan_tanimoto", _b(m).get("similarity_to_positive_control")), 3),
              _num(_b(m).get("maccs_tanimoto"), 3),
              CONSISTENCY_ZH.get(str(_b(m).get("structural_consistency")),
                                 _b(m).get("structural_consistency") or "—"),
              "是" if _b(m).get("anchor_match") else
              ("否" if _b(m).get("anchor_match") is False else "—"),
              _b(m).get("binding_mode_hint", "—")]
             for m in ranking],
            aligns=["l", "r", "r", "c", "c", "l"])

    # ===================================================================== #
    # 6. 方法与局限
    # ===================================================================== #
    L.append("## 6. 方法与局限")
    L.append("")
    L.append("> 本节说明本流程「能回答什么、不能回答什么」。以下所有结论都只在本节条件下成立，"
             "用于候选优先级排序，不构成活性、成药性或安全性结论。")
    L.append("")
    L.append("**（1）对接引擎与打分函数**：使用 AutoDock Vina 经验性打分函数，输出为经验分数"
             f"（kcal/mol，本次 exhaustiveness={exh_val if exh_val is not None else '—'}）。"
             "分数用于同一流程内的相对排序，不是结合自由能，不能直接换算 Ki/Kd/IC50；"
             "Vina 对同一分子的不同构象也给出单点最优值，不含熵贡献。")
    L.append("")
    L.append("**（2）单盒与分组的可比性**：主组所有分子共用同一个对接盒；`large` 组因配体 3D 跨度"
             "超出主盒，改用同一中心、更大盒子单独重跑，两组分数不可跨组比较。"
             "实测（开发手册 §15.7b）同一配体仅改变盒边长（18/22/28/34 Å），分数最大相差 "
             "**1.34 kcal/mol** 且**非单调**，因此本流程不采用「每分子自适应盒子」，"
             "以免把盒子效应混进排序。")
    L.append("")
    L.append("**（3）搜索强度与盒子**：exhaustiveness 越低，构象采样越不充分，分数的不确定度越大；"
             "提高搜索强度可降低采样噪声，但不能修正打分函数的系统偏差。盒子越大，采样空间越大、"
             "耗时越高（耗时大致随盒体积增长），因此盒子由口袋范围与库级下限共同决定并全局统一。")
    L.append("")
    L.append("**（4）未执行的重打分与共识打分**：本次未做重打分（rescore）、共识打分（consensus）、"
             "MM/GBSA 或分子动力学验证，也没有做多引擎交叉打分；如需更高置信度，建议对优选分子"
             "补充上述计算。")
    L.append("")
    if prot["policy"]:
        policy_zh = {"neutralize": "中和带净电荷的分子", "keep": "保持输入形式",
                     "ph": _ph_policy_text(prot, suffix="用内置 pKa 规则表分配")}.get(
            prot["policy"], prot["policy"])
        ph_note = ("**本条尤其需要注意**：pH 处理是**官能团 pKa 规则近似**，"
                   "对酰胺/杂环等特殊环境的 pKa 偏移不做校正，也未做微观态分布采样；"
                   "定量结论前建议用专业工具核对关键分子的质子化态。"
                   if prot["policy"] == "ph" else
                   "若要按目标 pH 处理离子态，请把质子化态策略改为 `ph` 重跑。")
        L.append(f"**（5）质子化态按运行级策略统一处理（`{prot['policy']}`：{policy_zh}），"
                 "但未做质子化态/互变异构的**枚举与打分对比**："
                 f"本次 {len(prot['applied'])} 个分子的质子化态被调整，其余分子保持输入形式；"
                 "生理 pH 下的微观质子化态分布、互变异构体与金属配位可能不同，"
                 f"本次只按策略处理了质子化态这一层，未逐一枚举比较。{ph_note}"
                 "若输入 SMILES 缺少立体信息，配体准备阶段会在运行笔记中给出告警。")
    else:
        L.append("**（5）未做质子化态与互变异构枚举**：分子按输入 SMILES 的默认质子化态与给定立体化学"
                 "对接；生理 pH 下的微观质子化态、互变异构与金属配位可能不同，本次未枚举。"
                 "若输入 SMILES 缺少立体信息，配体准备阶段会在运行笔记中给出告警。")
    L.append("")
    receptor_state = ("受体按标准流程去水并保持刚性（未做柔性受体 / 诱导契合）；"
                      + ("质子化态按**目标 pH 重算**（与配体同一口径，见第 1.2 节）；"
                         if any((b.get("receptor_protonation") or {}).get("applied")
                                for b in (result.get("receptors") or []))
                         else "质子化态**未按目标 pH 重算**（来自受体文件/模板默认态，见第 1.2 节）；")
                      + "去辅因子（apo）或保留金属/辅因子的处理以第 1.1 节运行笔记为准，"
                      "本次未做「保留辅因子 vs 去除辅因子」的对照实验。")
    L.append(f"**（6）受体状态**：{receptor_state}")
    L.append("")
    stats = _failure_stats(result)
    if stats["failed"] or stats["skipped"]:
        L.append(f"**（7）覆盖范围**：本次输入 {stats['total']} 个分子，成功对接 {stats['success']} 个，"
                 f"失败/跳过 {stats['failed'] + stats['skipped']} 个；第 2–5 节与结论**只覆盖成功"
                 "对接的分子**，失败分子不代表无活性，详见第 7 节。")
    else:
        L.append(f"**（7）覆盖范围**：本次输入 {stats['total']} 个分子全部成功对接，"
                 "第 2–5 节与结论覆盖全部输入分子。")
    L.append("")
    L.append("**（8）结论口径**：本报告给出的是「在本流程条件下」的相对优先级，"
             "排序靠前不等于实验有效，排序靠后也不等于无效；任何推进决策都应结合实验验证。")
    L.append("")

    # ===================================================================== #
    # 7. 失败与跳过
    # ===================================================================== #
    L.append("## 7. 失败与跳过")
    L.append("")
    if not stats["failed"] and not stats["skipped"]:
        L.append(f"本次输入 {stats['total']} 个分子全部成功对接，无失败或跳过；"
                 "第 2–3 节的排序与推荐覆盖全部输入分子。")
        L.append("")
    else:
        L.append("> 本章按原因分组列出未获得有效对接分数的分子。**第 2–5 节的排序、推荐与结论"
                 "只覆盖成功对接的分子**；失败/跳过分子未参与排序与命中判断，"
                 "也不代表它们没有活性。")
        L.append("")
        fail_total = stats["failed"] + stats["skipped"]
        L.append(f"- 输入分子：{stats['total']} 个")
        L.append(f"- 成功对接：{stats['success']} 个（{_pct(stats['success'], stats['total'])}）")
        L.append(f"- 失败 / 跳过：{fail_total} 个（{_pct(fail_total, stats['total'])}）")
        if stats["skipped"]:
            L.append(f"- 其中未进入对接（解析/准备阶段跳过）：{stats['skipped']} 个")
        L.append("")
        L += table_block(
            "失败与跳过清单（按原因分组）",
            ["原因", "分子数", "占比", "示例分子"],
            [[g["reason"], g["count"], _pct(g["count"], stats["total"]),
              "、".join(g["names"][:3]) + ("…" if len(g["names"]) > 3 else "")]
             for g in stats["groups"]],
            aligns=["l", "r", "r", "l"])
        L.append("> 完整逐分子错误信息（`status` / `error`）见产物 `docking.json` 与 "
                 "`ranking.csv` 的对应列（第 9 节）。")
        L.append("")

    # ===================================================================== #
    # 8. 结论与建议
    # ===================================================================== #
    L.append("## 8. 结论与建议")
    L.append("")
    L.append("> 本报告结论均限定在**本流程条件**下（见第 6 节方法与局限），用于候选优先级排序，"
             "需经实验验证。")
    L.append("")
    # 只取协调 Agent 的**结论/建议**类小节：整段报告贴进来会让同一批数字出现两遍
    # （第 1–7 节已用真实表格写过），用户明确要求"报告中不要重复内容"。
    narrative = extract_agent_conclusions(agent_narrative)
    if narrative:
        L.append("> 以下为协调 Agent 的分析与结论（只摘录其**结论/建议**类小节，"
                 "数据与参数见第 1–7 节，不在此重复；完整原文见产物 `agent_report.md`）。")
        L.append("")
        L.append(_strip_urls(_demote_headings(narrative)))
    else:
        if hits:
            best = hits[0]
            best_delta = round(best["affinity_kcal_mol"] - pc_aff, 2)
            L.append(f"1. **优先推进 {best.get('name') or best.get('smiles')}**：在本流程条件下，"
                     f"其对接亲和力 {_num(best.get('affinity_kcal_mol'))} kcal/mol，"
                     f"较阳性对照强 {_num(abs(best_delta))} kcal/mol"
                     f"（Δ = {_num(best_delta)} kcal/mol）。")
        L.append("2. 建议结合类药性（Lipinski 违例）、与对照的结合模式相似度与锚定基团匹配情况"
                 "综合选择候选分子；排序靠前但相似度低、类药性差的分子应谨慎。")
        L.append("3. 如需进一步验证，可对优选分子提高 exhaustiveness / n_poses 复算以确认结果稳定性，"
                 "或引入其他受体/引擎做交叉筛选（本报告未做共识打分，见第 6 节）。")
        if stats["failed"] or stats["skipped"]:
            L.append(f"4. 本次有 {stats['failed'] + stats['skipped']} 个分子未获得有效分数"
                     "（见第 7 节），在评估覆盖度时需计入，必要时修正输入或重跑。")
    L.append("")

    # ===================================================================== #
    # 9. 数据与产物
    # ===================================================================== #
    L.append("## 9. 数据与产物")
    L.append("")
    L.append("本次运行的全部中间数据（分子库、理化性质、对接明细、结合模式、排序 CSV/JSON、"
             "图表 PNG、位姿文件）均已落盘，可在网页报告的「中间数据」页签中按文件名单独下载，"
             "或使用页面上的「打包下载全部」整包获取；报告内的图片来自 `charts/` 目录下的同名 PNG。")
    L.append("")
    if artifacts:
        L += table_block(
            "本次运行产物清单",
            ["产物文件", "说明", "大小"],
            [[f"`{a.get('name')}`", a.get("label") or "—", _artifact_size(artifacts, a.get("name"))]
             for a in artifacts],
            aligns=["l", "l", "r"])
    L.append("---")
    L.append("")
    L.append("*本报告所有数值均来自真实计算（RDKit 理化性质与指纹 / AutoDock Vina 对接），"
             "由系统按固定模板生成，未使用任何模拟或编造数据；图与表均为本次运行的真实产物。*")
    return _strip_urls("\n".join(L))
