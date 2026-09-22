"""统一输入归一化层：把「脏」的分子库 / 受体文件归一化为标准表示。

用户给的输入几乎不会刚好是我们想要的格式：可能是 URL / 绝对路径 / 相对路径，
可能是 GBK 编码、带 BOM、带注释行，可能是 `.txt` 里装着 SDF、`.sdf` 里装着 CSV，
可能是 gzip / zip 压缩包，也可能只是聊天里粘贴的一段自由文本。本模块把这些差异
统一收敛为两类稳定产物：

  - 分子库：`[{"id","name","smiles","source_file","source_index","raw"}, ...]`
            其中 `smiles` 一律是 RDKit 规范 SMILES，按规范 SMILES 去重并合并别名；
  - 受体：`read_receptor_file()` 产出的受体路径 + 可解释的 normalization 摘要。

设计原则与 `core/ligands.py`、`core/receptors.py` 保持一致：**只报事实、不静默失败**。
一条坏记录不会丢掉整个分子库，但一定会在 `skipped` 里留下行号与原因；受体文件内容
不像结构时直接抛 `ValueError` 并说明「看起来是什么、缺什么、该怎么办」。
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import logging
import os
import re
from datetime import datetime, time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rdkit import Chem

from docking_agent.core.files import slug
from docking_agent.core.ligands import _looks_like_smiles
from docking_agent.paths import cache_dir

logger = logging.getLogger(__name__)

__all__ = [
    # 本模块只导出**自己定义**的东西：归一化层的对外入口在 `core/normalize.py`
    # （此前这里错列了 4 个 normalize.py 的名字，`__all__` 与实际内容不符）。
    "sniff_format",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 格式枚举（与 normalization["format"] 对齐）
FORMATS = ("sdf", "mol", "mol2", "csv", "tsv", "smi", "pdb", "cif", "xlsx", "unknown")
#: 内容真的是「受体结构」的格式
_STRUCTURE_FORMATS = frozenset({"pdb", "cif", "pdbqt"})
#: 文本类格式（需要解码；xlsx/unknown 不需要）
_TEXT_FORMATS = frozenset({"sdf", "mol", "mol2", "csv", "tsv", "smi", "pdb", "cif"})
#: 扩展名 → 格式（只在内容为空 / 无法判断时当提示用，绝不优先于内容）
_EXT_HINT = {
    ".sdf": "sdf", ".sd": "sdf", ".mol": "mol", ".mol2": "mol2",
    ".csv": "csv", ".tsv": "tsv", ".smi": "smi", ".smiles": "smi", ".txt": "smi",
    ".xlsx": "xlsx", ".xlsm": "xlsx",
    ".pdb": "pdb", ".ent": "pdb", ".pdb1": "pdb", ".pdbqt": "pdb",
    ".cif": "cif", ".mmcif": "cif",
}
_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "utf-16", "latin-1")
_DELIMS = (",", "\t", ";")

# 表头别名（大小写不敏感、去空格；`_norm_header` 会把空格/连字符归一为下划线）
_NAME_ALIASES = frozenset({
    "name", "nickname", "title", "compound", "compound_id", "id",
    "编号", "名称", "分子名", "配体", "化合物", "化合物编号",
})
_SMILES_ALIASES = frozenset({
    "smiles", "canonical_smiles", "smiles_str", "structure", "结构", "微笑",
})
_INCHI_ALIASES = frozenset({"inchi", "inchikey", "inchi_key", "inchi键"})

#: InChIKey 是**单向哈希**，RDKit / InChI 库都无法离线反解。这里内置常见化合物的
#: 键 → 规范 SMILES 映射（键由 RDKit `MolToInchiKey` 生成），并对未知键如实跳过。
_INCHIKEY_MAP = {
    "BNRNXUUZRGQAQC-UHFFFAOYSA-N": "CCCc1nn(C)c2c(=O)[nH]c(-c3cc(S(=O)(=O)N4CCN(C)CC4)ccc3OCC)nc12",  # sildenafil
    "BQJCRHHNABKAKU-KBQPJGBKSA-N": "CN1CC[C@]23c4c5ccc(O)c4O[C@H]2[C@@H](O)C=C[C@H]3[C@H]1C5",  # morphine
    "BSYNRYMUTXBXSQ-UHFFFAOYSA-N": "CC(=O)Oc1ccccc1C(=O)O",  # aspirin
    "CMWTZPSULFXXJA-SECBINFHSA-N": "COc1ccc2cc([C@@H](C)C(=O)O)ccc2c1",  # naproxen
    "HEFNNWSXXWATRW-UHFFFAOYSA-N": "CC(C)Cc1ccc(C(C)C(=O)O)cc1",  # ibuprofen
    "JGSARLDLIJGVTE-MBNYWOFBSA-N": "CC1(C)S[C@@H]2[C@H](NC(=O)Cc3ccccc3)C(=O)N2[C@H]1C(=O)O",  # penicillin G
    "KTUFNOKKBVMGRW-UHFFFAOYSA-N": "Cc1ccc(NC(=O)c2ccc(CN3CCN(C)CC3)cc2)cc1Nc1nccc(-c2cccnc2)n1",  # imatinib
    "LFQSCWFLJHTTHZ-UHFFFAOYSA-N": "CCO",  # ethanol
    "MUMGGOZAMZWBJJ-UHFFFAOYSA-N": "CC12CCC(=O)C=C1CCC1C2CCC2(C)C(O)CCC12",  # testosterone
    "NOOLISFMXDJSKH-KXUCPTDWSA-N": "CC(C)[C@@H]1CC[C@@H](C)C[C@H]1O",  # menthol
    "PJVWKTKQMONHTI-UHFFFAOYSA-N": "CC(=O)CC(c1ccccc1)c1c(O)c2ccccc2oc1=O",  # warfarin
    "RYYVLZVUVIJVGH-UHFFFAOYSA-N": "Cn1c(=O)c2c(ncn2C)n(C)c1=O",  # caffeine
    "RZEKVGVHFLEQIL-UHFFFAOYSA-N": "Cc1ccc(-c2cc(C(F)(F)F)nn2-c2ccc(S(N)(=O)=O)cc2)cc1",  # celecoxib
    "RZVAJINKPMORJF-UHFFFAOYSA-N": "CC(=O)Nc1ccc(O)cc1",  # paracetamol
    "SNICXCGAKADSCV-UHFFFAOYSA-N": "CN1CCCC1c1cccnc1",  # nicotine
    "UHOVQNZJYSORNB-UHFFFAOYSA-N": "c1ccccc1",  # benzene
    "WQZGKKKJIJFFOK-GASJEMHNSA-N": "OC[C@H]1OC(O)[C@H](O)[C@@H](O)[C@@H]1O",  # glucose
    "XSQUKJJJFZCRTK-UHFFFAOYSA-N": "NC(N)=O",  # urea
    "XUKUURHRXDUEBC-KAYWLYCHSA-N": "CC(C)c1c(C(=O)Nc2ccccc2)c(-c2ccccc2)c(-c2ccc(F)cc2)n1CC[C@@H](O)C[C@@H](O)CC(=O)O",  # atorvastatin
    "XZWYZXLIPXDOLR-UHFFFAOYSA-N": "CN(C)C(=N)NC(=N)N",  # metformin
    "YGSDEFSMJLZEOE-UHFFFAOYSA-N": "O=C(O)c1ccccc1O",  # salicylic acid
    "YXFVVABEGXRONW-UHFFFAOYSA-N": "Cc1ccccc1",  # toluene
}
_INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
_INCHIKEY_ANY_RE = re.compile(r"[A-Z]{14}-[A-Z]{10}-[A-Z]")


def _inchikeys_in(text: str) -> List[str]:
    """文本里出现过的所有 InChIKey（不要求独占一行）。

    用于如实上报「这个结构是从内置映射表还原来的」——工具只报事实。
    """
    return [m.group(0).upper() for m in _INCHIKEY_ANY_RE.finditer(str(text or ""))]

_GUESS_CN = {
    "sdf": "SDF 分子记录", "mol": "MOL 单分子记录", "mol2": "MOL2 分子记录",
    "csv": "逗号分隔表格（CSV）", "tsv": "制表符分隔表格（TSV）",
    "smi": "SMILES 文本", "xlsx": "Excel 表格", "unknown": "无法识别的二进制内容",
}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _decode_bytes(data: bytes) -> Tuple[str, str]:
    """按 utf-8-sig → utf-8 → gbk → utf-16 → latin-1 依次尝试，返回 (编码名, 文本)。"""
    for enc in _ENCODINGS:
        try:
            return enc, data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return "latin-1", data.decode("latin-1", errors="replace")


def _is_comment(stripped: str) -> bool:
    return stripped.startswith("#") or stripped.startswith("//")


def _significant_lines(text: str) -> List[Tuple[int, str, str]]:
    """返回 [(1-based 行号, 原始行, 去空白后的行)]，跳过空行与 `#` / `//` 注释行。"""
    rows: List[Tuple[int, str, str]] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        s = raw.strip()
        if s.startswith("\ufeff"):
            s = s.lstrip("\ufeff").strip()
        if not s or _is_comment(s):
            continue
        rows.append((i, raw, s))
    return rows


def _split_line(raw: str, delim: str) -> List[str]:
    if not delim:
        return [raw.strip()]
    try:
        return [c.strip() for c in next(csv.reader([raw], delimiter=delim))]
    except Exception:  # noqa: BLE001 - 引号不配对等，退化为朴素切分
        return [c.strip() for c in raw.split(delim)]


def _norm_header(field: str) -> str:
    s = (field or "").strip().lower()
    for ch in (" ", "\t", "-", "\u3000"):
        s = s.replace(ch, "_")
    return s.strip("_")


def _header_kind(field: str) -> str:
    key = _norm_header(field)
    if key in _NAME_ALIASES:
        return "name"
    if key in _SMILES_ALIASES:
        return "smiles"
    if key in _INCHI_ALIASES:
        return "inchi"
    return ""


def _looks_like_inchikey(value: str) -> bool:
    return bool(_INCHIKEY_RE.match((value or "").strip().upper()))


def _canonical(mol: Any) -> str:
    """分子 → 规范 SMILES（先隐式化显式氢）。失败返回 ""。"""
    if mol is None:
        return ""
    target = mol
    try:
        target = Chem.RemoveHs(mol)
    except Exception:  # noqa: BLE001 - 特殊价态/金属可能失败，退回原分子
        target = mol
    try:
        return Chem.MolToSmiles(target)
    except Exception:  # noqa: BLE001
        return ""


def _mol_name(mol: Any) -> str:
    for prop in ("_Name", "ID", "Name", "Title", "id", "name", "编号", "名称"):
        try:
            if mol.HasProp(prop):
                val = mol.GetProp(prop).strip()
                if val:
                    return val
        except Exception:  # noqa: BLE001
            continue
    return ""


def _canonical_from_smiles(value: str) -> Optional[str]:
    val = (value or "").strip()
    if not val:
        return None
    if not _looks_like_smiles(val):
        return _canonical_from_inchi(val)
    mol = Chem.MolFromSmiles(val)
    if mol is None:
        return _canonical_from_inchi(val)
    return _canonical(mol) or None


def _canonical_from_inchi(value: str) -> Optional[str]:
    """InChI（可逆）或 InChIKey（单向哈希：内置表 → 本地缓存 → 在线反查）。"""
    val = (value or "").strip()
    if not val:
        return None
    if val.startswith("InChI="):
        try:
            mol = Chem.MolFromInchi(val)
        except Exception:  # noqa: BLE001
            mol = None
        return _canonical(mol) or None
    if _looks_like_inchikey(val):
        from docking_agent.core.inchikey import resolve_inchikey  # 局部导入避免循环依赖

        hit = resolve_inchikey(val)
        smiles = str(hit.get("smiles") or "")
        if not smiles:
            return None
        return _canonical(Chem.MolFromSmiles(smiles)) or smiles
    return None


def _inchi_from_fields(fields: List[str], idx: Optional[int], delim: str) -> Optional[str]:
    """从表格字段里还原 InChI。

    InChI 本身含逗号，未加引号时会被分隔符切成多段（真实脏输入）。这里从最长候选
    开始逐步收缩，直到某一段能解析——既兼容正确加引号的 CSV，也兼容未加引号的情况。
    """
    if idx is None or idx >= len(fields):
        return None
    ends = range(len(fields), idx, -1) if delim else [idx + 1]
    for end in ends:
        candidate = delim.join(fields[idx:end]) if delim else fields[idx]
        smiles = _canonical_from_inchi(candidate)
        if smiles:
            return smiles
    return None


def _shorten_names(names: List[str], limit: int = 5) -> str:
    """把名字清单截断成 `a、b、c 等 N 个`（溯源说明里用，避免 notes 过长）。"""
    uniq = list(dict.fromkeys(str(n) for n in names if str(n).strip()))
    head = "、".join(uniq[:limit])
    return head + (f" 等 {len(uniq)} 个" if len(uniq) > limit else "")


def _xlsx_cell_text(value: Any) -> str:
    """把一个单元格值变成可解析的文本（数字/日期/布尔都要给出稳定形态）。

    Excel 里最容易踩的两个坑：整数被读成 `2244.0`（CID 之类会带小数点），
    日期被读成 `datetime`（`str()` 出来带 00:00:00）。两者都会污染 ID/名称列。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time.min else value.isoformat(sep=" ")
    return str(value).strip()


