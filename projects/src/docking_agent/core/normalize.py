"""统一输入归一化层（解析与标准表示）：把「脏」输入收敛为分子库/受体的稳定表示。

本模块是 `core/normalize_io.py`（容器解包 + 格式嗅探 + 表格/编码基础设施）之上的
**解析与归一化**部分，对外提供：

  - 分子库：`normalize_ligand_file` / `normalize_ligand_text` → 规范 SMILES + 原始 ID；
  - 受体：`normalize_receptor_source` → 可对接路径 + 可解释的化学溯源；
  - 溯源落盘：`record_input_normalization` → 运行产物 `input_normalization.json`。

设计原则：**只报事实、不静默失败**。一条坏记录不会丢掉整库，但一定在 `skipped` 里
留下行号与原因；受体内容不像结构时抛 `ValueError` 并说明「看起来是什么、缺什么、怎么办」。
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import os
import re
import zipfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from rdkit import Chem

from docking_agent.core.files import _fetch_file
from docking_agent.core.ligands import _looks_like_smiles, describe_ligand, parse_smiles_text
from docking_agent.core.normalize_io import (
    _GUESS_CN,
    xlsx_to_csv_sheets,
    _INCHIKEY_RE,
    _STRUCTURE_FORMATS,
    _TEXT_FORMATS,
    _analyze_tabular,
    _canonical,
    _canonical_from_inchi,
    _canonical_from_smiles,
    _decode_bytes,
    _header_kind,
    _inchi_from_fields,
    _inchikeys_in,
    _materialize,
    _mol_name,
    _pick_field,
    _read_bytes,
    _record,
    _significant_lines,
    _split_line,
    sniff_format,
)
from docking_agent.core.receptors import (
    RECEPTOR_EXTS,
    RECEPTOR_NON_STRUCTURE_EXTS,
    read_receptor_file,
)

logger = logging.getLogger(__name__)

__all__ = [
    "sniff_format",
    "normalize_ligand_file",
    "normalize_ligand_text",
    "normalize_receptor_source",
    "record_input_normalization",
]


#: 「第 N 行解析不出分子」提示里**必须隐去**的内容：疑似密钥/凭据的赋值行。
#: 为什么需要：这条提示会经 API 与报告展示给用户；用户误把 `.env` 之类交给解析器时，
#: 逐行回显原文等于把 API Key 打印出来（真实漏洞）。提示本身仍要有用 —— 只隐去
#: 疑似凭据行，其余照旧回显一小段。
_SECRETY_LINE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|authorization|bearer|private[_-]?key)")


def _line_hint(line: str, limit: int = 40) -> str:
    """解析失败行的可展示提示：疑似凭据行只报「已隐去」，其余回显前 `limit` 个字符。"""
    text = (line or "").strip()
    if not text:
        return "（空行）"
    if _SECRETY_LINE.search(text):
        return "（疑似凭据/密钥行，内容已隐去）"
    return repr(text[:limit])


# --------------------------------------------------------------------------- #
# 容器解包
# --------------------------------------------------------------------------- #


def _maybe_unpack(data: bytes, local: str, notes: List[str]) -> Tuple[bytes, str]:
    """透明解压 gzip / 单成员 zip（xlsx 除外，交给格式处理），返回 (数据, 本地路径)。"""
    base = os.path.basename(local)

    if data[:2] == b"\x1f\x8b" or local.lower().endswith(".gz"):
        try:
            inner = gzip.decompress(data)
        except OSError as exc:
            raise ValueError(f"gzip 容器解压失败（{base}）：{exc}") from exc
        inner_name = base[:-3] if base.lower().endswith(".gz") else (base + ".out")
        if not inner_name:
            inner_name = "archive.out"
        path = _materialize(inner, inner_name)
        notes.append(f"已解压 gzip 容器 {base} → {os.path.basename(path)}")
        return inner, path

    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                if any(n.startswith("xl/") for n in names) or local.lower().endswith((".xlsx", ".xlsm")):
                    return data, local  # xlsx 由格式分支读取
                candidates = [n for n in names
                              if not n.startswith("__MACOSX")
                              and not os.path.basename(n).startswith(".")]
                if not candidates:
                    candidates = names
                if not candidates:
                    notes.append(f"zip 容器 {base} 里没有文件，已忽略")
                    return data, local
                picked = None
                blob = b""
                for member in candidates:
                    try:
                        sub = zf.read(member)
                    except Exception:  # noqa: BLE001
                        continue
                    if sniff_format(member, sub) != "unknown":
                        picked, blob = member, sub
                        break
                if picked is None:
                    picked = candidates[0]
                    blob = zf.read(picked)
        except (zipfile.BadZipFile, OSError):
            return data, local

        if len(candidates) > 1:
            notes.append(f"zip 容器 {base} 含 {len(candidates)} 个候选文件，"
                         f"已取第一个受支持的：{picked}")
        else:
            notes.append(f"已解压 zip 容器 {base} → {picked}")
        path = _materialize(blob, picked)
        return blob, path

    return data, local


# --------------------------------------------------------------------------- #
# 各格式解析（返回「去重前」的记录）
# --------------------------------------------------------------------------- #


def _sdf_blocks(text: str) -> List[Tuple[int, str]]:
    """按 `$$$$` 切分 SDF，返回 [(块起始行号, 块文本)]。"""
    blocks: List[Tuple[int, str]] = []
    cur: List[str] = []
    start: Optional[int] = None
    for i, line in enumerate(text.splitlines(), start=1):
        if start is None:
            if not line.strip():
                continue
            start = i
        cur.append(line)
        if line.strip().startswith("$$$$"):
            blocks.append((start or i, "\n".join(cur)))
            cur = []
            start = None
    if cur:
        blocks.append((start or 1, "\n".join(cur)))
    return blocks


def _parse_sdf(data: bytes, local: Optional[str], source_file: str,
               name_prefix: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    path = local or _materialize(data, "input.sdf")
    text = _decode_bytes(data)[1]
    blocks = _sdf_blocks(text)
    records: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    idx = 0
    try:
        supplier = Chem.SDMolSupplier(path, sanitize=True, removeHs=False)
        for mol in supplier:
            start = blocks[idx][0] if idx < len(blocks) else idx + 1
            raw = blocks[idx][1] if idx < len(blocks) else ""
            idx += 1
            if mol is None:
                skipped.append({"line": start,
                                "reason": f"第 {idx} 条 SDF 记录无法被 RDKit 解析"})
                continue
            smiles = _canonical(mol)
            if not smiles:
                skipped.append({"line": start, "reason": f"第 {idx} 条 SDF 记录无法生成规范 SMILES"})
                continue
            # SDF 的附加字段（ID / CAS / MOLENAME / 编号…）：用户常要求"输出带上 ID 号"，
            # 这些字段就写在 SDF 里，必须在这里取出来并一路带到排序表/CSV/报告。
            try:
                props = {str(k): str(v).strip() for k, v in (mol.GetPropsAsDict() or {}).items()
                         if str(v).strip()}
            except Exception:  # noqa: BLE001 - 取不到属性不影响分子本身
                props = {}
            id_override = _pick_field(props, ("ID", "Id", "id", "编号", "化合物编号", "CATALOG_ID"))
            cas = _pick_field(props, ("CAS", "Cas", "cas", "CAS号", "CASRN", "CAS_NUMBER"))
            name = (_mol_name(mol) or _pick_field(props, ("MOLENAME", "Name", "NAME", "名称"))
                    or id_override or f"{name_prefix}{idx}")
            records.append(_record(name, smiles, raw, source_file, idx,
                                   id_override=id_override, fields=props, cas=cas))
    except Exception as exc:  # noqa: BLE001 - SDMolSupplier 对畸形文件可能抛异常
        skipped.append({"line": blocks[0][0] if blocks else 1,
                        "reason": f"SDF 读取异常：{exc}"})
        idx = len(blocks)
    # Supplier 可能因畸形块提前结束：剩余块按失败记录补报，绝不静默丢弃
    while idx < len(blocks):
        skipped.append({"line": blocks[idx][0],
                        "reason": f"第 {idx + 1} 条 SDF 记录不完整或无法解析"})
        idx += 1
    return records, skipped, {}


def _parse_mol(data: bytes, local: Optional[str], source_file: str, fmt: str,
               name_prefix: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    path = local or _materialize(data, f"input.{fmt}")
    text = _decode_bytes(data)[1]
    try:
        mol = (Chem.MolFromMol2File(path, sanitize=True, removeHs=False) if fmt == "mol2"
               else Chem.MolFromMolFile(path, sanitize=True, removeHs=False))
    except Exception:  # noqa: BLE001
        mol = None
    if mol is None:
        return [], [{"line": 1, "reason": f"{fmt.upper()} 文件无法被 RDKit 解析"}], {}
    smiles = _canonical(mol)
    if not smiles:
        return [], [{"line": 1, "reason": f"{fmt.upper()} 文件无法生成规范 SMILES"}], {}
    name = _mol_name(mol) or f"{name_prefix}1"
    return [_record(name, smiles, text.strip(), source_file, 1)], [], {}


#: 表格里「ID 列」的表头（中英文常见写法）——命中即作为分子 id
_ID_HEADER_RE = re.compile(
    r"^(id|编号|序号|分子id|分子编号|no\.?|num|number|catalog(\s*no\.?)?|cat\.?\s*no\.?|code|"
    r"compound(\s*id)?|zinc(\s*_?id)?|chembl(\s*_?id)?|pubchem(\s*_?cid)?|cid|cas(\s*_?no\.?)?)$",
    re.I)


def _parse_tabular(data: bytes, text: str, source_file: str,
                   name_prefix: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    tab = _analyze_tabular(text) or {}
    delim = tab.get("delimiter", "")
    header_fields = tab.get("header_fields")
    kinds = tab.get("kinds") or []
    rows = _significant_lines(text)

    name_idx = smiles_idx = inchi_idx = id_idx = None
    meta: Dict[str, Any] = {"delimiter": delim, "header_name": "", "header_smiles": ""}
    if header_fields is not None:
        for i, kind in enumerate(kinds):
            if kind == "name" and name_idx is None:
                name_idx = i
            elif kind == "smiles" and smiles_idx is None:
                smiles_idx = i
            elif kind == "inchi" and inchi_idx is None:
                inchi_idx = i
        # 表格里可能**单独有一列 ID**（id / 编号 / catalog / ChEMBL …）：抓出来当分子 id，
        # 这样「报告带上小分子 ID」拿到的是文件里那个编号，而不是与名称重复的值。
        for i, header in enumerate(header_fields):
            if i == smiles_idx or i == inchi_idx:
                continue
            if _ID_HEADER_RE.match(str(header or "").strip()):
                id_idx = i
                break
        # 启发式常把 ID 列判成 name（例如表头 `id,name,smiles`）：
        # 既然它是 ID 列，就把 name 让给后面真正的名称列（没有名称列时才回落到 ID）。
        if id_idx is not None and id_idx == name_idx:
            name_idx = next((i for i, kind in enumerate(kinds)
                             if kind == "name" and i != id_idx and i != smiles_idx), None)
        meta["header_name"] = header_fields[name_idx] if name_idx is not None else ""
        meta["header_smiles"] = header_fields[smiles_idx] if smiles_idx is not None else ""
        meta["header_id"] = header_fields[id_idx] if id_idx is not None else ""
        data_rows = rows[1:]
        if smiles_idx is None and inchi_idx is None:
            skipped = [{"line": lineno,
                        "reason": f"第 {lineno} 行：表头未包含 SMILES/InChI 列"}
                       for lineno, _raw, _s in data_rows]
            return [], skipped, meta
    else:
        data_rows = rows

    records: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    ordinal = 0
    for lineno, raw, stripped in data_rows:
        ordinal += 1
        fields = _split_line(raw, delim)
        smiles: Optional[str] = None
        name = ""
        id_override = ""
        struct_idx: Optional[int] = None
        if header_fields is not None:
            if smiles_idx is not None and smiles_idx < len(fields):
                smiles = _canonical_from_smiles(fields[smiles_idx])
            if smiles is None and inchi_idx is not None and inchi_idx < len(fields):
                smiles = _inchi_from_fields(fields, inchi_idx, delim)
            if name_idx is not None and name_idx < len(fields):
                name = fields[name_idx].strip()
            if id_idx is not None and id_idx < len(fields):
                id_override = fields[id_idx].strip()
        else:
            for i, field in enumerate(fields):
                candidate = _canonical_from_smiles(field)
                if candidate:
                    smiles, struct_idx = candidate, i
                    break
            if smiles is None:
                for i, field in enumerate(fields):
                    candidate = _inchi_from_fields(fields, i, delim)
                    if candidate:
                        smiles, struct_idx = candidate, i
                        break
            if smiles is not None:
                for i, field in enumerate(fields):
                    if i != struct_idx and field.strip():
                        name = field.strip()
                        break
        if not smiles:
            # 行里带 InChIKey 时，把**真实失败原因**带出来（网络/未收录/校验不一致），
            # 否则用户只看到"无法解析"，根本不知道是网络问题还是数据问题（真实缺陷）。
            detail = ""
            for key in _inchikeys_in(raw):
                note = _inchikey_failure_note(key)
                if note:
                    detail = f"（InChIKey {key}：{note}）"
                    break
            skipped.append({"line": lineno,
                            "reason": f"第 {lineno} 行无法解析出 SMILES/InChI："
                                      f"{stripped[:60]!r}{detail}"})
            continue
        records.append(_record(name or f"{name_prefix}{ordinal}", smiles, raw, source_file, ordinal,
                               id_override=id_override))
    return records, skipped, meta


def _parse_smiles_text(text: str, source_file: str,
                       name_prefix: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """逐行解析自由文本 / `.smi`：复用 `parse_smiles_text`，并补上 id 与 InChI 支持。"""
    records: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    sentinel = "\x00AUTO\x00"
    ordinal = 0
    for lineno, raw, stripped in _significant_lines(text):
        tokens = [t for t in re.split(r"[\s,;]+", stripped) if t]
        # `.smi` 常见的表头/分隔行（如 "SMILES name"）：不是记录，也不该报成坏行
        if tokens and all(_header_kind(t) for t in tokens):
            continue
        # InChI / InChIKey 里含逗号，先按整行正则取出，避免被 SMILES 分词器切碎
        match = re.search(r"InChI=[^\s,;]+", raw)
        if match:
            smiles = _canonical_from_inchi(match.group(0))
            if smiles:
                ordinal += 1
                name = raw.replace(match.group(0), " ").strip(" \t,;:")
                records.append(_record(name or f"{name_prefix}{ordinal}", smiles, raw,
                                       source_file, ordinal))
            else:
                skipped.append({"line": lineno,
                                "reason": f"第 {lineno} 行 InChI 无法解析：{stripped[:60]!r}"})
            continue
        match = _INCHIKEY_RE.search(raw.upper())
        if match:
            key = match.group(0)
            smiles = _canonical_from_inchi(key)
            ordinal += 1
            if smiles:
                name = raw.upper().replace(key, " ").strip(" \t,;:")
                records.append(_record(name or f"{name_prefix}{ordinal}", smiles, raw,
                                       source_file, ordinal))
            else:
                skipped.append({"line": lineno,
                                "reason": f"第 {lineno} 行 InChIKey {key} 未能还原结构："
                                          + _inchikey_failure_note(key)})
            continue
        # 标准 `.smi` 顺序是「SMILES<空白>名称」；parse_smiles_text 只认「名称 SMILES」，
        # 因此这里先补上「SMILES 在前」的常见写法（名称不是一个分子时才算名称）。
        if len(tokens) >= 2 and _looks_like_smiles(tokens[0]):
            first = Chem.MolFromSmiles(tokens[0])
            if first is not None:
                rest_are_molecules = any(
                    _looks_like_smiles(t) and Chem.MolFromSmiles(t) is not None
                    for t in tokens[1:]
                )
                if not rest_are_molecules:
                    ordinal += 1
                    name = " ".join(tokens[1:]).strip()
                    records.append(_record(name or f"{name_prefix}{ordinal}", _canonical(first),
                                           raw, source_file, ordinal))
                    continue
        items = parse_smiles_text(raw, default_name_prefix=sentinel)
        if not items:
            skipped.append({"line": lineno,
                            "reason": f"第 {lineno} 行无法解析出分子：{_line_hint(stripped)}"})
            continue
        for item in items:
            ordinal += 1
            name = item.get("name") or ""
            if name.startswith(sentinel):
                name = ""
            records.append(_record(name or f"{name_prefix}{ordinal}", item["smiles"], raw,
                                   source_file, ordinal))
    return records, skipped, {}


def _inchikey_failure_note(key: str) -> str:
    """把 InChIKey 解析失败的真实原因写进「跳过原因」（否则用户只看到"解析不出"）。"""
    from docking_agent.core.inchikey import resolution_error  # 局部导入避免循环依赖

    detail = resolution_error(key)
    if detail:
        return detail + "（可改用 InChI 或 SMILES；或把该结构补进本地缓存）"
    return "哈希不可逆且本地未收录（可改用 InChI 或 SMILES，或开启 InChIKey 在线反查）"


def _parse_xlsx(source_file: str, data: bytes, notes: List[str],
                name_prefix: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """读 Excel：**合并所有能解析出分子的工作表**（转换在 normalize_io，见那里的说明）。"""
    records: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}
    sheet_report: List[str] = []
    for title, csv_text in xlsx_to_csv_sheets(data, notes):
        sheet_records, sheet_skipped, sheet_meta = _parse_tabular(
            data, csv_text, source_file, name_prefix)
        sheet_report.append(f"{title}: {len(sheet_records)} 个")
        if not sheet_records:
            continue
        # 多表合并：行号加一个整表偏移，避免不同表的"第 3 行"混在一起无法定位
        offset = len(records)
        for rec in sheet_records:
            rec["source_index"] = int(rec.get("source_index") or 0) + offset
        records.extend(sheet_records)
        for item in sheet_skipped:
            # 内层原因自带「第 N 行」，这里换成工作表前缀（行号由外层统一补，避免重复两遍）
            inner = re.sub(r"^第\s*\d+\s*行[：:，,]?\s*", "", str(item.get("reason") or ""))
            skipped.append({"line": item.get("line"),
                            "reason": f"工作表「{title}」{inner}"})
        if not meta:
            meta = sheet_meta
    if sheet_report:
        notes.append("xlsx 工作表解析：" + "；".join(sheet_report))
    if not records:
        notes.append("所有工作表都没解析出分子：请确认表格里有一列 SMILES/结构（或整表都是 SMILES），"
                     "并检查表头是否被识别。")
    return records, skipped, meta


def _parse_by_format(data: bytes, text: str, fmt: str, local: Optional[str],
                     source_file: str, name_prefix: str,
                     notes: List[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    if fmt in ("sdf",):
        return _parse_sdf(data, local, source_file, name_prefix)
    if fmt in ("mol", "mol2"):
        return _parse_mol(data, local, source_file, fmt, name_prefix)
    if fmt in ("csv", "tsv"):
        return _parse_tabular(data, text, source_file, name_prefix)
    if fmt == "smi":
        return _parse_smiles_text(text, source_file, name_prefix)
    if fmt == "xlsx":
        return _parse_xlsx(source_file, data, notes, name_prefix)
    if fmt in ("pdb", "cif"):
        notes.append(f"内容看起来是 {fmt.upper()} 结构文件，不是小分子库；"
                     "如需对接该受体请改用 normalize_receptor_source。")
        return [], [], {}
    return [], [], {}


# --------------------------------------------------------------------------- #
# 去重与汇总
# --------------------------------------------------------------------------- #


def _dedupe(records: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, List[str]]]:
    """按规范 SMILES 去重：保留**第一条**记录，合并所有别名。"""
    kept: List[Dict[str, Any]] = []
    groups: Dict[str, Dict[str, Any]] = {}
    dup_records: List[Dict[str, Any]] = []
    for rec in records:
        smiles = rec["smiles"]
        if smiles in groups:
            groups[smiles]["aliases"].append(rec["id"])
            dup_records.append(rec)
            continue
        groups[smiles] = {"mol": rec, "aliases": [rec["id"]]}
        kept.append(rec)

    duplicates: List[Dict[str, Any]] = []
    aliases: Dict[str, List[str]] = {}
    for smiles, group in groups.items():
        seen: List[str] = []
        for name in group["aliases"]:
            if name and name not in seen:
                seen.append(name)
        key = group["mol"]["id"]
        aliases.setdefault(key, [])
        for name in seen:
            if name not in aliases[key]:
                aliases[key].append(name)
    for rec in dup_records:
        group = groups[rec["smiles"]]
        aliases_list: List[str] = []
        for name in group["aliases"]:
            if name and name not in aliases_list:
                aliases_list.append(name)
        duplicates.append({"name": rec["id"], "smiles": rec["smiles"],
                           "kept_name": group["mol"]["id"], "aliases": aliases_list})
    return kept, duplicates, aliases


def _finalize(source_file: str, fmt: str, records: List[Dict[str, Any]],
              skipped: List[Dict[str, Any]], meta: Dict[str, Any],
              encoding: str, notes: List[str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    kept, duplicates, aliases = _dedupe(records)
    total = len(kept) + len(duplicates) + len(skipped)

    summary = f"共 {total} 条，成功 {len(kept)}"
    if duplicates:
        summary += f"，去重移除 {len(duplicates)} 条"
    summary += f"；跳过 {len(skipped)} 条"
    if skipped:
        # 逐条原因里可能已经带了「第 N 行」，这里统一由外层补行号，避免出现「第 3 行 第 3 行…」
        detail = "、".join(
            f"第 {s['line']} 行 " + re.sub(r"^第\s*\d+\s*行[：:，,]?\s*", "", str(s.get("reason") or ""))
            for s in skipped[:5])
        summary += "：" + detail
        if len(skipped) > 5:
            summary += f" 等共 {len(skipped)} 条"

    all_notes: List[str] = [summary]
    all_notes.extend(notes)
    if meta.get("header_name") or meta.get("header_smiles") or meta.get("header_id"):
        id_part = f"；id={meta['header_id']}" if meta.get("header_id") else ""
        all_notes.append(f"表头识别：name={meta.get('header_name') or '（无）'}；"
                         f"smiles={meta.get('header_smiles') or '（无）'}{id_part}")
    if meta.get("delimiter"):
        shown = "\\t" if meta["delimiter"] == "\t" else meta["delimiter"]
        all_notes.append(f"分隔符：{shown!r}")
    from docking_agent.core.inchikey import provenance_notes  # 局部导入避免循环依赖

    all_notes.extend(provenance_notes(kept))
    # 多片段（盐/溶剂）只报事实，不改写 smiles：下游 describe_ligand 会按最大有机片段对接
    # 质子化态是**运行级**策略：这里只做库级汇总（每个分子都报会写出上百条重复 note），
    # 逐分子溯源在对接/性质结果与排序 CSV 里（`protonation_policy`/`charge_input`/`charge_used`）。
    protonated = 0
    for rec in kept:
        try:
            info: Dict[str, Any] = {}
            mol = Chem.MolFromSmiles(rec["smiles"])
            if mol is not None and len(Chem.GetMolFrags(mol)) > 1:
                info = describe_ligand(rec["smiles"]) or {}
                for warning in info.get("warnings", []) or []:
                    if "质子化态已按运行级策略调整" in warning:
                        continue
                    all_notes.append(f"{rec['id']}：{warning}")
            if not info:
                info = describe_ligand(rec["smiles"]) or {}
            if (info.get("protonation") or {}).get("applied"):
                protonated += 1
        except Exception:  # noqa: BLE001 - 体检失败不影响归一化结果
            continue
    if protonated:
        all_notes.append(f"质子化态策略：{protonated}/{len(kept)} 个分子带净电荷，已按运行级策略中和"
                         "（原始 SMILES 保留，逐分子净电荷前/后见排序 CSV 与产物 docking.json）")

    normalization: Dict[str, Any] = {
        "source_file": source_file,
        "format": fmt,
        "records_total": total,
        "records_ok": len(kept),
        "records_skipped": len(skipped),
        "skipped": skipped,
        "duplicates_removed": len(duplicates),
        "duplicates": duplicates,
        "aliases": aliases,
        "encoding": encoding,
        "delimiter": meta.get("delimiter", ""),
        "header": {"name": meta.get("header_name", ""), "smiles": meta.get("header_smiles", "")},
        "notes": all_notes,
    }
    return kept, normalization


# --------------------------------------------------------------------------- #
# 公开 API：分子
# --------------------------------------------------------------------------- #


def normalize_ligand_file(source: str, *, name_prefix: str = "MOL") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """把分子库文件（URL/绝对/相对路径；可 gzip/zip 压缩）归一化为标准分子列表。

    返回 `(molecules, normalization)`；坏记录进 `normalization["skipped"]`，不中断整个库。
    """
    if not source:
        raise FileNotFoundError("分子文件来源为空")
    local = _fetch_file(source, "molec")
    if not os.path.isfile(local):
        raise FileNotFoundError(f"找不到分子文件：{source}")
    local = os.path.abspath(local)
    data = _read_bytes(local)

    notes: List[str] = []
    data, local = _maybe_unpack(data, local, notes)
    fmt = sniff_format(local, data)
    encoding, text = _decode_bytes(data) if fmt in _TEXT_FORMATS else ("", "")
    records, skipped, meta = _parse_by_format(data, text, fmt, local, local, name_prefix, notes)
    return _finalize(local, fmt, records, skipped, meta, encoding, notes)


def normalize_ligand_text(text: str, *, name_prefix: str = "MOL") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """把自由文本（SMILES 列表 / `名称:SMILES` / SDF 块 / CSV 文本）归一化为标准分子列表。

    与 `normalize_ligand_file` 同构；`normalization["source_file"]` 为 `""`。
    """
    raw_text = text or ""
    data = raw_text.encode("utf-8")
    notes: List[str] = []
    fmt = sniff_format("", data)
    encoding, decoded = _decode_bytes(data) if fmt in _TEXT_FORMATS else ("", raw_text)
    records, skipped, meta = _parse_by_format(data, decoded or raw_text, fmt, None, "",
                                              name_prefix, notes)
    # 逗号会让 `sniff_format` 把「名称:SMILES,名称:SMILES」判成 CSV（真实回归：删掉流水线后
    # Agent 路径成了唯一入口，用户这样写就解析出 0 条）。一条都解析不出来时，用行内 SMILES
    # 解析器再试一次；成功则按 smi 归并，失败保持原结论（不掩盖真正的 CSV 解析问题）。
    # 只在**单行**文本上回退：多行说明它很可能真是 CSV/SDF（例如 name,inchikey 表里
    # InChIKey 需要在线反查，CSV 解析可能暂时 0 条），此时回退会改写跳过原因、掩盖真实原因。
    if not records and raw_text.strip() and "\n" not in raw_text.strip():
        parsed = parse_smiles_text(raw_text)
        if parsed:
            notes.append("按 CSV 解析无结果，已按行内 SMILES/名称:SMILES 文本解析")
            fmt = "smi"
            records, skipped, meta = _parse_by_format(data, raw_text, fmt, None, "",
                                                      name_prefix, notes)
    return _finalize("", fmt, records, skipped, meta, encoding, notes)


# --------------------------------------------------------------------------- #
# 公开 API：受体
# --------------------------------------------------------------------------- #


def _reject_receptor(src_name: str, fmt: str, ext: str) -> ValueError:
    guess = _GUESS_CN.get(fmt, f"{fmt} 内容")
    ext_desc = f"扩展名 {ext or '（无）'}"
    if ext in RECEPTOR_EXTS:
        ext_desc += "（像结构文件）"
    elif ext in RECEPTOR_NON_STRUCTURE_EXTS:
        ext_desc += "（不是结构文件扩展名）"
    return ValueError(
        f"无法把 {src_name} 当作受体：内容检测为 {fmt}（{guess}），"
        f"但缺少受体所需的 ATOM/HETATM 坐标记录（mmCIF 则为 data_/loop_ 块）；{ext_desc}。"
        "请提供 .pdb/.ent/.pdb1/.cif/.mmcif 坐标文件或已准备好的 .pdbqt；"
        "如果这其实是小分子库，请改用 normalize_ligand_file。"
    )


def normalize_receptor_source(source: str, *,
                              keep_hetatm: Sequence[str] = (),
                              protonation: Optional[str] = None,
                              ph: Any = None) -> Tuple[str, Dict[str, Any]]:
    """把受体来源（URL/路径；可 gzip/zip 压缩）归一化为可对接的受体文件路径。

    内容优先：扩展名像结构但内容不是结构 → `ValueError`（可执行的说明）；
    扩展名未知但内容确实是结构 → 照常接受。返回 `(受体文件路径, normalization)`。
    """
    if not source:
        raise FileNotFoundError("受体来源为空")
    local = _fetch_file(source, "receptor")
    if not os.path.isfile(local):
        raise FileNotFoundError(f"找不到受体文件：{source}")
    local = os.path.abspath(local)
    src_name = os.path.basename(local)
    data = _read_bytes(local)

    notes: List[str] = []
    data, local = _maybe_unpack(data, local, notes)
    fmt = sniff_format(local, data)
    ext = os.path.splitext(local)[1].lower()
    if fmt not in _STRUCTURE_FORMATS:
        raise _reject_receptor(src_name, fmt, ext)

    spec = read_receptor_file(local, keep_hetatm=keep_hetatm, protonation=protonation, ph=ph)
    path = str(spec.get("pdbqt") or spec.get("receptor_file") or spec.get("path") or "")

    notes.insert(0, f"受体来源：{src_name}（扩展名 {ext or '（无）'}）；内容检测格式：{fmt}")
    if fmt == "cif":
        notes.append("mmCIF 已先转换为 PDB，再按标准流程准备为 PDBQT。")
    notes.append(
        "杂原子处理：剔除水分子 "
        f"{int(spec.get('dropped_waters') or 0)} 个；"
        f"剔除杂原子 {dict(spec.get('dropped_hetatm') or {})}；"
        f"保留杂原子 {dict(spec.get('kept_hetatm') or {})}；"
        f"因缺少化学模板未能保留 {list(spec.get('unsupported_hetatm') or [])}。"
    )

    normalization: Dict[str, Any] = {
        "source_file": local,
        "format": fmt,
        "records_total": 0,
        "records_ok": 0,
        "records_skipped": 0,
        "skipped": [],
        "duplicates_removed": 0,
        "duplicates": [],
        "aliases": {},
        "encoding": "",
        "delimiter": "",
        "header": {"name": "", "smiles": ""},
        "notes": notes,
        # 受体侧附加字段（便于上游直接读取化学溯源）
        "receptor_file": path,
        "pdbqt": path,
        "dropped_hetatm": spec.get("dropped_hetatm") or {},
        "kept_hetatm": spec.get("kept_hetatm") or {},
        "dropped_waters": spec.get("dropped_waters") or 0,
        "unsupported_hetatm": list(spec.get("unsupported_hetatm") or []),
        # 位点与来源信息（上传端点直接回传，避免二次准备）
        "center": spec.get("center"),
        "size": spec.get("size"),
        "protein": spec.get("protein"),
        "site_source": (spec.get("site") or {}).get("source") or "",
        "cocrystal_ligand": spec.get("cocrystal_ligand") or {},
        # 受体质子化（是否与配体同一目标 pH）—— 报告与界面都要显示
        "receptor_protonation": spec.get("receptor_protonation") or {},
    }
    return path, normalization


# --------------------------------------------------------------------------- #
# 归一化溯源落盘：每次输入都并入运行产物 `input_normalization.json`
# --------------------------------------------------------------------------- #
def record_input_normalization(normalization: Dict[str, Any], *, run: Any = None,
                               kind: str = "ligand") -> Optional[str]:
    """把一次输入归一化的摘要并入运行目录的 `input_normalization.json`。

    结构（前端「中间数据」页可直接下载）：
        {"inputs": [ {kind, source_file, format, encoding, delimiter, header,
                      records_total, records_ok, records_skipped,
                      skipped:[{line,reason}], duplicates_removed, duplicates,
                      aliases, notes, ...}, ... ]}

    同一个 `(kind, source_file)` 重复登记时按最新覆盖，避免同一文件出现多条。
    没有运行上下文（CLI/单测）时静默返回 None。
    """
    from docking_agent import run_context

    target = run if run is not None else run_context.active_run_or_none()
    if target is None:
        return None
    entry = dict(normalization or {})
    entry["kind"] = kind
    rel = "input_normalization.json"
    try:
        path = target.path(rel)
        payload: Dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    payload = loaded
            except (OSError, ValueError):
                payload = {}
        inputs = payload.get("inputs")
        if not isinstance(inputs, list):
            inputs = []
        key = (entry.get("kind"), str(entry.get("source_file") or ""))
        inputs = [item for item in inputs
                  if (item.get("kind"), str(item.get("source_file") or "")) != key]
        inputs.append(entry)
        target.write_json("input_normalization", {"inputs": inputs}, rel_path=rel,
                          label="输入归一化（格式 / 编码 / 跳过行 / 去重）")
        return rel
    except Exception as e:  # noqa: BLE001
        logger.warning("input_normalization 落盘失败：%s", e)
        return None
