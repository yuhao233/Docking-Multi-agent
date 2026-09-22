"""配体质子化态：运行级策略（保持 / 中和 / **按目标 pH 规则化**）。

## 为什么需要这一层

同一分子的离子态与中性态对接行为差别很大（羧酸根 vs 羧酸、铵 vs 胺、脒/胍的阳离子），
而库里常见盐/离子形式。系统过去只在结果里"告警请确认质子化态"，不提供统一口径，
于是同一批筛选里混着两种化学形式 —— 排序失去可比性。

三种策略（**运行级**，同一次运行所有分子同口径）：

| 策略 | 含义 | 适用 |
| --- | --- | --- |
| `ph` | 先中和到中性形式，再按**目标 pH** 用内置 pKa 规则表重新分配质子化态 | **默认**（目标 pH 7.4，对接的通行假设） |
| `neutralize` | 只把**带净电荷**的分子中和（RDKit `Uncharger`），中性分子一字不改 | 不关心生理 pH 的快速筛选 / 只要"不带净电荷" |
| `keep` | 完全保持输入形式，仅告警（净电荷、无法中和的永久电荷） | 输入已是专业工具（如 Dimorphite-DL）生成的目标态 |

## pH 规则表的定位（必须如实说明）

这是**基于官能团 pKa 的规则近似**，不是 pKa 预测：命中哪个官能团、用哪个 pKa、
做了加/减质子都在返回的 `rules` 里逐条列出，可人工核对。
若要更精细的微观态分布，请用专业工具生成目标 pH 下的质子化态并以 SDF 提供，
再用策略 `keep` 对接 —— 系统不会假装自己是 pKa 计算器。

**主键纪律**：只处理"用什么化学形式对接"，**原始 SMILES 始终保留**（对接行主键不变）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from rdkit import Chem

logger = logging.getLogger(__name__)

#: 可选策略（运行级）
PROTONATION_POLICIES: Tuple[str, ...] = ("keep", "neutralize", "ph")
#: 默认目标 pH（生理 pH）
DEFAULT_PH = 7.4
#: 合法 pH 区间。**下界不能是 0**：0 是"未设置"哨兵（界面/接口用 0 表示"用设置页的值"），
#: 真实运行里踩到过 `protonation_ph=0` → pH 0 → 全部按极端强酸处理的事故。
PH_MIN = 0.5
PH_MAX = 14.0
#: 默认策略：按目标生理 pH 分配质子化态（对接的通行假设；比"一律中性"更接近真实条件）。
#: 仍然只是运行级的一个默认值，用户可在界面一键改为 neutralize / keep。
DEFAULT_POLICY = "ph"
#: 界面上的常用 pH 预设（仅作为 datalist 提示，不限制取值）
PH_PRESETS: Tuple[Tuple[str, float], ...] = (
    ("胃酸 1.5", 1.5), ("胃 2.0", 2.0), ("溶酶体 4.5", 4.5),
    ("弱酸 5.5", 5.5), ("生理 7.4", 7.4), ("弱碱 8.0", 8.0),
)

#: 内置 pKa 规则表：`site` 是 SMARTS 匹配元组里要加/减质子的原子下标。
#: `kind="acid"`：pH > pKa 时去质子化（阴离子）；`kind="base"`：pH < pKa 时质子化（阳离子）。
#: 顺序 = 处理顺序（先特殊、后常见；已处理过的原子不再重复处理）。
PKA_RULES: Tuple[Dict[str, Any], ...] = (
    # ---- 酸性基团 ----
    {"name": "磺酸", "smarts": "[SX4](=O)(=O)[OX2H1]", "site": 3, "pka": -1.0, "kind": "acid"},
    {"name": "磷酸/膦酸", "smarts": "[PX4](=O)[OX2H1]", "site": 2, "pka": 2.1, "kind": "acid"},
    {"name": "羧酸", "smarts": "[CX3](=O)[OX2H1]", "site": 2, "pka": 4.5, "kind": "acid"},
    {"name": "四氮唑", "smarts": "[nH1]1nnnc1", "site": 0, "pka": 4.9, "kind": "acid"},
    {"name": "硫醇", "smarts": "[SX2H1]", "site": 0, "pka": 8.5, "kind": "acid"},
    {"name": "磺酰胺", "smarts": "[SX4](=O)(=O)[NX3;H1,H2]", "site": 2, "pka": 9.5, "kind": "acid"},
    {"name": "酚羟基", "smarts": "[OX2H1]c", "site": 0, "pka": 10.0, "kind": "acid"},
    # ---- 碱性基团 ----
    {"name": "胍", "smarts": "[NX3][CX3](=[NX2])[NX3]", "site": 2, "pka": 13.0, "kind": "base"},
    {"name": "脒", "smarts": "[NX3][CX3]=[NX2]", "site": 2, "pka": 12.5, "kind": "base"},
    {"name": "脂肪胺",
     "smarts": "[NX3;H1,H2;!$(NC=O);!$(N=*);!$(N#*);!$(N[N+]#N);!$(N[SX4]=O);!$(Nc)]",
     "site": 0, "pka": 10.0, "kind": "base"},
    # 咪唑类：五元环里另一个 N 必须带 H（排除咖啡因/嘌呤这类 N-取代咪唑，其 pKa 远低于 7）
    {"name": "咪唑类 N", "smarts": "[nX2;H0]1cc[nH]c1", "site": 0, "pka": 7.0, "kind": "base"},
    # 吡啶类：**六元**芳环上的 N（r6 排除四氮唑/吡唑等酸性五元唑，避免把酸环当碱处理）
    {"name": "吡啶类 N", "smarts": "[nX2;H0;r6]", "site": 0, "pka": 5.2, "kind": "base"},
)
#: 规则表版本（写进溯源，便于审计"当时的判定依据是哪一版"）
PKA_TABLE_VERSION = "v1-2026.09"
#: 写进溯源与报告的免责说明
PKA_DISCLAIMER = ("内置官能团 pKa 规则表的近似处理，不是 pKa 预测；"
                  "如需微观态分布请用专业工具生成质子化态后以 SDF 提供并选择 keep")


def _env_policy() -> str:
    from docking_agent.config import env

    return str(env("LIGAND_PROTONATION", "") or "").strip().lower()


def _run_request() -> Dict[str, Any]:
    """本次运行请求里的参数（没有运行上下文时返回空）。"""
    from docking_agent import run_context

    return run_context.run_request_or_empty()


def protonation_policy(explicit: Optional[str] = None) -> str:
    """质子化策略的**分层解析**：显式参数 → 本次运行请求 → 环境变量（设置页）→ 默认 ph（pH 7.4）。

    运行请求优先于环境变量：同一次运行里的所有分子必须用同一条规则（口径一致），
    而环境变量是全局的，多会话并发时不能代表"这一次"。
    """
    from docking_agent.config import env

    candidates: List[Any] = [explicit, _run_request().get("protonation"), env("LIGAND_PROTONATION", "")]
    for candidate in candidates:
        policy = str(candidate or "").strip().lower()
        if policy in PROTONATION_POLICIES:
            return policy
    return DEFAULT_POLICY


def _coerce_ph(value: Any) -> Optional[float]:
    """把输入收敛成合法 pH；0 / 空 / 非数字 / 越界都返回 None（= 用下一层来源或默认）。"""
    if value is None or value == "" or value is False:
        return None
    try:
        ph = float(value)
    except (TypeError, ValueError):
        return None
    if ph == 0:            # 哨兵：界面/接口用 0 表示"用设置页的值"
        return None
    return ph if PH_MIN <= ph <= PH_MAX else None


def protonation_ph(explicit: Any = None) -> float:
    """目标 pH 的分层解析：显式参数 → 本次运行请求 → 环境变量 → 默认 7.4。

    非法值（非数字、0 哨兵、超出 {PH_MIN}–{PH_MAX}）一律回退下一层/默认 ——
    绝不因为一个设置写错就改变化学口径（真实事故：`protonation_ph=0` 被当成 pH 0 跑完一次运行）。
    """
    from docking_agent.config import env

    for candidate in (explicit, _run_request().get("protonation_ph"), env("LIGAND_PROTONATION_PH", "")):
        ph = _coerce_ph(candidate)
        if ph is not None:
            return ph
    return DEFAULT_PH


def _uncharge(mol: Any) -> Any:
    from rdkit.Chem.MolStandardize import rdMolStandardize  # noqa: PLC0415

    return rdMolStandardize.Uncharger().uncharge(mol)


def _set_site(mol: Any, idx: int, protonate: bool) -> bool:
    """在指定原子加/减一个质子并相应调整形式电荷。失败返回 False（调用方回滚）。"""
    try:
        atom = mol.GetAtomWithIdx(int(idx))
        total_h = int(atom.GetTotalNumHs())
        if protonate:
            atom.SetNumExplicitHs(total_h + 1)
            atom.SetFormalCharge(atom.GetFormalCharge() + 1)
        else:
            if total_h <= 0:
                return False
            atom.SetNumExplicitHs(total_h - 1)
            atom.SetFormalCharge(atom.GetFormalCharge() - 1)
        atom.SetNoImplicit(True)
        return True
    except Exception as e:  # noqa: BLE001 - 单个原子失败按未处理
        logger.debug("调整质子化态失败（原子 %s）：%s", idx, e)
        return False


def apply_ph_rules(mol: Any, ph: float) -> Tuple[Optional[Any], List[Dict[str, Any]], str]:
    """把中性分子按目标 pH 重新分配质子化态，返回 `(新分子|None, 命中规则明细, 错误说明)`。"""
    try:
        work = Chem.RWMol(mol)
    except Exception as e:  # noqa: BLE001
        return None, [], f"无法复制分子（{type(e).__name__}: {e}）"
    touched: set = set()
    hits: List[Dict[str, Any]] = []
    for rule in PKA_RULES:
        pattern = Chem.MolFromSmarts(str(rule["smarts"]))
        if pattern is None:  # pragma: no cover - 规则表写错时不应影响对接
            logger.warning("pKa 规则 SMARTS 无效，已跳过：%s", rule.get("name"))
            continue
        site_offset = int(rule["site"])
        pka = float(rule["pka"])
        if rule["kind"] == "acid":
            if float(ph) <= pka:
                continue          # pH 低于 pKa：酸保持中性（输入已先中和过，无需再动）
            protonate = False     # pH 高于 pKa：去质子化 → 阴离子
        else:
            if float(ph) >= pka:
                continue          # pH 高于 pKa：碱保持中性
            protonate = True      # pH 低于 pKa：加质子 → 阳离子
        for match in work.GetSubstructMatches(pattern):
            if len(match) <= site_offset:
                continue
            site = int(match[site_offset])
            if any(int(i) in touched for i in match):
                continue          # 同一官能团（或其中任一原子）已处理过 → 不重复加/减质子
            if not _set_site(work, site, protonate):
                continue
            # 整组原子都标记：胍/脒被质子化后，其余 N 不应再被"脂肪胺"规则二次质子化
            touched.update(int(i) for i in match)
            hits.append({"name": rule["name"], "pka": rule["pka"], "kind": rule["kind"],
                         "site_atom": site,
                         "action": "加质子（阳离子）" if protonate else "去质子（阴离子）",
                         "rule": f"pH {ph:g} {'<' if protonate else '>'} pKa {rule['pka']:g}"})
    if not touched:
        return None, [], ""
    try:
        Chem.SanitizeMol(work)
        return work.GetMol(), hits, ""
    except Exception as e:  # noqa: BLE001 - 规则表给出的状态不合法时回滚到中性形式
        return None, [], f"按 pH 分配后的结构不合法（{type(e).__name__}: {e}）"


#: 逐分子质子化溯源里**聚合口径**需要的字段（报告 §1.3/§4 的统计只读这些）。
#:
#: 其余字段（`method`/`note`/`variants`/`variant_rule`/`engine_window`/`engine_precision`/`rules`）
#: 是**审计明细**：完整记录留在工具产物里，给 Agent 的视图只带聚合字段 ——
#: 一个 `method` 字符串（"dimorphite-dl 2.0.2（专业 pKa 引擎；pH 7.4 ± 0.5）"）就占 60+ 字符，
#: 而它完全等价于 `engine`+`engine_version`+`ph`+`engine_window` 四个数据字段；
#: 逐分子重复一遍会让大库的属性/对接载荷白白膨胀（实测属性行 82% 的字节是这段溯源）。
PROTONATION_AGGREGATE_FIELDS = ("policy", "applied", "engine", "engine_version", "engine_fallback_reason",
                                "ph", "charge_before", "charge_after")


def compact_protonation(info: Any) -> Dict[str, Any]:
    """把逐分子质子化溯源压成**聚合口径**字段（数据保留，散文细节交给产物）。"""
    if not isinstance(info, dict):
        return {}
    out = {k: info[k] for k in PROTONATION_AGGREGATE_FIELDS if info.get(k) not in (None, "")}
    if "applied" in info:
        out["applied"] = bool(info.get("applied"))
    return out


def apply_protonation(smiles: str, policy: Optional[str] = None,
                      ph: Any = None) -> Tuple[str, Dict[str, Any]]:
    """按运行级策略处理配体质子化态，返回 `(处理后的 SMILES, 溯源)`。

    `applied` 以**规范 SMILES 是否变化**判定（不是净电荷是否变化）：
    像甘氨酸这种在 pH 7.4 变成两性离子（净电荷仍为 0）的情况，形式确实变了，必须如实记录。
    """
    raw = str(smiles or "").strip()
    chosen = protonation_policy(policy)
    info: Dict[str, Any] = {"policy": chosen, "applied": False, "method": ""}
    mol = Chem.MolFromSmiles(raw) if raw else None
    if mol is None:
        info["charge_after"] = None
        info["note"] = "SMILES 无法解析，质子化策略未执行"
        return raw, info
    before = int(Chem.GetFormalCharge(mol))
    canonical_in = Chem.MolToSmiles(mol)
    info["charge_before"] = before

    if chosen == "keep":
        info["charge_after"] = before
        info["note"] = (f"保留输入的净电荷 {before:+d}（未改动）" if before
                        else "中性分子，无电荷可处理")
        return raw, info

    if chosen == "ph":
        target_ph = protonation_ph(ph)
        # ---- 优先用**专业 pKa 引擎**（Dimorphite-DL）；不可用/异常时回退内置规则表 ----
        from docking_agent.core import ligand_pka

        engine_result = ligand_pka.protonate(raw, target_ph)
        if engine_result.get("ok"):
            out_smiles = str(engine_result["smiles"])
            after = int(engine_result.get("charge") or 0)
            variants = list(engine_result.get("variants") or [out_smiles])
            info.update({
                "ph": target_ph, "charge_after": after,
                "applied": out_smiles != canonical_in,
                "engine": engine_result.get("engine") or "",
                "engine_version": engine_result.get("version") or "",
                "engine_window": engine_result.get("window"),
                "engine_precision": engine_result.get("precision"),
                "variants": variants,
                "variant_rule": engine_result.get("rule") or "",
                "method": (f"{engine_result.get('engine')} {engine_result.get('version')}"
                           f"（专业 pKa 引擎；pH {target_ph:g}"
                           f" ± {float(engine_result.get('window') or 0):g}）"),
            })
            extra = ""
            if len(variants) > 1:
                extra = (f"；窗口内共 {len(variants)} 个微观态，"
                         f"按「{engine_result.get('rule')}」选定对接形式"
                         "（需要逐态枚举或指定其它微观态时，请以 SDF 提供并选择 `keep`）")
            info["note"] = (f"目标 pH {target_ph:g}：净电荷 {before:+d} → {after:+d}；"
                            f"引擎 {engine_result.get('engine')} "
                            f"{engine_result.get('version')}{extra}")
            return out_smiles, info

        # 引擎不可用 / 配置强制 rules → 内置规则表（近似），并把回退原因如实记录
        info["engine_fallback_reason"] = str(engine_result.get("reason") or "专业 pKa 引擎不可用")
        if engine_result.get("install"):
            info["engine_install"] = str(engine_result["install"])
        info.update({"ph": target_ph,
                     "method": f"内置 pKa 规则表 {PKA_TABLE_VERSION}（{PKA_DISCLAIMER}）"})
        try:
            neutral = _uncharge(mol)
        except Exception as e:  # noqa: BLE001
            info["charge_after"] = before
            info["note"] = f"pH 处理前的中和失败（{type(e).__name__}: {e}）"
            return raw, info
        new_mol, hits, err = apply_ph_rules(neutral, target_ph)
        if err or new_mol is None:
            # 规则命中但结构不合法 / 无规则命中：**退回中性形式**并说明（不静默保持离子态）
            fallback = Chem.MolToSmiles(neutral)
            info["charge_after"] = int(Chem.GetFormalCharge(neutral))
            info["rules"] = hits
            info["applied"] = fallback != canonical_in
            info["note"] = (err or f"pH {target_ph:g} 下没有命中的可电离官能团 → 按中性形式对接")
            if info["applied"]:
                info["note"] += f"（净电荷 {before:+d} → {info['charge_after']:+d}）"
            info["note"] += f"；专业引擎不可用（{info['engine_fallback_reason']}）"
            return fallback, info
        canonical_out = Chem.MolToSmiles(new_mol)
        after = int(Chem.GetFormalCharge(new_mol))
        info.update({"charge_after": after, "rules": hits, "applied": canonical_out != canonical_in})
        changed = "、".join(f"{h['name']}({h['action']}, pKa {h['pka']:g})" for h in hits) or "无"
        info["note"] = (f"目标 pH {target_ph:g}：净电荷 {before:+d} → {after:+d}；"
                        f"命中规则 {changed}；专业引擎不可用（{info['engine_fallback_reason']}）")
        return canonical_out, info

    # ---- neutralize：只处理带净电荷的分子（非默认策略） ----
    if before == 0:
        info["charge_after"] = 0
        info["note"] = "净电荷为 0，无需处理"
        return raw, info
    try:
        neutral = _uncharge(mol)
    except Exception as e:  # noqa: BLE001 - 中和失败按"未处理"如实上报
        info["charge_after"] = before
        info["note"] = f"中和失败（{type(e).__name__}: {e}）"
        return raw, info
    after = int(Chem.GetFormalCharge(neutral))
    canonical_out = Chem.MolToSmiles(neutral)
    info["charge_after"] = after
    if canonical_out == canonical_in:
        info["note"] = f"净电荷 {before:+d} 无法中和（例如季铵/永久电荷），保持原样"
        return raw, info
    info.update({"applied": True, "method": "rdkit.Uncharger",
                 "note": f"净电荷 {before:+d} → {after:+d}（原始 SMILES 原样保留在记录里）"})
    return canonical_out, info


# 兼容旧名（内部与测试用过 `_protonation_policy`）
_protonation_policy = protonation_policy
