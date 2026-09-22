"""路由：文件上传（只落盘）与「校验文件」（用户主动触发，才做解析/受体准备）。"""
from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from docking_agent.api.support import (
    _ligand_upload_payload,
    _receptor_upload_payload,
    _redact,
)
from docking_agent.config import env_int
from docking_agent.runs import ensure_inside

router = APIRouter()


@router.post("/api/uploads")
async def api_upload(file: UploadFile = File(...), kind: str = Form(default="auto")) -> Dict[str, Any]:
    """**只保存文件，不解析、不做任何准备**（用户要求：开始运行时才处理）。

    返回 `path` 可直接作为 `molecule_file` / `receptor_file` 传给运行接口；
    想在上传后先看一眼解析结果/位点盒，调用 `POST /api/uploads/inspect`（用户主动触发）。
    """
    from docking_agent.core.normalize import RECEPTOR_EXTS, sniff_format
    from docking_agent.paths import project_root, uploads_dir

    name = Path(file.filename or "upload").name
    ext = Path(name).suffix.lower()
    max_mb = env_int("UPLOAD_MAX_MB", 200)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传内容为空")
    if len(data) > max_mb * 1024 * 1024:
        raise HTTPException(status_code=413,
                            detail=f"文件过大（> {max_mb} MB），可通过 UPLOAD_MAX_MB 调整上限")

    target_dir = uploads_dir()
    safe = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", name) or "upload"
    key = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}-{safe}"
    dest = target_dir / key
    dest.write_bytes(data)

    requested = (kind or "auto").strip().lower()
    if requested == "ligand":
        is_ligand, is_receptor = True, False
    elif requested == "receptor":
        is_ligand, is_receptor = False, True
    elif ext in RECEPTOR_EXTS:
        is_ligand, is_receptor = False, True
    else:
        # 扩展名不认识时**按内容嗅探**（只读文件头，代价极小）：`.dat` 里装着 PDB 也算受体
        sniffed = sniff_format(str(dest), data)
        is_ligand, is_receptor = (False, True) if sniffed in ("pdb", "cif", "pdbqt") else (True, False)

    kind_name = "receptor" if is_receptor else "ligand"
    return {
        "status": "ok", "kind": kind_name, "file_name": name, "path": str(dest),
        "relative_path": str(dest.relative_to(project_root())),
        "size": len(data), "ext": ext, "pending": True,
        "message": ("文件已保存到服务端（尚未解析/准备）。"
                    + ("受体会在**开始运行时**现场准备为 PDBQT 并按目标 pH 处理"
                       if is_receptor else
                       "分子库会在**开始运行时**解析")
                    + "；需要先看解析结果可调用「校验文件」。"),
    }


@router.post("/api/uploads/inspect")
async def api_upload_inspect(payload: Dict[str, Any]) -> Dict[str, Any]:
    """**用户主动**校验已上传文件：解析分子数 / 现场准备受体并给出位点盒与化学溯源。

    与运行阶段共用同一套解析与准备实现（`_receptor_upload_payload` /
    `_ligand_upload_payload`），因此预览结果与真正运行时一致，不会出现「预览说 30 个、
    实际跑了 29 个」这种偏差。
    """
    from docking_agent.paths import uploads_dir

    raw_path = str((payload or {}).get("path") or "").strip()
    kind = str((payload or {}).get("kind") or "auto").strip().lower()
    if not raw_path:
        raise HTTPException(status_code=400, detail="缺少 path")
    dest = Path(raw_path)
    if not dest.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在或不可读：{raw_path}")
    # **只允许工作区内的上传文件**：本端点的语义是「校验**已上传**的文件」，
    # 而 `path` 来自请求体。不设限时它是一个**任意文件读取**原语：解析器会把内容
    # 读进内存，解析失败的信息还会经错误体回显（真实漏洞：.env 会被逐行读出）。
    try:
        dest = ensure_inside(uploads_dir(), dest, field="path")
    except ValueError as e:
        raise HTTPException(
            status_code=403,
            detail=("只允许校验本次上传的文件（位于 assets/uploads 之下）；"
                    f"拒绝该路径：{_redact(str(e), limit=120)}")) from e
    if not dest.is_file():
        raise HTTPException(status_code=404, detail="文件不存在或不可读")

    from docking_agent.core.normalize import RECEPTOR_EXTS, sniff_format

    if kind == "auto":
        ext = dest.suffix.lower()
        if ext in RECEPTOR_EXTS:
            kind = "receptor"
        else:
            kind = "receptor" if sniff_format(str(dest), dest.read_bytes()[:4096]) in (
                "pdb", "cif", "pdbqt") else "ligand"
    try:
        if kind == "receptor":
            return await asyncio.to_thread(_receptor_upload_payload, dest, dest.name)
        return await asyncio.to_thread(_ligand_upload_payload, dest, dest.name)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        label = "受体文件准备失败" if kind == "receptor" else "小分子文件解析失败"
        supported = "/".join(sorted(RECEPTOR_EXTS)) if kind == "receptor" else \
            "SDF/SMI/SMILES/CSV/TSV/MOL2/MOL（可 gzip/zip 压缩）"
        # 解析器的异常里可能带文件片段（例如「第 3 行 …」）→ 对外必须脱敏
        raise HTTPException(status_code=400,
                            detail=f"{label}：{_redact(str(e))}（支持 {supported}）")
