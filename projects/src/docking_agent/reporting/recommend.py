"""推荐化合物排行：把「对接亲和力」与「理化性质 / 类药性」合成一个**可解释**的综合分。

## 为什么要有这一节

只看对接分数会把「大而黏」的分子排到最前（结合位点多、熵代价大，Vina 打分天然偏好大分子），
所以本模块把四个**各自有明确含义**的分量按权重合成综合分，并逐分子给出分量明细：

| 分量 | 含义 | 满分口径（透明、绝对值，不依赖本批库的分布） |
| --- | --- | --- |
| `affinity` | 对接亲和力 | `-ΔG / 12`，即 −12 kcal/mol 记 1.0 |
| `ligand_efficiency` | 配体效率 LE = −ΔG / 重原子数 | `LE / 0.45`，即 0.45 kcal·mol⁻¹·重原子⁻¹ 记 1.0 |
| `drug_likeness` | Lipinski 违例数 | `1 − 0.25 × 违例数`（≥4 条违例记 0） |
| `physchem` | logP 与 TPSA 是否落在类药窗口 | logP∈[0,4]、TPSA≤120 Å² 记 1.0，超出线性衰减 |

**所有尺度都是绝对口径**（不按本批库做 min-max 归一化），因此不同运行之间的综合分可比、
也不会因为库换了一批分子而整体漂移。缺少某项数据的分子，该分量按 0 计入并明确标注
`missing`，不会静默丢弃；没有对接分数的分子根本不进排行（并如实计数）。

权重默认 `0.45 / 0.20 / 0.20 / 0.15`，可由设置项 `runtime.rank_weights`
（逗号分隔的 4 个权重，或 `affinity=0.5,le=0.2,...` 形式）覆盖；解析失败时回退默认并记 note。

**分工**：本模块只做**计算与规则化建议**（确定性的筛选建议），
"为什么推荐这几个"的**自然语言理由**由协调 Agent 通过 `submit_recommendations` 写入，
报告把两者并排呈现，谁的判断一目了然。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

from docking_agent.reporting.fields import REPORT_FIELD_LABELS  # noqa: E402

#: 权重分量顺序（报告与解析都按这个顺序）
WEIGHT_KEYS: Tuple[str, ...] = ("affinity", "ligand_efficiency", "drug_likeness", "physchem")
WEIGHT_LABELS: Dict[str, str] = {
    "affinity": "对接亲和力",
    "ligand_efficiency": "配体效率（LE）",
    "drug_likeness": "类药性（Lipinski）",
    "physchem": "理化性质窗口（logP/TPSA）",
}
#: 默认权重（和为 1）
DEFAULT_WEIGHTS: Dict[str, float] = {
    "affinity": 0.45, "ligand_efficiency": 0.20, "drug_likeness": 0.20, "physchem": 0.15,
}
#: 解析设置项时允许的别名
WEIGHT_ALIASES: Dict[str, str] = {
    "affinity": "affinity", "aff": "affinity", "亲和力": "affinity", "对接": "affinity",
    "le": "ligand_efficiency", "ligand_efficiency": "ligand_efficiency", "配体效率": "ligand_efficiency",
    "drug_likeness": "drug_likeness", "lipinski": "drug_likeness", "类药性": "drug_likeness",
    "physchem": "physchem", "理化": "physchem", "理化性质": "physchem",
}

#: 满分口径常量（改这里就等于改评分尺度，报告会自动跟着变）
AFFINITY_FULL_KCAL = 12.0        # −12 kcal/mol → 1.0
LE_FULL = 0.45                   # 0.45 kcal/mol/重原子 → 1.0
LOGP_WINDOW: Tuple[float, float] = (0.0, 4.0)
LOGP_FALLOFF = 3.0               # 超出窗口后每 3 个 log 单位衰减到 0
TPSA_FULL = 120.0                # ≤120 Å² → 1.0
TPSA_ZERO = 200.0                # ≥200 Å² → 0.0
LIPINSKI_STEP = 0.25             # 每条违例扣 0.25
#: 等级阈值
GRADE_A = 0.65
GRADE_B = 0.45
#: 亲和力门槛：弱于此值的分子即使其它分量满分也不评为 A/B ——
#: 综合分是"多目标权衡"，但**没有结合**的分子不该因为小而类药就被推荐。
AFFINITY_GATE_KCAL = -6.0
DEFAULT_TOP_N = 10
#: 排行顺序：等级优先（A→B→C），同级内按综合分降序。
#: 若只按综合分排，"被封顶为 C 的分子"可能排在 B 级之前 —— 排行与等级自相矛盾。
GRADE_ORDER: Dict[str, int] = {"A": 0, "B": 1, "C": 2}
#: 大分子 / 高柔性阈值（用于给出可操作的筛选建议）
BIG_MW = 600.0
MANY_ROTATABLE = 10
MANY_VIOLATIONS = 2


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_weights(text: Optional[str] = None) -> Tuple[Dict[str, float], List[str]]:
    """解析权重设置：`0.45,0.20,0.20,0.15` 或 `affinity=0.45,le=0.2,...`。

    只做**宽松解析 + 如实回退**：任何非法输入都回退到默认权重并给出 note，
    绝不因为一个设置项写错就让排行失败或悄悄换口径。
    """
    notes: List[str] = []
    if text is None:
        try:
            from docking_agent.config import env

            text = env("RANK_WEIGHTS", "") or ""
        except Exception:  # noqa: BLE001
            text = ""
    raw = str(text or "").strip()
    if not raw:
        return dict(DEFAULT_WEIGHTS), notes

    parsed: Dict[str, float] = {}
    parts = [p.strip() for p in raw.replace("；", ",").replace(";", ",").replace("，", ",").split(",")
             if p.strip()]
    unnamed: List[float] = []
    for part in parts:
        if "=" in part or ":" in part:
            key_raw, _, val_raw = part.replace(":", "=").partition("=")
            key = WEIGHT_ALIASES.get(key_raw.strip().lower())
            val = _num(val_raw)
            if not key or val is None or val < 0:
                notes.append(f"权重项「{part}」无法识别，已忽略")
                continue
            parsed[key] = val
        else:
            val = _num(part)
            if val is None or val < 0:
                notes.append(f"权重项「{part}」不是非负数，已忽略")
                continue
            unnamed.append(val)
    if unnamed and not parsed:
        if len(unnamed) != len(WEIGHT_KEYS):
            notes.append(f"无名称的权重需要正好 {len(WEIGHT_KEYS)} 个（按 "
                         + "/".join(WEIGHT_LABELS[k] for k in WEIGHT_KEYS) + f" 顺序），收到 {len(unnamed)} 个，已回退默认")
            return dict(DEFAULT_WEIGHTS), notes
        parsed = dict(zip(WEIGHT_KEYS, unnamed))
    elif unnamed:
        notes.append("权重混用了命名与无名写法，无名部分已忽略")
    if not parsed:
        return dict(DEFAULT_WEIGHTS), notes
    # 缺项用默认补齐（而不是按 0 处理，避免"没写就等于不要"的误读）
    merged = {k: float(parsed.get(k, DEFAULT_WEIGHTS[k])) for k in WEIGHT_KEYS}
    total = sum(merged.values())
    if total <= 0:
        notes.append("权重之和为 0，已回退默认权重")
        return dict(DEFAULT_WEIGHTS), notes
    if abs(total - 1.0) > 1e-9:
        merged = {k: v / total for k, v in merged.items()}
        notes.append(f"权重之和为 {total:g}，已按比例归一化到 1.0")
    return merged, notes


def score_compound(row: Dict[str, Any], weights: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """给单个分子算四个分量与综合分（缺数据的分量按 0 并记入 `missing`）。"""
    w = dict(weights or DEFAULT_WEIGHTS)
    out: Dict[str, Any] = {
        "name": row.get("name") or row.get("smiles") or "",
        "smiles": row.get("smiles") or "",
        "affinity_kcal_mol": _num(row.get("affinity_kcal_mol")),
        "molecular_weight": _num(row.get("molecular_weight")),
        "logP": _num(row.get("logP")),
        "tpsa": _num(row.get("tpsa")),
        "hbd": _num(row.get("hbd")),
        "hba": _num(row.get("hba")),
        "rotatable_bonds": _num(row.get("rotatable_bonds")),
        "heavy_atoms": _num(row.get("heavy_atoms")),
        "lipinski_violations": _num(row.get("lipinski_violations")),
        "drug_likeness_pass": row.get("drug_likeness_pass"),
        "similarity_to_positive_control": _num(row.get("similarity_to_positive_control")),
        "box_group": row.get("box_group") or "main",
        "missing": [],
    }
    # 透传报告可展示的字段（ID / 分子式 / 来源文件 / 引擎 …）：
    # 协调 Agent 会用 customize_report 指定「这次报告要哪些列」，因此这些字段必须一路带到
    # 排行行里；否则报告里只能出现固定那几列（用户要 ID 也无从取）。显式赋值的优先。
    for _key in REPORT_FIELD_LABELS:
        if _key not in out and row.get(_key) not in (None, "", [], {}):
            out[_key] = row[_key]
    missing: List[str] = out["missing"]
    aff = out["affinity_kcal_mol"]
    heavy = out["heavy_atoms"]
    violations = out["lipinski_violations"]
    logp = out["logP"]
    tpsa = out["tpsa"]

    components: Dict[str, float] = {}

    if aff is None:
        components["affinity"] = 0.0
        missing.append("对接亲和力")
    else:
        components["affinity"] = _clamp(-aff / AFFINITY_FULL_KCAL)

    if aff is None or not heavy or heavy <= 0:
        components["ligand_efficiency"] = 0.0
        out["ligand_efficiency"] = None
        if heavy is None or not heavy:
            missing.append("重原子数（无法计算配体效率）")
    else:
        le = -aff / float(heavy)
        out["ligand_efficiency"] = round(le, 4)
        components["ligand_efficiency"] = _clamp(le / LE_FULL)

    if violations is None:
        # 缺违例数时退到 drug_likeness_pass；两者都缺才算 missing
        if out.get("drug_likeness_pass") is None:
            components["drug_likeness"] = 0.0
            missing.append("Lipinski 违例数")
        else:
            components["drug_likeness"] = 1.0 if out["drug_likeness_pass"] else 0.5
    else:
        components["drug_likeness"] = _clamp(1.0 - LIPINSKI_STEP * float(violations))

    phys_parts: List[float] = []
    if logp is None:
        missing.append("logP")
    else:
        low, high = LOGP_WINDOW
        if logp < low:
            phys_parts.append(_clamp(1.0 - (low - logp) / LOGP_FALLOFF))
        elif logp > high:
            phys_parts.append(_clamp(1.0 - (logp - high) / LOGP_FALLOFF))
        else:
            phys_parts.append(1.0)
    if tpsa is None:
        missing.append("TPSA")
    else:
        phys_parts.append(_clamp((TPSA_ZERO - tpsa) / (TPSA_ZERO - TPSA_FULL)))
    components["physchem"] = round(sum(phys_parts) / len(phys_parts), 4) if phys_parts else 0.0

    out["components"] = {k: round(float(components.get(k, 0.0)), 4) for k in WEIGHT_KEYS}
    out["weights"] = {k: round(float(w.get(k, 0.0)), 4) for k in WEIGHT_KEYS}
    composite = sum(out["components"][k] * out["weights"][k] for k in WEIGHT_KEYS)
    out["composite"] = round(composite, 4)
    grade = "A" if composite >= GRADE_A else ("B" if composite >= GRADE_B else "C")
    # 硬门槛：亲和力太弱 → 等级封顶为 C（综合分本身照算，不掩盖数值）
    if aff is not None and aff > AFFINITY_GATE_KCAL and grade != "C":
        out["grade_gate"] = (f"对接亲和力 {aff:.2f} kcal/mol 弱于门槛 "
                             f"{AFFINITY_GATE_KCAL:.1f} kcal/mol → 等级封顶为 C")
        grade = "C"
    out["grade"] = grade
    out["suggestions"] = screening_suggestions(out)
    return out


def screening_suggestions(scored: Dict[str, Any]) -> List[str]:
    """按**可核对的规则**给出筛选建议（每条都能在该分子的分量明细里找到依据）。"""
    tips: List[str] = []
    grade = scored.get("grade")
    aff = scored.get("affinity_kcal_mol")
    mw = scored.get("molecular_weight")
    rot = scored.get("rotatable_bonds")
    viol = scored.get("lipinski_violations")
    logp = scored.get("logP")
    tpsa = scored.get("tpsa")
    le = scored.get("ligand_efficiency")
    if scored.get("grade_gate"):
        tips.append(str(scored["grade_gate"]) + "：结合强度不足，不建议仅凭类药性推进。")
    if grade == "A":
        tips.append("综合分 A 级：建议作为优先候选进入下一轮（提高 exhaustiveness 复算或直接做实验验证）。")
    elif grade == "B":
        tips.append("综合分 B 级：可作备选，建议先补齐短板（见下）再决定是否推进。")
    else:
        tips.append("综合分 C 级：建议暂缓，或作为低优先级/阴性参考。")
    if mw is not None and mw > BIG_MW:
        tips.append(f"分子量 {mw:.0f} Da（>{BIG_MW:.0f}）：吸收与合成难度风险高，且 Vina 对大配体采样可能不充分。")
    if rot is not None and rot > MANY_ROTATABLE:
        tips.append(f"可旋转键 {rot:.0f}（>{MANY_ROTATABLE}）：构象熵代价大、打分噪声高，建议提高搜索强度复算。")
    if le is not None and aff is not None and aff <= -7.0 and le < 0.25:
        tips.append("亲和力强但配体效率偏低：分数可能主要由分子体积贡献，建议优先找更小的骨架类似物。")
    if viol is not None and viol >= MANY_VIOLATIONS:
        tips.append(f"Lipinski 违例 {viol:.0f} 条：类药性偏差，投入实验前建议先做性质优化。")
    if logp is not None and logp > LOGP_WINDOW[1]:
        tips.append(f"logP {logp:.2f} 偏高：溶解度/选择性风险。")
    if logp is not None and logp < LOGP_WINDOW[0]:
        tips.append(f"logP {logp:.2f} 偏低：跨膜渗透风险（若靶点在胞内需特别注意）。")
    if tpsa is not None and tpsa > TPSA_FULL:
        tips.append(f"TPSA {tpsa:.0f} Å²（>{TPSA_FULL:.0f}）：口服吸收可能受限。")
    if scored.get("box_group") == "large":
        tips.append("该分子使用了大配体专用盒（与大组分数不可跨组直接比较），建议单独评估或统一盒子重跑。")
    if scored.get("missing"):
        tips.append("缺少数据分量：" + "、".join(scored["missing"]) + "（综合分已按 0 计入，请先补齐再据此决策）。")
    return tips


def build_recommendations(ranking: Sequence[Dict[str, Any]], *,
                          top_n: Optional[int] = None,
                          weights: Optional[Dict[str, float]] = None,
                          weight_text: Optional[str] = None,
                          reasons: Optional[Sequence[Dict[str, Any]]] = None,
                          positive_control_name: str = "",
                          ) -> Dict[str, Any]:
    """从排序结果构建推荐排行（综合分降序；无对接分数的分子不进排行并如实计数）。

    `reasons`：协调 Agent 通过 `submit_recommendations` 写入的自然语言理由，
    按 `smiles`（优先）或 `name` 与计算行匹配；匹配不上的条目原样放在
    `unmatched_reasons` 里（不静默丢弃，也不让模型的话覆盖真实数值）。
    """
    notes: List[str] = []
    if weights is None:
        weights, wnotes = parse_weights(weight_text)
        notes.extend(wnotes)
    weights = {k: float(weights.get(k, DEFAULT_WEIGHTS[k])) for k in WEIGHT_KEYS}
    reason_map: Dict[str, Dict[str, Any]] = {}
    for item in (reasons or []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("smiles") or "").strip() or str(item.get("name") or "").strip()
        if key:
            reason_map[key] = item
    matched_keys: set = set()

    scored: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    for row in (ranking or []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if positive_control_name and name == positive_control_name:
            continue
        if _num(row.get("affinity_kcal_mol")) is None:
            excluded.append({"name": row.get("name") or row.get("smiles") or "",
                             "reason": row.get("error") or "无有效对接分数"})
            continue
        item = score_compound(row, weights)
        lookup = [item["smiles"], item["name"]]
        for key in lookup:
            if key and key in reason_map:
                entry = reason_map[key]
                item["agent_reason"] = str(entry.get("reason") or "").strip()
                item["agent_suggestion"] = str(entry.get("suggestion") or "").strip()
                item["agent_priority"] = entry.get("priority")
                matched_keys.add(key)
                break
        scored.append(item)

    scored.sort(key=lambda r: (GRADE_ORDER.get(r.get("grade") or "C", 9),
                               -r["composite"],
                               r["affinity_kcal_mol"] if r["affinity_kcal_mol"] is not None else 0.0,
                               str(r.get("name") or "")))
    for idx, item in enumerate(scored, start=1):
        item["rank"] = idx

    limit = int(top_n or 0)
    rows = scored if limit <= 0 else scored[:limit]
    unmatched = [dict(v) for k, v in reason_map.items() if k not in matched_keys]
    grades = {"A": 0, "B": 0, "C": 0}
    for item in scored:
        grades[item["grade"]] = grades.get(item["grade"], 0) + 1

    if scored:
        gated = [r for r in scored if r.get("grade_gate")]
        # 等级分布由第 3.1 节「筛选建议」统一给出（这里不重复），只保留「封顶」这个需要解释的量
        if gated:
            notes.append(f"其中 {len(gated)} 个分子因亲和力弱于 {AFFINITY_GATE_KCAL:.1f} kcal/mol "
                         "被等级封顶为 C（综合分照算，不掩盖数值）。")
    if excluded:
        notes.append(f"{len(excluded)} 个分子没有有效对接分数，未进入排行"
                     "（明细见第 7 节与 `ranking.csv`）。")

    return {
        "status": "ok" if scored else "no_data",
        "top_n": limit or len(scored),
        "weights": {k: round(weights[k], 4) for k in WEIGHT_KEYS},
        "weight_labels": dict(WEIGHT_LABELS),
        "criteria": {
            "affinity_full_kcal": AFFINITY_FULL_KCAL,
            "le_full": LE_FULL,
            "logp_window": list(LOGP_WINDOW),
            "logp_falloff": LOGP_FALLOFF,
            "tpsa_full": TPSA_FULL,
            "tpsa_zero": TPSA_ZERO,
            "lipinski_step": LIPINSKI_STEP,
            "grade_a": GRADE_A,
            "grade_b": GRADE_B,
            "affinity_gate_kcal": AFFINITY_GATE_KCAL,
        },
        "rows": rows,
        "scored_total": len(scored),
        "excluded": excluded,
        "excluded_total": len(excluded),
        "grades": grades,
        "with_agent_reason": sum(1 for r in rows if r.get("agent_reason")),
        "unmatched_reasons": unmatched,
        "notes": notes,
    }
