"""运行级「小事实」记账：给**条件纪律段**（`agents/prompt_blocks.py`）提供判断依据。

**为什么需要**：协调 Agent 的「执行纪律」里有几段只在特定体系/规模下才用得上
（特殊化学体系、两阶段漏斗…）。要把它们做成「按需注入」，就得有人**记住**本次运行
到底发生过什么 —— 而这类事实只有执行层（对接工具）看得到。

**为什么放在 `runtime/`**：`tools/` 不得 import `agents/`（分层契约见
`tests/test_dependency_layering.py`），所以「事实」必须落在两边都能依赖的 `runtime/`；
`agents/prompt_blocks.py` 再读它。

**记账原则**：只记**布尔/规模**这类小量，且一旦为真就**粘住**；绝不在这里存明细
（明细走工具产物与黑板）。缺失一律当作「未知」—— 条件段在未知时**注入**（安全优先）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

#: `run.data` 里放本模块记录的位置
FACTS_KEY = "prompt_facts"

#: 「一旦为真就不再翻回假」的事实名（负面信号必须累积）：
#: 否则一次干净的精算轮次会把粗筛轮次发现的杂原子/特殊化学抹掉。
STICKY_TRUE = ("hetero_atoms", "special_chemistry", "docking_seen")


def read(run: Any) -> Dict[str, Any]:
    """读本次运行已记录的事实（没有就返回空 dict）。"""
    data = getattr(run, "data", None)
    if not isinstance(data, dict):
        return {}
    facts = data.get(FACTS_KEY)
    return dict(facts) if isinstance(facts, dict) else {}


def note(run: Any, **facts: Any) -> None:
    """把事实写进 `run.data`（`STICKY_TRUE` 里的键一旦为真就不再被覆盖为假）。"""
    if not facts:
        return
    data = getattr(run, "data", None)
    if not isinstance(data, dict):
        return
    try:
        store = data.setdefault(FACTS_KEY, {})
        if not isinstance(store, dict):     # 被外部写坏时重建，不让记账拖垮运行
            store = {}
            data[FACTS_KEY] = store
        for key, value in facts.items():
            if value is None:
                continue
            if key in STICKY_TRUE and store.get(key) is True:
                continue
            store[key] = value
    except Exception as e:                  # noqa: BLE001 - 记账失败绝不能影响计算
        logger.debug("记录运行事实失败：%s", e)


#: 受体溯源里允许落进 `run.data` / 报告的字段（其余一律丢弃：这里是**小状态**，不是明细）
RECEPTOR_PROVENANCE_KEYS = ("requested", "database", "structure_source", "accession", "entry_id",
                            "organism", "protein", "pdb_id", "structure_method",
                            "structure_resolution", "alphafold_model_version")


def note_receptor_provenance(run: Any, provenance: Any) -> None:
    """记下「本次受体是哪个结构、从哪来」（报告 §1.2 自己写明，不依赖 Agent 在结论里复述）。

    审计发现：`fetch_protein_structure` 的 accession/物种/结构来源原先只出现在工具返回与
    协调 Agent 的对话文本里 —— 一旦结论节按「只留结论/风险」精简，这份溯源就从报告里消失了。
    因此把它作为**运行事实**落盘，由报告直接渲染。
    """
    data = getattr(run, "data", None)
    if not isinstance(data, dict) or not isinstance(provenance, dict):
        return
    try:
        data["receptor_provenance"] = {k: provenance[k] for k in RECEPTOR_PROVENANCE_KEYS
                                       if provenance.get(k) not in (None, "")}
    except Exception as e:                  # noqa: BLE001 - 记账失败不能影响解析结果
        logger.debug("记录受体溯源失败：%s", e)


def docking_facts(payload: Any) -> Dict[str, bool]:
    """从一次对接结果里提取「本次体系是否需要特殊化学纪律」（纯函数）。

    - `hetero_atoms`：受体准备丢弃/保留过非水杂原子（金属、血红素、NAD/FAD…）；
    - `special_chemistry`：配体侧出现多片段/反离子拆分、含金属、质子化态改动等；
    - `docking_seen`：确实看过一份对接结果 —— 只有它为真，`prompt_blocks` 才敢
      因为「没看到异常」而省掉特殊体系那一段。
    """
    blocks = (payload or {}).get("receptors") or []
    hetero = False
    special = False
    seen_any = False
    for block in blocks:
        if not isinstance(block, dict):
            continue
        seen_any = True
        for key in ("dropped_hetatm", "kept_hetatm", "unsupported_hetatm"):
            if block.get(key):
                hetero = True
        for row in block.get("results") or []:
            if not isinstance(row, dict):
                continue
            seen_any = True
            facts = row.get("ligand_facts")
            if isinstance(facts, dict):
                if int(facts.get("num_fragments") or 1) > 1:
                    special = True
                if facts.get("has_metal"):
                    special = True
            if row.get("ligand_warnings") or row.get("removed_fragments"):
                special = True
            prot = row.get("protonation")
            if isinstance(prot, dict) and prot.get("applied"):
                special = True
    if not seen_any:
        return {}
    return {"docking_seen": True, "hetero_atoms": hetero, "special_chemistry": special}


__all__ = ["FACTS_KEY", "STICKY_TRUE", "RECEPTOR_PROVENANCE_KEYS", "read", "note",
           "note_receptor_provenance", "docking_facts"]
