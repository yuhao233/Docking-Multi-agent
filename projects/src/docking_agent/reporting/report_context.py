"""报告渲染的**只读上下文**与共用小工具。

`build_markdown_report()` 原本是一个 900+ 行的巨型函数：表格/图片编号、盒子溯源、
口袋章节、逐节排版全挤在同一个函数体里。这里把「算一次、多处复用」的数据集中到
`ReportContext`，各章节函数（见 `report_sections_setup.py` / `report_sections_results.py`）
只负责把该节的行拼出来，于是：

* 章节顺序仍是**固定**的，由 `report.py:build_markdown_report()` 显式列出；
* 图/表编号计数器（`fig` / `tbl`）挂在上下文上，跨章节仍然连续且缺图不占号；
* 每个章节函数都是纯函数式（输入上下文 → `List[str]`），可单独测试与复用。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from docking_agent.config import env_int
from docking_agent.core.docking import POSITIVE_CONTROL_NAME
from docking_agent.core.ranking import sort_by_affinity, split_box_groups
from docking_agent.reporting.content import (
    _box_size_text,
    _num,
    _seed_info,
    _table,
    _vec,
    failure_stats as _failure_stats,
    _param_plan,
    param_plan_lines as _param_plan_lines,
    protonation_summary,
)
from docking_agent.reporting.fields import REPORT_FIELD_LABELS
from docking_agent.reporting.recommend import build_recommendations
from docking_agent.reporting.tables import rank_molecules

logger = logging.getLogger(__name__)

# 图表产物名 → 本次运行目录内的相对路径（报告内嵌用相对路径；PDF 依此找回 PNG）。
# 这份映射是唯一事实源：`reporting/artifacts.py` 的 CHART_SPECS 直接复用它。
# 文件名与产物名一致（`charts/<name>.png`），网页报告页据此把相对路径解析成图片接口。
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
        out.append(f"  > **口径提示**：配体按目标 pH {ph_txt} 处理，上述受体未做同样处理，"
                   "两侧质子化条件**不完全一致**。如需一致，可提供已按目标 pH 备好的受体 "
                   "PDBQT/PQR，或用 `PDB2PQR_BIN` 指定可用的 pdb2pqr 让系统自动准备；"
                   "该局限已计入第 6 节。")
    return out


def _pocket_section_lines(result: Dict[str, Any], table_block: Any, figure_block: Any,
                          prot: Dict[str, Any]) -> List[str]:
    """`### 5.1 结合口袋分析`：说明本次用的是哪个口袋、依据是什么、里面有哪些残基。"""
    from docking_agent.core import interactions as I

    L: List[str] = ["### 5.1 结合口袋分析", ""]
    _printed_pocket_sigs: set = set()
    blocks = [b for b in (result.get("receptors") or []) if isinstance(b, dict)]
    if not blocks:
        L.append("本次没有受体信息，无法给出结合口袋分析。")
        L.append("")
        return L
    analysis = result.get("pocket_analysis") or {}
    pockets = result.get("pockets") or []
    # 多个受体块：要么是同一受体的多种准备（保留辅因子后重跑），要么是蛋白质库。
    # 两者都必须先说明，读者才不会把重复的盒子/残基当成排版错误。
    if len(blocks) > 1:
        centers = {tuple(b.get("box_center") or []) for b in blocks}
        if len(centers) == 1:
            L.append(f"> 本次对该受体有 {len(blocks)} 种准备（差异见 1.1 运行笔记与 1.2 质子化记录），"
                     "下面逐准备列出盒子与盒内残基。")
        else:
            L.append(f"> 本次对接了 {len(blocks)} 个受体，下面逐个列出盒子与盒内残基。")
        L.append("")
    for block in blocks:
        label = block.get("receptor") or block.get("receptor_key") or "受体"
        source = str(block.get("box_source") or (block.get("site") or {}).get("source") or "—")
        center = block.get("box_center") or []
        size = block.get("box_size") or []
        L.append(f"**受体 {label}**")
        L.append("")
        L.append(f"- 盒子来源：{source}")
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
            if aromatic or charged:
                L.append("  - 芳香残基偏 π–π/疏水作用，带电残基与配体质子化态的互补性影响盐桥/氢键；"
                         "逐分子的实际接触见 5.2 节。")
        # 口袋预测结果（有则给表格：多个候选口袋的评分与残基）
        receptor_key = str(block.get("receptor_key") or "")
        pocket_rows = [p for p in pockets
                       if not receptor_key or str(p.get("receptor_key") or receptor_key) == receptor_key]
        # 同一套口袋在多受体块里会完全重复（同结构的不同准备）→ 只印一次
        pocket_sig = tuple((p.get("rank"), p.get("name"), p.get("score")) for p in pocket_rows)
        if pocket_rows and pocket_sig not in _printed_pocket_sigs:
            _printed_pocket_sigs.add(pocket_sig)
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
            L.append(f"- 准备阶段丢弃的模板不匹配残基：{'、'.join(str(x) for x in dropped_bad[:10])}"
                     "（未进入对接）")
        L.append("")
    if analysis.get("engine"):
        engine_txt = str(analysis.get("engine"))
        L.append(f"> 口袋引擎：`{engine_txt}`"
                 + ("" if engine_txt.startswith("geometric") else "")
                 + "；口袋坐标与残基清单为工具输出。")
        L.append("")
    return L


def _receptor_provenance_text(result: Dict[str, Any]) -> str:
    """「受体来源」一行：哪个数据库、accession/物种、哪个结构、什么方法/精度。

    为什么单独一行：报告要能脱离对话独立交付。早期这份溯源只出现在协调 Agent 的
    「实际执行」小节里，而结论节按「只留结论/风险」精简后 —— 溯源就丢了。
    """
    prov = result.get("receptor_provenance") or {}
    if not isinstance(prov, dict) or not prov:
        return ""
    parts: List[str] = []
    db = str(prov.get("database") or ("RCSB PDB" if prov.get("structure_source") == "rcsb"
                                      else ("AlphaFold DB" if prov.get("structure_source") == "alphafold"
                                            else "")))
    acc, org, protein = (str(prov.get("accession") or ""), str(prov.get("organism") or ""),
                         str(prov.get("protein") or ""))
    if acc or org or protein:
        detail = "，".join(x for x in (protein, org) if x)
        parts.append(f"UniProt `{acc}`" + (f"（{detail}）" if detail else "") if acc
                     else (detail or ""))
    pdb_id = str(prov.get("pdb_id") or prov.get("entry_id") or "")
    struct = f"{db} `{pdb_id}`" if pdb_id else db
    meta = "，".join(x for x in (str(prov.get("structure_method") or ""),
                                 str(prov.get("structure_resolution") or "")) if x)
    if meta:
        struct += f"（{meta}）"
    if str(prov.get("alphafold_model_version") or ""):
        struct += f"（模型版本 {prov['alphafold_model_version']}）"
    if struct:
        parts.append("结构 " + struct)
    requested = str(prov.get("requested") or "")
    if requested and requested.upper() != pdb_id.upper():
        parts.append(f"检索词「{requested}」")
    return "；".join(p for p in parts if p)


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
            return "用户选择不使用"
        return "未检测到"
    label = str(offer.get("label") or offer.get("resname") or "")
    smiles = str(offer.get("smiles") or "")
    if decision == "use":
        return (f"检测到 {label} → 用户选择用作阳性对照"
                + (f"（SMILES `{smiles}`）" if smiles else ""))
    if decision == "skip":
        return f"检测到 {label} → 用户选择不使用"
    return f"检测到 {label} → 已询问，本次运行未见用户选择"


class ReportContext:
    """一次报告渲染的只读上下文：所有「算一次」的派生数据与图/表编号都挂在这里。

    各章节函数只读本对象，不修改它；图/表编号计数器是唯一的可变状态
    （`fig_caption` / `tbl_caption` 递增），因此章节必须**按固定顺序**渲染。
    """

    def __init__(self, result: Dict[str, Any], *, kind: str = "agent", run_id: str = "",
                 receptor_label: str = "", site: Optional[Dict[str, Any]] = None,
                 artifacts: Optional[List[Dict[str, Any]]] = None,
                 agent_narrative: str = "",
                 agent_models: Optional[Dict[str, Any]] = None,
                 created_at: str = "", top_n: int = 0) -> None:
        self.result = result
        self.kind = kind
        self.run_id = run_id
        self.receptor_label = receptor_label
        self.site = site
        self.artifacts = artifacts
        self.agent_narrative = agent_narrative
        self.agent_models = agent_models
        self.created_at = created_at
        self.top_n = top_n

        # ---- 排序、截断与对照 ----
        self.full_ranking = rank_molecules(result.get("ranking") or [])
        limit = top_n or env_int("REPORT_TOP_N", 50)
        self.ranking = self.full_ranking[:max(1, limit)] if limit > 0 else self.full_ranking
        self.truncated = len(self.full_ranking) > len(self.ranking)
        self.binding_rows = {r.get("smiles"): r
                             for r in (result.get("binding") or {}).get("rows", [])}
        self.pc = result.get("positive_control") or {}
        self.notes = result.get("notes") or []
        self.molecules = result.get("molecules") or []
        self.pc_aff = self.pc.get("affinity_kcal_mol")
        self.spec = result.get("task_spec") or {}
        self.hits = [m for m in self.full_ranking
                     if isinstance(m.get("affinity_kcal_mol"), (int, float))
                     and isinstance(self.pc_aff, (int, float))
                     and m["affinity_kcal_mol"] < self.pc_aff]
        # 运行模式文案：对接统一由 Agent 驱动（记录 60 移除流水线模式），
        # 历史运行目录里可能仍是旧 kind，保留映射以便旧报告照原样复看。
        self.mode_zh = {"agent": "多 Agent 协作", "studio": "多 Agent 协作（Studio）",
                        "pipeline": "确定性流水线（历史记录）"}.get(
            str(kind or "agent"), "多 Agent 协作")
        self.available = {str(a.get("name")) for a in (artifacts or []) if isinstance(a, dict)}

        # ---- 协调 Agent 的定制（标题 / 附加列 / 要点 / 要求与响应）----
        custom = result.get("report_customization") or {}
        self.custom = custom if isinstance(custom, dict) else {}
        self.requested_columns = [str(c) for c in (self.custom.get("extra_columns") or [])
                                  if str(c) in REPORT_FIELD_LABELS]
        # 数据自带分子 ID（来自输入文件、且与名称不同）→ 默认也带上 ID 列：
        # 报告应当跟着**本次数据**走，而不是永远只印固定的那几列。
        _id_rows = [r for r in self.full_ranking if str(r.get("id") or "").strip()]
        _id_distinct = any(str(r.get("id")) != str(r.get("name")) for r in _id_rows)
        self.extra_columns = list(self.requested_columns)
        if "id" not in self.extra_columns and _id_rows and _id_distinct:
            self.extra_columns = ["id"] + self.extra_columns

        # ---- 图/表编号：按出现顺序连续编号（缺图/缺表时不占号，编号始终连续）----
        self.counters: Dict[str, int] = {"fig": 0, "tbl": 0}

        # ---- 章节里固定要用的派生量（原实现按出现顺序就地计算，这里集中算好）----
        self.center_txt, self.size_txt = self._box_center_size()
        self.seed_value, self.seed_policy = _seed_info(result)
        self.prot = protonation_summary(self.full_ranking)
        self.large_rows = self._large_box_rows()
        self.plan = result.get("param_plan") or {}
        self.engine_val = ((self.ranking[0].get("engine") if self.ranking else None)
                           or self.plan.get("engine") or "—")
        self.exh_val = (self.ranking[0].get("exhaustiveness") if self.ranking else None)
        if self.exh_val is None:
            self.exh_val = self.plan.get("exhaustiveness")
        self.nposes = (self.ranking[0].get("n_poses") if self.ranking else None)
        if self.nposes is None:
            self.nposes = self.plan.get("n_poses")
        self.two_stage = bool(self.plan.get("two_stage"))
        self.concurrency = (result.get("docking") or {}).get("concurrency")
        self.stats = _failure_stats(result)
        self.rec = build_recommendations(
            self.full_ranking, top_n=max(1, env_int("RECOMMEND_TOP_N", 10)),
            reasons=result.get("agent_recommendations") or [],
            positive_control_name=POSITIVE_CONTROL_NAME)
        self.pose_by_name = {str(item.get("name") or ""): item
                             for item in (result.get("pose_analysis") or [])}

    # ------------------------------------------------------------------ #
    # 图 / 表
    # ------------------------------------------------------------------ #
    def fig_caption(self, title: str) -> str:
        self.counters["fig"] += 1
        return f"图 {self.counters['fig']} {title}"

    def tbl_caption(self, title: str) -> str:
        self.counters["tbl"] += 1
        return f"表 {self.counters['tbl']} {title}"

    def table_block(self, title: str, headers: List[str], rows: List[List[Any]],
                    aligns: Optional[List[str]] = None) -> List[str]:
        return [f"**{self.tbl_caption(title)}**", "", _table(headers, rows, aligns), ""]

    def figure_block(self, name: str, title: str, note: str = "") -> List[str]:
        """图：图片 + 图题（缺产物时不嵌图，也不占用图号）。"""
        rel = CHART_FILES.get(name)
        if not rel or (self.available and name not in self.available):
            return []
        caption = self.fig_caption(title)
        out = [f"![{caption}]({rel})", ""]
        if note:
            out += [note, ""]
        out += [f"**{caption}**", ""]
        return out

    def figure_by_rel(self, rel: str, title: str) -> List[str]:
        """按**相对路径**内嵌图片（用于按名次动态生成的 2D/3D 姿态图）。

        与 `figure_block` 的区别：这些图的产物名在编译期未知（`interaction_2d_01` 等），
        因此按「文件是否真的在产物清单里」判断，避免引用不存在的图。
        """
        name = rel.rsplit("/", 1)[-1][:-4]
        if self.available and name not in self.available:
            return []
        caption = self.fig_caption(title)
        return [f"![{caption}]({rel})", "", f"**{caption}**", ""]

    # ------------------------------------------------------------------ #
    # 数据取用
    # ------------------------------------------------------------------ #
    def binding_row(self, m: Dict[str, Any]) -> Dict[str, Any]:
        """取结合模式分析里对应分子的行（没有则退回对接结果行本身）。"""
        return self.binding_rows.get(m.get("smiles")) or m

    # ---- 对接盒与位点溯源 ----
    def _box_source(self) -> str:
        for block in ((self.result.get("docking") or {}).get("receptors") or []):
            if isinstance(block, dict) and (block.get("box_source") or (block.get("site") or {})):
                return str(block.get("box_source") or (block.get("site") or {}).get("source") or "—")
        if self.site:
            return str(self.site.get("source") or "—")
        return "—"

    def box_source(self) -> str:
        return self._box_source()

    def _box_center_size(self) -> Tuple[str, str]:
        for block in ((self.result.get("docking") or {}).get("receptors") or []):
            if isinstance(block, dict) and (block.get("box_center") or block.get("box_size")):
                return _vec(block.get("box_center")), _box_size_text(block.get("box_size"))
        if self.site:
            return _vec(self.site.get("center")), _box_size_text(self.site.get("size"))
        return "—", "—"

    def _floor(self) -> Dict[str, Any]:
        for block in ((self.result.get("docking") or {}).get("receptors") or []):
            if isinstance(block, dict) and block.get("box_library_floor"):
                return dict(block.get("box_library_floor") or {})
        return {}

    def _floor_text(self) -> str:
        floor = self._floor()
        if not floor:
            return "未记录"
        if floor.get("enabled") is False:
            return "已关闭（`BOX_SPAN_ENABLED=off`）"
        bound = _num(floor.get("bound"), 1)
        if floor.get("p95_span") is not None:
            return (f"**{bound} Å**（库内 P95 跨度 {_num(floor.get('p95_span'), 1)} Å，抽样 "
                    f"{floor.get('sample_n')}/{floor.get('library_n')} 个）；"
                    + ("**盒子因此扩大**" if floor.get("raised") else "盒子未因此改变"))
        return f"{bound} Å（未取得跨度样本，退回口袋驱动值）"

    def floor_text(self) -> str:
        return self._floor_text()

    def _large_box_rows(self) -> List[Dict[str, Any]]:
        """大配体组的结果行（盒子不同，单独一节展示，不并入主排序榜）。"""
        rows: List[Dict[str, Any]] = []
        for block in ((self.result.get("docking") or {}).get("receptors") or []):
            _main, large = split_box_groups(block.get("results") or [])
            rows.extend(large)
        return sort_by_affinity(rows)

    def pocket_table(self) -> List[str]:
        """口袋预测结果表（工具输出，含被采用的那个）。"""
        blocks = (self.result.get("docking") or {}).get("receptors") or []
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
        out = ["### 1.6 结合口袋预测", ""]
        out += self.table_block(
            "结合口袋预测（工具预测，不代替实验定位）",
            ["#", "口袋", "score", "中心 (Å)", "范围 (Å)", "附近残基", "选择"],
            rows, aligns=["r", "l", "r", "l", "l", "l", "c"])
        out += [f"> 引擎：`{engine or '—'}`；实际使用的盒见 1.2。", ""]
        return out

    def models_cell(self) -> Optional[str]:
        """各 Agent 角色实际使用的模型（同一角色可能用了不同模型端点）。"""
        if not self.agent_models:
            return None
        role_zh = _ROLE_ZH
        parts = []
        for role, meta in self.agent_models.items():
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

    def param_plan_lines(self) -> List[str]:
        """「1.5 参数自动规划」：没有规划内容时返回空列表（不占章节）。"""
        if not _param_plan(self.result):
            return []
        return _param_plan_lines(self.result, "### 1.5 参数自动规划",
                                 self.tbl_caption("对接参数自动规划结果"))
