"""报告可展示字段的**唯一权威清单**（工具白名单与报告渲染共用一份，避免两处漂移）。

语义键 → 中文列名。协调 Agent 通过 `customize_report` 只能从这份清单里挑，
报告渲染时也按同一份清单取列名与取数，因此「Agent 说要展示 ID」与实际列名不会对不上。
"""
from __future__ import annotations

from typing import Dict

REPORT_FIELD_LABELS: Dict[str, str] = {
    "id": "分子 ID",
    "cas": "CAS 号",
    "remark": "备注",
    "name": "分子",
    "smiles": "SMILES",
    "formula": "分子式",
    "molecular_weight": "分子量 (Da)",
    "logP": "logP",
    "tpsa": "TPSA (Å²)",
    "hbd": "氢键供体",
    "hba": "氢键受体",
    "rotatable_bonds": "可旋转键",
    "aromatic_rings": "芳香环",
    "lipinski_violations": "Lipinski 违例",
    "drug_likeness_pass": "类药性",
    "affinity_kcal_mol": "亲和力 (kcal/mol)",
    "ligand_efficiency": "配体效率 LE",
    "composite": "综合分",
    "grade": "等级",
    "engine": "引擎",
    "exhaustiveness": "搜索强度",
    "box_group": "对接盒子",
    "similarity_to_positive_control": "与对照相似度",
    "maccs_tanimoto": "MACCS 相似度",
    "structural_consistency": "结构一致性",
    "anchor_match": "药效团锚定",
    "source_index": "输入序号",
    "source_file": "来源文件",
}

#: 报告默认就展示的列（排行表已有的列）——额外列在此基础上追加，避免重复。
REPORT_DEFAULT_COLUMNS: tuple = (
    "rank", "name", "composite", "grade", "affinity_kcal_mol", "ligand_efficiency",
)