def xlsx_to_csv_sheets(data: bytes, notes: List[str]) -> List[Tuple[str, str]]:
    """把 xlsx 的**每张工作表**转成 CSV 文本，返回 `[(工作表名, csv文本)]`。

    为什么不是只读 `wb.active`：真实表格的第一张工作表常是「说明/目录」，数据在第二张，
    只读 active 会得到「0 个分子」（用户看到的就是「解析不出来」）。这里逐表转换，
    由上层合并并在 notes 里如实写出每张表解析到多少个分子，绝不静默丢数据。
    依赖缺失/文件损坏都只写 notes 并返回空列表，**不抛异常**。
    """
    try:
        import openpyxl  # type: ignore
    except Exception:  # noqa: BLE001 - 依赖缺失时如实告知（安装见 requirements-local.txt）
        notes.append("检测到 xlsx 表格，但当前环境未安装 openpyxl，无法读取；"
                     "请执行 `uv pip install openpyxl`（或把表格另存为 CSV）后重试。")
        return []
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"xlsx 读取失败（{exc}）；若为旧版 .xls 请另存为 .xlsx 或 CSV 后重试。")
        return []
    sheets: List[Tuple[str, str]] = []
    for worksheet in getattr(wb, "worksheets", []):
        rows = [[_xlsx_cell_text(c) for c in row] for row in worksheet.iter_rows(values_only=True)]
        rows = [r for r in rows if any(v for v in r)]
        if not rows:
            continue
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        sheets.append((str(worksheet.title), buf.getvalue()))
    try:
        wb.close()
    except Exception as e:  # noqa: BLE001 - 只读工作簿关闭失败不影响已解析结果
        logger.debug("xlsx 工作簿关闭失败：%s", e)
    return sheets


