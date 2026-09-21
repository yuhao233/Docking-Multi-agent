"""文件获取：URL 下载与本地路径解析。

用户提供的分子/受体文件可能以「本地绝对路径、相对路径、URL」三种形式传入，
这里统一归一化为可读的本地绝对路径。
"""
from __future__ import annotations

import logging
import os
import urllib.request
from pathlib import Path

from docking_agent.paths import cache_dir, uploads_dir, workspace_dir

logger = logging.getLogger(__name__)


def _fetch_file(source: str, hint: str = "file") -> str:
    """把传入的文件来源归一化为本地路径。

    - http(s) URL：下载到 assets/cache 下（同名文件已存在且非空则复用）
    - 绝对路径：原样返回
    - 相对路径：依次在 cwd、工作区根、assets/、assets/uploads/、assets/cache/ 下查找
    """
    if not source:
        raise FileNotFoundError("文件来源为空")

    if source.startswith(("http://", "https://")):
        base = os.path.basename(source.split("?")[0]) or f"{hint}.bin"
        local = cache_dir() / base
        if not local.exists() or local.stat().st_size == 0:
            logger.info("下载文件 %s -> %s", source, local)
            urllib.request.urlretrieve(source, str(local))
        return str(local)

    if os.path.isabs(source):
        return source

    candidates = [
        Path(source),
        workspace_dir() / source,
        workspace_dir() / "assets" / source,
        uploads_dir() / source,
        cache_dir() / Path(source).name,
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return source


def slug(text: str) -> str:
    """生成可安全用于文件名的标识（ASCII 名称规范化；非 ASCII 名称附短哈希避免重名）。

    放在 core 层是为了让对接 worker 与运行产物命名保持一致，同时不引入对上层模块的依赖。
    """
    import hashlib
    import re

    raw = (text or "ligand").strip() or "ligand"
    ascii_part = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_.")
    if len(ascii_part) >= 2:
        return ascii_part[:60]
    return "ligand_" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:6]
