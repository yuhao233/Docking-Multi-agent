"""结果整理：排序与排序 CSV 生成。"""
from __future__ import annotations

import csv
import io
from typing import Any, Dict, List

from docking_agent.core.ranking import sort_by_affinity

# 排序 CSV 列：带上 engine / exhaustiveness / box_group / box_size / seed，保证结果可追溯、可复现
# （`box_group`：main=主组共用同一个盒子；large=跨度超出主盒、用同中心的更大盒子单独重跑）
# `status` / `error`：成功行标 ok，失败/跳过行标 error 并写原因，绝不静默丢弃失败分子。
CSV_FIELDS = [
    "rank", "id", "name", "smiles", "affinity_kcal_mol", "engine", "exhaustiveness",
    "box_group", "box_size",
    "seed", "seed_policy",
    "intermolecular_kcal_mol", "intramolecular_kcal_mol", "torsion_kcal_mol",
    "molecular_weight", "logP", "tpsa", "hbd", "hba", "rotatable_bonds",
    "aromatic_rings", "formula", "lipinski_violations", "drug_likeness_pass",
    "protonation_policy", "charge_input", "charge_used",
    "similarity_to_positive_control", "maccs_tanimoto", "combined_similarity",
    "structural_consistency", "anchor_match", "binding_mode_hint",
    "vs_positive_control_kcal_mol",
    "source_index", "source_file",
    "status", "error",
]


def collect_failures(docking: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    """从对接明细里收集失败/跳过行（带去重），供报告与 ranking.csv 使用。"""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for block in ((docking or {}).get("receptors") or []):
        if not isinstance(block, dict):
            continue
        for r in (block.get("results") or []):
            if not isinstance(r, dict):
                continue
            key = str(r.get("smiles") or r.get("name") or "")
            has_aff = isinstance(r.get("affinity_kcal_mol"), (int, float))
            err = str(r.get("error") or "").strip()
            status = str(r.get("status") or "").lower()
            if not (err or status == "error" or (not has_aff and key)):
                continue
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            out.append(r)
    return out


def format_box_size(value: Any) -> str:
    """把盒子尺寸写成紧凑字符串：`22.0x22.0x22.0`（缺省为空字符串）。"""
    if value in (None, "", [], ()):
        return ""
    if isinstance(value, (list, tuple)):
        try:
            return "x".join(f"{float(v):.1f}" for v in value)
        except (TypeError, ValueError):
            return "x".join(str(v) for v in value)
    return str(value)


def rank_molecules(molecules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按对接亲和力升序排序（越负结合越强）；缺失亲和力的排在最后。

    报告工具对外承诺产出「ranking」产物，因此这里防御性排序，
    不依赖调用方（LLM）是否已经排好序。
    """
    return sort_by_affinity(molecules)


def _fmt(value: Any) -> Any:
    return "" if value is None else value


def _protonation_of(row: Dict[str, Any]) -> Dict[str, Any]:
    """取该分子的质子化溯源（对接行的 `ligand_facts.protonation`，或性质行的 `protonation`）。"""
    for candidate in ((row.get("ligand_facts") or {}).get("protonation"), row.get("protonation")):
        if isinstance(candidate, dict) and candidate:
            return candidate
    return {}


def build_ranking_csv(molecules: List[Dict[str, Any]], pos_control: Dict[str, Any] | None = None,
                      delta_vs_control: bool = True,
                      failures: List[Dict[str, Any]] | None = None) -> str:
    """生成排序 CSV 文本（RFC4180，UTF-8，含表头）。

    成功行在前（按亲和力排序），失败/跳过行以 `rank=FAIL`、`status=error` 追加在后，
    保证「失败可见」而不是只导出成功分子（见开发手册 §15.7 第 2 项）。
    """
    ordered = rank_molecules(molecules)
    pc_aff = (pos_control or {}).get("affinity_kcal_mol")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for i, m in enumerate(ordered, 1):
        row = {k: _fmt(m.get(k)) for k in CSV_FIELDS}
        row["rank"] = i
        row["box_group"] = m.get("box_group") or "main"
        row["box_size"] = format_box_size(m.get("box_size"))
        prot = _protonation_of(m)
        row["protonation_policy"] = prot.get("policy") or ""
        row["charge_input"] = _fmt(prot.get("charge_before"))
        row["charge_used"] = _fmt(prot.get("charge_after"))
        row["status"] = "error" if m.get("error") else "ok"
        row["error"] = _fmt(m.get("error")) if m.get("error") else ""
        if delta_vs_control and isinstance(m.get("affinity_kcal_mol"), (int, float)) and isinstance(pc_aff, (int, float)):
            row["vs_positive_control_kcal_mol"] = round(m["affinity_kcal_mol"] - pc_aff, 2)
        writer.writerow(row)
    for f in (failures or []):
        row = {k: _fmt(f.get(k)) for k in CSV_FIELDS}
        row["rank"] = "FAIL"
        row["box_group"] = f.get("box_group") or "main"
        row["box_size"] = format_box_size(f.get("box_size"))
        row["status"] = str(f.get("status") or "error")
        row["error"] = str(f.get("error") or "未产出有效分数")
        writer.writerow(row)
    if pos_control:
        pc_row = {k: "" for k in CSV_FIELDS}
        pc_row.update({
            "rank": "PC",
            "name": pos_control.get("name") or "PositiveControl",
            "smiles": pos_control.get("smiles"),
            "affinity_kcal_mol": _fmt(pc_aff),
            "engine": _fmt(pos_control.get("engine")),
            "exhaustiveness": _fmt(pos_control.get("exhaustiveness")),
            "box_group": _fmt(pos_control.get("box_group") or "main"),
            "box_size": format_box_size(pos_control.get("box_size")),
            "similarity_to_positive_control": 1.0,
            "status": "control",
        })
        writer.writerow(pc_row)
    return buf.getvalue()
