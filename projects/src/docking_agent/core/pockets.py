"""结合口袋预测与「对接盒子」确定。

为什么需要它：原先的位点盒只有三个来源 —— 注册表里人工标注的已知位点、共晶配体质心、
以及**整个蛋白质的质心**（最后这个基本等于把盒子放错地方）。本模块引入成熟的口袋预测工具，
把「盒子放哪、放多大」变成有依据、可追溯的计算结果。

引擎（按可靠性排序，`engine="auto"` 时依次尝试）：

1. **p2rank** —— 成熟的机器学习口袋预测工具（随机森林 + 溶剂可及表面特征），
   本地部署在 `assets/tools/p2rank/`（或 `P2RANK_HOME` / `PATH`）。
2. **geometric** —— 内置几何法：网格埋藏度 + 原子密度聚类（fpocket 的 alpha-sphere 思路的简化实现），
   无需任何外部依赖，保证「一键启动」也有工具可用。
3. **known_site** —— 受体注册表 / 共晶配体推断的实验位点。
4. **centroid** —— 蛋白质心（最差兜底，会在结果里明确标注为不可靠）。

盒子决策规则（`select_site`，有文档、有测试）：

- 用户显式指定的盒子**永远优先**（表单/指令/口袋 Agent 的选择）；
- 有**实验位点**（共晶配体或注册表标注）时：用工具预测做**独立验证**，
  一致（相距 ≤ `agree_radius`）则采用实验位点并把一致性写进溯源；
  不一致则仍采用实验位点，但记录警告（供独立复核与人工判断）；
- 只有**低可信兜底位点**（蛋白质心）时：改用工具预测的 top 口袋；
- 完全没有参考位点时：用工具预测的 top 口袋；
- 工具不可用时逐级回退，并把「实际使用的来源」写进 `source`。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from docking_agent.config import env, env_bool, env_float, env_int
from docking_agent.paths import cache_dir, project_root

logger = logging.getLogger(__name__)

POCKET_ENGINES = ("auto", "p2rank", "geometric", "known_site")
# 口袋中心与实验位点中心的「一致」判据（Å）：小于它认为工具验证通过
DEFAULT_AGREE_RADIUS = 8.0
DEFAULT_PADDING = 4.0
DEFAULT_MIN_SIZE = 18.0
DEFAULT_MAX_SIZE = 30.0
DEFAULT_TOP_N = 10

# ---- C 方案：库级配体感知下限 + 超限分子分组 ----
# 为什么需要：Vina 分数对盒子大小**敏感且非单调**（见 docs/architecture.md §15.7b 的 B0 表，
# 同一配体在 18³/22³/28³/34³ 间最大差 1.34 kcal/mol），所以**绝不能逐分子自适应盒子**。
# 折中方案：同一运行里主组共用同一个盒子（一致性第一），只有明显超出主盒的大配体才另起一组，
# 用该组自己的盒子重跑，并在结果里如实标注 box_group。
DEFAULT_BOX_SPAN_SAMPLE = 200        # 抽样分子数 K（按重原子数降序取前 K）
BOX_SPAN_SAMPLE_MIN = 20
BOX_SPAN_SAMPLE_MAX = 1000
# 库级下限 / 超限判据共用的「每侧 5 Å」余量 = 2×5 Å
DEFAULT_SPAN_MARGIN = 10.0
DEFAULT_BOX_GROUP_MARGIN = 10.0      # 超限判据：跨度 + 该值 > 主盒对应边 → large 组
DEFAULT_BOX_LARGE_PADDING = 12.0     # 大配体组盒子：组内最大跨度 + 该值
# 大配体组允许超过主盒的 MAX（否则「分组」没有意义），但仍设一个硬上限防止荒唐大盒
BOX_LARGE_MAX_SIZE = 60.0
_SPAN_SEED = 42


# --------------------------------------------------------------------------- #
# 受体原子读取（PDB / PDBQT）
# --------------------------------------------------------------------------- #
# AutoDock 原子类型 → 元素（PDBQT 的第 77-78 列是 AD 类型而不是元素）
_AD_ELEMENT = {
    "A": "C", "C": "C", "N": "N", "NA": "N", "OA": "O", "O": "O", "SA": "S", "S": "S",
    "H": "H", "HD": "H", "HS": "H", "F": "F", "Cl": "Cl", "CL": "Cl", "Br": "Br",
    "BR": "Br", "I": "I", "P": "P", "Mg": "Mg", "MG": "Mg", "Zn": "Zn", "ZN": "Zn",
    "Mn": "Mn", "MN": "Mn", "Ca": "Ca", "CA": "Ca", "Fe": "Fe", "FE": "Fe",
    "K": "K", "Na": "Na", "Se": "Se", "SE": "Se",
}
# 疏水/芳香倾向的元素（用于几何打分：埋藏且疏水的空腔更像结合口袋）
_HYDROPHOBIC = {"C", "F", "Cl", "Br", "I", "S"}


def _element_from_pdbqt(resname: str, atom_name: str) -> str:
    raw = (resname or "").strip()
    if raw in _AD_ELEMENT:
        return _AD_ELEMENT[raw]
    upper = raw.upper()
    if upper in _AD_ELEMENT:
        return _AD_ELEMENT[upper]
    name = (atom_name or "").strip()
    for candidate in (name[:2], name[:1]):
        if candidate in _AD_ELEMENT:
            return _AD_ELEMENT[candidate]
        if candidate.capitalize() in _AD_ELEMENT:
            return _AD_ELEMENT[candidate.capitalize()]
    return "C"


def _element_from_pdb(line: str) -> str:
    element = line[76:78].strip() if len(line) >= 78 else ""
    if element:
        return element.capitalize() if len(element) > 1 else element.upper()
    name = line[12:16].strip()
    for candidate in (name[:2], name[:1]):
        text = candidate.strip()
        if text:
            return text.capitalize() if len(text) > 1 else text.upper()
    return "C"


def read_receptor_atoms(path: str, *, include_hetatm: bool = True) -> List[Dict[str, Any]]:
    """读取受体原子（坐标 / 元素 / 残基 / 是否杂原子）。PDB 与 PDBQT 都支持。"""
    atoms: List[Dict[str, Any]] = []
    is_pdbqt = str(path).lower().endswith(".pdbqt")
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            record = line[:6].strip()
            if record not in ("ATOM", "HETATM"):
                continue
            if record == "HETATM" and not include_hetatm:
                continue
            resname = line[17:20].strip() if len(line) >= 20 else ""
            if resname in ("HOH", "WAT", "H2O"):
                continue
            try:
                xyz = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
            except ValueError:
                continue
            element = _element_from_pdbqt(resname, line[12:16]) if is_pdbqt else _element_from_pdb(line)
            atoms.append({
                "xyz": xyz,
                "element": element,
                "resname": resname,
                "resid": (line[22:27].strip() if len(line) >= 27 else ""),
                "chain": (line[21:22].strip() if len(line) >= 22 else ""),
                "atom": line[12:16].strip() if len(line) >= 16 else "",
                "hetero": record == "HETATM",
            })
    return atoms


def _centroid(atoms: Sequence[Dict[str, Any]]) -> Optional[List[float]]:
    if not atoms:
        return None
    n = float(len(atoms))
    return [sum(a["xyz"][i] for a in atoms) / n for i in range(3)]


def distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.dist([float(x) for x in a], [float(x) for x in b])


# --------------------------------------------------------------------------- #
# 配体 3D 跨度（库级下限 + 超限分组的唯一实现）
# --------------------------------------------------------------------------- #
def ligand_span(pdbqt: str) -> List[float]:
    """配体 PDBQT 的 3D 跨度（各轴 max-min，Å）。

    这是全项目**唯一**的跨度实现：对接时的盒适配检查、库级下限、超限分组都用它，
    避免三处各写一遍导致口径漂移。空输入/解析失败返回 []（调用方按「未知」处理）。
    """
    xs, ys, zs = [], [], []
    for line in str(pdbqt or "").splitlines():
        if line.startswith(("ATOM", "HETATM")):
            try:
                xs.append(float(line[30:38]))
                ys.append(float(line[38:46]))
                zs.append(float(line[46:54]))
            except (ValueError, IndexError):
                continue
    if not xs:
        return []
    return [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]


def _span_from_smiles(smiles: str, *, seed: int = _SPAN_SEED) -> List[float]:
    """SMILES → 3D → 跨度（与对接使用同一随机种子，保证跨度描述的就是被对接的构象）。"""
    from docking_agent.core.ligands import smiles_to_pdbqt  # noqa: PLC0415（重依赖 rdkit/meeko）

    return ligand_span(smiles_to_pdbqt(smiles, seed=seed))


#: 少于这个分子数就直接串行：进程启动开销大于收益。
_SPAN_PARALLEL_MIN = 8
#: 并发抽样的进程上限（RDKit 生成 3D 是纯 CPU 且单线程，多进程才有吞吐）。
_SPAN_WORKERS_MAX = 8


def _span_max_worker(smiles: str, *, seed: int = _SPAN_SEED) -> float:
    """子进程任务：单个分子的最大跨度；失败返回 0.0（由调用方按「失败样本」统计）。

    必须是**模块级**函数才可被 pickle 送进进程池。
    """
    try:
        span = _span_from_smiles(smiles, seed=seed)
    except Exception:  # noqa: BLE001 - 失败样本由调用方计数上报，这里不能吞掉整体
        return 0.0
    return max(span) if span else 0.0


def _span_max_many(smiles_list: Sequence[str], *, seed: int = _SPAN_SEED,
                   parallel_min: Optional[int] = None) -> List[float]:
    """批量算跨度：库大时多进程（真算 3D 很慢），任何异常都退回串行，绝不因此失败。

    为什么必须并发：单个大分子 ETKDG+MMFF 可要数秒，120 个大分子串行要 ~5 分钟，
    这段时间用户只能看到「正在抽样」；并发后降到几十秒（结果按库内容缓存）。
    """
    items = list(smiles_list)
    threshold = _SPAN_PARALLEL_MIN if parallel_min is None else int(parallel_min)
    if len(items) < threshold:
        return [_span_max_worker(s, seed=seed) for s in items]
    # 用**部署机器实际可用的核**（cgroup 配额 / 亲和性都算在内），避免容器里超订
    from docking_agent.core.docking import machine_profile  # 局部导入避免循环依赖

    workers = max(1, min(_SPAN_WORKERS_MAX, len(items), machine_profile()["budget"]))
    try:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(_span_max_worker, items,
                                 chunksize=max(1, len(items) // (workers * 4) or 1)))
    except Exception as e:  # noqa: BLE001 - 进程池不可用时串行结果必须一致
        logger.warning("库级跨度并发抽样不可用（%s），退回串行", e)
        return [_span_max_worker(s, seed=seed) for s in items]


def box_span_enabled() -> bool:
    """库级配体感知下限开关（默认 on；off 时完全退回旧行为）。"""
    return env_bool("BOX_SPAN_ENABLED", True)


def box_span_sample_size() -> int:
    """库级下限的抽样分子数 K（默认 200，夹在 20–1000）。"""
    return max(BOX_SPAN_SAMPLE_MIN,
               min(BOX_SPAN_SAMPLE_MAX, env_int("BOX_SPAN_SAMPLE", DEFAULT_BOX_SPAN_SAMPLE)))


def box_group_margin() -> float:
    """超限判据余量（Å）：跨度 + 该值 > 主盒对应边即划入 large 组。"""
    return max(0.0, env_float("BOX_GROUP_MARGIN", DEFAULT_BOX_GROUP_MARGIN))


def box_large_padding() -> float:
    """大配体组盒子余量（Å）：组内最大跨度 + 该值。"""
    return max(0.0, env_float("BOX_LARGE_PADDING", DEFAULT_BOX_LARGE_PADDING))


def _percentile_nearest_rank(values: Sequence[float], fraction: float) -> float:
    """最近秩分位数（`ceil(fraction×n)` 位，1-based）。

    小样本（n≤20）时 P95 就等于最大值；样本较大时才排除最高的约 5%。
    这样既抗单个异常值，又不会在库很小时低估大配体。
    """
    vals = sorted(float(v) for v in values)
    if not vals:
        return 0.0
    rank = max(1, math.ceil(fraction * len(vals)))
    return vals[min(rank, len(vals)) - 1]


def _heavy_atom_count(smiles: str) -> int:
    """2D 便宜描述符：重原子数（排序抽样用，失败返回 0 但仍参与抽样）。"""
    try:
        from rdkit import Chem  # noqa: PLC0415（重依赖）
        from rdkit.Chem import Descriptors  # noqa: PLC0415（重依赖）

        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return 0
        return int(Descriptors.HeavyAtomCount(mol))
    except Exception:  # noqa: BLE001
        return 0


def _library_smiles(molecules: Sequence[Dict[str, Any]]) -> List[str]:
    """整库去重后的 SMILES（排序后固定，保证哈希/缓存键可复现）。"""
    out = set()
    for m in molecules or []:
        smi = str((m or {}).get("smiles") or "").strip()
        if smi:
            out.add(smi)
    return sorted(out)


def library_span_digest(molecules: Sequence[Dict[str, Any]]) -> str:
    """库内容哈希（sha1 前 12 位）：用于库级下限的缓存键。"""
    joined = "\n".join(_library_smiles(molecules))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


def _bound_cache_path(digest: str, k: int) -> Path:
    return cache_dir() / "box_span" / f"library_{digest}-k{k}.json"


def library_span_bound(molecules: Sequence[Dict[str, Any]], *,
                       min_size: float = DEFAULT_MIN_SIZE,
                       sample: Optional[int] = None,
                       enabled: Optional[bool] = None,
                       use_cache: bool = True) -> Dict[str, Any]:
    """库级配体感知的盒子**下限**：`max(min_size, P95(库内配体 3D 最大跨度) + 10 Å)`。

    成本控制（必须）：**不为全库生成 3D**。先用便宜的 2D 描述符（重原子数）降序取前 K 个
    （`BOX_SPAN_SAMPLE`，默认 200，夹在 20–1000；库小于 K 时全算），只为这 K 个生成 3D；
    取 P95（最近秩，抗单个异常值）+ 10 Å。结果按「库 SMILES 哈希」缓存到 `cache_dir()`。

    降级保证：抽样/3D 生成失败时返回 `min_size` 并 `logger.warning`，
    **绝不让对接因为算跨度而失败**。

    返回：`{enabled, bound, p95_span, sample_n, library_n, k, cached, message}`。
    """
    k = box_span_sample_size() if sample is None else max(1, int(sample))
    library = _library_smiles(molecules)
    on = box_span_enabled() if enabled is None else bool(enabled)
    if not on:
        return {"enabled": False, "bound": float(min_size), "p95_span": None,
                "sample_n": 0, "library_n": len(library), "k": k, "cached": False,
                "message": "库级配体感知下限已关闭（BOX_SPAN_ENABLED=off）：盒子退回口袋驱动值"}
    if not library:
        return {"enabled": True, "bound": float(min_size), "p95_span": None,
                "sample_n": 0, "library_n": 0, "k": k, "cached": False,
                "message": "库为空，库级下限退回 min_size"}
    # 库小于 K 时全算；K 只是上限
    k = min(k, len(library))
    digest = library_span_digest(molecules)
    cache_file = _bound_cache_path(digest, k)
    if use_cache and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            # 缓存只存「库的 P95 跨度」；下限每次都按**当前** min_size 重算，
            # 否则改了 POCKET_MIN_SIZE 后会命中旧下限（缓存键只含库内容与 K）。
            p95 = cached.get("p95_span")
            bound = (max(float(min_size), round(float(p95) + DEFAULT_SPAN_MARGIN, 1))
                     if p95 is not None else float(min_size))
            cached.update({"enabled": True, "cached": True, "k": k, "bound": bound,
                           "message": (f"库级配体感知下限：抽样 {cached.get('sample_n')} 个 / "
                                       f"P95={p95} Å / 下限={bound} Å（缓存命中）")})
            logger.info("%s", cached["message"])
            return cached
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.debug("库级跨度缓存不可用（%s），改为现场计算", e)

    # 按重原子数降序取前 K（大配体最可能决定跨度上限，优先算它们）
    ranked = sorted(library, key=lambda s: -_heavy_atom_count(s))[:k]
    # 抽样要真的算 3D 构象，大库首次可能耗时几十秒；先报一条日志，
    # 否则这段时间「什么日志都没有」，看起来像卡死（真实踩坑：120 个大分子库）。
    logger.info("库级配体感知下限：开始抽样 %s 个分子的 3D 跨度（最多 %s 个，首次较慢，结果会缓存）",
                len(ranked), k)
    started = time.time()
    computed = _span_max_many(ranked)
    spans = [v for v in computed if v > 0]
    failures = len(computed) - len(spans)
    if len(ranked) >= _SPAN_PARALLEL_MIN:
        logger.info("库级跨度抽样完成：%s 个样本 / 耗时 %.0f s（失败 %s 个）",
                    len(spans), time.time() - started, failures)
    if not spans:
        logger.warning("库级跨度抽样全部失败（%s/%s），盒子下限退回 min_size=%.1f Å",
                       failures, len(ranked), min_size)
        return {"enabled": True, "bound": float(min_size), "p95_span": None,
                "sample_n": 0, "library_n": len(library), "k": k, "cached": False,
                "failed": failures,
                "message": f"库级跨度抽样全部失败（{failures}/{len(ranked)}），退回 min_size"}

    p95 = round(_percentile_nearest_rank(spans, 0.95), 1)
    bound = max(float(min_size), round(p95 + DEFAULT_SPAN_MARGIN, 1))
    result = {"enabled": True, "bound": bound, "p95_span": p95,
              "sample_n": len(spans), "library_n": len(library), "k": k,
              "cached": False, "failed": failures,
              "message": (f"库级配体感知下限：抽样 {len(spans)}/{len(library)} 个 / "
                          f"P95={p95} Å / 下限={bound} Å（重算）")}
    logger.info("%s", result["message"])
    if failures:
        logger.warning("库级跨度抽样有 %s/%s 个分子失败，按已成功的样本计算",
                       failures, len(ranked))
    if use_cache:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({key: result[key] for key in
                                              ("bound", "p95_span", "sample_n", "library_n")},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:  # noqa: BLE001
            logger.debug("库级跨度缓存写入失败：%s", e)
    return result


# --------------------------------------------------------------------------- #
# 共晶配体 / 已知位点
# --------------------------------------------------------------------------- #
def cocrystal_ligand(pdb_path: Optional[str], *, min_atoms: int = 6) -> Optional[Dict[str, Any]]:
    """从 PDB 里找共晶小分子配体（HETATM，排除水/离子/糖基化等），返回其质心与组成。"""
    if not pdb_path or not os.path.exists(pdb_path):
        return None
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for atom in read_receptor_atoms(pdb_path, include_hetatm=True):
        if not atom["hetero"]:
            continue
        key = f"{atom['chain']}:{atom['resid']}:{atom['resname']}"
        groups.setdefault(key, []).append(atom)
    candidates = []
    for key, atoms in groups.items():
        if len(atoms) < min_atoms:
            continue          # 离子 / 单原子金属 / 缓冲液碎片
        resname = atoms[0]["resname"]
        if resname in ("SO4", "PO4", "GOL", "EDO", "PEG", "ACT", "DMS", "TRS", "MES", "IMD"):
            continue          # 常见的结晶添加剂
        candidates.append({"key": key, "resname": resname, "n_atoms": len(atoms),
                           "center": _centroid(atoms)})
    if not candidates:
        return None
    candidates.sort(key=lambda c: -c["n_atoms"])
    best = candidates[0]
    best["others"] = [c["resname"] for c in candidates[1:6]]
    return best


# --------------------------------------------------------------------------- #
# 引擎一：P2Rank（成熟工具）
# --------------------------------------------------------------------------- #
def cocrystal_ligand_smiles(pdb_path: Optional[str],
                            ligand: Optional[Dict[str, Any]] = None) -> str:
    """解出共晶配体的 SMILES（用于「是否作为阳性对照」的询问）。

    做法：按 `chain:resid:resname` 取出该配体的 HETATM/ATOM 记录与它自己的 CONECT 记录，
    拼成最小 PDB 块交给 RDKit（proximity bonding + 标准化）；解析不出来返回空串 ——
    宁可**不询问**（并在结果里说明原因），也不给用户一个错误的对照结构。
    """
    if not pdb_path or not ligand:
        return ""
    key = str((ligand or {}).get("key") or "")
    if not key:
        return ""
    chain, resid, resname = (key.split(":") + ["", "", ""])[:3]
    try:
        from rdkit import Chem
    except Exception as exc:  # noqa: BLE001 - RDKit 不可用时如实返回空
        logger.warning("RDKit 不可用，无法解析共晶配体 SMILES：%s", exc)
        return ""
    lines: List[str] = []
    serial_to_index: Dict[str, int] = {}
    conect: List[str] = []
    try:
        with open(pdb_path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                record = raw[:6].strip().upper()
                if record in ("ATOM", "HETATM") and len(raw) >= 26:
                    same = (raw[21:22].strip() == chain and raw[22:26].strip() == resid
                            and raw[17:20].strip() == resname)
                    if same:
                        serial_to_index[raw[6:11].strip()] = len(lines)
                        lines.append(raw.rstrip("\n"))
                elif record == "CONECT":
                    conect.append(raw.rstrip("\n"))
    except OSError as exc:
        logger.warning("读取受体结构失败（无法解析共晶配体）：%s", exc)
        return ""
    if not lines:
        return ""
    # 只保留与该配体原子相关的连接记录，并重新编号，避免把整篇 CONECT 带进小块
    keep = set(serial_to_index)
    for raw in conect:
        parts = raw.split()
        if len(parts) >= 2 and parts[1] in keep:
            pairs = [p for p in parts[2:] if p in keep]
            if pairs:
                lines.append("CONECT" + "".join(f"{int(p):5d}" for p in [parts[1], *pairs]))
    block = "\n".join(lines) + "\nEND\n"
    try:
        mol = Chem.MolFromPDBBlock(block, sanitize=True, removeHs=False)
    except Exception as exc:  # noqa: BLE001 - 解析失败属预期可能（缺键级/缺氢）
        logger.info("共晶配体 PDB 块解析失败：%s", exc)
        return ""
    if mol is None:
        return ""
    try:
        return Chem.MolToSmiles(Chem.RemoveHs(mol))
    except Exception as exc:  # noqa: BLE001
        logger.info("共晶配体 SMILES 生成失败：%s", exc)
        return ""


def p2rank_home() -> Optional[Path]:
    """定位本地部署的 P2Rank（P2RANK_HOME → assets/tools/p2rank* → PATH）。"""
    env_home = env("P2RANK_HOME")
    if env_home:
        path = Path(env_home).expanduser()
        if (path / "prank").exists():
            return path
    tools = project_root() / "assets" / "tools"
    if tools.is_dir():
        for child in sorted(tools.glob("p2rank*"), reverse=True):
            if (child / "prank").exists():
                return child
    which = shutil.which("prank")
    if which:
        return Path(which).resolve().parent
    return None


def p2rank_command() -> Optional[List[str]]:
    home = p2rank_home()
    if home is None:
        return None
    script = home / "prank"
    java = shutil.which("java")
    if script.exists() and os.access(script, os.X_OK) and java:
        return [str(script)]
    return None


def available_engines() -> Dict[str, bool]:
    return {"p2rank": p2rank_command() is not None, "geometric": True,
            "known_site": True, "centroid": True}


def _pdbqt_to_pdb(pdbqt: str, target: str) -> str:
    """把 PDBQT 转成 PDB（P2Rank/fpocket 这类工具需要标准 PDB 的原子列）。"""
    out_lines: List[str] = []
    with open(pdbqt, "r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            record = line[:6].strip()
            if record not in ("ATOM", "HETATM"):
                continue
            element = _element_from_pdbqt(line[17:20].strip() if len(line) >= 20 else "",
                                          line[12:16])
            head = line[:66].rstrip("\n")
            if len(head) < 66:
                head = head.ljust(66)
            out_lines.append(f"{head}{element:>2}{'':>2}")
    Path(target).write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return target


def ensure_pdb_for_pockets(receptor_path: str, *, pdb_hint: str = "") -> str:
    """给出一个标准 PDB 文件供口袋预测工具使用（必要时从 PDBQT 转换）。"""
    for candidate in (pdb_hint, receptor_path):
        if candidate and str(candidate).lower().endswith(".pdb") and os.path.exists(candidate):
            return candidate
    target = cache_dir() / "pockets" / (Path(receptor_path).stem + ".pdb")
    target.parent.mkdir(parents=True, exist_ok=True)
    return _pdbqt_to_pdb(receptor_path, str(target))


def run_p2rank(pdb_path: str, *, top_n: int = DEFAULT_TOP_N,
               timeout: int = 900) -> Dict[str, Any]:
    """运行 P2Rank 并解析其预测结果（真实调用外部工具）。"""
    command = p2rank_command()
    if command is None:
        return {"status": "unavailable", "engine": "p2rank", "pockets": [],
                "message": "未找到本地 P2Rank（可用 P2RANK_HOME 指定，或放到 assets/tools/）"}
    out_dir = cache_dir() / "pockets" / "p2rank_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 注意：P2Rank 的启动脚本自己解析相对路径，**不要**改 cwd（改到安装目录会失败）；
    # 因此这里统一传绝对路径。线程数默认留一半 CPU，避免把对接服务饿死。
    abs_pdb = str(Path(pdb_path).resolve())
    threads = env("P2RANK_THREADS") or str(max(1, (os.cpu_count() or 4) // 2))
    cmd = command + ["predict", "-f", abs_pdb, "-o", str(out_dir.resolve()),
                     "-threads", str(threads)]
    logger.info("运行 P2Rank：%s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "error", "engine": "p2rank", "pockets": [],
                "message": f"P2Rank 超时（>{timeout}s）", "cmd": cmd}
    except OSError as e:
        return {"status": "error", "engine": "p2rank", "pockets": [],
                "message": f"P2Rank 启动失败：{e}", "cmd": cmd}

    stem = Path(pdb_path).name
    predictions = None
    for pattern in (f"{stem}_predictions.csv", f"{stem}.pdb_predictions.csv",
                    "*_predictions.csv"):
        found = sorted(out_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if found:
            predictions = found[0]
            break
    if predictions is None:
        tail = (proc.stderr or proc.stdout or "")[-400:]
        return {"status": "error", "engine": "p2rank", "pockets": [],
                "message": f"P2Rank 未产出 *_predictions.csv（退出码 {proc.returncode}）：{tail}",
                "cmd": cmd}
    if proc.returncode != 0:
        # P2Rank 某些情况下会「先写出结果再报错」（例如部分文件处理失败），
        # 只要预测文件可解析就照常使用，并把退出码记下来供排查。
        logger.warning("P2Rank 退出码 %s，但已产出预测文件，按成功处理", proc.returncode)

    pockets = _parse_p2rank_predictions(predictions, top_n=top_n)
    if pockets:
        atoms = read_receptor_atoms(pdb_path)
        for pocket in pockets:
            pocket.setdefault("extent", _extent_from_atoms(atoms, pocket["center"]))
            if not pocket.get("residues"):
                pocket["residues"] = _residues_near(atoms, pocket["center"])
    return {"status": "ok" if pockets else "no_pockets", "engine": "p2rank",
            "pockets": pockets, "predictions_csv": str(predictions),
            "command": " ".join(cmd), "returncode": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-300:]}


def _parse_p2rank_predictions(csv_path: Path, *, top_n: int) -> List[Dict[str, Any]]:
    """解析 P2Rank 的 predictions.csv（rank,score,probability,...,center_x/y/z,residue_ids）。"""
    import csv as _csv

    pockets: List[Dict[str, Any]] = []
    with open(csv_path, "r", encoding="utf-8", errors="ignore", newline="") as handle:
        reader = _csv.DictReader(handle)
        # P2Rank 的 CSV 表头带对齐空格（"  rank" / "   center_x"），必须先剥掉再取字段
        reader.fieldnames = [str(name or "").strip() for name in (reader.fieldnames or [])]
        for raw_row in reader:
            row = {str(k or "").strip(): v for k, v in raw_row.items()}
            def _num(key: str) -> Optional[float]:
                raw = str(row.get(key, "") or "").strip()
                if raw == "":
                    return None
                try:
                    return float(raw)
                except ValueError:
                    return None

            center = [_num("center_x"), _num("center_y"), _num("center_z")]
            if any(c is None for c in center):
                continue
            residues = [r for r in str(row.get("residue_ids", "") or "").split() if r]
            label = str(row.get("name") or "").strip()
            pockets.append({
                "name": label or f"pocket_{row.get('rank') or len(pockets) + 1}",
                "rank": int(float(row.get("rank") or len(pockets) + 1)),
                "score": _num("score"),
                "probability": _num("probability"),
                "sas_points": _num("sas_points"),
                "surf_atoms": _num("surf_atoms"),
                "center": [float(c) for c in center],
                "residues": residues[:40],
                "source": "p2rank",
            })
    pockets.sort(key=lambda p: (p["rank"], -(p.get("score") or 0.0)))
    return pockets[:top_n]


# --------------------------------------------------------------------------- #
# 引擎二：内置几何法（无外部依赖）
# --------------------------------------------------------------------------- #
def _extent_from_atoms(atoms: Sequence[Dict[str, Any]], center: Sequence[float],
                       *, radius: float = 8.0, fallback: float = 12.0) -> List[float]:
    """用口袋中心附近的受体原子范围估算口袋空间尺寸（P2Rank 只给中心）。"""
    near = [a["xyz"] for a in atoms
            if a["element"] != "H" and distance(a["xyz"], center) <= radius]
    if len(near) < 4:
        return [fallback, fallback, fallback]
    extent = []
    for axis in range(3):
        values = [p[axis] for p in near]
        extent.append(round(max(values) - min(values), 2))
    return [max(6.0, min(24.0, e)) for e in extent]


def _residues_near(atoms: Sequence[Dict[str, Any]], center: Sequence[float],
                   *, radius: float = 8.0, limit: int = 12) -> List[str]:
    counter: Dict[str, int] = {}
    for atom in atoms:
        if atom["element"] == "H":
            continue
        if distance(atom["xyz"], center) > radius:
            continue
        key = f"{atom['resname']}{atom['resid']}"
        counter[key] = counter.get(key, 0) + 1
    return [key for key, _ in sorted(counter.items(), key=lambda kv: -kv[1])][:limit]


def detect_geometric(atoms: Sequence[Dict[str, Any]], *, top_n: int = DEFAULT_TOP_N,
                     spacing: float = 1.0, probe_min: float = 3.2, probe_max: float = 5.2,
                     shell: float = 8.0, sector_radius: float = 10.0,
                     cluster_radius: float = 5.0, merge_radius: float = 6.0,
                     max_span: float = 20.0, min_points: int = 15) -> Dict[str, Any]:
    """内置几何口袋检测（无需外部工具）。

    算法（fpocket 的 alpha-sphere 思路的简化实现，已用共晶配体客观标定）：

      1. 在受体包围盒内铺网格，取「第一溶剂层」上的点（与最近原子距离在 probe_min~probe_max），
         也就是真正贴着蛋白表面的候选位点，而不是蛋白内部或自由溶剂；
      2. 对每个候选点算三个真实可解释的特征：
         - 埋藏度 burial：8 Å 内的重原子数（口袋比平坦表面更"被包围"）；
         - 包封度 enclosure：26 个方向中，10 Å 内有原子的方向数（凹坑 vs 凸面）；
         - 疏水接触 hydrophobic_contacts：5 Å 内的碳/硫原子数（可成药口袋偏疏水）。
      3. 打分 score = (enclosure/26)² × (burial/45) × (0.4 + 疏水/8)，三个因子都单调、不封顶；
      4. 按分数从高到低做**贪心球聚类**（半径 cluster_radius），再按中心距离与范围上限
         合并同一口袋的碎片（避免把整片表面串联成一个巨块）；
      5. 按分数排序，输出中心 / 范围 / 近似体积 / 附近残基。

    在凝血酶（1DWC，共晶配体 MIT 中心 31.5/13.74/24.36）上实测（2026-09-14，`detect_geometric`
    直接调用，共晶配体取自 `assets/receptors/structures/thrombin.pdb`）：

    | 网格间距 spacing | top-1 距共晶配体 | top-2 | top-3 | 耗时 |
    | --- | --- | --- | --- | --- |
    | **1.0 Å（默认）** | **2.6 Å** | 2.97 Å | 16.5 Å | ≈0.9 s |
    | 1.5 Å（测试用粗网格） | 3.60 Å | 22.8 Å | 34.3 Å | ≈0.3 s |

    注意：**精度与网格间距强相关**，引用数字时必须同时说明 spacing（测试为了速度用 1.5 Å，
    因此那里的阈值放宽到 ≤8 Å，见 `tests/test_pockets.py` 的永久质量门）。
    """
    import numpy as np
    from scipy.spatial import cKDTree

    heavy = [a for a in atoms if a["element"] != "H"]
    if len(heavy) < 50:
        return {"status": "no_pockets", "engine": "geometric", "pockets": [],
                "message": "受体重原子太少，无法做口袋检测"}

    coords = np.asarray([a["xyz"] for a in heavy], dtype=float)
    hydrophobic = np.asarray([1.0 if a["element"] in _HYDROPHOBIC else 0.0 for a in heavy],
                             dtype=float)
    tree = cKDTree(coords)

    # ---- 1) 网格 + 第一溶剂层候选点 ----
    lo = coords.min(axis=0) - 2.0
    hi = coords.max(axis=0) + 2.0
    for _ in range(8):        # 大蛋白自动放粗网格，控制内存与耗时
        shape = np.maximum(np.ceil((hi - lo) / spacing).astype(int), 1)
        if int(shape.prod()) <= 800_000:
            break
        spacing *= 1.15
    axes = [lo[i] + (np.arange(shape[i]) + 0.5) * spacing for i in range(3)]
    grid = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1).reshape(-1, 3)
    nearest, _ = tree.query(grid, k=1)
    layer = grid[(nearest >= probe_min) & (nearest <= probe_max)]
    if len(layer) < min_points:
        return {"status": "no_pockets", "engine": "geometric", "pockets": [],
                "message": "未找到表面层网格点", "grid_points": int(len(grid))}

    # ---- 2) 特征 ----
    burial = np.asarray(tree.query_ball_point(layer, r=shell, return_length=True), dtype=float)
    hydrophobic_contacts = np.asarray(
        [float(np.sum(hydrophobic[tree.query_ball_point(pt, r=5.0)])) for pt in layer],
        dtype=float)
    directions = np.asarray([[x, y, z] for x in (-1, 0, 1) for y in (-1, 0, 1)
                             for z in (-1, 0, 1) if (x, y, z) != (0, 0, 0)], dtype=float)
    directions /= np.linalg.norm(directions, axis=1, keepdims=True)
    enclosure = np.zeros(len(layer), dtype=float)
    for index, point in enumerate(layer):
        neighbours = tree.query_ball_point(point, r=sector_radius)
        if not neighbours:
            continue
        vectors = coords[neighbours] - point
        vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-9)
        enclosure[index] = np.count_nonzero((vectors @ directions.T > 0.72).any(axis=0))

    # ---- 3) 打分（三个因子都单调、不封顶）----
    score = (enclosure / 26.0) ** 2 * (burial / 45.0) * (0.4 + hydrophobic_contacts / 8.0)

    # ---- 4) 贪心球聚类 + 有限合并 ----
    order = np.argsort(-score)
    assigned = np.zeros(len(layer), dtype=bool)
    groups: List[Any] = []
    for index in order:
        if assigned[index] or score[index] <= 0:
            continue
        members = np.nonzero((~assigned) & (np.linalg.norm(layer - layer[index], axis=1)
                                            <= cluster_radius))[0]
        assigned[members] = True
        if len(members) >= min_points:
            groups.append(members)

    def _span(members: Any) -> float:
        pts = layer[members]
        return float((pts.max(axis=0) - pts.min(axis=0)).max())

    merged: List[Any] = []
    for members in sorted(groups, key=lambda g: -float(score[g].max())):
        placed = False
        for slot_index, slot in enumerate(merged):
            union = np.concatenate([slot, members])
            if (distance(layer[slot].mean(axis=0), layer[members].mean(axis=0)) <= merge_radius
                    and _span(union) <= max_span):
                merged[slot_index] = union     # 注意：不能用 list.index（numpy 数组比较是逐元素）
                placed = True
                break
        if not placed:
            merged.append(members)

    pockets: List[Dict[str, Any]] = []
    for members in merged:
        pts = layer[members]
        centre = pts.mean(axis=0)
        extent = pts.max(axis=0) - pts.min(axis=0)
        centre_list = [round(float(c), 3) for c in centre]
        pockets.append({
            "name": "pocket_1",
            "rank": 0,
            "score": round(float(score[members].max()), 3),
            "center": centre_list,
            "extent": [round(float(e), 2) for e in extent],
            "volume": round(float(len(members)) * spacing ** 3, 1),
            "n_points": int(len(members)),
            "burial": round(float(burial[members].mean()), 1),
            "enclosure": round(float(enclosure[members].mean()), 1),
            "hydrophobic_contacts": round(float(hydrophobic_contacts[members].mean()), 1),
            "residues": _residues_near(heavy, centre_list),
            "source": "geometric",
        })
    pockets.sort(key=lambda p: -(p.get("score") or 0.0))
    for rank, pocket in enumerate(pockets[:top_n], start=1):
        pocket["rank"] = rank
        pocket["name"] = f"pocket_{rank}"
    return {"status": "ok" if pockets else "no_pockets", "engine": "geometric",
            "pockets": pockets[:top_n], "spacing": round(spacing, 3),
            "grid_points": int(len(grid)), "surface_points": int(len(layer))}


# --------------------------------------------------------------------------- #
# 统一入口 + 缓存
# --------------------------------------------------------------------------- #
def _cache_key(path: str, engine: str, top_n: int) -> str:
    digest = hashlib.sha1()
    digest.update(Path(path).name.encode())
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as e:
        logger.debug("文件哈希计算失败（%s）：按“内容未知”处理", e)
    return f"{digest.hexdigest()[:16]}-{engine}-{top_n}"


def detect_pockets(receptor_path: str, *, engine: str = "auto", top_n: int = DEFAULT_TOP_N,
                   pdb_hint: str = "", use_cache: bool = True) -> Dict[str, Any]:
    """按引擎顺序检测口袋，返回 {status, engine, pockets, attempts, cached}。"""
    engine = (engine or "auto").strip().lower()
    if engine not in POCKET_ENGINES:
        engine = "auto"
    cache_file = cache_dir() / "pockets" / f"{_cache_key(receptor_path, engine, top_n)}.json"
    if use_cache and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            cached["cached"] = True
            return cached
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("口袋缓存不可用（%s），改为现场计算", e)

    attempts: List[Dict[str, Any]] = []
    order = ["p2rank", "geometric"] if engine in ("auto", "p2rank", "geometric") else []
    if engine == "p2rank":
        order = ["p2rank"]
    elif engine == "geometric":
        order = ["geometric"]
    elif engine == "known_site":
        order = []

    result: Dict[str, Any] = {"status": "no_pockets", "engine": engine, "pockets": []}
    for candidate in order:
        if candidate == "p2rank":
            pdb = ensure_pdb_for_pockets(receptor_path, pdb_hint=pdb_hint)
            out = run_p2rank(pdb, top_n=top_n)
            out["pdb_used"] = pdb
        else:
            atoms = read_receptor_atoms(receptor_path)
            out = detect_geometric(atoms, top_n=top_n)
        attempts.append({"engine": candidate, "status": out.get("status"),
                         "message": out.get("message", ""), "pockets": len(out.get("pockets") or [])})
        if out.get("pockets"):
            result = {**out, "cached": False, "attempts": attempts}
            break
        result = {**out, "attempts": attempts}
    else:
        result.setdefault("attempts", attempts)

    if use_cache and result.get("pockets"):
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except OSError as e:  # noqa: BLE001
            logger.debug("口袋预测缓存写入失败：%s", e)
    return result


# --------------------------------------------------------------------------- #
# 口袋 → 盒子
# --------------------------------------------------------------------------- #
def pocket_to_box(pocket: Dict[str, Any], *, padding: float = DEFAULT_PADDING,
                  min_size: float = DEFAULT_MIN_SIZE,
                  max_size: float = DEFAULT_MAX_SIZE) -> Tuple[List[float], List[float]]:
    """把口袋转成对接盒子：中心=口袋中心，尺寸=口袋范围+2×padding 并夹到合理区间。"""
    center = [float(c) for c in (pocket.get("center") or [])]
    extent = [float(e) for e in (pocket.get("extent") or [])]
    if len(center) != 3:
        raise ValueError("口袋缺少中心坐标")
    if len(extent) != 3:
        extent = [max_size - 2 * padding] * 3
    size = [max(min_size, min(max_size, e + 2 * padding)) for e in extent]
    return [round(c, 3) for c in center], [round(s, 2) for s in size]


def engine_settings() -> Dict[str, Any]:
    """口袋引擎与盒子参数（环境变量优先，设置页写入的就是这些）。"""
    return {
        "engine": (env("POCKET_ENGINE", "auto") or "auto").strip().lower(),
        "top_n": max(1, env_int("POCKET_TOP_N", DEFAULT_TOP_N)),
        "padding": env_float("POCKET_PADDING", DEFAULT_PADDING),
        "min_size": env_float("POCKET_MIN_SIZE", DEFAULT_MIN_SIZE),
        "max_size": env_float("POCKET_MAX_SIZE", DEFAULT_MAX_SIZE),
        # C 方案：库级配体感知下限 + 超限分子分组
        "box_span_enabled": box_span_enabled(),
        "box_span_sample": box_span_sample_size(),
        "box_group_margin": box_group_margin(),
        "box_large_padding": box_large_padding(),
    }


def known_site_from_spec(spec: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """把受体 spec 里的位点信息整理成「参考位点」（含可信度）。"""
    if not spec:
        return None
    site = spec.get("site") or {}
    center = spec.get("center") or site.get("center")
    if not center:
        return None
    source = str(site.get("source") or "")
    size = spec.get("size") or site.get("size") or []
    # 可信度判定：**共晶配体**质心是实验证据（可信）；只有「蛋白质/受体原子质心」这类
    # 无位点信息时的兜底才算低可信（此时应当改用工具预测的口袋）。
    fallback_marks = ("蛋白质质心", "受体原子质心", "蛋白质心", "未找到位点", "centroid")
    experimental_marks = ("共晶", "配体", "注册表", "已知位点", "实验", "用户")
    if not source.strip():
        # 没有标注来源 = 不知道盒子怎么来的：必须按低可信处理（交给工具预测），
        # 否则会重演「盒子放在蛋白质心却当成实验位点」的问题
        low_trust = True
        source = "未标注来源的位点盒（按低可信处理）"
    elif any(key in source for key in fallback_marks):
        low_trust = True          # 兜底来源优先判定（"未找到共晶配体" 里也含 "共晶"，不能靠子串猜）
    elif any(key in source for key in experimental_marks):
        low_trust = False
    else:
        low_trust = True          # 无法识别的来源一律保守处理
    residues: List[str] = []
    pdb = str(spec.get("pdb") or "")
    if pdb and os.path.exists(pdb):
        try:      # 参考位点附近的蛋白残基：供与预测口袋做残基重叠比对
            residues = _residues_near(read_receptor_atoms(pdb), center, radius=8.0, limit=40)
        except OSError:
            residues = []
    return {"center": [float(c) for c in center], "size": [float(s) for s in size],
            "source": source, "trust": "fallback" if low_trust else "experimental",
            "residues": residues}


def validate_pocket(pocket: Dict[str, Any], reference: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """用独立参考位点（实验/已知）验证预测口袋的一致性。"""
    if not reference:
        return {"status": "no_reference"}
    d = distance(pocket["center"], reference["center"])

    def _residue_numbers(values: Any) -> Dict[str, str]:
        """抽出残基编号用于跨引擎比对（P2Rank 给 `H_174`，我们给 `TRP215`）。"""
        out: Dict[str, str] = {}
        for item in values or []:
            digits = re.sub(r"\D", "", str(item))
            if digits:
                out[digits] = str(item)
        return out

    ref_residues = _residue_numbers(reference.get("residues"))
    pocket_residues = _residue_numbers(pocket.get("residues"))
    overlap = sorted(pocket_residues[k] for k in (set(ref_residues) & set(pocket_residues)))
    return {
        "status": "consistent" if d <= DEFAULT_AGREE_RADIUS else "inconsistent",
        "distance_angstrom": round(d, 2),
        "agree_radius": DEFAULT_AGREE_RADIUS,
        "reference_source": reference.get("source", ""),
        "shared_residues": overlap[:10],
        "shared_residue_count": len(overlap),
    }


def apply_box_floor(size: Sequence[float], box_floor: Optional[Dict[str, Any]], *,
                    max_size: float) -> Tuple[List[float], Optional[Dict[str, Any]], List[str]]:
    """把**库级配体感知下限**应用到已有盒子上：只抬高、只夹到 `max_size`，绝不缩小。

    公式 `size_i = clamp(extent_i + 2×padding, lib_lower_bound, max_size)`；已有盒子一定
    ≤ `max_size`（口袋路径已夹过），因此等价于 `max(size_i, min(bound, max_size))`。
    返回 `(size, floor_info, notes)`：`floor_info` 为 `None` 表示没生效（开关关闭或无下限），
    仍不改变原尺寸。**唯一实现**：自动定盒（`select_site`）与「口袋 Agent 提交的盒子」共用。
    """
    current = [float(s) for s in size]
    if not box_floor:
        return current, None, []
    if box_floor.get("enabled") is False:
        # 开关关闭：完全不影响盒子，但仍如实记一条「本可以抬到多少」便于复算与排查
        return current, {**box_floor, "raised": False,
                         "size_before": [round(x, 2) for x in current],
                         "size_after": [round(x, 2) for x in current]}, []
    if not box_floor.get("bound"):
        return current, None, []
    bound = float(box_floor["bound"])
    effective = min(float(max_size), bound)
    raised = [max(current[i], effective) for i in range(len(current))]
    changed = any(raised[i] > current[i] + 1e-9 for i in range(len(current)))
    floor_info = {**box_floor, "raised": changed,
                  "size_before": [round(x, 2) for x in current],
                  "size_after": [round(x, 2) for x in raised]}
    notes: List[str] = []
    if changed:
        p95 = box_floor.get("p95_span")
        p95_text = f"{p95} Å" if p95 is not None else "未知"
        note = f"为容纳库内大配体（P95 跨度 {p95_text}）把盒子下限提到 {bound:.1f} Å"
        if bound > float(max_size):
            note += f"（受盒上限 {float(max_size):.1f} Å 限制，更大的分子将进入 large 组单独重跑）"
        notes.append(note)
    return raised, floor_info, notes


def select_site(spec: Dict[str, Any], *, engine: str = "auto", top_n: int = DEFAULT_TOP_N,
                padding: float = DEFAULT_PADDING, min_size: float = DEFAULT_MIN_SIZE,
                max_size: float = DEFAULT_MAX_SIZE, use_cache: bool = True,
                override: Optional[Dict[str, Any]] = None,
                box_floor: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """确定该受体的对接盒子，返回带**溯源**的结果。

    override: 显式指定的盒子（用户表单 / 指令 / 口袋分析 Agent 的选择），永远优先。
    box_floor: 库级配体感知下限（`library_span_bound()` 的返回），把**服务端算出的**盒子
               按 `max(size, min(bound, max_size))` 抬高；**用户显式指定的盒子不受影响**
               （显式优先是不变量）。抬高时把原因写进 `warnings` 与 `source`。
    """
    reference = known_site_from_spec(spec)
    receptor_path = str(spec.get("pdbqt") or spec.get("file") or "")
    pdb_hint = str(spec.get("pdb") or "")

    if override and override.get("center"):
        return {
            "center": [float(c) for c in override["center"]],
            "size": [float(s) for s in (override.get("size") or spec.get("size") or
                                        [max_size - 2 * padding] * 3)],
            "source": str(override.get("source") or "显式指定"),
            "engine": str(override.get("engine") or "explicit"),
            "pocket": override.get("pocket"),
            "pockets": list(override.get("pockets") or []),
            "validation": override.get("validation") or {},
            "warnings": [],
            "library_floor": None,
            "chosen_by": str(override.get("chosen_by") or "request"),
        }

    warnings: List[str] = []
    detection: Dict[str, Any] = {"status": "skipped", "engine": engine, "pockets": []}
    if engine != "known_site" and receptor_path:
        detection = detect_pockets(receptor_path, engine=engine, top_n=top_n,
                                   pdb_hint=pdb_hint, use_cache=use_cache)
    pockets = detection.get("pockets") or []
    if detection.get("status") not in ("ok", "no_pockets", "skipped"):
        warnings.append(f"口袋预测未成功（{detection.get('engine')}）：{detection.get('message', '')}")
    for attempt in detection.get("attempts") or []:
        if attempt.get("status") not in ("ok", None):
            warnings.append(f"{attempt['engine']} 不可用/失败：{attempt.get('message', '')}")

    chosen: Optional[Dict[str, Any]] = None
    center: Optional[List[float]] = None
    size: Optional[List[float]] = None
    source = ""
    engine_used = detection.get("engine") or engine
    validation: Dict[str, Any] = {}
    chosen_by = ""

    if pockets:
        best = pockets[0]
        # 只有实验/已知位点才能作为一致性基准：低可信兜底（蛋白质心）不是基准，
        # 否则会出现「用工具预测替换了兜底，却被判成与兜底不一致」的误报。
        trusted_reference = reference if (reference and reference.get("trust") == "experimental") else None
        validation = validate_pocket(best, trusted_reference)
        if reference and reference.get("trust") == "experimental":
            # 实验位点更可信：采用它，用工具做独立验证
            center = list(reference["center"])
            size = list(reference["size"]) or pocket_to_box(best, padding=padding,
                                                            min_size=min_size,
                                                            max_size=max_size)[1]
            if validation.get("status") == "consistent":
                source = (f"实验位点（{reference['source']}）· {engine_used} 预测一致"
                          f"（相距 {validation['distance_angstrom']} Å）")
            else:
                source = f"实验位点（{reference['source']}）"
                warnings.append(
                    f"{engine_used} 预测的 top 口袋与实验位点相距 "
                    f"{validation.get('distance_angstrom')} Å（> {DEFAULT_AGREE_RADIUS} Å），"
                    "已优先采用实验位点；如需改用预测口袋，请让口袋分析 Agent 明确选择")
            chosen_by = "experimental_site"
        else:
            center, size = pocket_to_box(best, padding=padding, min_size=min_size, max_size=max_size)
            detail = f"，score={best.get('score')}" if best.get("score") is not None else ""
            source = f"{engine_used} 预测口袋 {best.get('name')}{detail}"
            if reference:
                source += f"（替代低可信参考位点：{reference['source']}）"
            chosen_by = "pocket_prediction"
        chosen = best
    elif reference:
        center = list(reference["center"])
        size = list(reference["size"]) or [max_size - 2 * padding] * 3
        source = f"已知位点（{reference['source']}）"
        chosen_by = "known_site"
        if detection.get("status") != "skipped":
            warnings.append("口袋预测未给出结果，已退回已知位点")
    else:
        from docking_agent.core.receptors import DEFAULT_BOX_SIZE

        center = _centroid(read_receptor_atoms(receptor_path)) if receptor_path else None
        center = [round(c, 3) for c in center] if center else [0.0, 0.0, 0.0]
        size = list(DEFAULT_BOX_SIZE)
        source = "蛋白质心（**不可靠**：既无已知位点，口袋预测也不可用）"
        chosen_by = "centroid"
        warnings.append("无法确定结合位点：盒子放在蛋白质心，结果仅供参考")

    if chosen is None and pockets:
        chosen = pockets[0]

    # ---- 库级配体感知下限：抬高服务端算出的盒子（显式指定不受影响，见上方 early return）----
    size, floor_info, floor_notes = apply_box_floor(size, box_floor, max_size=max_size)
    for note in floor_notes:
        source += f" · 库级下限 {float(box_floor['bound']):.1f} Å"
        warnings.append(note)

    return {
        "center": [round(float(c), 3) for c in center],
        "size": [round(float(s), 2) for s in size],
        "source": source,
        "engine": engine_used,
        "engine_available": available_engines(),
        "pocket": chosen,
        "pockets": pockets,
        "validation": validation,
        "reference": reference,
        "warnings": warnings,
        "library_floor": floor_info,
        "chosen_by": chosen_by,
        "detection": {k: v for k, v in detection.items() if k != "pockets"},
    }
