"""结果融合与排序：把理化性质、对接明细、结合模式三项结果按分子融合，并按亲和力排名。"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Sequence, Tuple

logger = logging.getLogger(__name__)

# 融合时以「名称/结构」字段为准，避免被其它来源覆盖
_KEEP_FROM_BASE = ("name", "smiles")
# 超限分子分组：large 组的盒子与主组不同，Vina 分数受盒子影响（非单调），因此
# **默认排序榜只含 main 组**，large 组单独在报告里列、在 docking.json/result.json 里保留。
LARGE_BOX_GROUP = "large"
MAIN_BOX_GROUP = "main"


def box_group_of(row: Dict[str, Any]) -> str:
    """结果行所属盒子分组（缺省视为 main，兼容旧数据）。"""
    return str((row or {}).get("box_group") or MAIN_BOX_GROUP).strip().lower()


def split_box_groups(rows: Sequence[Dict[str, Any]]
                     ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把结果行按盒子分组拆成 (main, large)。"""
    main = [r for r in rows if box_group_of(r) != LARGE_BOX_GROUP]
    large = [r for r in rows if box_group_of(r) == LARGE_BOX_GROUP]
    return main, large


def affinity_sort_key(row: Dict[str, Any]) -> tuple:
    """亲和力排序键：越负越靠前，**缺失/非数**的排最后（而不是当成 0 混进榜里）。"""
    v = row.get("affinity_kcal_mol")
    if isinstance(v, (int, float)):
        return (0, float(v))
    return (1, 0.0)


def sort_by_affinity(rows: List[Dict[str, Any]], *, drop_missing: bool = False
                     ) -> List[Dict[str, Any]]:
    """按对接亲和力排序（唯一实现）。

    原先报告表、图表、对接工具、融合层各写一遍排序，语义还不一致
    （有的丢弃缺失值、有的把缺失值排最后）。统一到这里：
    - drop_missing=False（默认）：缺失亲和力的行保留并排在最后（报告里要如实展示失败/无分数行）；
    - drop_missing=True：只返回有真实亲和力的行（画图、截断 Top-N 时用）。
    """
    if drop_missing:
        rows = [r for r in rows if isinstance(r.get("affinity_kcal_mol"), (int, float))]
    return sorted(rows, key=affinity_sort_key)


def merge_and_rank(properties: List[Dict[str, Any]],
                   dockings: List[Dict[str, Any]],
                   bindings: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按 smiles 融合三项结果，并按 Docking 亲和力升序（越负结合越强）排序。

    - 理化性质：作为基础字段；
    - 对接明细：覆盖同名字段（亲和力/能量项/引擎/搜索强度/位姿）；
    - 结合模式：除 name/smiles 外的全部字段（双指纹相似度、结构一致性、
      结合模式提示、药效团锚定、性质差异）都会并入，供报告与界面展示。

    **默认排序榜只含 main 组**：large 组的盒子不同、分数不可与主组直接比较
    （见 `docs/architecture.md` §15.7b），因此这里把 `box_group="large"` 的行排除；
    两组数据仍完整保留在结果行与 `docking.json` / `result.json` 中（行上有 `box_group` 标记）。
    """
    main_dockings, _large = split_box_groups(dockings)
    prop_by_smiles = {p["smiles"]: p for p in properties if p.get("smiles")}
    dock_by_smiles = {d["smiles"]: d for d in main_dockings if d.get("smiles")}
    binding_by_smiles = {r["smiles"]: r for r in (bindings or {}).get("rows", []) if r.get("smiles")}

    # 基准 = 性质 ∪ 对接：只做了对接（用户只要求亲和力排序、没跑性质评估）时也要能出排序表，
    # 否则报告里会出现「有对接结果但没有排序」的空档。
    base_smiles = list(prop_by_smiles)
    base_smiles += [s for s in dock_by_smiles if s not in prop_by_smiles]

    merged: List[Dict[str, Any]] = []
    for smiles in base_smiles:
        prop = prop_by_smiles.get(smiles, {})
        dock = dock_by_smiles.get(smiles, {})
        binding = {k: v for k, v in binding_by_smiles.get(smiles, {}).items()
                   if k not in _KEEP_FROM_BASE}
        merged.append({
            "name": dock.get("name") or prop.get("name") or smiles,
            "smiles": smiles,
            **prop,
            **dock,
            **binding,
        })

    # 只保留有真实亲和力的条目，并按亲和力升序（数值越低结合越强）
    return sort_by_affinity(merged, drop_missing=True)
