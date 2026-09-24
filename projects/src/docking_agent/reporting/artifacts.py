"""报告产物：把整套图表与 PDF 报告写入运行目录并登记为可下载产物。

两种运行模式（确定性流水线与多 Agent）共用，保证固定格式报告里内嵌的图片
在两种模式下都真实存在、可下载。

不导入 `docking_agent.runs`（避免与 runs -> reporting.store 形成循环依赖），
只要求传入的对象具备 `write_bytes(rel, data, name=, label=, content_type=)` 接口。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from docking_agent.core.docking import POSITIVE_CONTROL_NAME
from docking_agent.reporting.charts import (
    affinity_histogram,
    binding_interaction_map_2d,
    binding_pose_3d,
    binding_scatter,
    docking_bar_chart,
    property_scatter_chart,
    recommendation_structure_grid,
    similarity_chart,
    structure_grid,
)
from docking_agent.reporting.recommend import build_recommendations
from docking_agent.reporting.pdf import build_report_pdf
from docking_agent.reporting.report import CHART_FILES, build_markdown_report

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 受体结构入包：整包下载必须能独立复现本次对接
# --------------------------------------------------------------------------- #
_STRUCTURE_EXTS = (".pdb", ".ent", ".cif", ".mmcif", ".pdbqt")


def _resolve_candidate(raw: str) -> Optional[Path]:
    """把记录里的路径解析成存在的文件（支持绝对路径与项目相对路径）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith(("http://", "https://")):
        return None
    path = Path(text)
    if path.is_file():
        return path
    if not path.is_absolute():
        from docking_agent.paths import project_root

        alt = project_root() / text
        if alt.is_file():
            return alt
    return None


def copy_receptor_files(run: Any, receptors: List[Dict[str, Any]], *,
                        source_candidates: Any = ()) -> List[str]:
    """把本次对接**真正用到的受体结构**复制进运行目录（`receptor/`），随整包下载。

    为什么必须复制：`download.zip` 原本只有配体位姿与 JSON，受体结构只以**绝对路径**
    记在 `docking.json` 里（指向 `assets/receptors/...` 或 `assets/cache/...`）——
    换台机器、换个人拿到的包根本无法复现这次对接（用户实测提问）。

    每个受体放三类文件（取得到才放）：
      `receptor/<key>.pdbqt`        —— 对接实际使用的受体（AutoDock 输入，权威）
      `receptor/<key>_prepared.pdb` —— 准备阶段去水/去杂原子后的蛋白（PDB 文本，便于看图核对）
      `receptor/<key>_source.<ext>` —— 原始结构：上传的原文件 / URL 下载的原文件；
                                       注册表受体没有原文件时就不放这一类

    复制是**尽力而为**：任何一个文件取不到只记 warning，绝不影响运行与报告。
    返回登记成功的产物名列表。
    """
    added: List[str] = []
    blocks = [b for b in (receptors or []) if isinstance(b, dict)]
    if not blocks:
        return added

    dest_dir = Path(getattr(run, "dir", "")) / "receptor"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("创建受体目录失败（跳过受体入包）：%s", e)
        return added

    source_list = [c for c in (source_candidates or []) if str(c or "").strip()]
    for index, block in enumerate(blocks):
        key = str(block.get("receptor_key") or block.get("receptor") or f"receptor{index + 1}")
        key = _safe_key(key)
        label_name = str(block.get("receptor") or key)

        # 1) 对接实际使用的 PDBQT
        pdbqt_src = _resolve_candidate(str(block.get("pdbqt") or ""))
        if pdbqt_src is not None:
            added += _copy_one(run, pdbqt_src, dest_dir / f"{key}.pdbqt",
                               f"receptor_pdbqt_{key}",
                               f"受体结构（对接实际使用 · {label_name}）")
            # 2) 准备阶段产出的 PDB（内容寻址命名，按 pH 准备时还会多一层 _ph7.4 后缀）
            prepared = _find_prepared_pdb(pdbqt_src)
            if prepared is not None:
                added += _copy_one(run, prepared, dest_dir / f"{key}_prepared.pdb",
                                   f"receptor_prepared_{key}",
                                   f"受体结构（准备后蛋白 · 去水/去杂原子 · {label_name}）")
        else:
            logger.warning("受体 %s：找不到对接使用的 PDBQT（%s），未入包",
                           key, block.get("pdbqt"))

        # 3) 原始结构：只给第一个受体带（多受体时其余受体的原始文件通常不同源）
        if index == 0:
            for raw in source_list:
                candidate = _resolve_candidate(raw)
                if candidate is None:
                    continue
                if pdbqt_src is not None and candidate.resolve() == pdbqt_src.resolve():
                    continue
                if candidate.suffix.lower() not in _STRUCTURE_EXTS:
                    continue
                added += _copy_one(run, candidate,
                                   dest_dir / f"{key}_source{candidate.suffix.lower()}",
                                   f"receptor_source_{key}",
                                   f"受体结构（原始文件 · {candidate.name}）")
                break
    if added:
        logger.info("受体结构已入包：%s", "、".join(added))
    return added