def _record(id_: str, smiles: str, raw: str, source_file: str, source_index: int, *,
            id_override: str = "") -> Dict[str, Any]:
    """标准分子记录：默认 id 与 name 同值。

    `id_override`：表格文件里**单独有一列 ID**（如 `id,名称,smiles` 或 `编号,SMILES`）时，
    把那一列作为真正的 `id`，而 name 仍是名称列 —— 用户要求「报告带上小分子 ID」时，
    这个 ID 才是有信息量的（与名称不同）。
    """
    ident = str(id_override or "").strip() or id_
    return {
        "id": ident,
        "name": id_,
        "smiles": smiles,
        "source_file": source_file,
        "source_index": int(source_index),
        "raw": raw,
    }


def _materialize(data: bytes, inner_name: str) -> str:
    """把内存中的内容落盘到 cache（内容寻址，避免并发互相覆盖）。"""
    digest = hashlib.sha1(data).hexdigest()[:12]
    base = os.path.basename(inner_name or "input")
    stem = slug(os.path.splitext(base)[0]) or "input"
    suffix = os.path.splitext(base)[1] or ".dat"
    path = cache_dir() / f"norm_{digest}_{stem}{suffix}"
    try:
        if not path.exists() or path.stat().st_size != len(data):
            path.write_bytes(data)
    except OSError:
        return str(path)
    return str(path)


