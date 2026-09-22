"""姿态–口袋分析工具（协调 Agent 用）。

回答两个用户常问的问题，**全部基于真实坐标**：

1. 「这个口袋是什么口袋？」——盒子来源与依据、口袋引擎与评分、盒内残基清单与组成特征
   （芳香/疏水/带电/极性），以及与实验位点（共晶配体/注册位点）的一致性；
2. 「这些推荐分子是怎么结合上去的？」——每个分子**最优位姿**与受体的逐残基接触：
   氢键/盐桥/疏水接触/π 堆积/金属配位、最近距离、关键残基，
   并指向报告第 5.2 节的 2D 相互作用图与 3D 姿态图（由报告产物生成）。

判定口径是几何启发式（阈值写进返回值与报告），工具只报事实；结论与取舍由协调 Agent 写。
没有位姿（关闭了「保存位姿」）时**如实说明**，不臆测结合方式。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

from langchain.tools import tool

from docking_agent.runtime import tool_io
from docking_agent.core import POSITIVE_CONTROL_NAME
from docking_agent.core import interactions as I
from docking_agent.runtime.context import AgentContext, active_blackboard, active_run
from langchain.tools import ToolRuntime

logger = logging.getLogger(__name__)

#: 每个分子最多回传的残基条数（给模型看视图，完整明细在产物里）
RESIDUE_LIMIT = 6
#: 最多分析多少个分子（默认跟随设置里的推荐排行条数）
MAX_MOLECULES = 20


def _default_top_n() -> int:
    try:
        from docking_agent.config import env_int

        return max(1, env_int("RECOMMEND_TOP_N", 10))
    except Exception:  # noqa: BLE001
        return 10


def _flatten_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        rows: List[Dict[str, Any]] = []
        for block in payload.get("receptors") or []:
            if isinstance(block, dict):
                rows.extend(r for r in (block.get("results") or []) if isinstance(r, dict))
        if rows:
            return rows
        for key in ("rows", "results"):
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
    return []


def _receptor_blocks(runtime: Any = None) -> List[Dict[str, Any]]:
    """本次运行的受体块（含 pdbqt / 盒子 / 位点来源），文件优先、黑板兜底。"""

    payload = tool_io.load("docking", run=active_run(runtime))
    blocks = [b for b in ((payload or {}).get("receptors") or []) if isinstance(b, dict)]
    if blocks:
        return blocks
    board = active_blackboard(runtime)
    if board is not None:
        receptor = board.receptor() or {}
        site = board.site() or {}
        if receptor or site:
            return [{"receptor": receptor.get("name") or receptor.get("key") or "受体",
                     "receptor_key": receptor.get("key") or "",
                     "pdbqt": receptor.get("pdbqt") or "",
                     "box_center": site.get("center"), "box_size": site.get("size"),
                     "box_source": site.get("source") or "",
                     "results": board.docking() or []}]
    return []


def _ranked_rows(rows: List[Dict[str, Any]], top_n: int,
                 runtime: Any = None) -> List[Dict[str, Any]]:
    """优先按协调 Agent 已提交的推荐排行顺序，其次按亲和力排序。"""
    run = active_run(runtime)
    order: List[str] = []
    if run is not None:
        stored = (getattr(run, "data", None) or {}).get("recommendations") or {}
        for row in stored.get("rows") or []:
            key = str((row or {}).get("smiles") or (row or {}).get("name") or "")
            if key:
                order.append(key)
    by_key = {str(r.get("smiles") or ""): r for r in rows}
    by_name = {str(r.get("name") or ""): r for r in rows}
    picked: List[Dict[str, Any]] = []
    seen: set = set()
    for key in order:
        row = by_key.get(key) or by_name.get(key)
        if row is not None and id(row) not in seen:
            seen.add(id(row))
            picked.append(row)
    rest = [r for r in rows if id(r) not in seen
            and r.get("affinity_kcal_mol") is not None
            and str(r.get("name") or "") != POSITIVE_CONTROL_NAME]
    rest.sort(key=lambda r: float(r.get("affinity_kcal_mol") or 0.0))
    picked.extend(rest)
    return picked[:max(1, top_n)] if picked else []


def _pocket_description(block: Dict[str, Any]) -> Dict[str, Any]:
    pdbqt = str(block.get("pdbqt") or "")
    center = block.get("box_center") or []
    size = block.get("box_size") or []
    residues: List[Dict[str, Any]] = []
    if pdbqt and os.path.isfile(pdbqt) and center and size:
        residues = I.pocket_residues(pdbqt, center, size, limit=24)
    composition: Dict[str, List[str]] = {"aromatic": [], "hydrophobic": [], "charged": [], "polar": []}
    for residue in residues:
        name = str(residue.get("resname") or "")
        label = residue.get("residue")
        if name in ("PHE", "TYR", "TRP", "HIS"):
            composition["aromatic"].append(label)
        if name in ("ALA", "VAL", "LEU", "ILE", "MET", "PHE", "TRP", "PRO", "TYR", "CYS"):
            composition["hydrophobic"].append(label)
        if name in ("ASP", "GLU", "LYS", "ARG", "HIS"):
            composition["charged"].append(label)
        if name in ("ASN", "GLN", "SER", "THR", "TYR", "HIS", "CYS", "TRP"):
            composition["polar"].append(label)
    return {
        "receptor": block.get("receptor") or block.get("receptor_key"),
        "box_center": center, "box_size": size,
        "box_source": block.get("box_source") or (block.get("site") or {}).get("source") or "",
        "box_chosen_by": block.get("box_chosen_by") or "",
        "box_validation": block.get("box_validation") or {},
        "box_atom_stats": block.get("box_atom_stats") or {},
        "box_warnings": block.get("box_warnings") or [],
        "residues_in_box": [r.get("residue") for r in residues],
        "composition": {k: v[:10] for k, v in composition.items()},
        "protonation": block.get("receptor_protonation") or {},
    }


@tool
def analyze_pose_pocket(top_n: int = 0, runtime: ToolRuntime[AgentContext] = None) -> str:
    """分析「结合口袋是什么」与「推荐分子的最优位姿是怎么和口袋结合的」（真实坐标，非推测）。

    top_n: 分析前多少个分子（0 = 用设置里的推荐排行条数，默认 10；上限 20）。

    **口袋部分**：盒子来源与依据、盒内残基清单（按盒内原子数排序）与组成特征
    （芳香/疏水/带电/极性残基数量与名单）、受体质子化口径、口袋/盒子告警。
    **姿态部分**：每个分子最优位姿与受体的逐残基接触 ——
    氢键（配体 N/O 与受体 N/O ≤ 3.5 Å）、盐桥（|q| ≥ 0.3 且异号、≤ 4.0 Å）、
    疏水接触（双方碳 ≤ 4.2 Å）、π–π 堆叠（芳香碳对数 ≥ 3、≤ 5.5 Å）、金属配位（≤ 3.0 Å），
    含最近距离与关键残基示例；报告第 5.2 节会给出对应的 **2D 相互作用图与 3D 姿态图**。

    **用法（重要）**：写报告与推荐理由前调用一次，把这里返回的**残基名、相互作用类型、距离**
    原样用进你的叙述（例如「与 TYR353 形成氢键（3.28 Å），并与 TYR200/PHE134 有 π–π 堆积」）。
    不要编造工具没有给出的残基或相互作用；没有位姿（本次运行关闭了「保存位姿」）时如实说明
    「本次没有姿态–口袋分析」，并建议打开该选项后重跑。
    """
    try:
        blocks = _receptor_blocks(runtime)
        if not blocks:
            return json.dumps({"status": "no_data",
                               "message": "还没有对接结果与受体信息：请先完成一次对接。"},
                              ensure_ascii=False)
        top_n = int(top_n or 0) or _default_top_n()
        top_n = max(1, min(MAX_MOLECULES, top_n))

        payload: Dict[str, Any] = {
            "status": "ok",
            "thresholds": {"hbond_angstrom": I.HBOND_MAX, "salt_bridge_angstrom": I.SALT_MAX,
                           "hydrophobic_angstrom": I.CONTACT_MAX, "pi_angstrom": I.PI_MAX,
                           "metal_angstrom": I.METAL_MAX, "charge_min": I.CHARGE_MIN},
            "method_note": ("几何启发式（距离/部分电荷），未做氢键角度与质子化方向校正、"
                            "未做能量分解；用于说明结合在哪里、以什么方式接触，不替代 MD/MM-GBSA"),
            "receptors": [], "molecules": [], "missing_pose": [],
        }
        for block in blocks:
            pock = _pocket_description(block)
            rows = [r for r in _flatten_rows(block) if r.get("affinity_kcal_mol") is not None]
            selected = _ranked_rows(rows, top_n, runtime)
            pock["analyzed_molecules"] = len(selected)
            payload["receptors"].append(pock)
            pdbqt = str(block.get("pdbqt") or "")
            for index, row in enumerate(selected, start=1):
                pose = str(row.get("pose_file") or "")
                name = str(row.get("name") or row.get("smiles") or f"候选{index}")
                if not pose or not os.path.isfile(pose):
                    payload["missing_pose"].append(name)
                    continue
                analysis = I.analyze_pose_pocket(pdbqt, pose)
                if analysis.get("status") != "ok":
                    payload["missing_pose"].append(name)
                    continue
                summary = analysis.get("summary") or {}
                payload["molecules"].append({
                    "rank": index, "name": name, "smiles": row.get("smiles"),
                    "affinity_kcal_mol": row.get("affinity_kcal_mol"),
                    "pose_file": os.path.basename(pose),
                    "summary": summary,
                    "residues": [{"residue": r.get("residue"), "types": r.get("types"),
                                  "min_distance": (round(float(r["min_distance"]), 2)
                                                   if isinstance(r.get("min_distance"), (int, float))
                                                   else None),
                                  "example": (r.get("detail") or [{}])[0]}
                                 for r in (analysis.get("residues") or [])[:RESIDUE_LIMIT]],
                    "one_line": I.describe(analysis, max_residues=RESIDUE_LIMIT),
                    "figures": {"2d": f"charts/interaction_2d_{index:02d}.png",
                                "3d": f"charts/pose_3d_{index:02d}.png"},
                    "warnings": analysis.get("warnings") or [],
                })
        if not payload["molecules"]:
            payload["status"] = "no_pose"
            payload["message"] = ("没有任何可分析的位姿文件：本次运行可能关闭了「保存位姿」。"
                                  "请打开该选项后重跑，才能给出姿态–口袋结合分析。")
        payload["how_to_write"] = (
            "口袋说明用 receptors[0]（box_source / residues_in_box / composition / box_validation）；"
            "逐分子结合情况用 molecules[].residues 与 one_line（残基名 + 相互作用类型 + 距离原样引用）；"
            "2D/3D 图由报告第 5.2 节内嵌，不要在正文里写文件路径或链接。")
        return json.dumps(payload, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.exception("姿态–口袋分析失败")
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)
