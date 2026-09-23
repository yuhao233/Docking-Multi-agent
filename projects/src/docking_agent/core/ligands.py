"""小分子库：SMILES 文本解析、文件读取（SDF/SMI/CSV/MOL2）与 3D 构象/PDBQT 准备。"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from rdkit import Chem
from rdkit.Chem import AllChem, Descriptors

from docking_agent.core.files import _fetch_file

logger = logging.getLogger(__name__)



_SMILES_LIKE = None


def _looks_like_smiles(text: str) -> bool:
    """字符集启发式判断（用于区分「名称」与「SMILES」）。

    不使用 RDKit 试探解析：那会对中文名称打出一串 SMILES 解析错误日志，干扰用户。
    """
    global _SMILES_LIKE
    if _SMILES_LIKE is None:
        import re

        _SMILES_LIKE = re.compile(r"^[A-Za-z0-9@+\-\[\]\(\)=#$:/\\.*%]+$")
    text = (text or "").strip()
    return bool(text) and bool(_SMILES_LIKE.match(text))


def _split_candidates(text: str) -> List[str]:
    """把文本切成「候选片段」：分隔符包括 , ; 换行 与空白。

    为什么要切空白：聊天里最常见的写法是「华法林 CC(=O)CC(...)」，名称与 SMILES 之间是空格。
    只按 ,;
 切会把整句当成一个 SMILES，结果一个分子都解析不出来 —— 而系统接着会
    悄悄退回**示例分子库**，用户看到的是别人的分子被对接（真实发生过的坑）。
    """
    import re

    normalized = (text or "").replace("：", ":").replace("，", ",").replace("；", ";")
    # 「、」是中文里最常见的并列顿号（"筛一下 CCO、CCN"）。不认它就会把整段当成一个片段、
    # 一个分子都抽不出来 → 受理层误判「没有分子来源」，向用户重复索要已经给过的分子。
    normalized = normalized.replace("、", ",")
    normalized = normalized.replace("\t", " ").replace("\n", "\n")
    return [tok for tok in re.split(r"[,;\n\s]+", normalized) if tok.strip()]


def extract_smiles(text: str, default_name_prefix: str = "MOL") -> List[Dict[str, str]]:
    """从**自由文本**里尽力抽取 SMILES（确定性，零模型）。

    规则：按分隔符（含空白）切片段 → 能解析成分子的片段算 SMILES →
    紧邻其前、且**本身不是 SMILES** 的短片段当作它的名称（如「华法林 <smiles>」）。
    这样即使用户写成一句话（"帮我筛一下：华法林 <smiles>；布洛芬 <smiles>"）也能抽出两个分子。
    句子性文字（较长/含句末标点）只作散文忽略，不会被当成名称。
    """
    molecules: List[Dict[str, str]] = []
    seen: set = set()
    pending_name: Optional[str] = None
    for token in _split_candidates(text):
        token = token.strip()
        if not token:
            continue
        mol = Chem.MolFromSmiles(token) if _looks_like_smiles(token) else None
        if mol is None:
            # 不是分子 → 视作「可能的名字」：取最后一段冒号后的内容（"…：华法林" → "华法林"），
            # 并排除散文（过长或含句末标点），避免把整句话当分子名。
            raw_name = token.split(":")[-1].strip()
            pending_name = (raw_name if raw_name and len(raw_name) <= 24
                            and not any(c in raw_name for c in "。！？!?") else None)
            continue
        smiles = Chem.MolToSmiles(mol)
        if smiles in seen:
            pending_name = None
            continue
        seen.add(smiles)
        name = pending_name if (pending_name and not _looks_like_smiles(pending_name)) else None
        molecules.append({"name": name or f"{default_name_prefix}{len(molecules) + 1}",
                          "smiles": smiles})
        pending_name = None
    return molecules


def parse_smiles_text(text: str, default_name_prefix: str = "MOL") -> List[Dict[str, str]]:
    """从文本解析分子库。支持：
      - 纯 SMILES 列表（逗号/分号/换行/空白分隔）：SMILES1,SMILES2;SMILES3
      - '名称:SMILES' 显式命名格式（名称可为中文）
      - '名称 SMILES'（空格分隔，如「华法林 CC(=O)CC(...)」）
      - 一句话里夹带的分子（散文会被忽略，只取能解析成分子的片段）
    返回 [{"name":..., "smiles":...}]
    说明：RDKit 标准 SMILES 通常不含 ':'，因此可用 ':' 区分「名称:SMILES」。
    """

    molecules: List[Dict[str, str]] = []
    seen: set = set()
    # '名称:SMILES' 先单独处理（冒号左值可能是任意文本，交给通用抽取会丢名字）
    for token in _split_candidates(text):
        if ":" not in token:
            continue
        left, right = token.split(":", 1)
        right = right.strip()
        if not right:
            continue
        mol = Chem.MolFromSmiles(right) if _looks_like_smiles(right) else None
        if mol is None:
            continue
        smiles = Chem.MolToSmiles(mol)
        if smiles in seen:
            continue
        seen.add(smiles)
        name = left.strip() if (left.strip() and not _looks_like_smiles(left.strip())) else None
        molecules.append({"name": name or f"{default_name_prefix}{len(molecules) + 1}",
                          "smiles": smiles})
    for item in extract_smiles(text, default_name_prefix):
        if item["smiles"] in seen:
            continue
        seen.add(item["smiles"])
        molecules.append({"name": item["name"], "smiles": item["smiles"]})
    return molecules


def load_library_file(path: str) -> List[Dict[str, str]]:
    """从 CSV 分子库文件加载（表头 name,smiles）。"""
    import csv
    molecules = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("name") or "").strip()
            smiles = (row.get("smiles") or "").strip()
            if smiles and Chem.MolFromSmiles(smiles) is not None:
                molecules.append({"name": name or f"MOL{len(molecules)+1}", "smiles": smiles})
    return molecules


# --------------------------------------------------------------------------- #
# 分子属性评估（真实 RDKit 计算）
# --------------------------------------------------------------------------- #


# 对接前对"特殊化学"的处理原则：**工具只报事实，判断交给 Agent**。
# 盐/反离子属于最常见的"看起来能跑、其实跑错"的输入：
# 直接把 `CC(=O)O.[Na+]` 丢给 Vina，多出来的 Na+ 会参与打分并污染结果。
# 因此这里做**保守且可解释**的预处理：多片段时取最大的有机片段，
# 并把原始 SMILES、被移除片段与其判定理由逐条写进 facts，绝不悄悄改动。
_METALS = {
    "Li", "Be", "Na", "Mg", "Al", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni",
    "Cu", "Zn", "Ga", "Ge", "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag",
    "Cd", "In", "Sn", "Sb", "Cs", "Ba", "La", "Ce", "Pt", "Au", "Hg", "Tl", "Pb", "Bi",
}
_COMMON_COUNTERIONS = {
    "Na", "K", "Li", "Cs", "Rb", "Mg", "Ca", "Zn", "Fe", "Mn", "Cu", "Cl", "Br", "I", "F",
}
_SOLVENT_FRAGMENTS = {
    "O",                     # 水
    "CS(C)=O",               # DMSO
    "CC(N)=O",               # 乙酰胺
    "CC(=O)O",               # 乙酸
    "CC(=O)[O-]",
    "OCCO",                  # 乙二醇
    "C1COCCO1",              # 二氧六环
    "ClC(Cl)Cl",             # 氯仿
    "FC(F)(F)C(=O)O",        # TFA
    "OS(=O)(=O)O",           # 硫酸
    "O=C([O-])C(=O)[O-]",    # 草酸根
    "CCO",                   # 乙醇
    "CO",                    # 甲醇
    "CCOC(C)=O",             # 乙酸乙酯
}


#: 质子化态策略与 pH 处理见 `core/protonation.py`（运行级口径、内置 pKa 规则表、逐分子溯源）。
#: 这里按原路径再导出，保持既有 import/测试不漂移。
from docking_agent.core.protonation import (  # noqa: E402
    DEFAULT_PH,
    PKA_RULES,
    PKA_TABLE_VERSION,
    PH_PRESETS,
    PROTONATION_POLICIES,
    _protonation_policy,
    apply_protonation,
    protonation_ph,
    protonation_policy,
)

__all__ = ["PROTONATION_POLICIES", "apply_protonation", "protonation_policy", "protonation_ph",
           "DEFAULT_PH", "PH_PRESETS", "PKA_RULES", "PKA_TABLE_VERSION"]


def describe_ligand(smiles: str, protonation: Optional[str] = None,
                    ph: Any = None) -> Dict[str, Any]:
    """对配体 SMILES 做**事实性**体检：片段/盐、金属、电荷、大小、未定义手性、质子化态。

    `protonation` 为运行级质子化策略（ph/neutralize/keep，缺省读 `LIGAND_PROTONATION`，默认 ph），
    `ph` 为 `ph` 策略下的目标 pH（缺省读 `LIGAND_PROTONATION_PH`，默认 7.4）：
    片段剥离后再统一处理质子化态，逐分子记录溯源（见 `apply_protonation`）。

    返回 {"ok", "smiles"(建议用于对接), "original_smiles", "input_smiles", "facts": {...},
          "warnings": [...], "removed_fragments": [...], "protonation": {...}}；
    解析失败时 ok=False 且给出原因。
    """
    facts: Dict[str, Any] = {}
    warnings: List[str] = []
    removed: List[Dict[str, str]] = []
    raw = (smiles or "").strip()
    if not raw:
        return {"ok": False, "smiles": raw, "original_smiles": raw, "input_smiles": raw,
                "facts": facts, "warnings": ["SMILES 为空"], "removed_fragments": [],
                "protonation": {"policy": _protonation_policy(protonation), "applied": False,
                                "note": "SMILES 为空，未执行"}}
    mol = Chem.MolFromSmiles(raw)
    if mol is None:
        return {"ok": False, "smiles": raw, "original_smiles": raw, "input_smiles": raw,
                "facts": facts, "warnings": [f"SMILES 无法解析：{raw}"], "removed_fragments": [],
                "protonation": {"policy": _protonation_policy(protonation), "applied": False,
                                "note": "SMILES 无法解析，未执行"}}

    frags = Chem.GetMolFrags(mol, asMols=True, sanitizeFrags=True)
    facts["num_fragments"] = len(frags)
    use_smiles = raw
    if len(frags) > 1:
        scored = sorted(frags, key=lambda m: (-Descriptors.HeavyAtomCount(m),
                                              -Descriptors.MolWt(m)))
        main, extras = scored[0], scored[1:]
        main_smi = Chem.MolToSmiles(main)
        for extra in extras:
            smi = Chem.MolToSmiles(extra)
            syms = {a.GetSymbol() for a in extra.GetAtoms()}
            heavy = Descriptors.HeavyAtomCount(extra)
            if len(syms) == 1 and syms & _COMMON_COUNTERIONS and heavy == 1:
                kind = "反离子"
            elif smi in _SOLVENT_FRAGMENTS or set(syms) <= {"C", "O", "H"} and heavy <= 3:
                kind = "溶剂/小分子杂质"
            else:
                kind = "额外片段（可能是共晶/共价片段，需人工确认）"
            removed.append({"smiles": smi, "kind": kind, "heavy_atoms": int(heavy)})
        use_smiles = main_smi
        kinds = "、".join(f"{r['smiles']}({r['kind']})" for r in removed)
        warnings.append(f"SMILES 含 {len(frags)} 个片段，已按最大有机片段 {main_smi} 对接，"
                        f"移除：{kinds}。若这些片段对结合有意义（如共价片段、金属配位），"
                        "请改写输入后重跑。")
        mol = main

    syms_all = {a.GetSymbol() for a in mol.GetAtoms()}
    metal_hits = sorted(syms_all & _METALS)
    facts["has_metal"] = bool(metal_hits)
    if metal_hits:
        warnings.append("配体含金属原子 " + "/".join(metal_hits) +
                        "：Vina/AD4 的原子类型与力场通常不支持金属配位，分数不可信，"
                        "建议改用支持金属的引擎/参数，或由你判断后向用户说明。")
    input_smiles = use_smiles
    use_smiles, protonation = apply_protonation(use_smiles, protonation, ph)
    if protonation.get("applied"):
        mol = Chem.MolFromSmiles(use_smiles) or mol
    facts["formal_charge_input"] = int(protonation.get("charge_before") or 0)
    facts["formal_charge"] = int(Chem.GetFormalCharge(mol))
    facts["protonation"] = protonation
    if protonation.get("applied"):
        facts["canonical_smiles_input"] = Chem.MolToSmiles(Chem.MolFromSmiles(input_smiles)) \
            if Chem.MolFromSmiles(input_smiles) is not None else input_smiles
    # 两条告警彼此独立：**改动过**要说清改了什么；**仍带净电荷**要请用户确认（如无法中和的季铵）
    if protonation.get("applied"):
        # 质子化态调整是一次**有意的化学改动**，必须留痕并让用户能复核
        policy = str(protonation.get("policy") or "")
        hint = ("若目标 pH 下就是以这种形式存在，这就是期望结果；"
                "若要完全保持输入形式，请把「质子化态策略」改为 keep。"
                if policy == "ph" else
                "若要按目标 pH 处理离子态，请把「质子化态策略」改为 ph 并给出目标 pH。")
        warnings.append("质子化态已按运行级策略调整：" + str(protonation.get("note")) +
                        f"；对接用的是 {use_smiles}，原始输入 {input_smiles} 已记录。" + hint)
    if facts["formal_charge"]:
        warnings.append(f"配体带净电荷 {facts['formal_charge']}："
                        f"请确认这与目标条件一致（当前策略 {protonation.get('policy')}；"
                        "季铵等永久电荷无法中和，会原样保留）。")
    try:
        facts["mw"] = round(float(Descriptors.MolWt(mol)), 1)
        facts["heavy_atoms"] = int(Descriptors.HeavyAtomCount(mol))
        facts["rotatable_bonds"] = int(Descriptors.NumRotatableBonds(mol))
        facts["hbd"] = int(Descriptors.NumHDonors(mol))
        facts["hba"] = int(Descriptors.NumHAcceptors(mol))
        facts["tpsa"] = round(float(Descriptors.TPSA(mol)), 1)
        facts["logp"] = round(float(Descriptors.MolLogP(mol)), 2) if hasattr(Descriptors, "MolLogP") else None
        if facts["mw"] > 800 or facts["heavy_atoms"] > 60:
            warnings.append("配体偏大（MW>800 或重原子>60）：Vina 采样可能不充分，"
                            "建议提高 exhaustiveness/盒尺寸或改用大分子对接流程。")
    except Exception:  # noqa: BLE001
        logger.debug("配体描述符计算失败", exc_info=True)
    try:
        # 7 元及以上环：meeko 会按「大环」处理；我们统一按刚性环准备（见 smiles_to_pdbqt），
        # 因此如实记进事实字段，供运行级 notes 与报告说明「环构象未采样」。
        ring_sizes = sorted({len(r) for r in mol.GetRingInfo().AtomRings()} or [])
        facts["max_ring_size"] = max(ring_sizes) if ring_sizes else 0
        facts["rigid_macrocycle_rings"] = [n for n in ring_sizes if n >= 7]
    except Exception:  # noqa: BLE001
        logger.debug("环大小识别失败：%s", raw, exc_info=True)
    try:
        centers = Chem.FindMolChiralCenters(mol, includeUnassigned=True, useLegacyImplementation=False)
        unassigned = [i for i, tag in centers if tag == "?"]
        facts["undefined_stereocenters"] = len(unassigned)
        if unassigned:
            warnings.append(f"有 {len(unassigned)} 个未定义手性中心：3D 构象是其中随机一种，"
                            "如需区分立体异构请提供明确 SMILES。")
    except Exception:  # noqa: BLE001
        logger.debug("手性中心识别失败：%s", raw, exc_info=True)
    facts["canonical_smiles"] = Chem.MolToSmiles(mol)
    return {"ok": True, "smiles": use_smiles, "original_smiles": raw, "input_smiles": input_smiles,
            "facts": facts, "warnings": warnings, "removed_fragments": removed,
            "protonation": protonation}


_METAL_SYMBOLS = frozenset({
    "Li", "Be", "Na", "Mg", "Al", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni",
    "Cu", "Zn", "Ga", "Ge", "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag",
    "Cd", "In", "Sn", "Sb", "Cs", "Ba", "La", "Ce", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
    "Tl", "Pb", "Bi",
})


def _embed_failure_hint(mol: Any) -> str:
    """3D 嵌入失败时给出**可操作**的原因提示（工具只报事实，判断留给 Agent/用户）。"""
    metals: List[str] = []
    try:
        for atom in mol.GetAtoms():
            symbol = atom.GetSymbol()
            if symbol in _METAL_SYMBOLS and symbol not in metals:
                metals.append(symbol)
    except Exception:  # noqa: BLE001 - 提示失败不影响错误本身
        metals = []
    if metals:
        return ("（含金属 " + "/".join(metals) + "：配位结构没有可用的距离几何参数，"
                "建议改用带显式三维坐标的 SDF/MOL2 记录；本次按对接失败如实记录，不纳入排序）")
    return ("（该结构无法生成三维构象；建议改用带显式三维坐标的 SDF/MOL2 记录，"
            "本次按对接失败如实记录，不纳入排序）")


def smiles_to_pdbqt(smiles: str, seed: int = 42) -> str:
    """SMILES -> 3D 构象 -> PDBQT（RDKit ETKDG + meeko）。"""
    from meeko import MoleculePreparation, PDBQTWriterLegacy  # type: ignore
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError("无法解析 SMILES: " + smiles)
    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()  # type: ignore
    params.randomSeed = seed
    embedded = AllChem.EmbedMolecule(mol, params)  # type: ignore
    if embedded != 0:
        params = AllChem.ETKDGv2()  # type: ignore
        params.randomSeed = seed
        embedded = AllChem.EmbedMolecule(mol, params)  # type: ignore
    if embedded != 0:
        # 金属配合物/桥连多片段没有可用的距离几何约束，ETKDG 会失败；
        # 随机坐标初始化不改变任何化学信息，只是换一种起点，能救回一部分（真实缺陷：Mancozeb）。
        params = AllChem.ETKDGv3()  # type: ignore
        params.randomSeed = seed
        params.useRandomCoords = True
        embedded = AllChem.EmbedMolecule(mol, params)  # type: ignore
    if embedded != 0:
        raise ValueError("3D 构象生成失败: " + smiles + _embed_failure_hint(mol))
    try:
        AllChem.MMFFOptimizeMolecule(mol, maxIters=500)  # type: ignore
    except Exception:  # 某些元素缺少力场参数时忽略
        logger.warning("MMFF 优化跳过: %s", smiles)
    # 大环处理：meeko 默认会**切开大环**并插入两个 "glue" 伪原子（元素 G，类型 `CG0`/`G0`）。
    # 真实故障（2026-09-23）：autogrid4 的参数库没有这些类型（实测 CG0/G0/G1/CG/W 全部
    # "unknown ligand atom type"），AutoDock4 与 AutoDock-GPU 因此**必然失败**（一次 2961 条库
    # 里 113 条栽在这里）；内置 Vina 虽能解析，却会把伪原子当原子打分，跨引擎不可比。
    # 因此统一 `rigid_macrocycles=True`：环保持刚性、不切环、无伪原子，各引擎口径一致。
    # 代价（如实记录）：7–33 元环不再采样环构象，取 ETKDG 的单一构象。
    prep = MoleculePreparation(rigid_macrocycles=True)
    setups = prep.prepare(mol)
    if not setups:
        raise ValueError("meeko 配体预准备失败: " + smiles)
    pdbqt_str, ok, err = PDBQTWriterLegacy.write_string(setups[0])
    if not ok:
        raise ValueError(f"PDBQT 写入失败: {err}")
    return pdbqt_str


def read_molecules_any(source: str) -> Tuple[str, List[Dict[str, str]], Dict[str, Any]]:
    """读分子清单：**运行产物 JSON 与常规分子文件都支持**（Agent 之间按文件交接的统一入口）。

    为什么需要：上限 1 万条时把清单塞进消息/上下文不现实，子 Agent 之间应当传**文件路径**。
    运行产物形如 `molecules_tool.json`（`[{...}]` 或 `{"molecules": [...]}`），
    与用户上传的 SDF/CSV/SMI 走同一套归一化层；返回 `(格式, 分子列表, 归一化溯源)`。
    """
    text = str(source or "").strip()
    if text and os.path.isfile(text) and text.lower().endswith(".json"):
        try:
            with open(text, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("按 JSON 读取分子清单失败（%s）：%s", text, e)
            data = None
        rows = data.get("molecules") if isinstance(data, dict) else data
        if isinstance(rows, list) and rows:
            molecules, invalid = [], 0
            for i, m in enumerate(rows):
                if not isinstance(m, dict):
                    invalid += 1
                    continue
                smiles = str(m.get("smiles") or "").strip()
                if not smiles:
                    invalid += 1
                    continue
                molecules.append({"id": str(m.get("id") or m.get("name") or f"MOL{i + 1}"),
                                  "name": str(m.get("name") or m.get("id") or f"MOL{i + 1}"),
                                  "smiles": smiles,
                                  "source_file": text, "source_index": i + 1,
                                  "raw": json.dumps(m, ensure_ascii=False)})
            if molecules:
                return "json", molecules, {"source_file": text, "format": "json",
                                           "records_total": len(rows),
                                           "records_ok": len(molecules),
                                           "records_skipped": invalid, "skipped": [],
                                           "notes": [f"按运行产物 JSON 读取分子清单：{len(molecules)} 条"]}
    return read_molecule_file_normalized(source)


def read_molecule_file_normalized(source: str) -> Tuple[str, List[Dict[str, str]], Dict[str, Any]]:
    """**统一输入归一化入口**：内容嗅探优先于扩展名，支持 SDF/MOL2/MOL/CSV/TSV/SMILES 文本、
    gzip/zip 压缩包、异构表头（中英文 name/smiles/inchi 别名）与自动编码判定；逐行容错。

    返回 `(fmt, molecules, normalization)`：
      - molecules: `[{"id","name","smiles","source_file","source_index","raw"}, ...]`，
        `smiles` 为 RDKit 规范 SMILES，已按规范 SMILES 去重并合并别名；
      - normalization: 见 `core/normalize.py`（records_total/ok/skipped、skipped 行号与原因、
        duplicates、aliases、encoding/delimiter/header/notes），供落盘 `input_normalization.json`。
    """
    from docking_agent.core.normalize import normalize_ligand_file

    molecules, normalization = normalize_ligand_file(source)
    return (str(normalization.get("format") or ""), molecules, normalization)


def read_molecule_file(source: str) -> Tuple[str, List[Dict[str, str]]]:
    """从用户上传的小分子文件读取分子库，返回 (格式, [{name, smiles}])。

    兼容旧契约：内部走**统一归一化层**（`read_molecule_file_normalized`），
    分子字典额外带 `id/source_file/source_index`，但 `name`/`smiles` 语义不变。
    归一化层意外抛错时回退到旧解析器，保证单个脏文件不会让整条链路失败。
    """
    try:
        fmt, molecules, _normalization = read_molecule_file_normalized(source)
    except Exception as e:  # noqa: BLE001
        logger.warning("归一化读取失败，回退旧解析器：%s", e)
        return _read_molecule_file_legacy(source)
    if molecules:
        # 让下游（CSV/报告/结果卡片）能直接用原始 ID：id 缺失时用 name 兜底
        for index, mol in enumerate(molecules):
            mol.setdefault("id", mol.get("name") or f"MOL{index + 1}")
            mol.setdefault("name", mol["id"])
        return (fmt, molecules)
    if fmt and fmt != "unknown":
        # 归一化层已明确识别格式但没有分子（空文件 / xlsx 未安装支持等）→ 如实返回空
        return (fmt, [])
    return _read_molecule_file_legacy(source)


def _read_molecule_file_legacy(source: str) -> Tuple[str, List[Dict[str, str]]]:
    """旧版按扩展名分派的读取实现（保留为归一化层失败时的兜底）。"""
    local = _fetch_file(source, "molec")
    ext = os.path.splitext(source.split("?")[0])[1].lower()
    mols: List[Dict[str, str]] = []
    fmt = ext.lstrip(".")
    if ext in (".sdf", ".sd"):
        from rdkit.Chem import SDMolSupplier
        try:
            supp = SDMolSupplier(local, sanitize=True, removeHs=False)
            for m in supp:
                if m is None:
                    continue
                name = m.GetProp("_Name") if m.HasProp("_Name") and m.GetProp("_Name") else ""
                mols.append({"name": name, "smiles": Chem.MolToSmiles(Chem.RemoveHs(m))})
        except Exception as e:
            logger.warning("SDF 读取失败: %s", e)
        return ("sdf", mols)
    if ext in (".mol2", ".mol"):
        m = Chem.MolFromMol2File(local) if ext == ".mol2" else Chem.MolFromMolFile(local)
        if m is not None:
            mols.append({"name": "", "smiles": Chem.MolToSmiles(Chem.RemoveHs(m))})
        return (fmt, mols)
    if ext == ".csv":
        import csv
        with open(local, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                s = (row.get("smiles") or row.get("SMILES") or row.get("smiles_smiles") or "").strip()
                if s and Chem.MolFromSmiles(s):
                    mols.append({"name": (row.get("name") or row.get("Name") or "").strip(), "smiles": s})
        return ("csv", mols)
    # .smi / .txt / 其他：逐行 "SMILES [name]"，同时兼容 "名称 SMILES" 与 "名称:SMILES"
    with open(local, encoding="utf-8-sig") as f:
        content = f.read()
    for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "SMILES", "Canonical")):
                continue
            # 「名称:SMILES」单独前置处理：直接对右侧做解析，避免把整行（含冒号）丢给 RDKit
            # 刷出一大串 SMILES Parse Error，也避免丢掉名称。
            if ":" in line:
                left, _, right = line.partition(":")
                left, right = left.strip(), right.strip()
                if right and _looks_like_smiles(right) and Chem.MolFromSmiles(right):
                    mols.append({"name": left, "smiles": right})
                    continue
            parts = line.split()
            if not parts:
                continue
            first = parts[0]
            second = parts[1] if len(parts) > 1 else ""
            smile, name = "", ""
            # 先用字符集启发式判断哪一列是 SMILES，避免对中文名做 RDKit 探测解析（会刷错误日志）
            if _looks_like_smiles(first) and Chem.MolFromSmiles(first):
                smile, name = first, second
            elif second and _looks_like_smiles(second) and Chem.MolFromSmiles(second):
                # 兼容 "名称 SMILES"（中文用户常见写法）
                smile, name = second, first
            if smile:
                mols.append({"name": name, "smiles": smile})
    if not mols and ":" in content:
        # 常见写法「名称:SMILES」（本系统导出的清单就是这个格式）：
        # 交给 parse_smiles_text 统一处理，避免把整行当成 SMILES 去解析（还会刷 RDKit 错误日志）
        mols = parse_smiles_text(content)
    return ("smi", mols)
