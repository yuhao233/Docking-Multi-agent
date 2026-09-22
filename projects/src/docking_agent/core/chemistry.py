"""理化性质与结合模式相似度（全部为真实 RDKit 计算）。

结合模式分析由三部分构成，均基于真实计算、可复现：
  1. **双指纹相似度**：Morgan(radius=2, 2048bit) 与 MACCS keys 的 Tanimoto 相似度；
  2. **药效团锚定基团**：SMARTS 匹配脒基/胍基/羧酸等关键基团（丝氨酸蛋白酶 S1 口袋锚定特征）；
  3. **理化性质差异**：分子量 / logP / TPSA 相对阳性对照的差值。
综合以上给出 `structural_consistency`（高/中/低）与 `binding_mode_hint`。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from rdkit import Chem
from rdkit.Chem import AllChem, Crippen, Descriptors, rdMolDescriptors
from rdkit.DataStructs import TanimotoSimilarity
from rdkit.Chem import MACCSkeys

logger = logging.getLogger(__name__)

# 关键药效团基团（SMARTS，真实子结构匹配）
SMARTS_PATTERNS: Dict[str, str] = {
    "amidine": "[NX3][CX3]=[NX2]",                 # 脒基：凝血酶 S1 口袋经典锚定基团
    "guanidine": "[NX3][CX3](=[NX2])[NX3]",        # 胍基
    "carboxylic_acid": "[CX3](=O)[OX2H1]",         # 羧酸
    "sulfonamide": "[SX4](=O)(=O)[NX3]",           # 磺酰胺
    "aromatic_ring": "c1ccccc1",                   # 芳环
}
_PATTERNS: Dict[str, Any] = {}


def _pattern(name: str):
    if name not in _PATTERNS:
        _PATTERNS[name] = Chem.MolFromSmarts(SMARTS_PATTERNS[name])
    return _PATTERNS[name]


def _to_mol(smiles: str):
    mol = Chem.MolFromSmiles(smiles) if smiles else None
    if mol is None:
        raise ValueError("无法解析 SMILES: " + str(smiles))
    return mol


# --------------------------------------------------------------------------- #
# 理化性质
# --------------------------------------------------------------------------- #
def compute_properties(smiles: str, protonation: Optional[str] = None,
                       ph: Any = None) -> Dict[str, Any]:
    """计算分子真实物化性质与类药性指标。

    `protonation` 为运行级质子化策略（见 `core.protonation.apply_protonation`），
    `ph` 为 `ph` 策略下的目标 pH（缺省 7.4）。
    **与对接同口径**：对接用的是中和后的形式，性质就必须按同一形式算，
    否则报告里 MW/logP 与对接结果属于两种化学形式，排序会失去可比性。

    **主键纪律**：返回的 `smiles` 始终是**传入的原始 SMILES**（合并/排序/去重都以它为主键），
    实际用于计算的形式放在 `protonated_smiles`（仅当与原始不同时出现），
    溯源放在 `protonation`。真实缺陷（本轮 e2e 实测）：若把 `smiles` 换成中和后的形式，
    这些分子就与对接行（主键是原始 SMILES）**对不上**，排序表里的分子量/logP 整列变空。
    """
    from docking_agent.core.ligands import apply_protonation

    input_smiles = smiles
    smiles, prot = apply_protonation(smiles, protonation, ph)
    mol = _to_mol(smiles)
    mw = Descriptors.MolWt(mol)
    logp = Crippen.MolLogP(mol)
    tpsa = rdMolDescriptors.CalcTPSA(mol)
    hbd = rdMolDescriptors.CalcNumHBD(mol)
    hba = rdMolDescriptors.CalcNumHBA(mol)
    rotb = rdMolDescriptors.CalcNumRotatableBonds(mol)
    heavy = mol.GetNumHeavyAtoms()
    rings = rdMolDescriptors.CalcNumAromaticRings(mol)
    formula = rdMolDescriptors.CalcMolFormula(mol)
    # Lipinski 规则违反项（罗氏五规则）
    violations = 0
    if mw > 500:
        violations += 1
    if logp > 5:
        violations += 1
    if hbd > 5:
        violations += 1
    if hba > 10:
        violations += 1
    if rotb > 10:  # 可旋转键过多，仅作参考提示
        violations += 1
    return {
        "smiles": input_smiles,
        "protonation": prot,
        **({"protonated_smiles": smiles} if prot.get("applied") else {}),
        "molecular_weight": round(mw, 2),
        "logP": round(logp, 2),
        "tpsa": round(tpsa, 2),
        "hbd": hbd,
        "hba": hba,
        "rotatable_bonds": rotb,
        "heavy_atoms": heavy,
        "aromatic_rings": rings,
        "formula": formula,
        "lipinski_violations": violations,
        "drug_likeness_pass": violations <= 1,
    }


# --------------------------------------------------------------------------- #
# 指纹与相似度
# --------------------------------------------------------------------------- #
def morgan_fingerprint(smiles: str, radius: int = 2, n_bits: int = 2048):
    """Morgan 指纹（RDKit 2026 推荐 MorganGenerator，旧版本回退 AllChem 接口）。"""
    mol = _to_mol(smiles)
    try:
        from rdkit.Chem import rdFingerprintGenerator  # type: ignore

        return rdFingerprintGenerator.GetMorganGenerator(radius=radius, fpSize=n_bits).GetFingerprint(mol)
    except Exception:  # noqa: BLE001
        return AllChem.GetMorganFingerprintAsBitVect(mol, radius, nBits=n_bits)


# 兼容既有调用与外部脚本
_fingerprint = morgan_fingerprint


def maccs_fingerprint(smiles: str):
    """MACCS keys 指纹（166 位；与 Morgan 互补，用于交叉验证相似性判断）。"""
    return MACCSkeys.GenMACCSKeys(_to_mol(smiles))


def tanimoto(fp_a, fp_b) -> float:
    return float(TanimotoSimilarity(fp_a, fp_b))


def compute_tanimoto(smiles_a: str, smiles_b: str) -> float:
    """Morgan 指纹 Tanimoto 相似度（0~1）。"""
    return tanimoto(morgan_fingerprint(smiles_a), morgan_fingerprint(smiles_b))


def compute_maccs_similarity(smiles_a: str, smiles_b: str) -> float:
    """MACCS keys Tanimoto 相似度（0~1）。"""
    return tanimoto(maccs_fingerprint(smiles_a), maccs_fingerprint(smiles_b))


def pharmacophore_flags(smiles: str) -> Dict[str, Any]:
    """真实子结构匹配：关键药效团基团是否存在。"""
    mol = _to_mol(smiles)
    flags = {name: bool(mol.HasSubstructMatch(_pattern(name))) for name in SMARTS_PATTERNS}
    flags["cationic_anchor"] = bool(flags["amidine"] or flags["guanidine"])
    return flags


# --------------------------------------------------------------------------- #
# 结合模式分析（与阳性对照比较）
# --------------------------------------------------------------------------- #
def _consistency(morgan_sim: float, maccs_sim: float) -> str:
    """综合双指纹给出结构一致性等级。"""
    combined = (morgan_sim + maccs_sim) / 2.0
    if combined >= 0.7 or morgan_sim >= 0.85:
        return "high"
    if combined >= 0.4 or morgan_sim >= 0.5:
        return "medium"
    return "low"


def _hint(morgan_sim: float, anchor_match: bool, has_anchor: bool) -> str:
    if morgan_sim >= 0.7:
        base = "与阳性对照整体骨架高度相似，结合模式可能高度重叠"
    elif morgan_sim >= 0.4:
        base = "与阳性对照存在中等相似，可共享部分结合位点基团"
    else:
        base = "与阳性对照骨架差异较大，可能呈现不同的结合模式"
    if anchor_match:
        return base + "；且同样带有脒基/胍基类锚定基团，仍可能以相近取向锚定 S1 口袋"
    if not has_anchor:
        return base + "；分子本身缺少脒基/胍基锚定基团，与 S1 口袋的结合取向可能明显不同"
    return base


def binding_mode_profile(name: str, smiles: str, control_smiles: str,
                         control_props: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """单分子 vs 阳性对照的结合模式分析（真实计算，字段完整）。"""
    props = compute_properties(smiles)
    ctrl = control_props or compute_properties(control_smiles)

    morgan_sim = compute_tanimoto(smiles, control_smiles)
    maccs_sim = compute_maccs_similarity(smiles, control_smiles)
    flags = pharmacophore_flags(smiles)
    ctrl_flags = pharmacophore_flags(control_smiles)

    def _delta(key: str) -> Optional[float]:
        a, b = props.get(key), ctrl.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return round(a - b, 2)
        return None

    return {
        "name": name,
        "smiles": smiles,
        # 主相似度（Morgan）：图表与 CSV 沿用该字段，语义不变
        "similarity_to_positive_control": round(morgan_sim, 3),
        "morgan_tanimoto": round(morgan_sim, 3),
        "maccs_tanimoto": round(maccs_sim, 3),
        "combined_similarity": round((morgan_sim + maccs_sim) / 2.0, 3),
        "aromatic_rings": props.get("aromatic_rings"),
        "rotatable_bonds": props.get("rotatable_bonds"),
        "pharmacophore": flags,
        # 逐行**不再重复**对照药效团：它只与对照有关，已由 report 顶层的
        # `control_pharmacophore` 给一次（逐分子重复会让载荷按分子数线性膨胀）
        "anchor_match": bool(flags.get("cationic_anchor") and ctrl_flags.get("cationic_anchor")),
        "mw_delta": _delta("molecular_weight"),
        "logp_delta": _delta("logP"),
        "tpsa_delta": _delta("tpsa"),
        "structural_consistency": _consistency(morgan_sim, maccs_sim),
        "binding_mode_hint": _hint(morgan_sim, bool(flags.get("cationic_anchor") and ctrl_flags.get("cationic_anchor")),
                                   bool(flags.get("cationic_anchor"))),
    }


def compute_binding_report(molecules: List[Dict[str, str]],
                           positive_control: str) -> Dict[str, Any]:
    """对每个分子计算与阳性对照的结合模式分析，按相似度降序返回。

    返回 {"positive_control":..., "control_properties": {...}, "rows": [...]}，
    rows 中每项含 similarity_to_positive_control（Morgan）、maccs_tanimoto、
    structural_consistency 与 binding_mode_hint。
    """
    if not positive_control:
        raise ValueError("缺少阳性对照 SMILES，无法进行结合模式分析")
    ctrl_props = compute_properties(positive_control)
    ctrl_flags = pharmacophore_flags(positive_control)
    rows = [binding_mode_profile(m.get("name") or m.get("smiles"), m["smiles"], positive_control, ctrl_props)
            for m in molecules]
    rows.sort(key=lambda r: r["similarity_to_positive_control"], reverse=True)
    return {
        "positive_control": positive_control,
        "control_properties": ctrl_props,
        "control_pharmacophore": ctrl_flags,
        "rows": rows,
    }