# --------------------------------------------------------------------------- #
# 格式嗅探
# --------------------------------------------------------------------------- #


def _analyze_tabular(text: str) -> Optional[Dict[str, Any]]:
    """判断文本是否为表格，并给出分隔符 / 表头。

    分隔符在 `,` `\\t` `;` 中按「字段数一致性」打分：字段数众数越大、命中的行越多越好。
    没有表头别名时，如果所有字段都像 SMILES，则判定为 SMILES 列表而非表格
    （`CCO,CCN` 是 SMILES 列表，不是「结构+名称」的 CSV）。
    """
    rows = _significant_lines(text)
    if not rows:
        return None

    best: Optional[Tuple[Tuple[int, int], str]] = None
    for delim in _DELIMS:
        counts = [len(_split_line(raw, delim)) for _i, raw, _s in rows]
        if not counts:
            continue
        mode = max(set(counts), key=lambda n: (counts.count(n), n))
        if mode < 2:
            continue
        score = (counts.count(mode), mode)
        if best is None or score > best[0]:
            best = (score, delim)

    if best is not None:
        delim = best[1]
        header_fields = _split_line(rows[0][1], delim)
        kinds = [_header_kind(f) for f in header_fields]
        has_header = any(kinds)
        if not has_header:
            all_smiles_like = True
            for _i, raw, _s in rows:
                for field in _split_line(raw, delim):
                    if not (field and _looks_like_smiles(field)):
                        all_smiles_like = False
                        break
                if not all_smiles_like:
                    break
            if all_smiles_like:
                return None
        return {
            "format": "tsv" if delim == "\t" else "csv",
            "delimiter": delim,
            "header_fields": header_fields if has_header else None,
            "kinds": kinds if has_header else [],
        }

    # 单列文件：只有首行是已知表头关键字时才认定为 CSV（如「只有 SMILES 列」的表格）
    first = rows[0][2]
    if _header_kind(first):
        return {"format": "csv", "delimiter": "", "header_fields": [first],
                "kinds": [_header_kind(first)]}
    return None


