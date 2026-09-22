"""路由：元信息与轻量计算（健康检查 / 预置受体诊断 / 示例库 / 分子结构图与性质）。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from docking_agent import __version__
from docking_agent.api.support import _depict, _engine_available
from docking_agent.core import DEFAULT_RECEPTOR, RECEPTOR_ALIASES, list_receptors
from docking_agent.core.library import default_library_path, positive_control_info
from docking_agent.runtime.llm import load_llm_config

router = APIRouter()


@router.get("/api/health")
async def api_health() -> Dict[str, Any]:
    from docking_agent.core.docking import machine_profile, plan_concurrency
    from docking_agent.runtime.llm import describe_roles

    cfg = load_llm_config()["config"]
    big = plan_concurrency(10 ** 6)
    return {
        "status": "ok",
        "version": __version__,
        "llm_configured": bool(cfg.get("api_key")),
        "model": cfg.get("model"),
        "base_url": cfg.get("base_url"),
        "engine_available": _engine_available(),
        # 本机自动识别的算力与据此得出的对接并发（容器配额/亲和性都算在内），
        # 便于部署后一眼确认「这台机器会用几个进程 × 几个线程」
        "machine": machine_profile(),
        "docking_plan": {"large_library": {"workers": big["workers"],
                                           "threads": big["threads"]},
                         "single_molecule": {k: v for k, v in
                                             plan_concurrency(1).items()
                                             if k in ("workers", "threads")}},
        # 每个 Agent 角色最终生效的模型配置（可不同模型/端点）
        "roles": describe_roles(),
    }


@router.get("/api/receptors")
async def api_receptors() -> Dict[str, Any]:
    """**内部/诊断端点**：列出注册表里的预置受体（仅内部测试夹具，不面向用户）。

    用户界面**不再**调用它，也不再把它当作可选受体来源；用户只能通过
    PDB 编号 / UniProt accession / 基因或蛋白名称 / 上传结构文件指定受体。
    """
    return {
        "internal": True,
        "default": DEFAULT_RECEPTOR,
        "aliases": RECEPTOR_ALIASES,
        "receptors": list_receptors(),
    }


@router.get("/api/libraries")
async def api_libraries() -> Dict[str, Any]:
    from docking_agent.core import load_library_file

    libs: List[Dict[str, Any]] = []
    path = default_library_path()
    if path.exists():
        try:
            mols = load_library_file(str(path))
        except Exception:  # noqa: BLE001
            mols = []
        libs.append({"id": "example", "name": "示例分子库",
                     "path": str(path.relative_to(path.parents[2])), "count": len(mols),
                     "molecules": mols})
    return {"libraries": libs, "positive_control": positive_control_info()}


@router.get("/api/molecule/depict")
async def api_depict(smiles: str = Query(...), width: int = 320, height: int = 240) -> Response:
    """RDKit 二维结构图（PNG）。"""
    try:
        png = await asyncio.to_thread(_depict, smiles, width, height)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"无法绘制该 SMILES: {e}")
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/api/molecule/properties")
async def api_properties(smiles: str = Query(...)) -> Dict[str, Any]:
    from docking_agent.core import compute_properties

    try:
        return await asyncio.to_thread(compute_properties, smiles)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(e))
