#!/usr/bin/env python
"""生成物溯源戳：把「PDF/DOCX 产物 ← Markdown 源」的内容哈希写进 sidecar。

**为什么需要**：本仓同时维护 Markdown 源与由脚本生成的 PDF/DOCX 产物，而
`git` 不会在源变更时提醒你重出产物。2026-09-21 的实测事故：`技术报告.md` 改了
10 小时，`.docx` / `.pdf` 还是旧的 —— 按现状提交就会把**内容陈旧**的产物一起发出去，
而 PDF 恰恰是评审最可能读的形态。

**为什么不用 mtime**：干净克隆、CI 检出、`git checkout` 都会重写 mtime，
按时间判断既会误报也会漏报。因此这里记录**源文件内容的 sha256**，
一致性测试只比哈希（见 `tests/test_docs_consistency.py`）。

用法（在各生成器末尾调用）::

    from doc_stamp import write_stamp
    write_stamp(source.parent / ".generated.json", source, [docx_path, pdf_path])
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


def sha256_of(path: Path) -> str:
    """按块计算文件内容 sha256（大文件也不吃内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_stamp(stamp_path: Path, source: Path, outputs: Iterable[Path]) -> None:
    """把「产物名 → {source, sha256}」合并写进 sidecar（保留同目录其它生成器的条目）。"""
    data: Dict[str, Any] = {}
    if stamp_path.is_file():
        try:
            loaded = json.loads(stamp_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = {}
    entries = data.setdefault("artifacts", {})
    if not isinstance(entries, dict):  # 手工改坏时重建，不让生成器崩掉
        entries = {}
        data["artifacts"] = entries
    digest = sha256_of(source)
    for out in outputs:
        if out is None:
            continue
        entries[out.name] = {"source": source.name, "sha256": digest}
        print(f"   [stamp] {out.name} ← {source.name}@{digest[:12]}")
    stamp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")


def read_stamp(stamp_path: Path) -> List[Tuple[str, str, str]]:
    """读回 sidecar → [(产物名, 源名, 源 sha256)]；文件缺失/损坏时返回空列表。"""
    if not stamp_path.is_file():
        return []
    try:
        data = json.loads(stamp_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    entries = (data or {}).get("artifacts") or {}
    out: List[Tuple[str, str, str]] = []
    for name, info in sorted(entries.items()):
        if isinstance(info, dict) and info.get("source") and info.get("sha256"):
            out.append((str(name), str(info["source"]), str(info["sha256"])))
    return out
