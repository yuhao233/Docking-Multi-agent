"""路由：设置页（字段读写 / 端点模型列表 / 外部工具探测 / 角色连通性测试）与 Agent 重载。"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from docking_agent.api.support import _agent_model_map, _mask_secret, _redact, state

logger = logging.getLogger(__name__)

router = APIRouter()


def _llm_settings_view() -> Dict[str, Any]:
    """LLM 相关字段的当前值 / 生效值 / 来源。"""
    from docking_agent import settings as S
    from docking_agent.runtime.llm import resolve_role_config

    local = S.load_local_settings()
    local_llm = dict(local.get("llm") or {})
    values: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    effective: Dict[str, Any] = {}
    secret: Dict[str, Any] = {}

    global_cfg, global_src = resolve_role_config("")
    for spec in S.LLM_SPECS:
        field = spec.path.split(".", 1)[1]
        raw = local_llm.get(field)
        if field == "extra_headers" and isinstance(raw, dict):
            raw = S.mask_headers(raw)   # 敏感请求头只回显 ***
        values[spec.path] = None if spec.kind == "secret" else raw
        sources[spec.path] = ("界面设置" if raw not in (None, "")
                              else global_src.get(field, "内置默认"))
        if spec.kind == "secret":
            effective[spec.path] = ""
            secret["api_key_set"] = bool(global_cfg.get("api_key"))
            secret["api_key_hint"] = _mask_secret(global_cfg.get("api_key"))
        elif field == "extra_headers":
            # 生效值同样要掩码：否则「隐藏的明文」会从 effective 泄露出去
            effective[spec.path] = S.mask_headers(global_cfg.get(field) or {})
        else:
            effective[spec.path] = global_cfg.get(field)

    role_cfgs = {role: resolve_role_config(role) for role in S.ROLES}
    for spec in S.SPECS:
        if spec.group != "roles":
            continue
        local_role = (local.get("roles") or {}).get(spec.role) or {}
        raw = local_role.get(spec.field) if isinstance(local_role, dict) else None
        cfg, src = role_cfgs[spec.role]
        values[spec.path] = None if spec.kind == "secret" else raw
        sources[spec.path] = ("界面设置" if raw not in (None, "")
                              else src.get(spec.field, "内置默认"))
        if spec.kind == "secret":
            effective[spec.path] = ""
            secret[f"{spec.role}_api_key_set"] = bool(cfg.get("api_key"))
        else:
            effective[spec.path] = cfg.get(spec.field)
    return {"values": values, "sources": sources, "effective": effective, "secret": secret}


def _settings_payload() -> Dict[str, Any]:
    """设置页面的完整数据：字段规格 + 当前值 + 生效值 + 来源 + 元信息。"""
    from docking_agent import settings as S

    view = _llm_settings_view()
    values = dict(view["values"])
    sources = dict(view["sources"])
    effective = dict(view["effective"])
    stored_all = S.load_local_settings()

    for spec in S.DOCKING_SPECS:
        stored = S.get_dotted(stored_all, spec.path)
        values[spec.path] = stored
        effective[spec.path] = stored if stored is not None else spec.default
        sources[spec.path] = "界面设置" if stored is not None else "内置默认"
    for spec in S.EXTERNAL_SPECS + S.RUNTIME_SPECS + S.DEPLOY_SPECS:
        stored = S.get_dotted(stored_all, spec.path)
        values[spec.path] = stored
        effective[spec.path] = S.runtime_effective(spec)
        sources[spec.path] = S.runtime_source(spec)

    return {
        "specs": [S.spec_to_dict(s) for s in S.SPECS],
        "groups": [dict(g) for g in S.GROUPS],
        "roles": list(S.ROLES),
        "role_labels": dict(S.ROLE_LABEL),
        "values": values,
        "effective": effective,
        "sources": sources,
        "secret": view["secret"],
        "meta": {**S.settings_meta(),
                 "needs_restart": sorted({s.env for s in S.SPECS
                                          if s.needs_restart and s.env})},
        "agent_models": _agent_model_map(),
    }


def _fetch_endpoint_models(timeout: float = 15.0) -> Dict[str, Any]:
    """从当前端点拉取可用模型列表（OpenAI 兼容 GET /models）。"""
    import urllib.error
    import urllib.request

    from docking_agent.runtime.llm import effective_config

    cfg = effective_config("")
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key")
    if not base_url or not api_key:
        return {"status": "error", "error": "尚未配置 base_url / api_key", "models": []}
    request = urllib.request.Request(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            detail = ""
        return {"status": "error", "error": _redact(f"HTTP {e.code}：{detail or e.reason}"),
                "models": []}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": _redact(f"{type(e).__name__}: {e}"), "models": []}

    raw_items = data.get("data") if isinstance(data, dict) else None
    models: List[str] = []
    for item in raw_items or []:
        if isinstance(item, dict) and item.get("id"):
            models.append(str(item["id"]))
        elif isinstance(item, str):
            models.append(item)
    # 不回显上游 base_url：本端点无鉴权，端点指纹属于不必要的信息暴露
    # （用户自己的端点值在「设置」页可见，不需要这里再确认一次）。
    return {"status": "ok", "models": sorted(set(models))}


def _test_role_llm(role: str) -> Dict[str, Any]:
    """用某角色自己的配置发一次极小请求，回报服务端确认的模型与延迟。"""
    import time as _time

    from docking_agent.runtime.llm import (build_chat_llm, llm_registry,
                                           registry_entry, restore_registry_entry)

    if role not in ("intake", "coordinator", "property", "pocket", "docking", "binding"):
        return {"status": "error", "role": role, "error": "未知角色"}
    started = _time.time()
    # 只临时接管该角色这一个条目，测试结束后原样恢复：
    # 不能清空整张登记表，否则会破坏运行记录/报告里的「各 Agent 模型」溯源。
    saved = registry_entry(role)
    try:
        llm = build_chat_llm(None, role=role)
        reply = llm.invoke("只回复两个字：就绪")
        text = getattr(reply, "content", "") or ""
        meta = getattr(reply, "response_metadata", {}) or {}
        entry = llm_registry().get(role, {})
        return {
            "status": "ok",
            "role": role,
            "model": entry.get("model"),
            "actual_model": meta.get("model_name") or meta.get("model") or "",
            "latency_ms": round((_time.time() - started) * 1000),
            "sample": str(text)[:80],
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "role": role,
                "error": _redact(f"{type(e).__name__}: {e}", 300),
                "latency_ms": round((_time.time() - started) * 1000)}
    finally:
        # 还原该角色的历史登记（测试用的临时实例不应污染溯源信息）
        restore_registry_entry(role, saved)


def _reload_agents() -> Dict[str, Any]:
    """丢弃已构建的 Agent 与模型实例，使设置改动在下一次运行生效。"""
    from docking_agent.agents import workers
    from docking_agent.runtime.llm import reset_llm_registry

    state.graph = None
    workers.reset_workers()
    reset_llm_registry()
    from docking_agent.config import ensure_runtime_env

    ensure_runtime_env()
    logger.info("已重载 Agent 与模型实例（设置改动生效）")
    return {"status": "ok", "reloaded": True, "agent_models": _agent_model_map()}


@router.get("/api/settings")
async def api_settings_get() -> Dict[str, Any]:
    """设置页面的全部字段、当前值、生效值与来源。"""

    return await asyncio.to_thread(lambda: _settings_payload())


@router.put("/api/settings")
async def api_settings_put(request: Request) -> Any:
    """保存设置：只写入 config/local_settings.json，并立即刷新运行类参数。"""
    from docking_agent import settings as S

    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"请求体不是合法 JSON：{e}")
    updates = body.get("settings") if isinstance(body, dict) and "settings" in body else body
    if not isinstance(updates, dict):
        # 标准 FastAPI 错误体是 {"detail": ...}；error_message 是旧字段，
        # 保留它是为了让尚未升级的前端/脚本仍能读到同一句话。
        return JSONResponse(status_code=400, content={
            "detail": '请求体需要 {"settings": {...}}',
            "error_message": '请求体需要 {"settings": {...}}'})
    # 只读项直接拒绝；请求头里的 *** 表示「保持原值」（前端看到的是掩码）
    errors = S.reject_readonly(updates)
    current = S.load_local_settings()
    headers_in = (updates.get("llm") or {}).get("extra_headers") if isinstance(updates.get("llm"), dict) else None
    if isinstance(headers_in, dict):
        updates = dict(updates)
        updates["llm"] = dict(updates["llm"])
        updates["llm"]["extra_headers"] = S.unmask_headers(
            headers_in, (current.get("llm") or {}).get("extra_headers"))
    clean, more = S.normalize_updates(updates)
    errors.extend(more)
    if errors:
        message = "设置校验失败：" + "；".join(
            f"{e['path']} {e['message']}" for e in errors[:6])
        return JSONResponse(status_code=400, content={
            "detail": message, "error_message": message, "errors": errors})
    try:
        merged = S.merge_settings(S.load_local_settings(), clean)
        if merged:
            path = S.save_local_settings(merged)
            removed = False
        else:
            # 所有项都被清除：直接删掉文件，而不是留下一个空对象
            S.clear_local_settings()
            path = S.local_settings_path()
            removed = True
        applied = S.apply_runtime_env()
    except OSError as e:
        message = f"写入设置文件失败：{e}"
        return JSONResponse(status_code=500, content={
            "detail": message, "error_message": message})
    payload = await asyncio.to_thread(_settings_payload)
    return {"status": "ok", "path": str(path), "applied_env": applied, "removed": removed,
            "updated": sorted(S.flatten_paths(clean)), "settings": payload}


@router.post("/api/settings/reset")
async def api_settings_reset() -> Dict[str, Any]:
    """清除界面设置（回到 .env + 内置默认）。"""
    from docking_agent import settings as S

    removed = S.clear_local_settings()
    # 只撤销「设置页面自己注入」的环境变量（恢复到注入前基线），
    # 不碰 shell / CI 外部 export 的值。
    S.apply_runtime_env()
    for name in S.injected_env_keys():
        S._INJECTED.pop(name, None)
    payload = await asyncio.to_thread(_settings_payload)
    return {"status": "ok", "removed": removed, "settings": payload}


@router.post("/api/settings/reload")
async def api_settings_reload() -> Dict[str, Any]:
    """重建 Agent 与模型实例：让模型/端点/提示词类改动的下一次运行生效。"""
    return await asyncio.to_thread(_reload_agents)


@router.get("/api/models")
async def api_models() -> Dict[str, Any]:
    """从当前端点拉取可用模型列表（OpenAI 兼容 GET /models）。"""
    return await asyncio.to_thread(_fetch_endpoint_models)


@router.post("/api/tools/probe")
async def api_tools_probe() -> Dict[str, Any]:
    """探测用户提供的外部工具（GPU 对接引擎 / P2Rank / pdb2pqr）。

    与 `scripts/doctor.sh`、设置页「检测」共用同一份实现（`core.external_tools`），
    因此三处结论必然一致；返回 `ok=false` 时 `hint` 给出补齐方法。
    """
    from docking_agent.core.external_tools import collect
    from docking_agent.core.pockets import p2rank_command
    from docking_agent.core.receptor_ph import pdb2pqr_bin

    def probe() -> Dict[str, Any]:
        from docking_agent.core import ligand_pka

        engine = collect()
        p2rank = p2rank_command()
        pka = ligand_pka.engine_status()
        return {
            "engine": engine,
            "p2rank": {"ok": bool(p2rank), "detail": " ".join(str(x) for x in (p2rank or [])),
                       "hint": "" if p2rank else "bash scripts/fetch_tools.sh 或设置 P2RANK_HOME"},
            "pdb2pqr": {"ok": bool(pdb2pqr_bin()), "detail": pdb2pqr_bin() or "",
                        "hint": "" if pdb2pqr_bin() else "设置 PDB2PQR_BIN 指向可用的 pdb2pqr"},
            # 配体质子化的专业 pKa 引擎：缺了会回退内置规则表（报告会注明）
            "pka": {"ok": bool(pka.get("available")),
                    "detail": (f"{pka.get('engine')} {pka.get('version')}"
                               if pka.get("available") else "未安装（回退内置 pKa 规则表）"),
                    "hint": "" if pka.get("available") else str(pka.get("install") or "")},
        }

    return await asyncio.to_thread(probe)


@router.post("/api/settings/test")
async def api_settings_test(request: Request) -> Dict[str, Any]:
    """用某个角色自己的配置做一次最小连通性测试。"""
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"请求体不是合法 JSON：{e}")
    role = str((body or {}).get("role") or "").strip()
    return await asyncio.to_thread(_test_role_llm, role)
