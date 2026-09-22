"""分子文件「来源 → 真实本地路径」的解析（`tools/` 内的公共底层）。

为什么单独成模块：这几个函数原先是 `agents/dispatch.py` 的一部分，而
`tools/docking.py` / `tools/properties.py` 也各自需要它们 —— 于是两个子 Agent 工具
**函数内**反向 import 协调层工具 `agents.dispatch`，把 `agents.workers ⇄ agents.dispatch`
依赖环糊在函数体里（审计 SCC-3）。抽到本模块后，依赖方向变成纯下行：

    agents.workers → tools.{docking,properties,dispatch} → tools.molecule_paths

真实缺陷背景（20260917-112206-5017）：上传端点的落盘名带时间戳前缀
（`20260917-112109-3e5739-PGR.sdf`），而模型往往只拿到显示名 `PGR.sdf`，
于是把裸文件名当路径传给工具，RDKit 报 `Bad input file PGR.sdf`，最终静默退回示例库。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Tuple

from docking_agent.paths import cache_dir, libraries_dir, uploads_dir, workspace_dir

# 分子库文件后缀（用于把「路径」与「SMILES 文本」区分开）
_MOLECULE_FILE_EXTS = (".sdf", ".sd", ".smi", ".smiles", ".csv", ".tsv", ".mol2", ".mol",
                       ".txt", ".json")   # .json：运行产物（molecules_tool.json）按文件交接


def looks_like_molecule_path(value: str) -> bool:
    """启发式判断某个参数值是不是「分子文件路径」而不是 SMILES/名称文本。

    真实缺陷 20260917-112206-5017：模型把上传文件的显示名（`PGR.sdf`）塞进参数，
    工具却按纯文本解析，什么都没得到。规则（任一命中即算路径）：
      - http(s) URL；
      - 字符串本身是存在的本地文件；
      - 扩展名属于分子文件格式（SMILES 几乎不会以 .sdf/.smi/.csv/.mol2 结尾）。
    """
    text = str(value or "").strip()
    if not text:
        return False
    if text.startswith(("http://", "https://")):
        return True
    try:
        if os.path.isfile(text):
            return True
    except (OSError, ValueError):  # 超长字符串 / 含 NUL：一定不是路径
        return False
    ext = os.path.splitext(text.split("?")[0])[1].lower()
    return ext in _MOLECULE_FILE_EXTS


def molecule_search_dirs() -> List[Path]:
    """解析分子文件时按序查找的目录（cwd → 工作区 → assets → uploads → cache → 示例库）。"""
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
    """把模型/用户给出的分子文件来源解析成**真实存在的本地路径**。

    为什么需要：上传端点的落盘名带时间戳前缀（`20260917-112109-3e5739-PGR.sdf`），
    而模型往往只拿到显示名 `PGR.sdf`，于是把裸文件名当路径传给工具，
    RDKit 报 `Bad input file PGR.sdf`，最终静默退回示例库（真实缺陷 20260917-112206-5017）。
    这里按「绝对/相对 → 各候选目录精确文件名 → `<前缀>-<原名>` 后缀匹配」逐级解析，
    并把每次尝试的候选与原因一并返回，便于如实上报而不是让模型瞎猜。

    返回 `(resolved, attempts, candidates)`：
      - 唯一命中：`resolved` = 本地绝对路径（或 URL），`candidates` 为空；
      - 命中多个不同文件（歧义）：`resolved` 原样返回、**不猜**，`candidates` 为候选绝对路径清单，
        由上层决定报错/向用户提问；
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

    # ① 候选目录下的精确文件名（相对路径 `assets/uploads/x.sdf` 也会在此命中）
    exact_hits: List[Path] = []
    for directory in dirs:
        candidate = directory / name
        try:
            if candidate.is_file():
                exact_hits.append(candidate)
                continue
        except OSError:  # 允许静默：候选路径不可访问等同于不存在
            pass
        attempts.append(f"{candidate}：不存在")
    hits = _unique_paths(exact_hits)
    if len(hits) == 1:
        return str(hits[0]), attempts, []
    if len(hits) > 1:
        attempts.append(f"{name}：在多个目录命中同名文件，无法确定用哪一个")
        return text, attempts, [str(p) for p in hits]

    # ② 后缀匹配：上传文件形如 `<时间戳>-<哈希>-<原名>`，用原名结尾即可命中；
    #    无扩展名时按词干匹配（`PGR` → `<...>-PGR.sdf`）。
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
    """按解析后的绝对路径去重并保持顺序（同一文件经不同相对路径命中只算一次）。"""
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