def sniff_format(path: str, data: bytes | None = None) -> str:
    """按**内容**嗅探格式，扩展名只作提示。

    返回 `"sdf"|"mol"|"mol2"|"csv"|"tsv"|"smi"|"pdb"|"cif"|"xlsx"|"unknown"`。
    gzip / 普通 zip 会被透明地看进去（选第一个受支持成员）；xlsx 的 zip 签名会被
    识别为 `"xlsx"`。
    """
    name = str(path or "").lower()
    ext = os.path.splitext(name)[1]

    if data is None:
        try:
            data = Path(path).read_bytes()
        except OSError:
            data = b""
    if not data:
        return _EXT_HINT.get(ext, "unknown")

    # ---- 容器：透明看进去 ----
    if data[:2] == b"\x1f\x8b":
        try:
            return sniff_format("", gzip.decompress(data))
        except OSError:
            return "unknown"
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                if any(n.startswith("xl/") for n in names) or ext in (".xlsx", ".xlsm"):
                    return "xlsx"
                for member in names:
                    if member.startswith("__MACOSX") or os.path.basename(member).startswith("."):
                        continue
                    try:
                        sub = zf.read(member)
                    except Exception:  # noqa: BLE001 - 加密/损坏成员跳过
                        continue
                    fmt = sniff_format(member, sub)
                    if fmt != "unknown":
                        return fmt
        except zipfile.BadZipFile:  # 允许静默：不是 zip 包，按非压缩内容继续嗅探
            pass
        return "unknown"

    text = _decode_bytes(data)[1]
    # 控制字符（NUL 等）说明这是二进制而不是文本结构/表格
    if any(ord(ch) < 32 and ch not in "\t\n\r\f\v" for ch in text[:8192]):
        return "unknown"

    # ---- MOL2 ----
    if "@<TRIPOS>MOLECULE" in text.upper():
        return "mol2"

    # ---- SDF / MOL ----
    if "V2000" in text or "V3000" in text:
        return "sdf" if "$$$$" in text else "mol"

    # ---- PDB / CIF ----
    for raw in text.splitlines():
        if raw.lstrip().startswith(("ATOM", "HETATM", "HEADER")):
            return "pdb"
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("data_") or s.startswith("loop_"):
            return "cif"

    # ---- CSV / TSV ----
    tabular = _analyze_tabular(text)
    if tabular is not None:
        return tabular["format"]

    if _significant_lines(text):
        return "smi"
    return _EXT_HINT.get(ext, "unknown")




__all__ = [
    "FORMATS",
    "sniff_format",
    "_read_bytes",
    "_decode_bytes",
    "_analyze_tabular",
    "_canonical",
    "_INCHIKEY_MAP",
    "_canonical_from_inchi",
    "_inchikeys_in",
    "_canonical_from_smiles",
    "_inchi_from_fields",
    "_looks_like_inchikey",
    "_materialize",
    "_mol_name",
    "_record",
    "_header_kind",
    "_significant_lines",
    "_split_line",
]