def _find_prepared_pdb(pdbqt: Path) -> Optional[Path]:
    """找出与某个 PDBQT 对应的「准备后蛋白 PDB」。

    准备阶段的产物是内容寻址命名的：`<base>_<hash>[_ph7.4].pdbqt` 旁边的
    `<base>_<hash>_prot.pdb`（去水/去杂原子后的蛋白）。按 pH 质子化时会多一层
    `_ph7.4` 后缀，所以不能只做一次 `stem + '_prot.pdb'` 拼接。
    """
    import re as _re

    stem = pdbqt.stem
    candidates = [pdbqt.with_name(stem + "_prot.pdb")]
    base = _re.sub(r"(_ph[0-9.]+|_in|_meeko|_prepared)$", "", stem)
    if base != stem:
        candidates.append(pdbqt.with_name(base + "_prot.pdb"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # 兜底：同目录里以受体基名开头的 *_prot.pdb（避免因准备阶段改了后缀而漏掉）
    try:
        prefix = base.split("_")[0]
        for found in sorted(pdbqt.parent.glob(prefix + "*_prot.pdb")):
            if found.is_file():
                return found
    except OSError:
        return None
    return None

def _safe_key(text: str) -> str:
    """受体名 → 安全文件名（与 core.files.slug 同口径，避免上下层重复实现）。"""
    from docking_agent.core.files import slug

    return slug(text) or "receptor"


def _copy_one(run: Any, src: Path, dest: Path, name: str, label: str) -> List[str]:
    import shutil

    try:
        shutil.copyfile(src, dest)
    except OSError as e:
        logger.warning("复制受体文件失败（%s → %s）：%s", src, dest, e)
        return []
    rel = str(dest.relative_to(Path(run.dir)))
    try:
        run.add_artifact(rel, name, label)
    except Exception as e:  # noqa: BLE001 - 登记失败不能影响运行
        logger.warning("登记受体产物失败（%s）：%s", rel, e)
        return []
    return [name]


# name -> (相对路径, 中文说明, 生成函数(ranking, pos_control, top_rows) -> bytes|None)
# 相对路径统一来自 report.CHART_FILES：报告内嵌的相对图路径与这里落盘的产物路径不会漂移。
# top_rows = 推荐排行前 N 个分子（与报告第 3.1 节同一集合）：理化性质空间图只画它们，
# 2D 结构图也据此绘制 —— 整库散点在大库时会糊成一团，用户明确要求"只展示 top 的分子"。
CHART_SPECS = {
    "docking_chart": (CHART_FILES["docking_chart"], "对接亲和力对比图",
                      lambda r, pc, top: docking_bar_chart(r, pc)),
    "similarity_chart": (CHART_FILES["similarity_chart"], "阳性对照相似度图",
                         lambda r, pc, top: similarity_chart(r)),
    "affinity_histogram": (CHART_FILES["affinity_histogram"], "亲和力分布直方图",
                           lambda r, pc, top: affinity_histogram(r, pc)),
    "property_chart": (CHART_FILES["property_chart"], "理化性质空间图（仅推荐排行前 N 个）",
                       lambda r, pc, top: property_scatter_chart(top or r, top_only=bool(top))),
    # 结构对比图按**推荐分子**（用户要求：报告展示的部分也用推荐的分子）；
    # 没有推荐排行时退回对接排序表（`structure_grid` 自身按传入顺序绘制）
    "structure_grid": (CHART_FILES["structure_grid"], "候选分子结构对比（含阳性对照，按推荐排行）",
                       lambda r, pc, top: structure_grid(
                           top or r, pc, top_n=(len(top) if top else 12))),
    "binding_scatter": (CHART_FILES["binding_scatter"], "结合模式散点图（相似度 vs 亲和力）",
                        lambda r, pc, top: binding_scatter(r, pc)),
    "recommend_chart": (CHART_FILES["recommend_chart"], "推荐化合物 2D 结构图（综合分降序）",
                        lambda r, pc, top: recommendation_structure_grid(top)),
}


def _write_binding_analysis(run: Any, result: Optional[Dict[str, Any]],
                            top_rows: List[Dict[str, Any]],
                            *, top_n: Optional[int] = None,
                            ranking: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """为推荐排行前 N 个分子做姿态–口袋相互作用分析 + 2D/3D 图（真实坐标）。

    - 数据来源：**对接真实写出的位姿**（`poses/pose_*.pdbqt`）+ 受体 PDBQT；
    - 产物命名：`charts/interaction_2d_01.png` / `charts/pose_3d_01.png`（序号 = 推荐名次），
      与产物名一致，网页与 PDF 都能按相对路径解析；
    - 结果写回 `result["pose_analysis"]`（含逐残基明细），报告 §5.2 与推荐理由都引用它。
    - 关闭了「保存位姿」时**没有位姿可分析**：如实写进 `result["pose_analysis_note"]`，不编造。
    """
    from docking_agent.core import interactions as I

    if result is None:
        return []
    blocks = [b for b in (result.get("receptors") or []) if isinstance(b, dict)]
    receptor_pdbqt = ""
    box: Dict[str, Any] = {}
    if blocks:
        receptor_pdbqt = str(blocks[0].get("pdbqt") or "")
        box = {"box_center": blocks[0].get("box_center"), "box_size": blocks[0].get("box_size")}
    if not receptor_pdbqt or not os.path.isfile(receptor_pdbqt):
        result["pose_analysis_note"] = "缺少可读的受体 PDBQT，未做姿态–口袋分析"
        return []

    # 推荐排行行只保留评分需要的字段（不含 pose_file）→ 按 smiles 回查完整对接行拿位姿路径
    pose_by_smiles = {str(r.get("smiles") or ""): str(r.get("pose_file") or "")
                      for r in (ranking or []) if isinstance(r, dict)}
    candidates = list(top_rows or [])[:int(top_n or 0) or len(top_rows or [])]
    analyses: List[Dict[str, Any]] = []
    written: List[str] = []
    missing_pose = 0
    for index, row in enumerate(candidates, start=1):
        pose = str(row.get("pose_file") or pose_by_smiles.get(str(row.get("smiles") or "")) or "")
        if not pose or not os.path.isfile(pose):
            missing_pose += 1
            continue
        analysis = I.analyze_pose_pocket(receptor_pdbqt, pose)
        if analysis.get("status") != "ok":
            continue
        name = str(row.get("name") or row.get("smiles") or f"候选{index}")
        tag = f"{index:02d}"
        figures: Dict[str, str] = {}
        png2d = binding_interaction_map_2d(
            analysis, pose_path=pose,
            title=f"#{index} {name}｜{row.get('affinity_kcal_mol')} kcal/mol")
        if png2d:
            rel = f"charts/interaction_2d_{tag}.png"
            run.write_bytes(rel, png2d, name=f"interaction_2d_{tag}",
                            label=f"{name} 2D 相互作用图", content_type="image/png")
            figures["figure_2d"] = f"interaction_2d_{tag}"
            written.append(f"interaction_2d_{tag}")
        png3d = binding_pose_3d(receptor_pdbqt, pose, analysis, title=f"#{index} {name}",
                               box_center=box.get("box_center"), box_size=box.get("box_size"))
        if png3d:
            rel = f"charts/pose_3d_{tag}.png"
            run.write_bytes(rel, png3d, name=f"pose_3d_{tag}",
                            label=f"{name} 3D 姿态图", content_type="image/png")
            figures["figure_3d"] = f"pose_3d_{tag}"
            written.append(f"pose_3d_{tag}")
        analyses.append({
            "rank": index, "name": name, "smiles": row.get("smiles"),
            "affinity_kcal_mol": row.get("affinity_kcal_mol"),
            "pose_file": os.path.basename(pose),
            "summary": analysis.get("summary") or {},
            "residues": analysis.get("residues") or [],
            "pocket_residues": analysis.get("pocket_residues") or [],
            "warnings": analysis.get("warnings") or [],
            **figures,
        })

    if analyses:
        result["pose_analysis"] = analyses
    if missing_pose:
        result["pose_analysis_note"] = (
            f"有 {missing_pose} 个推荐分子没有可用位姿文件（本次运行关闭了「保存位姿」？）——"
            "因此这些分子没有姿态–口袋结合分析；在设置或表单里打开「保存位姿」后重跑即可获得。")
        logger.info("姿态–口袋分析：%s", result["pose_analysis_note"])
    if not analyses and "pose_analysis_note" not in result:
        result["pose_analysis_note"] = "没有可用于姿态–口袋分析的位姿文件"
    return written


def recommendation_rows(run: Any, ranking: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """推荐排行的前 N 行（与报告第 3.1 节口径一致：同一权重、同一理由、同一 top_n）。"""
    from docking_agent.config import env_int

    reasons = ((getattr(run, "data", None) or {}).get("recommendation_reasons") or [])
    try:
        rec = build_recommendations(
            ranking, top_n=max(1, env_int("RECOMMEND_TOP_N", 10)),
            reasons=[r for r in reasons if isinstance(r, dict)],
            positive_control_name=POSITIVE_CONTROL_NAME)
    except Exception as e:  # noqa: BLE001 - 排行失败不该拖垮整个报告产物
        logger.warning("推荐排行计算失败（报告图表将退化为整库口径）：%s", e)
        return []
    return list(rec.get("rows") or [])


def write_report_charts(run: Any, ranking: List[Dict[str, Any]],
                        pos_control: Optional[Dict[str, Any]] = None, *,
                        result: Optional[Dict[str, Any]] = None,
                        top_n: Optional[int] = None) -> List[str]:
    """生成并登记全部图表，返回成功写入的产物名列表。

    传入 `result` 时额外做**姿态–口袋结合分析**（真实坐标）：为推荐排行前 N 个分子各出
    一张 2D 相互作用图与一张 3D 姿态图，并把逐残基相互作用明细写回
    `result["pose_analysis"]`，供报告与协调 Agent 引用（不传 result 时行为不变）。
    """
    written: List[str] = []
    top_rows = recommendation_rows(run, ranking or [])
    try:
        written += _write_binding_analysis(run, result, top_rows, top_n=top_n, ranking=ranking)
    except Exception as e:  # noqa: BLE001 - 结合分析失败不影响其它图表与报告
        logger.warning("生成姿态–口袋结合分析产物失败（已跳过）：%s", e)
    for name, (rel, label, builder) in CHART_SPECS.items():
        try:
            png = builder(ranking or [], pos_control or {}, top_rows)
        except Exception as e:  # noqa: BLE001
            logger.warning("生成图表 %s 失败：%s", name, e)
            continue
        if not png:
            continue  # 例如无对照数据时的结合模式图
        run.write_bytes(rel, png, name=name, label=label, content_type="image/png")
        written.append(name)
    written += _write_recommend_cards(run, top_rows)
    return written


def _write_recommend_cards(run: Any, top_rows: List[Dict[str, Any]]) -> List[str]:
    """为推荐排行前 N 名各写一张「2D 结构 + 指标表」卡片图（报告 3.1 节挨着用）。

    用户要求：推荐化合物排行榜里，结构图要**就在数据旁边**。markdown/PDF 都没有
    「图片放进表格单元格」的可靠画法，所以把「结构 + 指标」画成一张卡片图，
    网页、PDF 与离线包三处长得完全一样。
    """
    from docking_agent.reporting.cards import recommendation_card

    written: List[str] = []
    for index, row in enumerate([r for r in (top_rows or []) if r.get("smiles")], start=1):
        name = f"recommend_card_{index:02d}"
        try:
            png = recommendation_card(row, index=index)
        except Exception as e:  # noqa: BLE001 - 卡片失败不影响其它产物
            logger.warning("生成推荐卡片 %s 失败：%s", name, e)
            continue
        if not png:
            continue
        label = f"推荐 #{row.get('rank') or index} {row.get('name') or ''}：2D 结构 + 关键指标"
        run.write_bytes(f"charts/{name}.png", png, name=name, label=label, content_type="image/png")
        written.append(name)
    return written


def _chart_paths(run: Any) -> Dict[str, Path]:
    """已登记图片产物 → 本地路径；清单里没有时回退扫 charts/ 目录。"""
    paths: Dict[str, Path] = {}
    try:
        artifacts = run.artifacts()
    except Exception as e:  # noqa: BLE001
        logger.warning("读取产物清单失败，PDF 将无法内嵌已登记图片：%s", e)
        artifacts = []
    for art in artifacts or []:
        if not str(art.get("content_type") or "").startswith("image/"):
            continue
        path = Path(run.dir) / str(art.get("path") or "")
        if path.is_file():
            paths[str(art.get("name"))] = path
    if not paths:
        for name, (rel, _label, _builder) in CHART_SPECS.items():
            path = Path(run.dir) / rel
            if path.is_file():
                paths[name] = path
    return paths


def _receptor_label(result: Dict[str, Any]) -> str:
    blocks = result.get("receptors") or []
    if blocks and isinstance(blocks[0], dict):
        return str(blocks[0].get("receptor") or "")
    return ""


def write_report_pdf(run: Any, result: Dict[str, Any], *, markdown: str = "") -> bool:
    """生成 PDF 版报告并登记为 `report_pdf` 产物。

    为什么失败只记 warning：PDF 是「附加交付物」，渲染依赖字体/图片等环境因素；
    它出问题不应该让已经算完的对接结果整体判为失败，用户仍可下载 CSV/Markdown。
    返回是否成功，便于测试与调用方记录，但都不要据此中断主流程。
    """
    try:
        text = markdown
        if not text:
            report_md = Path(run.dir) / "report.md"
            if report_md.is_file():
                # 直接渲染落盘的 report.md：保证 PDF 与页面上看到的报告内容完全一致
                text = report_md.read_text(encoding="utf-8")
        run_kind = str(getattr(run, "kind", "agent") or "agent")
        run_id = str(getattr(run, "id", "") or "")
        data = getattr(run, "data", {}) or {}
        # 流水线在写产物之后才 run.set(molecule_count=...)，这里直接从 result 兜底，
        # 否则 PDF 封面的「候选分子数」会在本次运行里显示成未知。
        molecule_count = int(data.get("molecule_count") or 0)
        if not molecule_count:
            molecule_count = len(result.get("molecules") or [])
        if not text:
            text = build_markdown_report(result, kind=run_kind, run_id=run_id,
                                         receptor_label=_receptor_label(result),
                                         artifacts=run.artifacts(),
                                         created_at=str(data.get("created_at") or ""))
        pdf = build_report_pdf(
            result, kind=run_kind, run_id=run_id,
            receptor_label=_receptor_label(result),
            created_at=str(data.get("created_at") or ""),
            markdown=text, chart_paths=_chart_paths(run),
            molecule_count=molecule_count)
        run.write_bytes("report.pdf", pdf, name="report_pdf",
                        label="分析报告（PDF）", content_type="application/pdf")
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("生成 PDF 报告失败（已跳过，不影响主流程；可稍后重试或在页面下载 CSV）：%s", e)
        return False
