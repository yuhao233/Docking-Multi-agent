"""路由：运行记录检索、结果分页、产物下载（单个产物 / CSV / PDF / 数据包 / 位姿包）。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response

from docking_agent.api.support import (
    _artifact_filename,
    _build_run_pdf,
    _run_download_names,
)
from docking_agent.reporting import content_type_for
from docking_agent.runs import download_names, get_run_store

router = APIRouter()


@router.get("/api/runs")
async def api_runs(limit: int = 20, offset: int = 0, q: str = "", status: str = "",
                   kind: str = "", receptor: str = "", since: str = "",
                   until: str = "") -> Dict[str, Any]:
    """历史运行列表 / 检索。

    无检索参数时行为与以前一致（`limit` 条、按时间倒序）；带 `q`/`status`/`kind`/
    `receptor`/`since`/`until` 时返回 `{runs, total, offset, limit, query}`，
    `q` 会匹配 run_id、受体、状态、任务描述与排序表里的分子名/ID
    （空格分隔多词 = AND），从而支持"关掉页面后再找回之前的运行结果"。
    """
    store = get_run_store()
    if not any(str(x or "").strip() for x in (q, status, kind, receptor, since, until)):
        return {"runs": store.list(limit=max(1, min(limit, 200)))}
    return await asyncio.to_thread(
        store.search, q=q, status=status, kind=kind, receptor=receptor,
        since=since, until=until, offset=max(0, offset), limit=max(1, min(limit, 200)))


@router.delete("/api/runs/{run_id}")
async def api_run_delete(run_id: str) -> Any:
    """删除一条运行记录（含其产物目录）。

    门禁脚本用它清理**自己创建**的运行记录（每跑一次 ui_e2e / browser_check 会真实创建
    十几条，长期会挤满历史列表）；用户也可用它清理不再需要的运行。删除不可撤销。
    同源校验由全局中间件负责（带 Origin 且跨站的 DELETE 会被拒）。
    """
    try:
        ok = await asyncio.to_thread(get_run_store().delete, run_id)
    except ValueError as exc:
        return JSONResponse(status_code=400,
                            content={"status": "error", "error_message": str(exc)})
    if not ok:
        return JSONResponse(status_code=404,
                            content={"status": "error", "error_message": "运行记录不存在"})
    return {"status": "ok", "run_id": run_id}


@router.get("/api/runs/{run_id}")
async def api_run_detail(run_id: str) -> Dict[str, Any]:
    detail = get_run_store().detail(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
    return detail


@router.get("/api/runs/{run_id}/artifacts/{name}")
async def api_run_artifact(run_id: str, name: str, inline: int = 0) -> FileResponse:
    path = get_run_store().artifact_path(run_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"产物不存在: {name}")
    filename = _artifact_filename(run_id, name, path.name)
    disposition = "inline" if inline else "attachment"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    return FileResponse(str(path), media_type=content_type_for(path), headers=headers)


@router.get("/api/runs/{run_id}/ranking")
async def api_run_ranking(run_id: str, offset: int = 0, limit: int = 100,
                          sort: str = "affinity_kcal_mol", order: str = "asc",
                          q: str = "", hits_only: bool = False) -> Dict[str, Any]:
    """结果分页：服务端排序/搜索/「只看优于对照」，供大库场景的表格与卡片使用。"""
    store = get_run_store()
    if not store.exists(run_id):
        raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
    return await asyncio.to_thread(
        store.ranking_page, run_id,
        offset=offset, limit=limit, sort=sort, order=order, q=q, hits_only=hits_only)


@router.get("/api/runs/{run_id}/export.csv")
async def api_run_export_csv(run_id: str) -> Response:
    """导出完整排序 CSV（不受分页限制）。产物缺失时由 ranking.json 现场生成。"""
    store = get_run_store()
    if not store.exists(run_id):
        raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
    filename = _run_download_names(run_id)["ranking_csv"]
    path = store.artifact_path(run_id, "ranking_csv")
    if path is not None:
        return FileResponse(str(path), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    rows = await asyncio.to_thread(store.ranking_rows, run_id)
    if not rows:
        raise HTTPException(status_code=404, detail="该运行没有排序结果")
    from docking_agent.reporting import build_ranking_csv

    pc_affinity = await asyncio.to_thread(store.positive_control_affinity, run_id)
    pos_control = ({"name": "阳性对照", "affinity_kcal_mol": pc_affinity}
                   if pc_affinity is not None else {})
    csv_text = build_ranking_csv(rows, pos_control)
    return Response(content=csv_text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/api/runs/{run_id}/report.pdf")
async def api_run_report_pdf(run_id: str) -> Any:
    """下载 PDF 版报告：已有产物直接返回，缺失时现场生成（在线程中渲染，不阻塞事件循环）。"""
    store = get_run_store()
    meta = store.meta(run_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
    filename = download_names(run_id, meta)["report_pdf"]
    path = store.artifact_path(run_id, "report_pdf")
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if path is not None:
        return FileResponse(str(path), media_type="application/pdf", headers=headers)
    content = await asyncio.to_thread(_build_run_pdf, run_id)
    if content is None:
        raise HTTPException(status_code=404,
                            detail="该运行没有 report.md，无法生成 PDF 报告"
                                   "（请确认运行已完成，或改用 Markdown 报告）")
    return Response(content=content, media_type="application/pdf", headers=headers)


@router.get("/api/runs/{run_id}/download.zip")
async def api_run_zip(run_id: str) -> FileResponse:
    store = get_run_store()
    if not store.exists(run_id):
        raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
    path = await asyncio.to_thread(store.zip, run_id)
    if path is None:
        raise HTTPException(status_code=404, detail="该运行暂无可打包的数据")
    filename = _run_download_names(run_id)["data_zip"]
    return FileResponse(str(path), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/api/runs/{run_id}/poses.zip")
async def api_run_poses_zip(run_id: str) -> FileResponse:
    store = get_run_store()
    if not store.exists(run_id):
        raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
    path = await asyncio.to_thread(lambda: store.zip(run_id, only_poses=True))
    if path is None:
        raise HTTPException(status_code=404, detail="该运行没有位姿文件（可在配置中开启“保存位姿”）")
    filename = _run_download_names(run_id)["poses_zip"]
    return FileResponse(str(path), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
