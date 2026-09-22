"""受体质子化：与配体**同一目标 pH** 的准备流程（pdb2pqr + PROPKA → PDBQT）。

## 为什么必须做

对接是"受体 + 配体"的相互作用：配体按目标 pH 分配了质子化态，受体却停在
meeko 残基模板的默认态（≈pH 7 的固定状态），两边就**不是同一套化学条件**：

- HIS 的质子化/互变异构（HID/HIE/HIP）直接决定它能否作为氢键供体；
- ASP/GLU 在酸性条件下应质子化，否则 S1/活性中心的静电与氢键互补全错；
- 凝血酶的 ASP189（PROPKA pKa ≈ 6.6）正是决定 P1 精氨酸/脒基结合的关键残基。

本模块用 `pdb2pqr --ph-calc-method=propka --with-ph=<pH>` 生成 PQR（含氢与电荷/半径），
再交给 meeko `--read_pqr` 写出受体 PDBQT。**逐残基的 PROPKA pKa 与最终 HIS 状态都会留痕**，
可人工核对；PQR 与 PROPKA 文本会随运行产物交付。

## 失败时的口径（绝不假装做过）

- 找不到 pdb2pqr / pdb2pqr 失败 / meeko 读 PQR 失败 → **回退**到原标准准备流程，
  并在 `receptor_protonation` 里写明 `applied=False` 与原因，报告同时给出
  「受体质子化为模板默认态，未与配体目标 pH 对齐」的提示；
- 要求保留的杂原子若不是**单原子离子**（金属离子/卤素），说明有有机辅因子：
  pdb2pqr 无法为它们参数化，此时同样回退标准流程（保辅因子优先），并如实记录。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: 主链原子：缺任一即认为该残基不完整（pdb2pqr 会因缺原子直接报错退出）
_BACKBONE = ("N", "CA", "C", "O")
#: pdb2pqr 能直接参数化的单原子离子（可随蛋白一起做 pH 处理）
SIMPLE_IONS = frozenset({
    "LI", "NA", "K", "RB", "CS", "MG", "CA", "SR", "BA", "ZN", "MN", "FE", "FE2", "CO",
    "NI", "CU", "CU1", "CD", "HG", "AL", "GA", "CL", "BR", "IOD", "F", "SO4", "PO4",
})
#: PROPKA 摘要里的可滴定残基（酸/碱判定方向不同）
_ACIDIC = frozenset({"ASP", "GLU", "CYS", "TYR"})
_BASIC = frozenset({"HIS", "LYS"})
#: 与 pKa 距离小于该值视为「边界残基，状态不确定」，要在报告里点出来
BOUNDARY_DELTA = 0.5

_PROPKA_ROW = re.compile(r"^\s+(ASP|GLU|HIS|LYS|CYS|TYR)\s+(-?\d+)\s+([A-Za-z]?)\s+(-?\d+\.\d+)", re.M)


def _env(name: str, default: str = "") -> str:
    try:
        from docking_agent.config import env as _config_env

        return str(_config_env(name, default) or default)
    except Exception:  # noqa: BLE001 - 配置层不可用时用默认
        return os.environ.get(name, default) or default


def pdb2pqr_bin() -> str:
    """定位 pdb2pqr：`PDB2PQR_BIN` → PATH → 常见本地安装（PyMOL 自带）。找不到返回空串。"""
    candidate = _env("PDB2PQR_BIN")
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    for name in ("pdb2pqr", "pdb2pqr30"):
        found = shutil.which(name)
        if found:
            return found
    for extra in (Path.home() / "pymol" / "bin" / "pdb2pqr",
                  Path("/usr/local/bin/pdb2pqr"), Path("/opt/pdb2pqr/pdb2pqr")):
        if extra.is_file() and os.access(extra, os.X_OK):
            return str(extra)
    return ""


def is_available() -> bool:
    return bool(pdb2pqr_bin())


def tool_version(binary: str = "") -> str:
    """pdb2pqr 版本（仅供溯源；取不到写「未知」）。"""
    binary = binary or pdb2pqr_bin()
    if not binary:
        return "未安装"
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, timeout=60)
        text = ((proc.stdout or b"") + (proc.stderr or b"")).decode(errors="replace").strip()
        first = text.splitlines()[0].strip() if text else ""
        # 老版 pdb2pqr 脚本里的版本号是未展开的模板（`@PDB2PQR_VERSION@`），如实标注
        if not first or "@" in first:
            return "未知版本（脚本未展开版本号）"
        return first[:120]
    except Exception as e:  # noqa: BLE001
        logger.debug("读取 pdb2pqr 版本失败：%s", e)
        return "未知"


def filter_incomplete_residues(pdb_text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """剔除主链不全的残基（pdb2pqr 遇到会直接报错退出）。

    真实拦截：1DWC 的 `GLY H 246` 只留了部分原子，pdb2pqr 报
    "Too few atoms present to reconstruct or cap residue"，整条 pH 准备就废了。
    剔除以残基为单位、逐个记录，报告里能看出少了哪个残基。
    """
    atoms: Dict[Tuple[str, str], set] = {}
    order: List[Tuple[str, str]] = []
    names: Dict[Tuple[str, str], str] = {}
    lines = pdb_text.splitlines()
    for line in lines:
        if line.startswith("ATOM"):
            key = (line[21], line[22:27])
            if key not in atoms:
                atoms[key] = set()
                order.append(key)
                names[key] = line[17:20].strip()
            atoms[key].add(line[12:16].strip())
    keep = {k for k in order if set(_BACKBONE) <= atoms[k]}
    dropped = [{"chain": k[0], "resnum": k[1].strip(), "resname": names.get(k, "")}
               for k in order if k not in keep]
    out: List[str] = []
    for line in lines:
        if line.startswith("ATOM"):
            if (line[21], line[22:27]) not in keep:
                continue
        out.append(line)
    return "\n".join(out) + "\n", dropped


def hetatm_are_simple_ions(pdb_text: str) -> Tuple[bool, List[str]]:
    """要保留的杂原子是否都是单原子离子（否则 pdb2pqr 无法参数化 → 不应走 pH 路径）。"""
    residues = {line[17:20].strip().upper() for line in pdb_text.splitlines()
                if line.startswith("HETATM")}
    unsupported = sorted(r for r in residues if r not in SIMPLE_IONS)
    return (not unsupported), unsupported


#: PQR 残基键 token：可选链号 + 残基号 + 可选插入码（可能两两粘连，见 parse_pqr_atom_line）
_PQR_RESID_RE = re.compile(r"^([A-Za-z]?)(-?\d+)([A-Za-z]?)$")


def parse_pqr_atom_line(line: str) -> Optional[Dict[str, Any]]:
    """解析一条 PQR 的 ATOM/HETATM 行，返回字段字典；不是原子行或无法解析时返回 None。

    **为什么不能只按白空格切**：pdb2pqr 按 PDB 固定列写 PQR，白空格切分有两种粘连：
      * 插入码贴在残基号上：`ILE H 36A` → 第 6 个 token 是 `36A`；
      * **4 位残基号**把链号挤到没有空格：`GLU A1005` → 该行只有 10 个 token，
        meeko 会把 `A1005` 当链号、把 x 坐标当残基号 → `int('128.990')` 崩
        （真实缺陷：7YHP 的残基号到 1xxx，整条 pH 准备因此失败、静默回退标准流程）。
    这里按「最后 5 个 token 一定是 x y z charge radius」反推残基键，不依赖列宽。
    """
    parts = line.split()
    if len(parts) < 9 or parts[0] not in ("ATOM", "HETATM"):
        return None
    try:
        x, y, z, charge, radius = (float(value) for value in parts[-5:])
    except ValueError:
        return None
    match = _PQR_RESID_RE.match("".join(parts[4:-5]))
    if not match:
        return None
    return {"record": parts[0], "serial": parts[1], "name": parts[2], "resname": parts[3],
            "chain": match.group(1), "resnum": int(match.group(2)), "icode": match.group(3),
            "x": x, "y": y, "z": z, "charge": charge, "radius": radius}


def format_pqr_atom_line(atom: Dict[str, Any]) -> str:
    """把 `parse_pqr_atom_line` 的结果写成 meeko 一定能解析的形态（链/残基号/插入码各自独立）。"""
    parts = [str(atom["record"]), str(atom["serial"]), str(atom["name"]), str(atom["resname"])]
    if atom.get("chain"):
        parts.append(str(atom["chain"]))
    parts.append(str(atom["resnum"]))
    if atom.get("icode"):
        parts.append(str(atom["icode"]))
    parts += [f"{atom['x']:.3f}", f"{atom['y']:.3f}", f"{atom['z']:.3f}",
              f"{atom['charge']:.4f}", f"{atom['radius']:.4f}"]
    return " ".join(parts)


def normalize_pqr_for_meeko(pqr_text: str) -> Tuple[str, int]:
    """把 pdb2pqr 的 PQR 规范化成 meeko 能解析的形式，返回 (规范化文本, 改写行数)。

    meeko 的 PQR 解析器要求 `serial name resName [chain] resSeq [icode] x y z charge radius`
    是**独立字段**；pdb2pqr 的固定列写法在两种情况下会让白空格切分粘连（插入码、4 位残基号），
    两种都由 `parse_pqr_atom_line` 统一拆开。**不能丢掉插入码**（1DWC 里 36 与 36A 是不同残基）。
    非原子行（REMARK/TER/END…）原样保留。
    """
    split = 0
    out: List[str] = []
    for line in pqr_text.splitlines():
        if line.startswith(("ATOM", "HETATM")):
            parts = line.split()
            key_tokens = parts[4:-5] if len(parts) >= 9 else []
            # 已经是规范形状（链与残基号各自独立）→ 原样保留，不做无谓的数值重排
            canonical = ((len(key_tokens) == 1 and key_tokens[0].lstrip("-").isdigit())
                         or (len(key_tokens) == 2 and len(key_tokens[0]) == 1
                             and key_tokens[1].lstrip("-").isdigit()))
            if canonical:
                out.append(line)
                continue
            atom = parse_pqr_atom_line(line)
            if atom is not None:
                fixed = format_pqr_atom_line(atom)
                if fixed != line:
                    split += 1
                out.append(fixed)
                continue
        out.append(line)
    return "\n".join(out) + "\n", split


def parse_propka_summary(text: str) -> List[Dict[str, Any]]:
    """解析 PROPKA 输出的逐残基 pKa 表（`SUMMARY OF THIS PREDICTION` 段）。"""
    start = text.find("SUMMARY OF THIS PREDICTION")
    body = text[start:] if start >= 0 else text
    rows: List[Dict[str, Any]] = []
    for resname, resnum, chain, pka in _PROPKA_ROW.findall(body):
        try:
            value = float(pka)
        except ValueError:  # pragma: no cover - 正则已保证是数字
            continue
        rows.append({"resname": resname, "resnum": int(resnum), "chain": chain, "pka": value})
    return rows


def titratable_states(rows: Sequence[Dict[str, Any]], ph: float) -> Dict[str, Any]:
    """按 PROPKA 的 pKa 判定各可滴定残基在目标 pH 下是否质子化（与 pdb2pqr 的规则一致）。"""
    summary: Dict[str, Dict[str, int]] = {}
    boundary: List[Dict[str, Any]] = []
    for row in rows:
        resname = str(row.get("resname") or "")
        if resname not in _ACIDIC and resname not in _BASIC:
            continue
        pka = float(row.get("pka") or 0.0)
        protonated = ph < pka          # 酸: pH<pKa 质子化；碱: pH<pKa 质子化 —— 判据同一形式
        bucket = summary.setdefault(resname, {"total": 0, "protonated": 0})
        bucket["total"] += 1
        bucket["protonated"] += 1 if protonated else 0
        if abs(pka - ph) < BOUNDARY_DELTA:
            boundary.append({"resname": resname, "resnum": row.get("resnum"),
                             "chain": row.get("chain"), "pka": pka,
                             "state": "质子化" if protonated else "去质子化"})
    return {"by_residue": summary, "boundary": boundary}


def his_states_from_pqr(pqr_text: str) -> Dict[str, int]:
    """从 PQR 的氢原子位置读出 HIS 实际被赋予的状态（HID / HIE / HIP）。

    PROPKA 只给 pKa，互变异构由 pdb2pqr 优化决定 —— 所以这里读**真正应用了什么**，
    而不是用 pKa 猜。HD1 与 HE2 同时在 → 双质子化（HIP）。
    """
    atoms: Dict[Tuple[str, str], set] = {}
    for line in pqr_text.splitlines():
        if not line.startswith(("ATOM", "HETATM")):
            continue
        parsed = parse_pqr_atom_line(line)
        if parsed is None or parsed.get("resname") != "HIS":
            continue
        key = (str(parsed.get("chain") or ""),
               f"{parsed.get('resnum')}{parsed.get('icode') or ''}")
        atoms.setdefault(key, set()).add(str(parsed.get("name") or ""))
    counts = {"HID": 0, "HIE": 0, "HIP": 0, "未判定": 0}
    for names in atoms.values():
        has_d1, has_e2 = "HD1" in names, "HE2" in names
        if has_d1 and has_e2:
            counts["HIP"] += 1
        elif has_d1:
            counts["HID"] += 1
        elif has_e2:
            counts["HIE"] += 1
        else:
            counts["未判定"] += 1
    return counts


def _cache_key(prot_text: str, ph: float) -> str:
    return hashlib.sha1(f"{hashlib.sha1(prot_text.encode()).hexdigest()}|ph={ph:g}".encode()).hexdigest()[:12]


def parse_unmatched_residues(text: str) -> List[str]:
    """从 meeko 输出里解析「模板匹配失败」的残基键（如 `A:402`）。

    meeko 的两种表现都要认：
      - 严格模式：整条准备报错退出，错误里带 `Template matching failed for: ['A:402', ...]`；
      - 容错模式（`-x/--delete_bad_res`）：打 warning 后丢弃这些残基继续（我们**必须**把它们如实上报，
        否则用户不会知道受体少了一段）。
    """
    if not text or "Template matching failed" not in text:
        return []
    keys: List[str] = []
    for line in text.splitlines():
        if "Template matching failed" not in line:
            continue
        for token in re.findall(r"'([^']+)'|\"([^\"]+)\"", line):
            value = token[0] or token[1]
            if re.match(r"^[A-Za-z0-9_]*:?\-?\d+[A-Za-z]?$", value.strip()):
                keys.append(value.strip())
    # 去重且保持顺序
    seen = set()
    out: List[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _run_pdb2pqr(binary: str, filtered_path: str, pqr_path: str, ph: float,
                 extra: Sequence[str]) -> Tuple[bool, str]:
    """跑一次 pdb2pqr，返回 (是否成功, 错误摘要)。"""
    try:
        proc = subprocess.run(
            [binary, "--ff=AMBER", "--ph-calc-method=propka", f"--with-ph={ph:g}", "--chain",
             *extra, filtered_path, pqr_path],
            capture_output=True, timeout=max(60, int(_env("PDB2PQR_TIMEOUT", "900") or 900)))
    except subprocess.TimeoutExpired:
        return False, "pdb2pqr 超时（可用 PDB2PQR_TIMEOUT 调整上限）"
    except Exception as e:  # noqa: BLE001
        return False, f"pdb2pqr 启动失败（{type(e).__name__}: {e}）"
    if proc.returncode != 0 or not os.path.isfile(pqr_path):
        tail = ((proc.stderr or b"") + (proc.stdout or b"")).decode(errors="replace").strip()[-400:]
        return False, f"pdb2pqr 失败（returncode={proc.returncode}）：{tail or '无输出'}"
    return True, ""


def _run_meeko_pqr(meeko_pqr: str, out_base_ph: str, extra: Sequence[str]) -> Tuple[bool, str, str]:
    """跑一次 meeko `--read_pqr`，返回 (是否成功, 输出文本, 错误摘要)。"""
    tmp = f"{out_base_ph}_tmp"
    try:
        prep = subprocess.run(
            [_python(), _mk_prepare_receptor(), "--read_pqr", meeko_pqr, "-o", tmp, "-p", *extra],
            capture_output=True, timeout=900)
    except subprocess.TimeoutExpired:
        return False, "", "meeko 准备 PDBQT 超时"
    except Exception as e:  # noqa: BLE001
        return False, "", f"meeko 启动失败（{type(e).__name__}: {e}）"
    text = ((prep.stdout or b"") + (prep.stderr or b"")).decode(errors="replace")
    if prep.returncode != 0 or not os.path.isfile(tmp + ".pdbqt"):
        return False, text, f"meeko 读取 PQR 失败（returncode={prep.returncode}）：{text.strip()[-400:] or '无输出'}"
    return True, text, ""


#: 准备阶梯（按**质量从优到劣**）。为什么是这个顺序：
#:   1. `opt`：pdb2pqr 默认会做氢键网络优化（His 的 HID/HIE 由氢键环境决定，最接近真实）；
#:   2. `noopt`：只按几何摆氢。某些结构里 pdb2pqr 的优化会把羟基氢**摆到受体羰基氧 1.1 Å 处**
#:      （8ZE2 的 ILE402···THR406：晶体 O···O 仅 2.15 Å），meeko 的距离法键感知会把它当成
#:      **残基间共价键**而拒绝整条 PQR。此时退到几何摆氢可以**保住全部残基**，只损失 His 互变异构的择优；
#:   3. `delete`：仍然不行才删掉模板不匹配的残基（并逐个上报）。
#: 明确**不**先删残基：删掉的可能正是活性位点附近的残基，代价比"氢摆放不最优"大得多。
_PREP_LADDER: Tuple[Tuple[str, Tuple[str, ...], Tuple[str, ...]], ...] = (
    ("opt", (), ()),
    ("noopt", ("--noopt",), ()),
    ("delete", (), ("-x",)),
    ("noopt+delete", ("--noopt",), ("-x",)),
)


def prepare_pdbqt_at_ph(prot_pdb: str, out_base: str, ph: float,
                        cache_dir: Optional[str] = None) -> Dict[str, Any]:
    """按目标 pH 把蛋白准备成受体 PDBQT，返回 `{ok, pdbqt, pqr, propka, info, error}`。

    产物一律写到 `out_base` 前缀下（调用方已按「源文件内容 + keep 集」内容寻址），
    因此并发运行不会互相覆盖；同一 (蛋白内容, pH) 组合再次请求时直接复用缓存。

    失败时依次尝试 `_PREP_LADDER` 里的降级方案（见其注释），每一步的成败都记进
    `info["attempts"]`，最终用的是哪一档写 `info["variant"]`，被 meeko 丢弃的残基写
    `info["dropped_bad_residues"]` —— 报告与运行笔记都会显示。
    """
    _ = cache_dir or os.path.dirname(out_base) or "."   # 产物统一写在 out_base 前缀下（内容寻址）
    pqr_path = f"{out_base}_ph{ph:g}.pqr"
    propka_path = f"{out_base}_ph{ph:g}.propka"
    meeko_pqr = f"{out_base}_ph{ph:g}_meeko.pqr"
    pdbqt_path = f"{out_base}_ph{ph:g}.pdbqt"
    filtered_path = f"{out_base}_ph{ph:g}_in.pdb"
    binary = pdb2pqr_bin()
    info: Dict[str, Any] = {"applied": False, "policy": "ph", "ph": ph}

    if not binary:
        info["reason"] = ("未找到 pdb2pqr（可用 PDB2PQR_BIN 指定路径）；"
                          "受体质子化保持 meeko 残基模板默认态")
        return {"ok": False, "error": info["reason"], "info": info}

    text = Path(prot_pdb).read_text(encoding="utf-8", errors="ignore")
    filtered, dropped = filter_incomplete_residues(text)
    ions_ok, bad_hetatm = hetatm_are_simple_ions(filtered)
    info["dropped_incomplete_residues"] = dropped
    if bad_hetatm:
        info["reason"] = (f"要求保留的杂原子 {'、'.join(bad_hetatm)} 不是单原子离子，"
                          "pdb2pqr 无法为其参数化 → 为保证辅因子不丢失，回退标准准备流程")
        return {"ok": False, "error": info["reason"], "info": info}
    if not filtered.strip():
        info["reason"] = "过滤后没有可用于 pH 准备的蛋白原子"
        return {"ok": False, "error": info["reason"], "info": info}

    Path(filtered_path).write_text(filtered, encoding="utf-8")

    # ---- 缓存命中：复用上次成功的那一档（含变体与丢弃残基的溯源）----
    cached_info = read_sidecar(out_base, ph).get("receptor_protonation") or {}
    cached_variant = str(cached_info.get("variant") or "")
    if os.path.isfile(pdbqt_path) and os.path.isfile(pqr_path) and cached_variant:
        best_variant = _PREP_LADDER[0][0]
        # 只有「上次就是最优档」或「已记录最优档确实失败」时才复用降级产物；
        # 否则重算一次去争取更优的档（避免一次偶然失败被永久缓存）。
        reusable = cached_variant == best_variant or cached_info.get("best_variant_failed")
        if reusable:
            info.update({"applied": True, "cached": True,
                         "tool": "pdb2pqr + PROPKA + meeko",
                         "pdb2pqr_version": tool_version(binary),
                         "pdbqt": pdbqt_path, "pqr": pqr_path,
                         "propka": propka_path if os.path.isfile(propka_path) else "",
                         **{k: cached_info.get(k) for k in
                            ("variant", "dropped_bad_residues", "attempts", "pdb2pqr_noopt")}})
            _attach_states(info, Path(pqr_path).read_text(errors="ignore"),
                           Path(propka_path).read_text(errors="ignore") if os.path.isfile(propka_path) else "")
            return {"ok": True, "pdbqt": pdbqt_path, "pqr": pqr_path,
                    "propka": info.get("propka", ""), "info": info}

    attempts: List[Dict[str, Any]] = []
    last_error = ""
    best_failed = False
    for index, (variant, pdb2pqr_flags, meeko_flags) in enumerate(_PREP_LADDER):
        entry: Dict[str, Any] = {"variant": variant, "ok": False}
        ok, err = _run_pdb2pqr(binary, filtered_path, pqr_path, ph, pdb2pqr_flags)
        if not ok:
            entry["reason"] = err
            attempts.append(entry)
            last_error = err
            continue
        pqr_text = Path(pqr_path).read_text(errors="ignore")
        normalized, split = normalize_pqr_for_meeko(pqr_text)
        Path(meeko_pqr).write_text(normalized, encoding="utf-8")
        ok, meeko_text, err = _run_meeko_pqr(meeko_pqr, f"{out_base}_ph{ph:g}", meeko_flags)
        unmatched = parse_unmatched_residues(meeko_text) or (
            parse_unmatched_residues(err) if not ok else [])
        if unmatched:
            entry["unmatched_residues"] = unmatched
        if not ok:
            entry["reason"] = err
            attempts.append(entry)
            last_error = err
            if index == 0:
                best_failed = True          # 最优档失败要记下来，供缓存判定
            continue
        entry["ok"] = True
        attempts.append(entry)
        os.replace(f"{out_base}_ph{ph:g}_tmp.pdbqt", pdbqt_path)
        propka_text = Path(propka_path).read_text(errors="ignore") if os.path.isfile(propka_path) else ""
        info.update({"applied": True, "cached": False,
                     "tool": "pdb2pqr + PROPKA + meeko",
                     "pdb2pqr_version": tool_version(binary),
                     "pdbqt": pdbqt_path, "pqr": pqr_path,
                     "propka": propka_path if propka_text else "",
                     "variant": variant,
                     "pdb2pqr_noopt": bool(pdb2pqr_flags),
                     "attempts": attempts,
                     "best_variant_failed": best_failed,
                     "dropped_bad_residues": unmatched,
                     "insertion_codes_split": split})
        _attach_states(info, pqr_text, propka_text)
        _write_sidecar(out_base, ph, info, pqr_path)     # 必须在 applied=True 之后写
        return {"ok": True, "pdbqt": pdbqt_path, "pqr": pqr_path,
                "propka": info.get("propka", ""), "info": info}

    info["attempts"] = attempts
    info["best_variant_failed"] = best_failed
    info["reason"] = (f"按 pH 准备受体失败（已依次尝试 {' → '.join(v for v, _, _ in _PREP_LADDER)}）："
                      f"{last_error}")
    return {"ok": False, "error": info["reason"], "info": info}


def sidecar_path(out_base: str, ph: float) -> str:
    """pH 受体的溯源侧车：与 `<out_base>_ph<ph>.pdbqt` 同名同目录。

    为什么单独写一份：`prepare_user_receptor` 的位点侧车挂在**基础名**上
    （`<base>.site.json`），而 pH 产物是 `<base>_ph7.4.pdbqt` —— 下游只拿到这个 PDBQT 时
    按名字找不到位点侧车，也就丢掉了「确实按 pH 7.4 准备过」的溯源（真实踩到过：
    报告把一次**做过** pH 处理的受体误报成「未按 pH 准备」）。
    """
    return f"{out_base}_ph{ph:g}.site.json"


def _write_sidecar(out_base: str, ph: float, info: Dict[str, Any], pqr_path: str) -> None:
    try:
        path = sidecar_path(out_base, ph)
        payload = {
            "kind": "receptor_ph",
            "ph": ph,
            "tool": info.get("tool"),
            "pdb2pqr_version": info.get("pdb2pqr_version"),
            "propka_version": info.get("propka_version"),
            "his_states": info.get("his_states") or {},
            "titratable": info.get("titratable") or {},
            "dropped_incomplete_residues": info.get("dropped_incomplete_residues") or [],
            "pqr": pqr_path,
            "propka": info.get("propka") or "",
            "receptor_protonation": {**{k: v for k, v in info.items() if k != "pka_residues"}},
        }
        Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - 溯源写失败不影响准备本身
        logger.warning("受体 pH 溯源侧车写入失败（%s）：%s", out_base, e)


def read_sidecar(out_base: str, ph: float) -> Dict[str, Any]:
    """读取 pH 受体溯源侧车（不存在/损坏时返回空 dict）。"""
    try:
        return json.loads(Path(sidecar_path(out_base, ph)).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _attach_states(info: Dict[str, Any], pqr_text: str, propka_text: str) -> None:
    """把「实际应用了什么」写进 info：HIS 状态 + 各可滴定残基的质子化计数 + 边界残基。"""
    try:
        info["his_states"] = his_states_from_pqr(pqr_text)
    except Exception as e:  # noqa: BLE001
        logger.debug("解析 HIS 状态失败：%s", e)
    if not propka_text:
        return
    head = propka_text.splitlines()[0].strip() if propka_text.splitlines() else ""
    if head:
        info.setdefault("propka_version", head[:80])
    try:
        rows = parse_propka_summary(propka_text)
        info["titratable"] = titratable_states(rows, float(info.get("ph") or 7.4))
        info["pka_residues"] = len(rows)
    except Exception as e:  # noqa: BLE001
        logger.debug("解析 PROPKA 摘要失败：%s", e)


def _python() -> str:
    import sys

    return sys.executable


def _mk_prepare_receptor() -> str:
    from docking_agent.core.receptors import _find_mk_prepare_receptor  # 局部导入避免循环

    return _find_mk_prepare_receptor()


def describe(info: Dict[str, Any]) -> str:
    """一句话说明受体质子化处理结果（用于运行笔记与报告）。"""
    if not info:
        return ""
    ph = info.get("ph")
    ph_txt = f"{ph:g}" if isinstance(ph, (int, float)) else "—"
    if not info.get("applied"):
        return (f"受体未按目标 pH 准备（{info.get('reason') or '未知原因'}）；"
                "其质子化态为 meeko 残基模板默认态（≈pH 7 标准态），与配体目标 pH 可能不一致")
    his = info.get("his_states") or {}
    his_txt = "、".join(f"{k}×{v}" for k, v in his.items() if k != "未判定" and v) or "—"
    by_res = (info.get("titratable") or {}).get("by_residue") or {}
    prot_txt = "、".join(f"{k} {v.get('protonated', 0)}/{v.get('total', 0)}"
                        for k, v in sorted(by_res.items()) if v.get("protonated"))
    tool = info.get("tool") or "pdb2pqr + PROPKA"
    propka_ver = str(info.get("propka_version") or "").split(",")[0].strip()
    tool_txt = f"{tool}（{propka_ver}）" if propka_ver else tool
    variant = str(info.get("variant") or "")
    variant_txt = {"opt": "氢键网络优化", "noopt": "几何摆氢（该结构下氢键优化会让 meeko 误判共价键）",
                   "delete": "氢键优化 + 丢弃模板不匹配残基",
                   "noopt+delete": "几何摆氢 + 丢弃模板不匹配残基"}.get(variant, "")
    parts = [f"受体按目标 pH {ph_txt} 准备（{tool_txt}）",
             "与配体同一 pH", f"HIS 状态 {his_txt}"]
    if variant_txt:
        parts.append(f"氢处理：{variant_txt}")
    dropped_bad = info.get("dropped_bad_residues") or []
    if dropped_bad:
        parts.append("**因模板不匹配被 meeko 丢弃的残基**：" + "、".join(dropped_bad[:8])
                     + ("…" if len(dropped_bad) > 8 else "")
                     + "（请确认它们不参与结合；必要时修结构后重跑）")
    if prot_txt:
        parts.append(f"在 pH {ph_txt} 下仍质子化的可滴定残基：{prot_txt}")
    boundary = (info.get("titratable") or {}).get("boundary") or []
    if boundary:
        parts.append("边界残基（pKa 距目标 pH <%.1f，状态不确定）：" % BOUNDARY_DELTA
                     + "、".join(f"{b['resname']}{b.get('resnum')}(pKa {b['pka']:g})" for b in boundary[:5]))
    dropped = info.get("dropped_incomplete_residues") or []
    if dropped:
        parts.append("剔除主链不全的残基 "
                     + "、".join(f"{d['resname']}{d['resnum']}{d['chain']}" for d in dropped[:5]))
    if info.get("cached"):
        parts.append("（复用已准备的受体）")
    return "；".join(parts)
