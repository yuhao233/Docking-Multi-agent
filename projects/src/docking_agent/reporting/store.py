"""产物存储：`var/outputs/` 扁平产物目录 + 内容类型推断。

运行级产物（含产物清单与打包下载）由 `docking_agent.runs.RunStore` 管理；
这里保留扁平目录用于兼容旧接口 `GET /files/{key}` 与简单的单文件产物。
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Dict, Optional
from uuid import uuid4

from docking_agent.config import env
from docking_agent.paths import outputs_dir

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

CONTENT_TYPES = {
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".pdbqt": "chemical/x-pdbqt",
    ".pdb": "chemical/x-pdb",
    ".ent": "chemical/x-pdb",
    ".cif": "chemical/x-cif",
    ".mmcif": "chemical/x-cif",
    ".mol2": "chemical/x-mol2",
    ".sdf": "chemical/x-mdl-sdfile",
    ".zip": "application/zip",
}


def safe_name(file_name: str) -> str:
    p = Path(file_name or "artifact")
    stem = _SAFE.sub("_", p.stem) or "artifact"
    suffix = _SAFE.sub("", p.suffix)[:16]
    return f"{stem}{suffix}"


def content_type_for(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def artifact_base_url() -> str:
    """产物下载地址前缀。默认指向本机 HTTP 服务，可用 ARTIFACT_BASE_URL 覆盖。"""
    explicit = env("ARTIFACT_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    host = env("ARTIFACT_HOST", "127.0.0.1")
    port = env("PORT") or env("DEPLOY_RUN_PORT") or "5000"
    return f"http://{host}:{port}"


def save_artifact(data: bytes, file_name: str, content_type: str = "application/octet-stream") -> Dict[str, str]:
    """保存到 `var/outputs/`，返回 {"key","path","url","content_type"}。"""
    safe = safe_name(file_name)
    key = f"{Path(safe).stem}_{uuid4().hex[:8]}{Path(safe).suffix}"
    path = outputs_dir() / key
    path.write_bytes(data)
    return {
        "key": key,
        "path": str(path),
        "url": f"{artifact_base_url()}/files/{key}",
        "content_type": content_type or content_type_for(path),
    }


def resolve_output(key: str) -> Optional[Path]:
    """按 key 定位扁平产物；拒绝路径穿越。"""
    if not key or "/" in key or "\\" in key or ".." in key:
        return None
    path = outputs_dir() / key
    return path if path.is_file() else None
