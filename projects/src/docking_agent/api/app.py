"""FastAPI 应用：交互式网页 + REST/SSE 接口（实现 docs/api.md 契约）。

分层：本模块只做「HTTP 适配」——参数校验、SSE 转发、文件下载；
计算在 `docking_agent.core`，编排在 `docking_agent.agents`，
产物与运行记录在 `docking_agent.runs` / `docking_agent.reporting`。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from docking_agent import __version__
from docking_agent.api.schemas import AgentRequest
from docking_agent.agents.blackboard import (Blackboard, current_blackboard, shared_store,
                                              store_blackboard)
from docking_agent.cancellation import cancel_flag, clear_cancel, request_cancel
from docking_agent.config import env, env_int, load_env
from docking_agent.core import DEFAULT_RECEPTOR, RECEPTOR_ALIASES, list_receptors
from docking_agent.paths import project_root, runs_dir, web_dir
from docking_agent.core.library import default_library_path, positive_control_info
from docking_agent.reporting import content_type_for
from docking_agent.reporting.store import resolve_output
from docking_agent.runs import (
    Run,
    current_run,
    download_name,
    download_names,
    get_run_store,
)
from docking_agent.runtime.context import (AgentContext, current_agent_context,
                                          new_context, request_context)
from docking_agent.runtime.errors import ErrorClassifier, core_stack, error_payload
from docking_agent.runtime.llm import load_llm_config
from docking_agent import intake
from docking_agent.runtime.payload import PayloadError, as_text, normalize_agent_input
from docking_agent.runtime.streaming import parse_sse_data, sse_event, stream_agent_sse

logger = logging.getLogger(__name__)
ERRORS = ErrorClassifier()

TIMEOUT_SECONDS = env_int("RUN_TIMEOUT_SECONDS", 900)


# --------------------------------------------------------------------------- #
# 服务状态
# --------------------------------------------------------------------------- #
class ServiceState:
    def __init__(self) -> None:
        self.graph = None
        self.graph_lock = threading.Lock()
        self.tasks: Dict[str, asyncio.Task] = {}

    def get_graph(self):
        """懒加载整体协调 Agent（首次调用才需要 LLM 配置）。"""
        if self.graph is not None:
            return self.graph
        with self.graph_lock:
            if self.graph is None:
                from docking_agent.agents.coordinator import build_agent  # 延迟导入

                self.graph = build_agent(None)
            return self.graph


state = ServiceState()


def _engine_available() -> Dict[str, bool]:
    vina_ok = False
    try:
        import vina  # noqa: F401

        vina_ok = True
    except Exception as e:  # noqa: BLE001
        logger.info("Vina 不可用（%s）：对接将只能走 AutoDock4 或直接失败", e)
    return {"vina": vina_ok, "autodock": bool(shutil.which("autodock4") and shutil.which("autogrid4"))}


def _agent_model_map() -> Dict[str, Any]:
    """各 Agent 角色实际使用的模型（每个角色一个独立 LLM 实例）。
    来源：runtime.llm 的实例登记表（进程内、按角色记录）+ 服务端回执的模型名。
    - `model`：构建实例时请求的模型；
    - `actual_model` / `actual_models`：服务端响应中确认的模型（运行结束后刷新）；
    - `calls`：该角色实际发起的模型调用次数；
    - `instance_id`：实例号，不同即证明三者为独立实例。
    """
    from docking_agent.runtime.llm import llm_registry

    out: Dict[str, Any] = {}
    for role, meta in llm_registry().items():
        out[role] = {
            "model": meta.get("model"),
            "temperature": meta.get("temperature"),
            "base_url": meta.get("base_url"),
            "instance_id": meta.get("instance_id"),
            "calls": meta.get("calls", 0),
        }
        if meta.get("actual_model"):
            out[role]["actual_model"] = meta["actual_model"]
    return out


# --------------------------------------------------------------------------- #
# 设置（设置页面）：字段表来自 docking_agent.settings，接口只做读写与生效
# --------------------------------------------------------------------------- #
def _mask_secret(value: Optional[str]) -> str:
    """密钥只回显首尾 4 位（用于确认「配的是哪一把」），绝不下发明文。"""
    if not value:
        return ""
    text = str(value)
    if len(text) <= 8:
        return "•" * len(text)
    return f"{text[:4]}…{text[-4:]}"


# 上游/异常文本里的凭证痕迹（sk-xxx、Bearer xxx、*_api_key=xxx 等）
_SECRET_PATTERNS = (
    re.compile(r"(sk|rk|pk)-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-.=]{6,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)[\"'\s:=]+[A-Za-z0-9_\-.]{6,}"),
)


def _redact(text: str, limit: int = 200) -> str:
    """对外错误信息必须脱敏：上游可能把请求头/密钥原样回显在错误体里。"""
    out = str(text or "")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: m.group(0)[:4] + "***", out)
    return out[:limit]


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
                "models": [], "base_url": base_url}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": _redact(f"{type(e).__name__}: {e}"),
                "models": [], "base_url": base_url}

    raw_items = data.get("data") if isinstance(data, dict) else None
    models: List[str] = []
    for item in raw_items or []:
        if isinstance(item, dict) and item.get("id"):
            models.append(str(item["id"]))
        elif isinstance(item, str):
            models.append(item)
    return {"status": "ok", "base_url": base_url, "models": sorted(set(models))}


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


# --------------------------------------------------------------------------- #
# 应用
# --------------------------------------------------------------------------- #
# 产物名 → 规范下载名 key：单个产物下载与专用端点共用同一套命名规则
_ARTIFACT_DOWNLOAD_KEYS = {
    "report_pdf": "report_pdf",
    "report_md": "report_md",
    "ranking_csv": "ranking_csv",
}


def _run_download_names(run_id: str) -> Dict[str, str]:
    """按运行元信息算出全部规范下载名（缺失元信息时也返回一套稳定可用的名字）。"""
    store = get_run_store()
    return download_names(run_id, store.meta(run_id) or {})


def _receptor_upload_payload(dest: Path, name: str) -> Dict[str, Any]:
    """受体的**重活**：现场准备 PDBQT + 标定位点盒 + 化学溯源。

    为什么单独抽出来：上传接口**不再**做这件事（用户要求「不要一上传就开始处理文件」），
    只有用户主动「校验文件」或真正开始运行时才执行。
    """
    from docking_agent.core.normalize import normalize_receptor_source
    from docking_agent.paths import project_root

    receptor_path, normalization = normalize_receptor_source(str(dest))
    source = str(normalization.get("site_source") or "")
    payload: Dict[str, Any] = {
        "status": "ok", "kind": "receptor", "file_name": name, "path": str(dest),
        "relative_path": str(dest.relative_to(project_root())),
        "size": dest.stat().st_size, "ext": dest.suffix.lower(), "pending": False,
        "receptor_file": receptor_path,
        "box_center": normalization.get("center"), "box_size": normalization.get("size"),
        "site_source": source, "protein": normalization.get("protein"),
        "receptor_format": normalization.get("format"),
        "dropped_hetatm": normalization.get("dropped_hetatm") or {},
        "kept_hetatm": normalization.get("kept_hetatm") or {},
        "dropped_waters": normalization.get("dropped_waters") or 0,
        "unsupported_hetatm": normalization.get("unsupported_hetatm") or [],
        "cocrystal_ligand": normalization.get("cocrystal_ligand") or {},
        "receptor_protonation": normalization.get("receptor_protonation") or {},
        "input_normalization": normalization,
        "message": f"受体已现场准备为 PDBQT（{Path(str(receptor_path)).name}）",
    }
    # 化学溯源必须在上传/校验这一步就告诉用户：等到对接结果里才发现
    # 「金属/辅因子被剔除了」就太晚了，用户无从判断结果是否还代表真实体系。
    spec = normalization
    dropped = spec.get("dropped_hetatm") or {}
    if dropped:
        top = "、".join(f"{k}×{v}" for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])[:6])
        payload["chemistry_warning"] = (
            f"受体准备按标准流程剔除了非水杂原子：{top}"
            f"（水 {spec.get('dropped_waters') or 0} 个）。"
            "若这些金属/辅因子（如血红素、NAD/FAD、结构金属）对结合重要，"
            "当前对接结果只代表「去辅因子」体系；"
            "可在对接时用 keep_hetatm 指定残基名保留后重跑"
            "（金属离子可直接保留；大辅因子需要 meeko 化学模板）。")
    unsupported = spec.get("unsupported_hetatm") or []
    if unsupported:
        payload["chemistry_warning"] = (
            (payload.get("chemistry_warning", "") + " " if payload.get("chemistry_warning") else "")
            + f"另有 {'、'.join(unsupported)} 缺少对接所需化学模板，未能保留。")
    if "质心" in source and "未找到位点" in source:
        payload["warning"] = ("未能从该文件中确定已知结合位点，位点盒暂按受体原子质心推定，"
                              "**很可能不在活性位点**。建议改传 .pdb（可从共晶配体自动定位），"
                              "或手工填写位点盒中心/尺寸。")
    return payload


def _ligand_upload_payload(dest: Path, name: str) -> Dict[str, Any]:
    """小分子库的**重活**：解析分子（大 SDF 可能很慢）。上传时不做，运行时/校验时才做。"""
    from docking_agent.core.normalize import normalize_ligand_file
    from docking_agent.paths import project_root

    molecules, normalization = normalize_ligand_file(str(dest))
    fmt = str(normalization.get("format") or "")
    if not molecules:
        notes = "；".join(str(n) for n in (normalization.get("notes") or [])[:3])
        raise ValueError("未从文件中解析到任何有效分子，请检查文件格式与内容"
                         + (f"（{notes}）" if notes else ""))
    return {
        "status": "ok", "kind": "ligand", "file_name": name, "path": str(dest),
        "relative_path": str(dest.relative_to(project_root())),
        "size": dest.stat().st_size, "ext": dest.suffix.lower(), "pending": False,
        "format": fmt, "count": len(molecules), "molecules": molecules[:5],
        "input_normalization": normalization,
        "message": f"已解析 {len(molecules)} 个分子（格式 {fmt}）",
    }


def _artifact_filename(run_id: str, name: str, fallback: str) -> str:
    key = _ARTIFACT_DOWNLOAD_KEYS.get(name)
    if not key:
        return fallback
    return _run_download_names(run_id).get(key) or fallback


def _build_run_pdf(run_id: str) -> Optional[bytes]:
    """现场为已有运行渲染 PDF 报告字节；缺 `report.md` 时返回 None。

    直接用落盘的 report.md 渲染：与页面显示的报告内容完全一致，且无需重算结果。
    """
    from docking_agent.reporting.pdf import build_report_pdf

    store = get_run_store()
    meta = store.meta(run_id) or {}
    run_dir = store.root / run_id
    report_md = run_dir / "report.md"
    if not report_md.is_file():
        return None
    chart_paths: Dict[str, Path] = {}
    for art in meta.get("artifacts", []) or []:
        if not str(art.get("content_type") or "").startswith("image/"):
            continue
        path = run_dir / str(art.get("path") or "")
        if path.is_file():
            chart_paths[str(art.get("name"))] = path
    ranking = store.ranking_rows(run_id)
    receptor = str(meta.get("receptor_label") or meta.get("receptor") or "")
    return build_report_pdf(
        {"ranking": ranking}, kind=str(meta.get("kind") or "agent"), run_id=run_id,
        receptor_label=receptor, created_at=str(meta.get("created_at") or ""),
        markdown=report_md.read_text(encoding="utf-8"), chart_paths=chart_paths,
        molecule_count=int(meta.get("molecule_count") or 0))


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_env()
    cfg = load_llm_config()["config"]
    if cfg.get("api_key"):
        logger.info("LLM 已配置：model=%s base_url=%s", cfg.get("model"), cfg.get("base_url"))
    else:
        logger.warning("未配置 LLM_API_KEY：多 Agent 模式不可用；确定性流水线不受影响")
    logger.info("引擎可用性：%s", _engine_available())
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Docking Multi-Agent", version=__version__, lifespan=lifespan)

    # ---------------- 设置页面 ----------------
    @app.get("/api/settings")
    async def api_settings_get() -> Dict[str, Any]:
        """设置页面的全部字段、当前值、生效值与来源。"""
        from docking_agent import settings as S

        return await asyncio.to_thread(lambda: _settings_payload())

    @app.put("/api/settings")
    async def api_settings_put(request: Request) -> Dict[str, Any]:
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

    @app.post("/api/settings/reset")
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

    @app.post("/api/settings/reload")
    async def api_settings_reload() -> Dict[str, Any]:
        """重建 Agent 与模型实例：让模型/端点/提示词类改动的下一次运行生效。"""
        return await asyncio.to_thread(_reload_agents)

    @app.get("/api/models")
    async def api_models() -> Dict[str, Any]:
        """从当前端点拉取可用模型列表（OpenAI 兼容 GET /models）。"""
        return await asyncio.to_thread(_fetch_endpoint_models)

    @app.post("/api/tools/probe")
    async def api_tools_probe() -> Dict[str, Any]:
        """探测用户提供的外部工具（GPU 对接引擎 / P2Rank / pdb2pqr）。

        与 `scripts/doctor.sh`、设置页「检测」共用同一份实现（`core.external_tools`），
        因此三处结论必然一致；返回 `ok=false` 时 `hint` 给出补齐方法。
        """
        from docking_agent.core.external_tools import collect
        from docking_agent.core.pockets import p2rank_command
        from docking_agent.core.receptor_ph import pdb2pqr_bin

        def probe() -> Dict[str, Any]:
            engine = collect()
            p2rank = p2rank_command()
            return {
                "engine": engine,
                "p2rank": {"ok": bool(p2rank), "detail": " ".join(str(x) for x in (p2rank or [])),
                           "hint": "" if p2rank else "bash scripts/fetch_tools.sh 或设置 P2RANK_HOME"},
                "pdb2pqr": {"ok": bool(pdb2pqr_bin()), "detail": pdb2pqr_bin() or "",
                            "hint": "" if pdb2pqr_bin() else "设置 PDB2PQR_BIN 指向可用的 pdb2pqr"},
            }

        return await asyncio.to_thread(probe)

    @app.post("/api/settings/test")
    async def api_settings_test(request: Request) -> Dict[str, Any]:
        """用某个角色自己的配置做一次最小连通性测试。"""
        try:
            body = await request.json()
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"请求体不是合法 JSON：{e}")
        role = str((body or {}).get("role") or "").strip()
        return await asyncio.to_thread(_test_role_llm, role)

    # ---------------- 元信息 ----------------
    @app.get("/api/health")
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

    @app.get("/api/receptors")
    async def api_receptors() -> Dict[str, Any]:
        return {
            "default": DEFAULT_RECEPTOR,
            "aliases": RECEPTOR_ALIASES,
            "receptors": list_receptors(),
        }

    @app.get("/api/libraries")
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

    @app.get("/api/molecule/depict")
    async def api_depict(smiles: str = Query(...), width: int = 320, height: int = 240):
        """RDKit 二维结构图（PNG）。"""
        try:
            png = await asyncio.to_thread(_depict, smiles, width, height)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"无法绘制该 SMILES: {e}")
        return Response(content=png, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/molecule/properties")
    async def api_properties(smiles: str = Query(...)) -> Dict[str, Any]:
        from docking_agent.core import compute_properties

        try:
            return await asyncio.to_thread(compute_properties, smiles)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/agent/stream", deprecated=True,
              summary="[已废弃] 多 Agent 协作流式执行（请改用标准 Agent Protocol）")
    async def api_agent_stream(req: AgentRequest, request: Request):
        payload = dict(req.model_dump())
        run = get_run_store().new("agent", payload)
        # 本次运行的调用计数从零开始（否则 calls 会累计上一次运行，无法判断本次调用量）
        from docking_agent.runtime.llm import reset_registry_counters

        reset_registry_counters()
        # 会话 id：同一 id = 同一段对话。LangGraph checkpointer 按 thread_id 记忆，
        # 因此**不能**再用每次都会变的 run.id 当 thread_id，否则永远命中不到上一轮。
        conversation_id = (req.conversation_id or "").strip()
        thread_id = conversation_id or run.id
        run.data["conversation_id"] = conversation_id
        run.data["thread_id"] = thread_id
        run_config: Dict[str, Any] = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": env_int("RECURSION_LIMIT", 60),
        }
        # 受理层要「看见上一轮」：从 checkpointer 读该 thread 的历史消息（只读，用于继承分子/受体）
        prior_turns: List[Dict[str, str]] = []
        try:
            graph = state.get_graph()
            # 先自愈：上一轮被取消/失败时可能留下「模型发了 tool_calls 但工具没回执」的历史，
            # 直接续聊会被 OpenAI 以 400 拒绝（insufficient tool messages）——补占位回执后再读历史。
            await _heal_thread(graph, run_config, run)
            prior_turns = await _recent_prior_turns(graph, run_config)
        except Exception as e:  # noqa: BLE001
            logger.warning("读取会话历史失败（按新会话处理）：%s", e)
        if prior_turns:
            run.data["conversation_turns"] = len(prior_turns)
        # 受理层（intake）：理解用户要什么 → 任务规约（确定性优先，只在对话模式下才调模型）。
        # 结构化字段（分子库/受体/位点/参数）始终随指令一起下发，避免表单与自然语言互相覆盖。
        message, task_spec = await asyncio.to_thread(
            intake.build_message, req, run=run, prior_turns=prior_turns or None)
        run.data["request"] = {**payload, "message": message}
        run.data["task_spec"] = task_spec          # 可观测：本次任务到底被理解成了什么
        run.log(f"任务受理：task_type={task_spec.get('task_type')} "
                f"authority={task_spec.get('authority')} decision={task_spec.get('decision')} "
                f"来源={task_spec.get('source')}"
                + (f"（延续会话 {conversation_id[:8]}，历史 {len(prior_turns)} 条）"
                   if conversation_id and prior_turns else ""))
        run.save()
        if task_spec.get("assumptions"):
            for note in task_spec["assumptions"][:4]:
                run.log(f"受理假设：{note}")

        async def gen() -> AsyncGenerator[str, None]:
            yield sse_event({"type": "start", "run_id": run.id,
                             "conversation_id": conversation_id,
                             "thread_id": thread_id,
                             "request": run.data.get("request"),
                             "task_spec": run.data.get("task_spec")})
            token = current_run.set(run)
            board = store_blackboard(shared_store(), run.id)
            board_token = current_blackboard.set(board)
            cancel_event = cancel_flag(run.id)
            try:
                graph = state.get_graph()
                # 记录各 Agent 角色实际使用的模型（每个角色独立实例）
                run.data["agent_models"] = _agent_model_map()
                run.save()
                config = run_config
                started = time.time()
                last_progress: Dict[str, Any] = {}

                def _choices_event() -> Any:
                    """结构化「候选选择项」事件（受体/分子解析不确定时下发，供前端点选）。

                    去重：同一份 choices 只发一次（心跳 `_tick` 与 final 前各有一条触发路径）。
                    """
                    choices = run.data.get("choices")
                    if not choices or last_progress.get("choices_sent") == list(choices):
                        return None
                    last_progress["choices_sent"] = list(choices)
                    return sse_event({"type": "choices", "run_id": run.id,
                                      "choices": choices,
                                      "note": run.data.get("choices_note") or ""})

                def _tick() -> List[str]:
                    """心跳：把子 Agent 上报的实时对接进度与逐分子结果转成 SSE 事件。"""
                    out = _drain_live_molecules(run)
                    choice_event = _choices_event()
                    if choice_event:
                        out.append(choice_event)
                    info = run.data.get("live_progress")
                    if info and info != last_progress.get("info"):
                        last_progress["info"] = dict(info)
                        out.append(sse_event({"type": "progress", **info,
                                              "elapsed_sec": round(time.time() - started, 1)}))
                    return out

                stream = stream_agent_sse(graph, {"messages": [{"role": "user", "content": message}]},
                                          config, run.id, context=current_agent_context())
                async for chunk in _interleave(stream, 1.0, _tick):
                    if cancel_event.is_set():
                        run.log("已被用户取消")
                        run.finish("cancelled", error="用户取消")
                        yield sse_event({"type": "cancelled", "run_id": run.id,
                                         "message": "运行已被用户取消",
                                         "summary": run.to_dict()})
                        yield sse_event({"type": "done", "run_id": run.id, "summary": run.to_dict()})
                        return
                    data = parse_sse_data(chunk)
                    # start/done 由本函数统一发送（done 需带 summary）
                    if data and data.get("type") in ("start", "done"):
                        continue
                    # final 之前补发一次 choices（若心跳还没发过），确保前端一定拿得到可选项
                    if data and data.get("type") == "final":
                        choice_event = _choices_event()
                        if choice_event:
                            yield choice_event
                    yield chunk

                # 图执行完毕：把最后一个心跳窗口内产生的逐分子结果补齐再做持久化，
                # 否则对接最后一秒的分子只出现在结果里、不出现「实时」流。
                for extra in _drain_live_molecules(run):
                    yield extra
                run.data.pop("live_molecules", None)
                run.data.pop("live_molecules_seen", None)

                final_text, messages = await _final_state(graph, config)
                from docking_agent.agents.persistence import persist_agent_run

                # 图执行完毕：此刻各角色已真实调用过模型，
                # 刷新为「服务端确认的实际模型 + 调用次数」，报告与运行记录都用这份
                run.data["agent_models"] = _agent_model_map()
                run.save()

                result = await asyncio.to_thread(persist_agent_run, run, messages, final_text)

                # 流程控制权完全在主管 Agent 手里：服务端**不再**接管补齐或复核，
                # 只把「实际完成了什么」记录成 completeness 供查看（信息，不是控制）。
                run.set(completeness=_completeness(run, result, False))
                # 没算任何东西（例如用户只说了句「你好」，受理层 reject）→ 状态 no_op，
                # 历史列表显示 [ SKIP ]，页面也不会把用户甩到空的结果总览。
                run.finish("no_op" if result.get("no_op") else "ok")
                yield sse_event({"type": "done", "run_id": run.id, "summary": run.to_dict(),
                                 "ranking_size": len(result.get("ranking") or [])})
            except Exception as e:  # noqa: BLE001
                logger.exception("多 Agent 执行失败")
                run.finish("error", error=str(e))
                yield sse_event({**error_payload(e, {"node_name": "agent", "run_id": run.id}),
                                 "type": "error"}, event="error")
                yield sse_event({"type": "done", "run_id": run.id, "summary": run.to_dict()})
            finally:
                # 共享黑板快照落盘：可以看到各 Agent 在协作区里留下了什么
                try:
                    run.write_json("blackboard", board.snapshot(), label="共享黑板快照")
                    run.set(blackboard_stats=board.stats())
                except Exception:  # noqa: BLE001
                    logger.debug("黑板快照落盘失败", exc_info=True)
                # 逐分子实时缓冲只在运行期存在：异常/取消路径也不能把它写进运行元数据
                run.data.pop("live_molecules", None)
                run.data.pop("live_molecules_seen", None)
                # 收尾刷新：此刻协调/子 Agent 都已真实调用过模型，
                # 把各角色「服务端确认的模型 + 调用次数」写入运行记录（与报告口径一致）
                run.data["agent_models"] = _agent_model_map()
                run.save()   # 黑板快照与统计在上一步写入内存，这里必须落盘
                current_blackboard.reset(board_token)
                current_run.reset(token)
                clear_cancel(run.id)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 标准 Agent Protocol 面复用这条链路（同一个 Run / 产物 / 黑板），因此把生成器挂到 app.state
    app.state.legacy_agent_stream = api_agent_stream
    app.state.get_graph = state.get_graph

    # ---------------- 文件上传 ----------------
    LIGAND_EXTS = {".sdf", ".sd", ".smi", ".smiles", ".txt", ".csv", ".mol2", ".mol"}
    # 受体扩展名**不在这里另立一份**：与受体解析链共用 core.receptors 的定义，
    # 否则会出现「上传端点认 .ent、resolve_receptor_specs 不认」这类两处漂移（真实缺陷）。
    from docking_agent.core.receptors import RECEPTOR_EXTS

    @app.post("/api/uploads")
    async def api_upload(file: UploadFile = File(...), kind: str = Form(default="auto")):
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

    @app.post("/api/uploads/inspect")
    async def api_upload_inspect(payload: Dict[str, Any]) -> Dict[str, Any]:
        """**用户主动**校验已上传文件：解析分子数 / 现场准备受体并给出位点盒与化学溯源。

        与运行阶段共用同一套解析与准备实现（`_receptor_upload_payload` /
        `_ligand_upload_payload`），因此预览结果与真正运行时一致，不会出现「预览说 30 个、
        实际跑了 29 个」这种偏差。
        """
        raw_path = str((payload or {}).get("path") or "").strip()
        kind = str((payload or {}).get("kind") or "auto").strip().lower()
        if not raw_path:
            raise HTTPException(status_code=400, detail="缺少 path")
        dest = Path(raw_path)
        if not dest.is_file():
            raise HTTPException(status_code=404, detail=f"文件不存在或不可读：{raw_path}")

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
            raise HTTPException(status_code=400, detail=f"{label}：{e}（支持 {supported}）")

    # ---------------- 运行记录与中间数据 ----------------
    @app.get("/api/runs")
    async def api_runs(limit: int = 20) -> Dict[str, Any]:
        return {"runs": get_run_store().list(limit=max(1, min(limit, 200)))}

    @app.get("/api/runs/{run_id}")
    async def api_run_detail(run_id: str) -> Dict[str, Any]:
        detail = get_run_store().detail(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
        return detail

    @app.get("/api/runs/{run_id}/artifacts/{name}")
    async def api_run_artifact(run_id: str, name: str, inline: int = 0):
        path = get_run_store().artifact_path(run_id, name)
        if path is None:
            raise HTTPException(status_code=404, detail=f"产物不存在: {name}")
        filename = _artifact_filename(run_id, name, path.name)
        disposition = "inline" if inline else "attachment"
        headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
        return FileResponse(str(path), media_type=content_type_for(path), headers=headers)

    @app.get("/api/runs/{run_id}/ranking")
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

    @app.get("/api/runs/{run_id}/export.csv")
    async def api_run_export_csv(run_id: str):
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

    @app.get("/api/runs/{run_id}/report.pdf")
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

    @app.get("/api/runs/{run_id}/download.zip")
    async def api_run_zip(run_id: str):
        store = get_run_store()
        if not store.exists(run_id):
            raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
        path = await asyncio.to_thread(store.zip, run_id)
        if path is None:
            raise HTTPException(status_code=404, detail="该运行暂无可打包的数据")
        filename = _run_download_names(run_id)["data_zip"]
        return FileResponse(str(path), media_type="application/zip",
                            headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/api/runs/{run_id}/poses.zip")
    async def api_run_poses_zip(run_id: str):
        store = get_run_store()
        if not store.exists(run_id):
            raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
        path = await asyncio.to_thread(lambda: store.zip(run_id, only_poses=True))
        if path is None:
            raise HTTPException(status_code=404, detail="该运行没有位姿文件（可在配置中开启“保存位姿”）")
        filename = _run_download_names(run_id)["poses_zip"]
        return FileResponse(str(path), media_type="application/zip",
                            headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    # ---------------- 兼容接口（Coze 时代遗留；新客户端请走标准 Agent Protocol） ----------------
    @app.get("/health", deprecated=True, summary="[已废弃] 请改用 /ok 或 /api/health")
    async def legacy_health() -> Dict[str, Any]:
        return await api_health()

    @app.post("/run", deprecated=True,
              summary="[已废弃] Coze 遗留执行入口（请改用 /threads/{tid}/runs/wait）")
    async def legacy_run(request: Request) -> Dict[str, Any]:
        ctx = new_context(method="run", headers=request.headers)
        upstream = request.headers.get("x-run-id")
        if upstream:
            ctx.run_id = upstream
        request_context.set(ctx)
        try:
            payload = await request.json()
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
        run = get_run_store().new("agent", {"mode": "run", "payload": payload}, run_id=ctx.run_id)
        token = current_run.set(run)
        board_token = current_blackboard.set(store_blackboard(shared_store(), run.id))
        try:
            agent_input = normalize_agent_input(payload)
            graph = state.get_graph()
            conversation_id = _payload_conversation_id(payload)
            thread_id = conversation_id or run.id
            run.data["conversation_id"] = conversation_id
            run.data["thread_id"] = thread_id
            config = {"configurable": {"thread_id": thread_id}}
            await _heal_thread(graph, config, run)
            result = await asyncio.wait_for(
                graph.ainvoke(agent_input, config=config, context=current_agent_context()),
                timeout=float(TIMEOUT_SECONDS))
            from docking_agent.agents.persistence import persist_agent_run

            await asyncio.to_thread(persist_agent_run, run, result.get("messages", []),
                                    _last_ai_text(result.get("messages", [])))
            run.finish("ok")
            out = _serialize(result)
            out["run_id"] = run.id
            return out
        except PayloadError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except asyncio.TimeoutError:
            run.finish("error", error="timeout")
            raise HTTPException(status_code=504, detail=f"执行超时（>{TIMEOUT_SECONDS}s）")
        except Exception as e:  # noqa: BLE001
            run.finish("error", error=str(e))
            raise HTTPException(status_code=500, detail=ERRORS.get_error_response(e, {"node_name": "run"}))
        finally:
            current_blackboard.reset(board_token)
            current_run.reset(token)

    @app.post("/stream_run", deprecated=True,
              summary="[已废弃] 与 /api/agent/stream 等价（请改用标准 Agent Protocol）")
    async def legacy_stream_run(request: Request):
        """兼容旧接口：与 /api/agent/stream 行为一致。"""
        try:
            body = await request.json()
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
        req = AgentRequest(message=_extract_text(body), **_agent_fields(body))
        return await api_agent_stream(req, request)

    @app.get("/files/{key}")
    async def legacy_files(key: str):
        path = resolve_output(key)
        if path is None:
            raise HTTPException(status_code=404, detail=f"artifact not found: {key}")
        return FileResponse(str(path))

    @app.post("/api/runs/{run_id}/cancel")
    async def api_run_cancel(run_id: str) -> Dict[str, Any]:
        """请求取消运行。

        对接跑在工作线程 + 子进程里，`asyncio.Task.cancel()` 无法让它停下，
        因此真正的机制是**协作式取消标志**：置位后对接会在收集结果时终止进程池并停止。
        """
        store = get_run_store()
        meta = store.meta(run_id)
        task = state.tasks.get(run_id)
        running = bool(task and not task.done())
        if meta is None and not running:
            raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
        if meta and meta.get("status") not in ("running", None):
            return {"status": "already_finished", "run_id": run_id,
                    "message": f"该运行已结束（状态：{meta.get('status')}）"}
        first = request_cancel(run_id)
        if task is not None and not task.done():
            try:
                task.cancel()  # 协程层面（多 Agent 的模型调用）立即收到取消
            except Exception:  # noqa: BLE001
                logger.debug("取消协程任务失败", exc_info=True)
        return {"status": "cancelling" if first else "already_cancelling",
                "run_id": run_id,
                "message": "已发送取消请求，正在停止对接与后续步骤"}

    @app.post("/cancel/{run_id}", deprecated=True,
              summary="[已废弃] 请改用 /threads/{tid}/runs/{rid}/cancel 或 /api/runs/{run_id}/cancel")
    async def api_cancel(run_id: str) -> Dict[str, Any]:
        """兼容旧接口。"""
        try:
            return await api_run_cancel(run_id)
        except HTTPException:
            task = state.tasks.get(run_id)
            if task is None or task.done():
                return {"status": "not_found", "run_id": run_id, "message": "未找到运行中的任务"}
            task.cancel()
            return {"status": "success", "run_id": run_id, "message": "已发送取消信号"}

    @app.get("/graph_parameter", deprecated=True,
             summary="[已废弃] Coze 遗留 graph 参数描述（请改用 /assistants/{id}/schemas）")
    async def graph_parameter() -> Dict[str, Any]:
        msg = {"type": "array", "items": {"type": "object",
                                          "properties": {"role": {"type": "string"},
                                                         "content": {"type": "string"}}}}
        return {"input_schema": {"type": "object", "properties": {"messages": msg}},
                "output_schema": {"type": "object", "properties": {"messages": msg}},
                "code": 0, "msg": ""}

    @app.post("/v1/chat/completions")
    async def openai_chat(request: Request) -> Any:
        try:
            payload = await request.json()
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
        try:
            agent_input = normalize_agent_input(payload)
        except PayloadError as e:
            raise HTTPException(status_code=400, detail=str(e))
        cfg = load_llm_config()["config"]
        model = str(payload.get("model") or cfg.get("model") or "docking-agent")
        run = get_run_store().new("agent", {"mode": "openai", "model": model})
        graph = state.get_graph()
        conversation_id = _payload_conversation_id(payload)
        thread_id = conversation_id or run.id
        run.data["conversation_id"] = conversation_id
        run.data["thread_id"] = thread_id
        config = {"configurable": {"thread_id": thread_id}}
        await _heal_thread(graph, config, run)
        try:
            # 这个兼容端点没有设置 ContextVar：显式把 run 作为 context 传下去
            # （否则工具层的 active_run() 拿不到运行目录，产物登记会缺失）
            result = await graph.ainvoke(agent_input, config=config,
                                         context=AgentContext(run=run))
        except Exception as e:  # noqa: BLE001
            run.finish("error", error=str(e))
            raise HTTPException(status_code=500, detail=str(e))
        text = _last_ai_text(result.get("messages", []))
        run.finish("ok", report=bool(text))
        return {
            "id": f"chatcmpl-{run.id}", "object": "chat.completion", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    # ---------------- 静态前端 ----------------
    @app.get("/")
    async def index():
        html = web_dir() / "index.html"
        if not html.is_file():
            return JSONResponse(status_code=503, content={
                "error_message": "前端资源缺失：未找到 web/index.html",
                "hint": "请确认 web/ 目录完整；API 文档见 docs/api.md",
            })
        return FileResponse(str(html))

    static_dir = web_dir()
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 标准 Agent Protocol 面（纯增量：assistants / threads / runs + 标准 SSE 帧）
    from docking_agent.api.agent_service import register_agent_service

    register_agent_service(app)
    return app


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _docked_smiles(result: Dict[str, Any]) -> set:
    docking = result.get("docking") or {}
    return {r.get("smiles")
            for block in (docking.get("receptors") or [])
            for r in (block.get("results") or []) if r.get("smiles")}


def _completeness(run: Run, result: Dict[str, Any], auto_completed: bool = False) -> Dict[str, Any]:
    """记录本次实际完成了什么（**只作信息展示**：流程控制权在主管 Agent，服务端不接管）。"""

    molecules = result.get("molecules") or []
    if not molecules:
        return {"docking": "not_applicable", "ranking": "not_applicable", "report": "not_applicable"}
    have = _docked_smiles(result)
    docked = len([m for m in molecules if m.get("smiles") in have])
    if docked >= len(molecules):
        dock_state = "auto" if auto_completed else "agent"
    else:
        dock_state = "partial" if docked else "missing"
    return {"docking": dock_state, "docked": docked, "total": len(molecules),
            "ranking": "ok" if result.get("ranking") else "missing",
            "report": "ok" if (run.dir / "report.md").is_file() else "missing",
            "positive_control": "ok" if result.get("positive_control") else "skipped"}


def _attach_pose_url(item: Dict[str, Any], run_id: str) -> None:
    """把分子事件里的位姿产物名转成可下载 URL。"""
    if not isinstance(item, dict):
        return
    artifact = item.pop("pose_artifact", None)
    if artifact:
        item["pose_url"] = f"/api/runs/{run_id}/artifacts/{artifact}"


def _drain_live_molecules(run: Run) -> List[str]:
    """把子 Agent 上报的逐分子结果转成 `molecules` SSE 事件（心跳调用）。

    多 Agent 模式下 `run_docking` 工具在子 Agent 里跑，父流程阻塞，工具通过
    `run.data["live_molecules"]` 逐条上报；这里按已消费下标取增量，既不丢也不重复
    （用下标而不是 `del`，避免与工具线程的 append 竞争）。同时补上位姿下载地址。
    """
    rows = run.data.get("live_molecules")
    if not rows:
        return []
    seen = int(run.data.get("live_molecules_seen") or 0)
    items = list(rows)[seen:]
    if not items:
        return []
    run.data["live_molecules_seen"] = seen + len(items)
    for item in items:
        _attach_pose_url(item, run.id)
    return [sse_event({"type": "molecules", "items": items})]


def _site_from(center: Optional[List[float]], size: Optional[List[float]]) -> Optional[Dict[str, Any]]:
    if not center and not size:
        return None
    return {"center": center, "size": size}


def _depict(smiles: str, width: int, height: int) -> bytes:
    from rdkit import Chem
    from rdkit.Chem.Draw import rdMolDraw2D

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError("SMILES 解析失败")
    drawer = rdMolDraw2D.MolDraw2DCairo(int(width), int(height))
    opts = drawer.drawOptions()
    opts.bondLineWidth = 2
    opts.minFontSize = 12
    opts.maxFontSize = 16
    drawer.DrawMolecule(mol)
    drawer.FinishDrawing()
    return drawer.GetDrawingText()


def _last_ai_text(messages: List[Any]) -> str:
    for m in reversed(messages or []):
        if type(m).__name__ in ("AIMessage", "AIMessageChunk"):
            content = getattr(m, "content", "")
            if isinstance(content, list):
                content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
            if str(content).strip():
                return str(content).strip()
    return ""


async def _interleave(stream: Any, interval: float,
                      tick: Any) -> AsyncGenerator[str, None]:
    """把「周期心跳」插进 SSE 事件流。

    多 Agent 模式下父流程在 graph.invoke 上阻塞，子 Agent 内部的实时进度只能靠心跳
    （`tick()` 返回的进度事件）推给前端；否则要等整轮工具调用结束才看到任何动静。
    """
    queue: "asyncio.Queue[Any]" = asyncio.Queue()
    DONE = object()

    async def _pump() -> None:
        try:
            async for item in stream:
                await queue.put(item)
        except Exception as e:  # noqa: BLE001
            logger.warning("事件流中断：%s", e)
        finally:
            await queue.put(DONE)

    task = asyncio.create_task(_pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                for extra in (tick() or []):
                    yield extra
                continue
            if item is DONE:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()


async def _final_state(graph: Any, config: Dict[str, Any]) -> tuple[str, List[Any]]:
    try:
        snapshot = await graph.aget_state(config)
        values = getattr(snapshot, "values", {}) or {}
        messages = values.get("messages", []) or []
        return _last_ai_text(messages), messages
    except Exception as e:  # noqa: BLE001
        logger.warning("读取最终状态失败: %s", e)
        return "", []


# 单条消息类型 → 受理层用的角色（tool/system 不参与多轮上下文）
_MESSAGE_ROLES = {"HumanMessage": "user", "AIMessage": "assistant",
                  "AIMessageChunk": "assistant"}


async def _heal_thread(graph: Any, config: Dict[str, Any], run: Any) -> int:
    """续聊前自愈线程状态：补齐上一轮中断留下的未回填工具调用（返回补了几条）。

    为什么必须做：模型已经发出 `tool_calls` 而工具被中止（用户取消/运行失败）时，
    checkpointer 里会留下悬空调用；下一轮把这段历史发给 OpenAI 会直接 400
    （`An assistant message with 'tool_calls' must be followed by tool messages…`），
    整段对话无法继续。这里补的是**占位回执（事实说明：该调用被中断、没有结果）**，不是编造数据。
    """
    from docking_agent.agents.threads import repair_thread_state

    try:
        healed = await repair_thread_state(graph, config)
    except Exception as e:  # noqa: BLE001 - 自愈失败不该让运行起不来
        logger.warning("线程状态自愈失败（继续运行）：%s", e)
        return 0
    if healed.get("repaired"):
        run.log(f"已修复上一轮中断留下的 {healed['repaired']} 个未回填工具调用"
                f"（补占位回执，避免模型报 tool_calls 校验错误）")
    return int(healed.get("repaired") or 0)


async def _recent_prior_turns(graph: Any, config: Dict[str, Any],
                              limit: int = 8) -> List[Dict[str, str]]:
    """从 checkpointer 读取该 thread 的历史消息，取最近 limit 条 human/ai（忽略 tool/system）。

    用途仅限「让受理层看见上一轮」：用户回答追问时继承上一轮的分子/受体，不再重复判 ask。
    真正的上下文延续由 checkpointer + thread_id 完成，本函数**不修改**任何状态。
    """
    snapshot = await graph.aget_state(config)
    values = getattr(snapshot, "values", None) or {}
    messages = values.get("messages") or []
    turns: List[Dict[str, str]] = []
    for m in messages:
        role = _MESSAGE_ROLES.get(type(m).__name__, "")
        if not role:
            continue
        text = as_text(getattr(m, "content", "")).strip()
        if not text:
            continue
        turns.append({"role": role, "content": text})
    return turns[-max(0, int(limit)):]


def _payload_conversation_id(payload: Any) -> str:
    """从任意请求体里取会话 id（兼容 legacy /run 与 /v1/chat/completions 的原始 JSON）。"""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("conversation_id") or "").strip()


def _extract_text(body: Any) -> str:
    try:
        return normalize_agent_input(body)["messages"][0].content
    except Exception:  # noqa: BLE001
        return ""


def _agent_fields(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    keep = ("mode", "advanced", "receptor", "receptor_file", "ligands_text", "molecule_file",
            "positive_control", "exhaustiveness", "n_poses", "engine", "site_center", "site_size",
            "max_ligands", "save_poses", "skip_positive_control", "conversation_id")
    return {k: body[k] for k in keep if k in body and body[k] is not None}


DEFAULT_AGENT_TASK = ("请完成一次完整的分子筛选：导入候选分子库 → 物化性质评估 → 分子对接 → "
                       "结合模式分析 → 生成筛选报告并给出按亲和力排序的结论。")






def _serialize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    if type(obj).__name__.endswith("Message"):
        role = {"HumanMessage": "user", "SystemMessage": "system", "ToolMessage": "tool"}.get(
            type(obj).__name__, "assistant")
        content = getattr(obj, "content", "")
        if isinstance(content, list):
            content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        out: Dict[str, Any] = {"role": role, "content": content}
        if getattr(obj, "tool_calls", None):
            out["tool_calls"] = [{"name": tc.get("name"), "args": tc.get("args")} for tc in obj.tool_calls]
        return out
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


app = create_app()
