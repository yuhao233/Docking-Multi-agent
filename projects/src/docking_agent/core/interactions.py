"""对接姿态 × 结合口袋的**几何相互作用分析**（真实坐标计算，不含任何预测模型）。

## 为什么需要

对接分数只说"结合得多紧"，不说"**为什么**紧、结合在哪里"。用户要看的是：
这个分子落在哪个口袋、贴着哪些残基、形成了几个氢键/盐桥/疏水接触、有没有 π 堆积，
以及这些相互作用是否与推荐理由一致。因此这里直接从**真实位姿**（`poses/pose_*.pdbqt`）
与**真实受体**（PDBQT）坐标算，逐残基、逐原子对留痕。

## 判定口径（全部是几何启发式，报告中会标注"近似"）

| 相互作用 | 判定 |
| --- | --- |
| 氢键 | 配体极性原子（N/O）与受体极性原子（N/O）距离 ≤ `hbond` (3.5 Å)；供/受体由 AD4 类型（`HD`/`HS` vs `N`/`NA`/`OA`）推断 |
| 盐桥 | 双方部分电荷 |q| ≥ 0.3 且符号相反、距离 ≤ `salt` (4.0 Å) 的 N/O 对 |
| 疏水接触 | 双方碳（`C`/`A`）距离 ≤ `contact` (4.2 Å) |
| π–π 堆叠 | 配体与受体的芳香碳（AD4 类型 `A`）在 `pi` (5.5 Å) 内有 ≥ 3 对（环–环近似的代理判据） |
| π–阳离子 | 芳香碳与带正电的 N（|q| ≥ 0.3）距离 ≤ `pi` (5.5 Å) |
| 金属配位 | 受体金属离子（`ZN/FE/MG/MN/CA/CU/NI/CO/NA/K`）与配体 N/O 距离 ≤ 3.0 Å |

**局限（报告里也会写）**：只做距离/电荷判据，没有做氢键角度、质子化方向与 ring-normal 角度的精确判定，
也没有做能量分解；它回答"贴到哪里、以什么方式接触"，不替代 MD/MM-GBSA。
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: 判定阈值（Å）
HBOND_MAX = 3.5
SALT_MAX = 4.0
CONTACT_MAX = 4.2
PI_MAX = 5.5
METAL_MAX = 3.0
#: 电荷阈值（部分电荷，e）
CHARGE_MIN = 0.3
#: 芳香碳对数达到该值才算 π–π 堆叠（单原子偶然靠近不算）
PI_MIN_PAIRS = 3
#: 口袋残基统计半径：以配体为参照（姿态周边），比"口袋中心"更贴近实际结合环境
POCKET_RADIUS = 8.0

POLAR = frozenset({"N", "NA", "OA", "O", "SA"})
DONOR = frozenset({"HD", "HS", "H"})
CARBON = frozenset({"C", "A"})
AROMATIC = frozenset({"A"})
METALS = frozenset({"ZN", "FE", "MG", "MN", "CA", "CU", "NI", "CO", "NA", "K", "CD", "HG"})

_ATOM_RE = re.compile(r"^(ATOM|HETATM)")


def _num(text: str) -> Optional[float]:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def read_pdbqt(path: str, *, first_model_only: bool = True) -> List[Dict[str, Any]]:
    """读 PDBQT 的原子（坐标 + 残基 + AD4 类型 + 部分电荷）。

    AutoDock PDBQT 的列约定：`1-6` 记录名、`7-11` 序号、`13-16` 原子名、`18-20` 残基名、
    `22` 链、`23-26` 残基号、`31-54` 坐标、`55-60` 占据、`61-66` B 因子、`67-76` 电荷、`77-79` AD4 类型。
    配体位姿文件可能含多个 `MODEL`；默认只取第一个（= 最优位姿，与报告里展示的分数一致）。
    """
    atoms: List[Dict[str, Any]] = []
    in_first_model = True
    model_seen = False
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith("MODEL"):
                    if model_seen:
                        in_first_model = False
                    model_seen = True
                    continue
                if line.startswith("ENDMDL") and model_seen:
                    break
                if not _ATOM_RE.match(line):
                    continue
                if first_model_only and model_seen and not in_first_model:
                    continue
                xyz = [_num(line[30:38]), _num(line[38:46]), _num(line[46:54])]
                if any(v is None for v in xyz):
                    continue
                serial = None
                try:
                    serial = int(line[6:11])
                except ValueError:
                    serial = None
                atoms.append({
                    "serial": serial,
                    "name": line[12:16].strip(),
                    "resname": line[17:20].strip(),
                    "chain": line[21].strip(),
                    "resnum": line[22:26].strip(),
                    "xyz": [float(xyz[0]), float(xyz[1]), float(xyz[2])],
                    "charge": _num(line[66:76]) or 0.0,
                    "ad4": line[77:79].strip().upper(),
                    "element": _element(line),
                })
    except OSError as e:
        logger.warning("读取 PDBQT 失败 %s：%s", path, e)
    return atoms


def _element(line: str) -> str:
    """元素：优先 AD4 类型，其次原子名首字母。"""
    ad4 = line[77:79].strip().upper()
    if ad4 and ad4[0] in "CNOSHPFIMZKBRA":
        if ad4 in METALS:
            return ad4
        return {"OA": "O", "NA": "N", "SA": "S", "HD": "H", "HS": "H", "A": "C"}.get(ad4, ad4[0])
    name = line[12:16].strip()
    for ch in name:
        if ch.isalpha():
            return ch.upper()
    return ""


def pose_smiles(pose_path: str) -> str:
    """位姿文件里记录的配体 SMILES（meeko 的 `REMARK SMILES ...`）。

    为什么用它而不是原始输入 SMILES：位姿是**对接实际用的化学形式**（可能已按目标 pH 中和/重分配），
    2D 图必须画与位姿一致的分子。
    """
    try:
        with open(pose_path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith("REMARK SMILES ") and "IDX" not in line:
                    return line[len("REMARK SMILES "):].strip()
                if line.startswith("ROOT"):
                    break
    except OSError as e:
        logger.debug("读取位姿 SMILES 失败 %s：%s", pose_path, e)
    return ""


def pose_smiles_index_map(pose_path: str) -> Dict[int, int]:
    """`REMARK SMILES IDX` → {PDBQT 原子序号: SMILES 原子序号(1-based)}。

    用于把相互作用里的原子名对应到 2D 结构图上的原子（不靠猜、也不靠名字匹配）。
    """
    mapping: Dict[int, int] = {}
    try:
        with open(pose_path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.startswith("ROOT"):
                    break
                if not line.startswith("REMARK SMILES IDX"):
                    continue
                # meeko 会把较长的映射**折成多行** REMARK —— 必须逐行累加，只看第一行会漏原子
                values = [int(v) for v in re.findall(r"-?\d+", line[len("REMARK SMILES IDX"):])]
                for i in range(0, len(values) - 1, 2):
                    mapping[values[i]] = values[i + 1]
    except (OSError, ValueError) as e:
        logger.debug("解析位姿 SMILES IDX 失败 %s：%s", pose_path, e)
    return mapping


def distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


def _residue_label(atom: Dict[str, Any]) -> str:
    chain = str(atom.get("chain") or "")
    return f"{atom.get('resname')}{atom.get('resnum')}" + (f"（链 {chain}）" if chain else "")


def analyze_pose_pocket(receptor_pdbqt: str, pose_pdbqt: str, *,
                        hbond: float = HBOND_MAX, salt: float = SALT_MAX,
                        contact: float = CONTACT_MAX, pi: float = PI_MAX,
                        metal: float = METAL_MAX,
                        pocket_radius: float = POCKET_RADIUS) -> Dict[str, Any]:
    """分析单个位姿与受体的相互作用，返回逐残基明细 + 汇总（全部来自真实坐标）。"""
    receptor = read_pdbqt(receptor_pdbqt)
    ligand = read_pdbqt(pose_pdbqt)
    out: Dict[str, Any] = {
        "status": "ok" if (receptor and ligand) else "no_data",
        "receptor_atoms": len(receptor), "ligand_atoms": len(ligand),
        "thresholds": {"hbond": hbond, "salt": salt, "hydrophobic": contact,
                       "pi": pi, "metal": metal},
        "residues": [], "interactions": [], "summary": {}, "warnings": [],
    }
    if not receptor:
        out["message"] = f"受体 PDBQT 不可读或为空：{receptor_pdbqt}"
        out["status"] = "no_data"
        return out
    if not ligand:
        out["message"] = ("没有可用的位姿文件（本次运行可能关闭了「保存位姿」；"
                          "请在设置或表单里打开后重跑，才能做姿态–口袋分析）")
        out["status"] = "no_pose"
        return out

    heavy_lig = [a for a in ligand if a["element"] != "H"]
    # 只保留配体附近的受体原子：3000 残基的全蛋白两两比较没有必要（也慢）
    nearby = [a for a in receptor
              if a["element"] != "H" and any(distance(a["xyz"], b["xyz"]) <= pocket_radius
                                             for b in heavy_lig)]

    detail: List[Dict[str, Any]] = []
    per_res: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    aromatic_pairs: Dict[Tuple[str, str, str], int] = {}

    for lig in heavy_lig:
        for rec in nearby:
            d = distance(lig["xyz"], rec["xyz"])
            kinds: List[str] = []
            if d <= hbond and (lig["element"] in ("N", "O") and rec["element"] in ("N", "O")):
                kinds.append("氢键")
            if d <= salt and (lig["element"] in ("N", "O") and rec["element"] in ("N", "O")) \
                    and abs(lig.get("charge") or 0.0) >= CHARGE_MIN \
                    and abs(rec.get("charge") or 0.0) >= CHARGE_MIN \
                    and (lig.get("charge") or 0.0) * (rec.get("charge") or 0.0) < 0:
                kinds.append("盐桥")
            if d <= contact and lig["ad4"] in CARBON and rec["ad4"] in CARBON:
                kinds.append("疏水接触")
            if d <= pi and (lig["ad4"] in AROMATIC and rec["ad4"] in AROMATIC):
                key = (rec.get("chain") or "", rec.get("resnum") or "", rec.get("resname") or "")
                aromatic_pairs[key] = aromatic_pairs.get(key, 0) + 1
            if d <= pi and ((lig["ad4"] in AROMATIC and rec["element"] == "N"
                             and (rec.get("charge") or 0.0) >= CHARGE_MIN)
                            or (rec["ad4"] in AROMATIC and lig["element"] == "N"
                                and (lig.get("charge") or 0.0) >= CHARGE_MIN)):
                kinds.append("π–阳离子")
            if d <= metal and (rec["ad4"] in METALS and lig["element"] in ("N", "O")):
                kinds.append("金属配位")
            if not kinds:
                continue
            key = (rec.get("chain") or "", rec.get("resnum") or "", rec.get("resname") or "")
            entry = per_res.setdefault(key, {
                "chain": key[0], "resnum": key[1], "resname": key[2],
                "residue": f"{key[2]}{key[1]}" + (f"（链 {key[0]}）" if key[0] else ""),
                "min_distance": d, "n_contacts": 0, "types": [], "detail": []})
            entry["min_distance"] = min(entry["min_distance"], d)
            entry["n_contacts"] += 1
            for kind in kinds:
                if kind not in entry["types"]:
                    entry["types"].append(kind)
            if len(entry["detail"]) < 6:
                entry["detail"].append({"type": "、".join(kinds),
                                        "ligand_atom": lig["name"],
                                        "ligand_serial": lig.get("serial"),
                                        "receptor_atom": rec["name"],
                                        "receptor_serial": rec.get("serial"),
                                        "distance": round(d, 2)})
            detail.append({"residue": entry["residue"], "types": kinds,
                           "ligand_atom": lig["name"], "ligand_serial": lig.get("serial"),
                           "receptor_atom": rec["name"], "receptor_serial": rec.get("serial"),
                           "distance": round(d, 2)})

    # π–π 用"芳香碳对数"代理判据（单对靠近不算堆叠）
    for key, pairs in aromatic_pairs.items():
        if pairs < PI_MIN_PAIRS:
            continue
        entry = per_res.get(key)
        if entry is None:
            entry = per_res.setdefault(key, {
                "chain": key[0], "resnum": key[1], "resname": key[2],
                "residue": f"{key[2]}{key[1]}" + (f"（链 {key[0]}）" if key[0] else ""),
                "min_distance": None, "n_contacts": 0, "types": [], "detail": []})
        if "π–π 堆叠" not in entry["types"]:
            entry["types"].append("π–π 堆叠")
        entry["detail"].append({"type": "π–π 堆叠（近似）", "ligand_atom": "芳香碳",
                                "receptor_atom": "芳香碳", "distance": None,
                                "pairs": pairs})

    residues = sorted(per_res.values(),
                      key=lambda r: (r["min_distance"] if r["min_distance"] is not None else 99.0,
                                     -r["n_contacts"]))
    type_counts: Dict[str, int] = {}
    for item in detail:
        for kind in item["types"]:
            type_counts[kind] = type_counts.get(kind, 0) + 1
    for entry in residues:
        for kind in entry["types"]:
            if kind == "π–π 堆叠":
                pass

    pocket_residues: List[str] = []
    seen_pocket = set()
    for rec in nearby:
        label = f"{rec.get('resname')}{rec.get('resnum')}" + (
            f"（链 {rec.get('chain')}）" if rec.get("chain") else "")
        if label not in seen_pocket:
            seen_pocket.add(label)
            pocket_residues.append(label)

    involved_lig = {item["ligand_atom"] for item in detail}
    out.update({
        "residues": residues,
        "interactions": detail[:200],
        "pocket_residues": pocket_residues,
        "summary": {
            "contact_residues": len(residues),
            "type_counts": type_counts,
            "hbond_residues": [r["residue"] for r in residues if "氢键" in r["types"]][:8],
            "salt_bridge_residues": [r["residue"] for r in residues if "盐桥" in r["types"]][:6],
            "hydrophobic_residues": [r["residue"] for r in residues if "疏水接触" in r["types"]][:8],
            "pi_residues": [r["residue"] for r in residues
                            if any(t.startswith("π") for t in r["types"])][:6],
            "metal_residues": [r["residue"] for r in residues if "金属配位" in r["types"]][:6],
            "closest": ({"residue": residues[0]["residue"], "distance": residues[0]["min_distance"]}
                        if residues else None),
            "ligand_atoms_in_contact": len(involved_lig),
            "ligand_heavy_atoms": len(heavy_lig),
            "pocket_radius": pocket_radius,
        },
    })
    if not residues:
        out["warnings"].append(
            f"配体 {pocket_radius:g} Å 内没有任何达到阈值的受体接触原子："
            "可能是位姿落在口袋外（盒子/位点需核对），或阈值过严。")
    return out


def describe(analysis: Dict[str, Any], *, max_residues: int = 8) -> str:
    """一句话总结（用于报告与运行笔记；全部数字来自几何分析）。"""
    if not analysis or analysis.get("status") != "ok":
        return str((analysis or {}).get("message") or "无姿态–口袋分析数据")
    summary = analysis.get("summary") or {}
    counts = summary.get("type_counts") or {}
    parts = [f"与 {summary.get('contact_residues', 0)} 个受体残基有接触"]
    order = ["氢键", "盐桥", "疏水接触", "π–π 堆叠", "π–阳离子", "金属配位"]
    detail = "、".join(f"{k}×{counts[k]}" for k in order if counts.get(k))
    if detail:
        parts.append(detail)
    closest = summary.get("closest")
    if closest and closest.get("distance") is not None:
        parts.append(f"最近接触 {closest['residue']} {closest['distance']:.2f} Å")
    residues = analysis.get("residues") or []
    if residues:
        top = "、".join(
            f"{r['residue']}（{'/'.join(r['types'])}，{r['min_distance']:.2f} Å）"
            if r.get("min_distance") is not None else f"{r['residue']}（{'/'.join(r['types'])}）"
            for r in residues[:max_residues])
        parts.append("关键残基：" + top)
    return "；".join(parts)


def pocket_residues(receptor_pdbqt: str, center: Sequence[float], size: Sequence[float], *,
                    limit: int = 24, min_atoms: int = 1) -> List[Dict[str, Any]]:
    """对接盒内的受体残基清单（按盒内原子数排序）——用于「结合口袋说明」。

    这是**不依赖口袋预测引擎**的口袋描述：即使位点来自共晶配体或用户手填坐标，
    也能说清"这个盒子里有哪些残基"。
    """
    atoms = read_pdbqt(receptor_pdbqt)
    if not atoms or not center or not size:
        return []
    cx, cy, cz = (float(v) for v in center)
    sx, sy, sz = (float(v) / 2.0 for v in size)
    counter: Dict[Tuple[str, str, str], int] = {}
    for atom in atoms:
        if atom["element"] == "H":
            continue
        x, y, z = atom["xyz"]
        if abs(x - cx) > sx or abs(y - cy) > sy or abs(z - cz) > sz:
            continue
        key = (str(atom.get("chain") or ""), str(atom.get("resnum") or ""),
               str(atom.get("resname") or ""))
        counter[key] = counter.get(key, 0) + 1
    out = [{"chain": k[0], "resnum": k[1], "resname": k[2],
            "residue": f"{k[2]}{k[1]}" + (f"（链 {k[0]}）" if k[0] else ""),
            "atoms_in_box": n}
           for k, n in counter.items() if n >= min_atoms]
    out.sort(key=lambda r: -r["atoms_in_box"])
    return out[:limit]
