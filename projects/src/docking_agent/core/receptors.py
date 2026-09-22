"""蛋白质受体：注册表（已知结合位点）、用户受体现场准备与受体解析。

受体与「已知结合位点」由 `config/receptors.json` 声明，新增受体无需改代码：
```json
{"receptors": {"myprotein": {"pdbqt": "assets/receptors/registry/x.pdbqt",
                             "site": {"center": [..], "size": [..]}}}}
```
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from docking_agent.core.files import _fetch_file
from docking_agent.paths import cache_dir, project_root, workspace_dir

logger = logging.getLogger(__name__)

REGISTRY_CONFIG_REL = "config/receptors.json"
DEFAULT_BOX_SIZE: List[float] = [22.0, 22.0, 22.0]


# --------------------------------------------------------------------------- #
# 受体文件扩展名（**只在这里定义一份**：上传端点与受体解析链共用，避免两处漂移）
# --------------------------------------------------------------------------- #
# 需要现场准备为 PDBQT 的「结构文件」。`.ent` 是 PDB 的另一种常见后缀
# （RCSB 下载的坐标文件就叫 *.ent）。历史上只有上传端点认它、解析链不认，
# 于是 `.ent` 落到「未识别受体 → 回退默认 thrombin」（真实缺陷）。
RECEPTOR_STRUCTURE_EXTS = frozenset({".pdb", ".ent", ".pdb1", ".cif", ".mmcif"})
# 已经是受体成品的扩展名：无需现场准备，直接包装为受体 spec
RECEPTOR_PDBQT_EXTS = frozenset({".pdbqt"})
# 上传端点接受 / 解析链认同的受体扩展名全集
RECEPTOR_EXTS = RECEPTOR_STRUCTURE_EXTS | RECEPTOR_PDBQT_EXTS
# 已知「不是受体结构」的扩展名：即使文件存在也不去猜它是蛋白（猜错会把小分子库当受体）
RECEPTOR_NON_STRUCTURE_EXTS = frozenset({
    ".sdf", ".sd", ".smi", ".smiles", ".mol", ".mol2", ".csv", ".tsv", ".txt",
    ".json", ".xml", ".html", ".htm", ".yaml", ".yml", ".toml", ".ini", ".log", ".env",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".docx", ".xlsx",
    ".zip", ".gz", ".tar", ".tgz", ".py", ".js", ".sh",
})

# mmCIF（含 .cif/.mmcif）不是 PDB 文本格式，meeko 的 `--read_pdb` 读不了；
# gemmi 随 meeko 一起装（无需新增依赖），因此这里先转成 PDB 再做标准准备流程。
_CIF_EXTS = frozenset({".cif", ".mmcif"})


def receptor_ext(path: str) -> str:
    """取小写扩展名（'.ent' / '.pdbqt' …）；大小写不敏感。"""
    return os.path.splitext(str(path).strip())[1].lower()


def is_structure_source(source: str) -> bool:
    """该来源是否应按「用户提供的结构文件」走 `prepare_user_receptor`。

    判定顺序（宁可尝试后在准备阶段如实报错，也不要静默回退默认受体）：
      1. http(s) URL → 是（下载后按内容准备）；
      2. 扩展名在 `RECEPTOR_STRUCTURE_EXTS`（.pdb/.ent/.pdb1/.cif/.mmcif）→ 是；
      3. 扩展名是 `.pdbqt` 或落在已知「非结构」名单里 → 否；
      4. 文件确实存在且后缀不在名单里 → 也尝试（结构文件后缀五花八门）。
    """
    text = str(source or "").strip()
    if text.startswith(("http://", "https://")):
        return True
    ext = receptor_ext(text)
    if ext in RECEPTOR_STRUCTURE_EXTS:
        return True
    if ext in RECEPTOR_PDBQT_EXTS or ext in RECEPTOR_NON_STRUCTURE_EXTS:
        return False
    # 后缀未知：只有文件确实存在才尝试（相对路径按 cwd 与工作区根各查一次，
    # 与 core.files._fetch_file 的查找口径一致）。
    if os.path.isfile(os.path.expanduser(text)):
        return True
    return Path(workspace_dir() / text).is_file()


def convert_mmcif_to_pdb(source: str) -> str:
    """把 mmCIF 结构转换为同目录下的 PDB 文本，返回新路径；失败时抛异常（由上层如实报错）。

    为什么不静默跳过：`.cif/.mmcif` 与 `.ent` 一样是用户明确给出的结构文件，
    必须要么真的用上它，要么在 note 里说清为什么用不上。
    """
    import gemmi  # 随 meeko 安装，非新增依赖

    structure = gemmi.read_structure(str(source))
    if not structure:
        raise RuntimeError("mmCIF 解析结果为空，无法转换为 PDB")
    out = os.path.splitext(str(source))[0] + "_from_cif.pdb"
    structure.setup_entities()
    structure.write_pdb(out)
    return out
def _resolve_pdbqt_path(value: str) -> str:
    """把注册表中的受体路径解析为绝对路径（相对路径基于项目根目录）。"""
    p = Path(value)
    return str(p if p.is_absolute() else (project_root() / p).resolve())


def load_registry(path: Optional[str] = None) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str], str]:
    """读取受体注册表，返回 (receptors, aliases, default)。"""
    cfg_path = Path(path) if path else (project_root() / REGISTRY_CONFIG_REL)
    if not cfg_path.exists():
        logger.warning("未找到受体注册表 %s，注册表为空", cfg_path)
        return {}, {}, ""

    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    receptors: Dict[str, Dict[str, Any]] = {}
    for key, item in (raw.get("receptors") or {}).items():
        spec = dict(item)
        spec.setdefault("key", key)
        spec.setdefault("name", key)
        pdbqt = _resolve_pdbqt_path(spec.get("pdbqt") or "")
        site = dict(spec.get("site") or {})
        site.setdefault("center", [0.0, 0.0, 0.0])
        site.setdefault("size", list(DEFAULT_BOX_SIZE))
        spec["pdbqt"] = pdbqt
        # 兼容既有字段：center/size 直接挂到 spec 上，供对接直接使用
        spec["center"] = [float(x) for x in site["center"]]
        spec["size"] = [float(x) for x in site["size"]]
        spec["site"] = site
        spec["available"] = os.path.isfile(pdbqt)
        receptors[key] = spec

    aliases = {str(k).lower(): str(v) for k, v in (raw.get("aliases") or {}).items()}
    default = str(raw.get("default") or (next(iter(receptors), "")))
    return receptors, aliases, default


RECEPTOR_REGISTRY, RECEPTOR_ALIASES, DEFAULT_RECEPTOR = load_registry()



def list_receptors() -> List[Dict[str, Any]]:
    """供 API/UI 使用：受体清单（含已知位点信息）。"""
    out = []
    for key, spec in RECEPTOR_REGISTRY.items():
        out.append({
            "key": key,
            "name": spec.get("name", key),
            "pdb": spec.get("pdb", ""),
            "protein": spec.get("protein", ""),
            "pdbqt": os.path.relpath(spec["pdbqt"], project_root()) if spec.get("pdbqt") else "",
            "available": bool(spec.get("available")),
            "site": spec.get("site", {}),
        })
    return out


# --------------------------------------------------------------------------- #
# 受体解析
# --------------------------------------------------------------------------- #


def _parse_unmatched_residues(text: str) -> List[str]:
    """从 meeko 输出里解析「没有化学模板被跳过的残基名」。

    meeko 的报错形如：
      RuntimeError: unable to build rdkit mol for residue HEM corresponding to key A:102
      ... residues that don't match templates: HEM, NAD
    两种都要覆盖；解析不出来就返回空列表（调用方会退化成"部分残基"的措辞）。
    """
    import re as _re

    found: List[str] = []
    for pat in (r"residue\s+([A-Za-z0-9]{1,4})\s+corresponding",
                r"residues?\s+([A-Za-z0-9]+(?:\s*,\s*[A-Za-z0-9]+)*)\s+(?:do not|don't|does not) match"):
        for m in _re.finditer(pat, text or ""):
            for name in _re.split(r"\s*,\s*", m.group(1)):
                name = name.strip().upper()
                if name and name not in found:
                    found.append(name)
    return found


class ReceptorInputError(ValueError):
    """用户提供的受体**不可用**：文件准备失败，或名称无法识别。

    产品底线：计算对象不可用 / 不明确时**绝不计算**，也绝不改用任何预置受体
    （预置受体只用于内部测试，不是用户可选来源）。

    历史缺陷：这两种情况原先都「静默回退 凝血酶(thrombin) 继续跑完」，只在小字笔记里说明 ——
    用户会拿到一份**以凝血酶为受体**的答非所问报告。现在改为硬错误：由调用方
    （`tools/docking.py` / `tools/pockets.py`）转成 `needs_user_input`，把选择权交回用户。

    `payload` 是给调用方与界面用的结构化信息（`reason` / `options` / `file`）。
    """

    def __init__(self, message: str, *, reason: str = "", source: str = "",
                 payload: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.source = source
        self.payload = dict(payload or {})


def guess_cocrystal_ligand(pdb_path: Optional[str]) -> Optional[Dict[str, Any]]:
    """找共晶小分子配体（最大的一团非水/非添加剂 HETATM）。

    委托给 `core.pockets.cocrystal_ligand`：它会按 (链:残基号:残基名) 分组、
    过滤离子与常见结晶添加剂（SO4/GOL/EDO…）、按原子数取最大团。
    直接对**所有** HETATM 求质心是错的 —— 金属离子、硫酸根、甘油会把中心带偏。
    """
    if not pdb_path or not os.path.exists(pdb_path):
        return None
    try:
        from docking_agent.core.pockets import cocrystal_ligand  # 局部导入避免循环依赖
        return cocrystal_ligand(pdb_path)
    except Exception:  # noqa: BLE001
        logger.debug("共晶配体识别失败", exc_info=True)
        return None



def _protein_centroid(prot_pdb: str) -> List[float]:
    """蛋白残基质心（当无共晶配体时兜底）。"""
    xs, ys, zs = [], [], []
    for line in open(prot_pdb, encoding="utf-8", errors="ignore"):
        if line.startswith("ATOM"):
            try:
                xs.append(float(line[30:38]))
                ys.append(float(line[38:46]))
                zs.append(float(line[46:54]))
            except ValueError:  # 允许静默：个别行坐标列非法，逐行记日志会刷屏
                pass
    if not xs:
        return list(DEFAULT_BOX_SIZE)
    return [sum(xs) / len(xs), sum(ys) / len(ys), sum(zs) / len(zs)]


def _find_mk_prepare_receptor() -> str:
    """定位 meeko 提供的 mk_prepare_receptor.py（venv/bin 与控制台脚本两种安装方式）。"""
    import shutil
    import sys as _sys
    cand = shutil.which("mk_prepare_receptor.py") or shutil.which("mk_prepare_receptor")
    if cand:
        return cand
    near_exe = os.path.join(os.path.dirname(_sys.executable), "mk_prepare_receptor.py")
    if os.path.exists(near_exe):
        return near_exe
    try:  # 退化：从 meeko 包目录里找
        import meeko  # type: ignore
        pkg = os.path.dirname(meeko.__file__)
        for root, _dirs, files in os.walk(pkg):
            if "mk_prepare_receptor.py" in files:
                return os.path.join(root, "mk_prepare_receptor.py")
    except Exception as e:  # noqa: BLE001
        logger.debug("从 meeko 包目录定位 mk_prepare_receptor 失败：%s", e)
    raise RuntimeError(
        "未找到 mk_prepare_receptor.py（用于把 PDB 现场准备为 PDBQT）。"
        "请确认已在本项目 venv 中安装 meeko：.venv/bin/python -m pip install meeko"
    )


# mk_prepare_receptor 的准备尝试顺序：交替构象（altloc）是最常见的失败点，
# 仅 -a 不够，需要显式指定默认 altloc；最后再试不带该参数（干净结构）。
_PREP_ATTEMPTS: List[List[str]] = [["--default_altloc", "A"], ["--default_altloc", "B"],
                                   ["--default_altloc", "C"], []]


def _write_prep_pdb(raw: str, prot: str, keep: set) -> Tuple[int, Dict[str, int], Dict[str, int]]:
    """写「准备用」PDB：只保留 ATOM 记录 + 白名单内的 HETATM。

    返回 (丢弃的水分子数, 丢弃的非水杂原子计数, 写出的非水杂原子计数)。
    统计**必须**返回给上层：金属/辅因子被丢掉这件事不能是无声的。
    """
    waters = 0
    dropped: Dict[str, int] = {}
    kept: Dict[str, int] = {}
    with open(prot, "w", encoding="utf-8") as f:
        f.write("REMARK receptor prepared from user input\n")
        for line in open(raw, encoding="utf-8", errors="ignore"):
            if line.startswith("ATOM"):
                f.write(line)
                continue
            if line.startswith("HETATM"):
                res = line[17:20].strip().upper()
                if res in ("HOH", "WAT", "H2O"):
                    waters += 1
                    continue
                if res in keep:
                    f.write(line)
                    kept[res] = kept.get(res, 0) + 1
                else:
                    dropped[res] = dropped.get(res, 0) + 1
    return waters, dropped, kept


def _run_mk_prepare(prot: str, out_base: str) -> Tuple[bool, str, List[str]]:
    """跑 mk_prepare_receptor（按 _PREP_ATTEMPTS 依次尝试）。

    返回 `(是否成功, 错误摘要, meeko 丢弃的残基键)`。

    `-a/--allow_bad_res` 会让 meeko **静默丢**掉模板不匹配的残基（8ZE2 这类结构里
    残基间距离异常时很常见）；丢弃的残基必须如实上报，否则用户不会知道受体少了一段。
    """
    import subprocess
    import sys as _sys

    prep_script = _find_mk_prepare_receptor()
    last_err = ""
    for extra in _PREP_ATTEMPTS:
        try:
            proc = subprocess.run(
                [_sys.executable, prep_script, "--read_pdb", prot, "-a", "-o",
                 out_base, "-p", *extra],
                capture_output=True, timeout=900)
        except subprocess.TimeoutExpired:
            last_err = "准备超时（>900s）"
            continue
        if proc.returncode == 0 and os.path.exists(out_base + ".pdbqt"):
            if extra:
                logger.info("受体准备使用 %s 处理交替构象", " ".join(extra))
            text = ((proc.stdout or b"") + (proc.stderr or b"")).decode(errors="replace")
            from docking_agent.core.receptor_ph import parse_unmatched_residues

            unmatched = parse_unmatched_residues(text)
            if unmatched:
                logger.warning("受体准备丢弃了模板不匹配的残基：%s", "、".join(unmatched))
            return True, "", unmatched
        last_err = ((proc.stderr or b"") + (proc.stdout or b"")).decode(errors="replace").strip()
        if os.path.exists(out_base + ".pdbqt"):   # 清掉半成品，避免下次误判为已准备
            try:
                os.remove(out_base + ".pdbqt")
            except OSError as e:
                logger.debug("清理未完成的受体 PDBQT 失败：%s", e)
    return False, last_err, []


def prepare_user_receptor(source: str,
                          center: Optional[Sequence[float]] = None,
                          box_size: Optional[Sequence[float]] = None,
                          keep_hetatm: Sequence[str] = (),
                          source_ext: str = "",
                          protonation: Optional[str] = None,
                          ph: Any = None) -> Dict[str, Any]:
    """把用户提供的受体结构文件（.pdb/.ent/.pdb1/.cif/.mmcif，本地路径或 URL）
    现场准备为 PDBQT，并推算活性位点盒。

    盒中心优先取用户传入 center；否则取共晶配体(HETATM)质心；再无则取蛋白质心。

    `protonation` / `ph`：受体质子化策略与目标 pH，**默认与配体同一口径**
    （策略缺省 ph、pH 缺省 7.4）。策略为 ph 时走 `core/receptor_ph`（pdb2pqr + PROPKA）按 pH
    分配 HIS 互变异构与 ASP/GLU/LYS/CYS/TYR 的质子化态；失败或需要保留有机辅因子时
    **回退**标准流程，并在 `receptor_protonation` 里写明原因（绝不假装做过）。

    关于「标准流程去水去杂原子」：金属离子、血红素、NAD/FAD 等辅因子对结合可能至关重要，
    因此本函数**不替用户做假设** —— 默认剔除，但把剔除的残基**逐条计数**返回
    （`dropped_hetatm` / `dropped_waters`），并提供 `keep_hetatm` 白名单让上层明确要求保留。
    保留时若某残基缺少 meeko 化学模板（HEM/NAD 等大辅因子常见），会**逐个剔除**直到准备成功，
    并把剔除名单放进 `unsupported_hetatm` —— 既不静默丢弃，也不让整个对接直接失败。
    """
    import hashlib as _hashlib

    cache_dir_path = str(cache_dir())
    raw = source
    if source.startswith(("http://", "https://")):
        raw = os.path.join(cache_dir_path, "user_receptor.pdb")
        import urllib.request as _ur
        _ur.urlretrieve(source, raw)
    if not os.path.exists(raw):
        raise FileNotFoundError(f"受体文件不存在: {source}")
    base = os.path.splitext(os.path.basename(raw))[0] or "user_receptor"
    ext = (str(source_ext).strip().lower() or receptor_ext(raw))
    if ext in _CIF_EXTS:
        # mmCIF 不是 PDB 文本格式：meeko 的 --read_pdb 会读到空结构。先用 gemmi 转 PDB
        # 再走同一套标准准备流程（gemmi 随 meeko 安装，非新增依赖）。
        converted = convert_mmcif_to_pdb(raw)
        logger.info("受体 %s：mmCIF 已转换为 PDB %s", base, os.path.basename(converted))
        raw = converted
    keep = {str(x).strip().upper() for x in (keep_hetatm or []) if str(x).strip()}

    # 准备产物一律**内容寻址**：文件名里带上「源文件内容 + keep 集」的哈希。
    # 为什么必须这样：缓存目录是全局共享的，而文件名只取源文件 basename ——
    # 两个并发的运行（或两个都叫 receptor.pdb 的上传）会写同一组
    # `_prot.pdb` / `.pdbqt` / `.site.json`，彼此覆盖，结果里出现「我请求保留 ZN，
    # 返回的却是别人的 HEM+ZN 结果」这种串数据（实测可复现，见
    # tests/test_receptor_race.py）。加上内容哈希后，不同输入天然落到不同文件。
    try:
        src_hash = _hashlib.sha1(Path(raw).read_bytes()).hexdigest()[:10]
    except OSError:
        src_hash = "0000000000"
    keep_tag = _hashlib.sha1("|".join(sorted(keep)).encode("utf-8")).hexdigest()[:6]
    stem = f"{base}_{src_hash}{keep_tag}"
    prot = os.path.join(cache_dir_path, stem + "_prot.pdb")
    out_base = os.path.join(cache_dir_path, stem)
    out_pdbqt = out_base + ".pdbqt"
    hash_file = os.path.join(cache_dir_path, stem + "_prot.sha1")

    waters, dropped, kept = _write_prep_pdb(raw, prot, keep)
    prot_hash = _hashlib.sha1(open(prot, "rb").read()).hexdigest()

    # ---- 受体质子化：与配体同一目标 pH（策略 ph 时用 pdb2pqr + PROPKA 重新分配）----
    from docking_agent.core import receptor_ph
    from docking_agent.core.protonation import protonation_ph, protonation_policy

    prot_policy = protonation_policy(protonation)
    prot_ph = protonation_ph(ph)
    ph_result: Dict[str, Any] = {}
    if prot_policy == "ph":
        try:
            ph_result = receptor_ph.prepare_pdbqt_at_ph(prot, out_base, prot_ph,
                                                        cache_dir=cache_dir_path)
        except Exception as e:  # noqa: BLE001 - pH 准备失败必须回退，不能拖垮对接
            logger.warning("受体 %s：按 pH 准备失败，回退标准流程：%s", base, e)
            ph_result = {"ok": False, "info": {"applied": False, "policy": "ph", "ph": prot_ph,
                                               "reason": f"pH 准备异常（{type(e).__name__}: {e}）"}}
        if ph_result.get("ok"):
            out_pdbqt = ph_result["pdbqt"]
            logger.info("受体 %s：已按目标 pH %s 准备（%s）", base, prot_ph,
                        receptor_ph.describe(ph_result.get("info") or {}))
        else:
            logger.warning("受体 %s：按 pH 准备未成功（%s），回退标准准备流程",
                           base, (ph_result.get("info") or {}).get("reason"))
    receptor_protonation: Dict[str, Any] = dict(ph_result.get("info") or {})
    if not receptor_protonation:
        receptor_protonation = {
            "applied": False, "policy": prot_policy, "ph": prot_ph,
            "reason": ("策略为 " + prot_policy + "，未按目标 pH 处理受体"
                       if prot_policy != "ph" else "未知原因")}

    # 缓存判定必须看「准备后 PDB 的内容」而不是时间戳：prot 每次都会重写，
    # 时间戳比较会让缓存永远失效（每次都重跑 meeko）；而 keep_hetatm 变化时
    # 内容哈希变化又必须重新准备。用哈希 sidecar 同时满足两点。
    cached_hash = ""
    try:
        cached_hash = open(hash_file, encoding="utf-8").read().strip()
    except OSError:  # 允许静默：首次准备时还没有哈希 sidecar，属预期情况
        pass

    unsupported: List[str] = []
    if ph_result.get("ok"):
        # 已由 pH 路径产出 PDBQT：跳过标准 meeko 重准备（否则会把 pH 状态覆盖回模板默认态）
        logger.info("受体 %s：使用按 pH 准备的 PDBQT %s", base, os.path.basename(out_pdbqt))
    elif os.path.exists(out_pdbqt) and cached_hash == prot_hash:
        logger.info("复用已准备的受体 PDBQT：%s", out_pdbqt)
    else:
        ok, err, dropped_bad = _run_mk_prepare(prot, out_base)
        if dropped_bad:
            receptor_protonation["dropped_bad_residues"] = dropped_bad
        if not ok and keep:
            # meeko 需要每个残基都有化学模板。大辅因子（HEM/NAD/FAD）与部分离子（K/CU…）
            # 要么没模板、要么会触发 meeko 内部错误。逐个剔除，找到**最大可用子集**：
            # 能留的留下，留不下的记进 unsupported 交回 Agent 判断。
            current = sorted(keep)
            while not ok and current:
                for cand in list(current):
                    trial = [x for x in current if x != cand]
                    waters, dropped, kept = _write_prep_pdb(raw, prot, set(trial))
                    ok, err2, dropped_bad2 = _run_mk_prepare(prot, out_base)
                    if dropped_bad2:
                        receptor_protonation["dropped_bad_residues"] = dropped_bad2
                    if ok:
                        unsupported.append(cand)
                        current = trial
                        logger.warning("受体 %s：杂原子 %s 缺少对接所需化学模板，"
                                       "已从保留清单剔除（其余照常对接）", base, cand)
                        break
                else:
                    break
        if not ok:
            parsed = _parse_unmatched_residues(err)
            raise RuntimeError(
                f"受体 PDBQT 准备失败：{err[-600:] or '未知错误'}"
                + (f"（meeko 指出的问题残基：{'、'.join(parsed)}）" if parsed else "")
                + ("；已尝试交替构象 A/B/C、干净结构，以及逐个剔除无法参数化的杂原子"
                   if keep else "；已尝试交替构象 A/B/C 与干净结构")
                + (f"。要保留的杂原子为 {sorted(keep)}，若问题出在它们身上，可择一处理："
                   "① 去掉 keep_hetatm 按标准流程重跑（会剔除水与杂原子）；"
                   "② 提供该残基的化学模板（meeko --add_templates 的 SDF）；"
                   "③ 直接提供已准备好的受体 PDBQT 文件。" if keep else "。"))
        # 侧车哈希必须对应**最终**的 prot（可能已剔除若干残基），否则下次会误判缓存
        prot_hash = _hashlib.sha1(open(prot, "rb").read()).hexdigest()

    if not os.path.exists(out_pdbqt):
        raise RuntimeError("受体 PDBQT 准备失败：未生成输出文件")
    try:
        with open(hash_file, "w", encoding="utf-8") as f:
            f.write(prot_hash)
    except OSError as e:
        logger.warning("受体准备缓存标记写入失败：%s", e)

    # 以**最终 PDBQT** 为准统计保留结果（缓存命中时同样正确）：
    # 只有真的进了受体的杂原子才算 kept；因缺模板被跳过的记为 unmatched，
    # 这样 `kept_hetatm` 不会出现「说保留了、其实没进对接」的假信息。
    final_kept: Dict[str, int] = {}
    try:
        for line in open(out_pdbqt, encoding="utf-8", errors="ignore"):
            if line.startswith(("ATOM", "HETATM")):
                res = line[17:20].strip().upper()
                if res in keep:
                    final_kept[res] = final_kept.get(res, 0) + 1
    except OSError:
        final_kept = dict(kept)
    unmatched: Dict[str, int] = {r: n for r, n in kept.items() if r not in final_kept}
    kept = final_kept
    try:
        with open(hash_file, "w", encoding="utf-8") as f:
            f.write(prot_hash)
    except OSError as e:
        logger.warning("受体准备缓存标记写入失败：%s", e)
    lig_info = guess_cocrystal_ligand(raw)
    guessed = list(lig_info["center"]) if lig_info else None
    if lig_info:
        logger.info("受体 %s 共晶配体：%s（%d 原子）", base,
                    lig_info.get("resname"), lig_info.get("n_atoms") or 0)
    center = list(center) if center is not None else (guessed or _protein_centroid(prot))
    box_size = list(box_size) if box_size is not None else list(DEFAULT_BOX_SIZE)
    # 位点 sidecar：下游可能只拿到 .pdbqt（例如上传后按文件路径对接），
    # 没有它就只能退化成「全蛋白质心」，会把盒子放到错误的位点。
    try:
        with open(os.path.join(cache_dir_path, stem + ".site.json"), "w", encoding="utf-8") as f:
            json.dump({"center": [float(x) for x in center],
                       "size": [float(x) for x in box_size],
                       "source": (f"共晶配体({lig_info.get('resname')})质心"
                                  if lig_info else "蛋白质质心"),
                       "origin": os.path.basename(raw),
                       "origin_path": os.path.abspath(raw),
                       # 化学溯源也要落到 sidecar：下游常常只拿到 .pdbqt，
                       # 没有这些字段「丢了哪些金属/辅因子」就会被静默遗忘。
                       "dropped_hetatm": dropped, "kept_hetatm": kept,
                       "dropped_waters": waters,
                       "unsupported_hetatm": sorted(set(unsupported) | set(unmatched)),
                       # 共晶配体存**完整**信息（resname + chain:resid:resname 的 key + 原子数）：
                       # 只存残基名时，下游拿 .pdbqt 就再也解不出 SMILES ——「是否把受体自带配体
                       # 当阳性对照」的询问因此永远不会触发（真实缺陷，用户实测反馈）。
                       "cocrystal_ligand": lig_info or {},
                       # 原始结构路径：.pdbqt 是**去配体**的，只有回到这里才能取回配体原子
                       "source_pdb": os.path.abspath(raw),
                       "receptor_protonation": receptor_protonation},
                      f, ensure_ascii=False)
    except OSError as e:
        logger.warning("位点 sidecar 写入失败：%s", e)
    site_source = (f"共晶配体({lig_info.get('resname')})质心" if lig_info else
                   "蛋白质质心（未找到共晶配体，建议由口袋预测工具确定位点）")
    return {"key": base, "name": base, "pdb": base, "protein": f"用户自定义受体 ({base})",
            "pdbqt": out_pdbqt, "center": center, "size": box_size, "user_provided": True,
            "source_pdb": os.path.abspath(raw),
            "dropped_hetatm": dropped, "kept_hetatm": kept, "dropped_waters": waters,
            "cocrystal_ligand": lig_info or {},
            "unmatched_residues": sorted(unmatched),
            "unmatched_hetatm": unmatched,
            "unsupported_hetatm": sorted(set(unsupported) | set(unmatched)),
            "receptor_protonation": receptor_protonation,
            "site": {"center": list(center), "size": list(box_size), "source": site_source}}


def resolve_receptor_specs(receptor_arg: Any = None,
                          keep_hetatm: Sequence[str] = (),
                          protonation: Optional[str] = None,
                          ph: Any = None
                          ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """把对接输入中的受体指定解析成受体清单（蛋白质库），并收集提示信息。

    receptor_arg 支持：
      - None / "" / "default" ·············· 默认受体(thrombin)+提示
      - 注册表 key 或别名（thrombin/1DWC/trypsin/1PTU/...）
      - PDB/PDBQT 文件路径 或 URL（用户自定义受体，现场准备）
      - 以上若干项组成的列表（蛋白质库：多受体 × 小分子库）
    """
    notes: List[str] = []
    specs: List[Dict[str, Any]] = []
    from docking_agent.core import receptor_ph
    from docking_agent.core.protonation import protonation_ph as _ph_value
    from docking_agent.core.protonation import protonation_policy as _policy

    prot_policy = _policy(protonation)
    prot_ph = _ph_value(ph)

    def _note_protonation() -> None:
        """把每个受体的质子化处理如实写进 notes（成功与回退都要说清）。"""
        if prot_policy != "ph":
            return
        for spec in specs:
            info = spec.get("receptor_protonation") or {}
            text = receptor_ph.describe(info)
            if text:
                notes.append(f"受体 {spec.get('key')} 质子化：{text}")
            if info and not info.get("applied"):
                notes.append(
                    f"受体 {spec.get('key')} 的质子化态未按目标 pH {prot_ph:g} 准备，"
                    "与配体口径不一致（原因见本受体的质子化记录）；对 His/Asp/Glu 敏感的体系"
                    "宜提供已按目标 pH 备好的受体 PDBQT/PQR，或用 PDB2PQR_BIN 指定可用的 pdb2pqr。")

    def _norm(v: Any) -> List[Any]:
        if v is None:
            return []
        return list(v) if isinstance(v, (list, tuple, set)) else [v]

    def _is_blank(v: Any) -> bool:
        """空值语义：None / 空串 / 纯空白 / 字面量 "default" 都表示「未指定受体」。

        只保留 `None` 会踩坑：受理层「未指定受体」时会传空串，旧实现把 `""` 当成
        「一个无法识别的受体名」，于是 specs 为空、既没有默认受体也没有提示，
        对接直接落空。这里统一按文档承诺的空值回退处理。
        """
        if v is None:
            return True
        if isinstance(v, str):
            text = v.strip().lower()
            return text == "" or text == "default"
        return False

    items = [it for it in _norm(receptor_arg) if not _is_blank(it)]
    if not items:
        # 用户要求：**彻底删除「未指定受体就回退默认受体」**。
        # 计算对象没给定时不能替用户挑一个靶点开跑（旧实现会静默用凝血酶），
        # 这里直接报错；上层（对接工具）会把它转成「请用户指定受体」的提问与可选项。
        raise ValueError(
            "未指定受体：计算对象不明确，不能默认使用任何受体。"
            "请提供 PDB 编号 / UniProt accession、受体名称/基因名，"
            "或上传受体结构文件（.pdb/.cif/.pdbqt）。")

    for it in items:
        if isinstance(it, dict):
            it = it.get("receptor") or it.get("key") or it.get("name") or it.get("pdb")
        if _is_blank(it):
            continue
        raw = str(it).strip()
        key = raw.lower()
        if key in RECEPTOR_ALIASES:
            key = RECEPTOR_ALIASES[key]
        if key in RECEPTOR_REGISTRY:
            spec = dict(RECEPTOR_REGISTRY[key])
            spec["receptor_protonation"] = {
                "applied": False, "policy": prot_policy, "ph": prot_ph,
                "reason": "注册表预置 PDBQT：质子化态由该文件本身决定，未按运行 pH 重新准备"}
            specs.append(spec)
            continue
        ext = receptor_ext(raw)
        if ext in RECEPTOR_PDBQT_EXTS:
            local = _fetch_file(raw, "rec_pdbqt")
            specs.append(_pdbqt_spec(local, keep_hetatm=keep_hetatm,
                                     policy=prot_policy, ph_value=prot_ph))
            notes.append(f"用户上传受体(PDBQT)已就绪：{os.path.basename(local)}")
            continue
        # .pdb / .ent / .pdb1 / .cif / .mmcif 以及「文件存在但后缀未知」的情况，
        # 一律按用户提供的结构文件现场准备（`.ent` 只判断 `.pdb` 曾导致静默回退默认受体）。
        if is_structure_source(raw):
            try:
                local = _fetch_file(raw, "receptor")
                specs.append(prepare_user_receptor(local, keep_hetatm=keep_hetatm,
                                                   source_ext=ext, protonation=prot_policy,
                                                   ph=prot_ph))
            except Exception as e:  # noqa: BLE001
                # **绝不静默改用预置受体**（旧行为：回退 thrombin 跑完并出报告 —— 答非所问）。
                # 原因可能很长（meeko 的完整 stderr），截断到可读长度，避免刷屏。
                detail = " ".join(str(e).split())
                if len(detail) > 300:
                    detail = detail[:300] + "…"
                raise ReceptorInputError(
                    f"上传的受体文件无法用于对接：{os.path.basename(raw)}（{detail}）。"
                    "本次**不执行任何计算**。请用户三选一："
                    "① 换一个结构文件（.pdb/.ent/.cif/.pdbqt，或先自行去水/去杂原子）；"
                    "② 给出该受体的 PDB 编号 / UniProt accession / 基因或蛋白名，由系统在线解析；"
                    "③ 按上面的原因修正文件后重试。",
                    reason="prepare_failed", source=raw,
                    payload={"file": raw, "detail": detail,
                             "options": ["replace_file", "resolve_by_name", "fix_and_retry"]},
                ) from e
            else:
                notes.append(f"用户上传受体({ext.lstrip('.') or '结构文件'})已现场准备："
                             f"{os.path.basename(local)}")
            continue
        # 既不是注册表里的显式名字，也不是可读的结构文件 → 停下来问用户（不替用户挑靶点）
        raise ReceptorInputError(
            f"受体 {it!r} 无法识别：它既不是 PDB 编号 / UniProt accession / 基因或蛋白名，"
            "也不是可读的结构文件（.pdb/.ent/.cif/.pdbqt）。本次**不执行任何计算**。"
            "请用户三选一：① 提供 PDB 编号或 UniProt accession；"
            "② 写出受体的基因名/蛋白名（中英文均可，系统会在线检索）；③ 上传受体结构文件。",
            reason="unrecognized", source=str(it),
            payload={"source": str(it), "options": ["pdb_or_uniprot", "name", "upload_file"]},
        )
    _note_protonation()
    return specs, notes


def box_atom_stats(pdbqt_path: str, center: Sequence[float],
                   size: Sequence[float]) -> Dict[str, Any]:
    """统计「盒子范围内有多少受体原子」，以及最近原子到盒中心的距离（Å）。

    **为什么必须有它**：Vina 在盒子内**没有任何受体原子**时不报错、不告警，而是返回
    **全 0 能量**（`affinity_kcal_mol = 0.0`）。0.0 不是分数，是「什么都没算」。
    真实事故：受体被回退成凝血酶、盒子却来自另一个蛋白（盒中心距受体最近原子 75 Å），
    147 个分子跑了 7.5 分钟得到一堆 0.0，还差点被当成结果写进报告。
    这里让调用方能在**调用引擎之前**识别出这种盒子。

    返回 `{"atoms": 盒内原子数, "nearest_angstrom": 最近距离, "nearest_atom": "残基/原子名",
    "center": [...], "size": [...]}`；读不到坐标时 `atoms` 为 `None`（不据此拒绝，交由引擎报错）。
    """
    try:
        cx, cy, cz = (float(center[0]), float(center[1]), float(center[2]))
        sx, sy, sz = (float(size[0]), float(size[1]), float(size[2]))
    except (TypeError, ValueError, IndexError):
        return {"atoms": None, "nearest_angstrom": None, "nearest_atom": "",
                "center": list(center or []), "size": list(size or [])}
    half = (sx / 2.0, sy / 2.0, sz / 2.0)
    inside = 0
    nearest = None
    nearest_atom = ""
    seen = False
    try:
        with open(pdbqt_path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if not line.startswith(("ATOM", "HETATM")):
                    continue
                try:
                    x, y, z = float(line[30:38]), float(line[38:46]), float(line[46:54])
                except ValueError:  # 允许静默：坏坐标行按无效行跳过（内容问题由引擎报错）
                    continue
                seen = True
                if abs(x - cx) <= half[0] and abs(y - cy) <= half[1] and abs(z - cz) <= half[2]:
                    inside += 1
                d = math.dist((x, y, z), (cx, cy, cz))
                if nearest is None or d < nearest:
                    nearest = d
                    nearest_atom = f"{line[17:20].strip()}{line[22:27].strip()}"
    except OSError as e:
        logger.debug("读取受体坐标失败（%s）：%s", pdbqt_path, e)
        return {"atoms": None, "nearest_angstrom": None, "nearest_atom": "",
                "center": [cx, cy, cz], "size": [sx, sy, sz]}
    if not seen:
        return {"atoms": None, "nearest_angstrom": None, "nearest_atom": "",
                "center": [cx, cy, cz], "size": [sx, sy, sz]}
    return {"atoms": inside, "nearest_angstrom": round(float(nearest), 2) if nearest is not None else None,
            "nearest_atom": nearest_atom, "center": [cx, cy, cz], "size": [sx, sy, sz]}


def _pdbqt_centroid(pdbqt_path: str) -> List[float]:
    xs, ys, zs = [], [], []
    try:
        for line in open(pdbqt_path, encoding="utf-8", errors="ignore"):
            if line.startswith(("ATOM", "HETATM")):
                try:
                    xs.append(float(line[30:38])); ys.append(float(line[38:46])); zs.append(float(line[46:54]))
                except ValueError:  # 允许静默：同上
                    pass
    except OSError as e:  # 允许静默：文件读不到时由调用方按“无坐标”兜底
        logger.debug("读取受体坐标失败：%s", e)
    if not xs:
        return list(RECEPTOR_REGISTRY[DEFAULT_RECEPTOR]["center"])
    return [sum(xs) / len(xs), sum(ys) / len(ys), sum(zs) / len(zs)]


_REGISTRY_HASHES: Optional[Dict[str, str]] = None


def _file_sha1(path: str) -> str:
    import hashlib

    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def registry_site_for(local: str) -> Optional[Dict[str, Any]]:
    """如果该 PDBQT 与注册表受体是同一个文件（路径相同或**内容哈希相同**），返回其已知位点。

    内容哈希匹配用于覆盖「用户把预置受体复制/上传了一份」的常见场景：
    路径不同但内容一致，理应沿用同一活性位点，而不是退化成全蛋白质心。
    """
    global _REGISTRY_HASHES
    try:
        target = os.path.realpath(local)
        by_path = {}
        for key, spec in RECEPTOR_REGISTRY.items():
            pdbqt = spec.get("pdbqt")
            if not pdbqt or not os.path.isfile(pdbqt):
                continue
            by_path[os.path.realpath(pdbqt)] = (key, spec)
        if target in by_path:
            key, spec = by_path[target]
            return {"center": list(spec.get("center") or []),
                    "size": list(spec.get("size") or []),
                    "source": f"注册表已知位点（{spec.get('pdb', key)}）"}

        if not os.path.isfile(local):
            return None
        if _REGISTRY_HASHES is None:
            _REGISTRY_HASHES = {}
            for key, spec in RECEPTOR_REGISTRY.items():
                pdbqt = spec.get("pdbqt")
                if pdbqt and os.path.isfile(pdbqt):
                    try:
                        _REGISTRY_HASHES[_file_sha1(pdbqt)] = key
                    except OSError:
                        continue
        key = _REGISTRY_HASHES.get(_file_sha1(local))
        if key:
            spec = RECEPTOR_REGISTRY[key]
            return {"center": list(spec.get("center") or []),
                    "size": list(spec.get("size") or []),
                    "source": f"注册表已知位点（{spec.get('pdb', key)}，内容一致）"}
    except (OSError, KeyError):  # 允许静默：注册表里没有该受体 = 正常分支
        pass
    return None


def _pretty_name(name: str) -> str:
    """去掉上传时加的时间戳前缀，得到便于展示的受体名。"""

    return re.sub(r"^\d{8}-\d{6}-[0-9a-f]{6}-", "", name) or name


def _pdbqt_spec(local: str, keep_hetatm: Sequence[str] = (),
                policy: str = "", ph_value: Any = None) -> Dict[str, Any]:
    """把 .pdbqt 文件包装为受体 spec。

    位点盒优先读同目录下的 `<base>.site.json`（由 `prepare_user_receptor` 写出，
    记录的是共晶配体质心/蛋白质心），否则退化为受体原子质心。
    """
    stem = os.path.splitext(os.path.basename(local))[0]
    # 内容寻址文件名形如 `<base>_<src10><keep6>`：对外展示/注册表用去掉后缀的 base
    base = re.sub(r"_[0-9a-f]{16}$", "", stem) or stem
    center, size, source = None, None, ""
    chem: Dict[str, Any] = {}
    origin_path = ""
    # pH 产物形如 `<base>_<src10><keep6>_ph7.4`：位点侧车挂在**基础名**上，
    # 受体质子化溯源挂在**带 pH 后缀**的同名侧车上 —— 两份都要看。
    ph_match = re.match(r"^(?P<base>.+)_ph(?P<ph>\d+(?:\.\d+)?)$", stem)
    sidecar = os.path.join(os.path.dirname(local), (ph_match.group("base") if ph_match else stem)
                           + ".site.json")
    ph_sidecar = (os.path.join(os.path.dirname(local), stem + ".site.json")
                  if ph_match else "")
    # 只有 .pdbqt 而没有原始 PDB 时，keep_hetatm 是无从谈起的（杂原子在上传准备时就已剔除）。
    # 上传准备会把自己的来源 PDB 写进 sidecar，因此这里可以**回到原始 PDB 重新准备**，
    # 让 Agent 在「用户上传的是已准备好的 PDBQT」这种常见情形下依然能保留金属/辅因子。
    if keep_hetatm:
        try:
            with open(sidecar, "r", encoding="utf-8") as f:
                _info = json.load(f)
            origin = str(_info.get("origin_path") or "")
        except (OSError, ValueError, TypeError):
            origin = ""
        if origin and os.path.exists(origin):
            logger.info("受体 %s 要求保留 %s → 回到原始结构重新准备", base, list(keep_hetatm))
            return prepare_user_receptor(origin, center=center, box_size=size,
                                         keep_hetatm=keep_hetatm)
    if os.path.exists(sidecar):
        try:
            with open(sidecar, "r", encoding="utf-8") as f:
                info = json.load(f)
            center = [float(x) for x in (info.get("center") or [])] or None
            size = [float(x) for x in (info.get("size") or [])] or None
            source = str(info.get("source") or "")
            origin_path = str(info.get("origin_path") or info.get("source_pdb") or "")
            raw_lig = info.get("cocrystal_ligand")
            if isinstance(raw_lig, dict) and raw_lig.get("resname"):
                cocrystal = dict(raw_lig)
            elif raw_lig:
                # 旧 sidecar 只存了残基名 → 回到原始结构把 key/原子数补回来
                cocrystal = {"resname": str(raw_lig)}
            else:
                cocrystal = {}
            # 没有 key 就解不出 SMILES（`cocrystal_ligand_smiles` 按 chain:resid:resname 取原子）
            if cocrystal and not cocrystal.get("key") and origin_path and os.path.isfile(origin_path):
                recovered = guess_cocrystal_ligand(origin_path) or {}
                if recovered.get("key"):
                    cocrystal = recovered
            chem = {"dropped_hetatm": info.get("dropped_hetatm") or {},
                    "kept_hetatm": info.get("kept_hetatm") or {},
                    "dropped_waters": info.get("dropped_waters") or 0,
                    "unsupported_hetatm": info.get("unsupported_hetatm") or [],
                    "cocrystal_ligand": cocrystal,
                    # 受体质子化溯源也在 sidecar 里：这份 PDBQT 若是按目标 pH 准备的，
                    # 必须把「确实按 pH 7.4 做过」带出来 —— 否则下游只看到 .pdbqt，
                    # 会把一次**做过 pH 处理**的准备误报成「未按 pH 准备」（真实踩到过）。
                    "receptor_protonation": info.get("receptor_protonation") or {}}
        except (OSError, ValueError, TypeError) as e:
            logger.warning("位点 sidecar 解析失败 %s：%s", sidecar, e)
    if ph_sidecar and os.path.exists(ph_sidecar):
        try:
            with open(ph_sidecar, "r", encoding="utf-8") as f:
                ph_info = json.load(f)
            recovered = ph_info.get("receptor_protonation") or {}
            if recovered:
                chem["receptor_protonation"] = recovered
        except (OSError, ValueError, TypeError) as e:
            logger.warning("受体 pH 溯源侧车解析失败 %s：%s", ph_sidecar, e)
    if center is None:
        known = registry_site_for(local)
        if known and known.get("center"):
            center = [float(x) for x in known["center"]]
            size = [float(x) for x in (known.get("size") or [])] or size
            source = str(known.get("source") or "")
        else:
            center = _pdbqt_centroid(local)
            source = "受体原子质心（未找到位点信息，建议显式指定位点）"
    label = _pretty_name(base)
    return {"key": base, "name": label, "pdb": "",
            "file": os.path.basename(local),
            # 原始结构（准备这份 PDBQT 的 .pdb）：去配体的 PDBQT 里取不回共晶配体原子
            "source_pdb": origin_path,
            "protein": f"用户上传受体 (PDBQT: {label})",
            "pdbqt": local, "center": center,
            "size": size or list(DEFAULT_BOX_SIZE),
            "site": {"center": center, "size": size or list(DEFAULT_BOX_SIZE), "source": source},
            **chem,
            "receptor_protonation": chem.get("receptor_protonation") or {
                "applied": False, "policy": policy, "ph": ph_value,
                "reason": ("已准备好的 PDBQT（无准备侧车记录）：质子化态由该文件本身决定，"
                           "无法确认是否与目标 pH 一致")},
            "user_provided": True}


def read_receptor_file(source: str, keep_hetatm: Sequence[str] = (),
                       protonation: Optional[str] = None,
                       ph: Any = None) -> Dict[str, Any]:
    """读取用户上传的蛋白质受体文件（结构文件/PDBQT 或 URL），返回受体 spec。

    这是给「协调/对接子 Agent」与上传端点处理用户提供蛋白质文件的标准入口：
      - .pdb / .ent / .pdb1 / .cif / .mmcif（含未知结构后缀）
                              -> 提取蛋白并现场准备为 PDBQT（活性位点盒自动从共晶配体推断）
      - .pdbqt              -> 直接作为受体使用（盒中心取原子质心，可用 box_size 覆盖）

    扩展名集合与 `resolve_receptor_specs`、上传端点共用 `RECEPTOR_EXTS` 一份定义，
    避免历史上「上传端点认 .ent、解析链不认」的两处漂移。
    """
    from docking_agent.core.protonation import protonation_ph as _ph_value
    from docking_agent.core.protonation import protonation_policy as _policy

    prot_policy = _policy(protonation)
    prot_ph = _ph_value(ph)
    local = _fetch_file(source, "receptor")
    ext = receptor_ext(local)
    if ext in RECEPTOR_PDBQT_EXTS:
        # 若这份 PDBQT 是我们自己按 pH 准备的，sidecar 里带着溯源 → 由 _pdbqt_spec 恢复；
        # 没带（外来文件）时 _pdbqt_spec 会如实标注「无法确认是否与目标 pH 一致」。
        return _pdbqt_spec(local, keep_hetatm=keep_hetatm, policy=prot_policy, ph_value=prot_ph)
    return prepare_user_receptor(local, keep_hetatm=keep_hetatm, source_ext=ext,
                                 protonation=protonation, ph=ph)

def receptor_catalog() -> Dict[str, Any]:
    """受体目录（预置受体清单 + 默认受体名）：供各处工具输出统一的 JSON。

    此前 `list_known_receptors`（受理/分发侧）与 `available_receptors`（对接侧）各写一遍，
    两处一旦漂移，模型看到的清单与工具实际接受的受体就会不一致。
    """
    from docking_agent.core import DEFAULT_RECEPTOR, list_receptors

    return {"status": "ok", "default": DEFAULT_RECEPTOR, "receptors": list_receptors()}

def receptor_unspecified(receptor: Any, run: Any = None, receptor_file: str = "") -> bool:
    """用户是否**没有指定受体**（受理层判定 default，或压根没给来源）。

    判定只看两件事：有没有上传受体文件、受理层的 `task_spec.receptor.source` 是不是 `default`，
    以及传进来的受体来源是不是空的。预置受体注册表**仅供内部测试**，不再作为用户可选来源，
    因此这里不做任何「回退默认」的动作，只回答「是否未指定」。

    为什么不再比对受体名：受理层判定「未指定」时 `task_spec.receptor.name` 已经是空串
    （没有任何内建默认受体名了），而 `source == "default"` 本身就把「主管 Agent 习惯性
    写上的默认受体名」拦住了。反过来，**没有 task_spec 的直调**（流水线/工具级调用）里
    显式传入的受体名就是调用方的真实意图，不能再被当成「未指定」（否则 `thrombin` 这种
    合法受体名会被误判）。
    """
    if str(receptor_file or "").strip():
        return False
    spec_receptor = (((getattr(run, "data", None) or {}).get("task_spec") or {})
                     .get("receptor") or {}) if run is not None else {}
    if str(spec_receptor.get("source") or "") == "default":
        return True
    if isinstance(receptor, (list, tuple)):
        return not receptor
    return not str(receptor or "").strip()
