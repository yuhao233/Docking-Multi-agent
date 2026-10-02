"""分子文件来源到真实本地路径的解析，作为 `tools/` 内的公共底层。

这几个函数原先是 `agents/dispatch.py` 的一部分，`tools/docking.py` 与 `tools/properties.py`
也需要它们，两个子 Agent 工具因此在函数体内反向 import 协调层工具 `agents.dispatch`，
形成 `agents.workers ⇄ agents.dispatch` 依赖环（结构分析条目 SCC-3）。抽到本模块后依赖方向
为单向下行：

    agents.workers 依赖 tools.{docking,properties,dispatch}，后者依赖 tools.molecule_paths

背景：上传端点的落盘名带时间戳前缀
（`20260917-112109-3e5739-PGR.sdf`），而模型通常只拿到显示名 `PGR.sdf`，
于是把裸文件名当路径传给工具，RDKit 报 `Bad input file PGR.sdf`，最终静默退回示例库。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple

from docking_agent.paths import cache_dir, libraries_dir, uploads_dir, workspace_dir

# 分子库文件后缀（用于区分路径与 SMILES 文本）
_MOLECULE_FILE_EXTS = (".sdf", ".sd", ".smi", ".smiles", ".csv", ".tsv", ".mol2", ".mol",
                       ".txt", ".json")   # `.json`：运行产物（`molecules_tool.json`）按文件交接


def looks_like_molecule_path(value: str) -> bool:
    """启发式判断参数值是分子文件路径还是 SMILES 或名称文本。

    原实现会按纯文本解析模型塞进参数的上传文件显示名（`PGR.sdf`），结果为空。
    规则为任一命中即算路径：
      - http(s) URL；
      - 字符串本身是存在的本地文件；
      - 扩展名属于分子文件格式（SMILES 几乎不以 .sdf/.smi/.csv/.mol2 结尾）。
    """
    text = str(value or "").strip()
    if not text:
        return False
    if text.startswith(("http://", "https://")):
        return True
    try:
        if os.path.isfile(text):
            return True
    except (OSError, ValueError):  # 超长字符串或含 NUL：不是路径
        return False
    ext = os.path.splitext(text.split("?")[0])[1].lower()
    return ext in _MOLECULE_FILE_EXTS


def molecule_search_dirs() -> List[Path]:
    """解析分子文件时按序查找的目录：`cwd`、工作区、`assets`、`uploads`、`cache`、示例库。"""
    candidates = [Path.cwd(), workspace_dir(), workspace_dir() / "assets",
                  uploads_dir(), cache_dir(), libraries_dir()]
    out: List[Path] = []
    seen = set()
    for path in candidates:
        try:
            key = str(path.resolve())
        except OSError:
            continue
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def resolve_molecule_file(value: str) -> Tuple[str, List[str], List[str]]:
    """把模型或调用方给出的分子文件来源解析成实际存在的本地路径。

    用途：上传端点的落盘名带时间戳前缀（`20260917-112109-3e5739-PGR.sdf`），
    而模型通常只拿到显示名 `PGR.sdf`，于是把裸文件名当路径传给工具，
    RDKit 报 `Bad input file PGR.sdf`，最终静默退回示例库。
    本函数按顺序匹配绝对或相对路径、各候选目录下的精确文件名、`<前缀>-<原名>` 后缀匹配，
    并返回每一步的候选与原因，供上层如实上报。

    返回 `(resolved, attempts, candidates)`：
      - 唯一命中：`resolved` 为本地绝对路径（或 URL），`candidates` 为空；
      - 命中多个不同文件（歧义）：`resolved` 原样返回且不做猜测，`candidates` 为候选绝对路径清单，
        由上层决定报错或向调用方提问；
      - 未命中：`resolved` 原样返回，`candidates` 为空，`attempts` 逐条记录失败原因。
    """
    text = str(value or "").strip()
    attempts: List[str] = []
    if not text:
        return text, attempts, []
    if text.startswith(("http://", "https://")):
        return text, attempts, []
    if os.path.isfile(text):
        return os.path.abspath(text), attempts, []

    name = os.path.basename(text.replace("\\", "/"))
    stem = os.path.splitext(name)[0]
    target = name.lower()
    target_stem = stem.lower()
    dirs = molecule_search_dirs()
    empty: List[str] = []

    # ① 候选目录下的精确文件名（相对路径 `assets/uploads/x.sdf` 也在此命中）
    exact_hits: List[Path] = []
    for directory in dirs:
        candidate = directory / name
        try:
            if candidate.is_file():
                exact_hits.append(candidate)
                continue
        except OSError:  # 允许静默：候选路径不可访问时按不存在处理
            pass
        attempts.append(f"{candidate}：不存在")
    hits = _unique_paths(exact_hits)
    if len(hits) == 1:
        return str(hits[0]), attempts, []
    if len(hits) > 1:
        attempts.append(f"{name}：在多个目录命中同名文件，无法确定用哪一个")
        return text, attempts, [str(p) for p in hits]

    # ② 后缀匹配：上传文件形如 `<时间戳>-<哈希>-<原名>`，以原名结尾即可命中；
    #    无扩展名时按词干匹配（`PGR` 命中 `<...>-PGR.sdf`）。
    suffix_hits: List[Path] = []
    for directory in dirs:
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file():
                continue
            lowered = entry.name.lower()
            ext = os.path.splitext(lowered)[1]
            if lowered == target or lowered.endswith(target):
                suffix_hits.append(entry)
            elif target_stem and ext in _MOLECULE_FILE_EXTS \
                    and os.path.splitext(lowered)[0].endswith(target_stem):
                suffix_hits.append(entry)
    hits = _unique_paths(suffix_hits)
    if len(hits) == 1:
        return str(hits[0]), attempts, []
    if len(hits) > 1:
        attempts.append(f"{name}：后缀匹配到 {len(hits)} 个候选文件，无法确定用哪一个")
        return text, attempts, [str(p) for p in hits]

    attempts.append(f"{name}：在上传/缓存目录中未找到同名或 `<前缀>-{name}` 形式的文件")
    return text, attempts, empty


def _unique_paths(paths: List[Path]) -> List[Path]:
    """按解析后的绝对路径去重并保持顺序（同一文件经不同相对路径命中只计一次）。"""
    out: List[Path] = []
    seen = set()
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out
