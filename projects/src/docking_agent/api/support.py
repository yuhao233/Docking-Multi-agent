"""api 层共享支撑：进程内状态、安全边界、脱敏、模型/引擎探测、上传准备、产物下载名。

`api/app.py` 原先是一个 700+ 行的 `create_app()` 闭包，~36 条路由与全部辅助函数挤在一起。
第 2 波按「路由模块（`api/routers/*`）+ 共享支撑（本模块）+ Agent 编排支撑（`agent_flow.py`）」
拆分：`app.py` 只负责组装（lifespan / 安全中间件 / 挂路由 / 静态前端）。
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Request

from docking_agent.config import env, env_int
from docking_agent.runs import Run, download_names, get_run_store
from docking_agent.runtime.errors import ErrorClassifier

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
        self.tasks: Dict[str, Any] = {}

    def get_graph(self) -> Any:
        """懒加载整体协调 Agent（首次调用才需要 LLM 配置）。"""
        if self.graph is not None:
            return self.graph
        with self.graph_lock:
            if self.graph is None:
                from docking_agent.agents.coordinator import build_agent  # 延迟导入

                self.graph = build_agent(None)
            return self.graph


state = ServiceState()


# --------------------------------------------------------------------------- #
# 安全边界：同源校验 + 安全响应头
# --------------------------------------------------------------------------- #
# 本服务**没有鉴权**（定位是本机工具），所以浏览器侧的跨站请求必须挡住：
# 任何网页都能对 127.0.0.1 发「简单请求」（浏览器不做预检），否则
# `PUT /api/settings`（改端点与密钥）、`POST /api/uploads`、
# `POST /api/settings/test`（会真实消耗 LLM 额度）都可被跨站触发。
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# CSP 按当前前端实际形态收紧：只有一个外部脚本、无内联脚本、无内联 style 属性。
# style-src 保留 'unsafe-inline'（动态样式不影响脚本执行），script-src 必须严格。
_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def _allowed_origins() -> set:
    raw = str(env("DOCKING_ALLOWED_ORIGINS") or "")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _same_origin(request: Request) -> bool:
    """请求是否来自同源（或非浏览器客户端）。

    保守取向：**只有明确不同源才拒绝**。缺 Origin 的一律放行 —— 否则 CLI、
    测试与同源导航会被误伤（浏览器在跨站请求上一定会带 Origin）。
    """
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if site == "cross-site":
        return False
    origin = (request.headers.get("origin") or "").strip().lower()
    if not origin:
        return True
    if origin == "null":                       # sandboxed iframe / file:// → 不可信
        return False
    if origin in _allowed_origins():
        return True
    host = (request.headers.get("host") or "").strip().lower()
    if not host:
        return True
    try:
        from urllib.parse import urlsplit  # noqa: PLC0415

        return urlsplit(origin).netloc.lower() == host
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# 脱敏与引擎/模型探测
# --------------------------------------------------------------------------- #
def _engine_available() -> Dict[str, Any]:
    """本机可用引擎：内置 Vina / 经典 AutoDock4 / 用户登记的外部引擎。

    路径判定一律走项目自己的定位逻辑（`AUTODOCK4_BIN`/`AUTOGRID4_BIN` → PATH），
    因为本机的 AD4 常常**不在 PATH 上**（conda 独立前缀）；只看 `shutil.which` 会把
    "已配置" 错报成 "不可用"。外部引擎按探测结果如实标注（登记了但不可用也不算可用）。
    """
    vina_ok = False
    try:
        import vina  # noqa: F401

        vina_ok = True
    except Exception as e:  # noqa: BLE001
        logger.info("Vina 不可用（%s）：对接将只能走 AutoDock4 或直接失败", e)
    try:
        from docking_agent.core.docking import _autodock_bin
        from docking_agent.core.external_tools import collect as _collect_external

        autodock_ok = bool(_autodock_bin("autodock4") and _autodock_bin("autogrid4"))
        external = _collect_external()
    except Exception as e:  # noqa: BLE001 - 健康检查不能因为探测异常而失败
        logger.info("引擎探测失败（%s）", e)
        return {"vina": vina_ok, "autodock": False}
    return {
        "vina": vina_ok,
        "autodock": autodock_ok,
        "external": bool(external.get("ok") and external.get("configured")),
        "external_flavor": external.get("flavor") or "",
    }


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


# --------------------------------------------------------------------------- #
# 产物下载名与 PDF 现场渲染
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


# --------------------------------------------------------------------------- #
# 上传文件的「重活」（仅在用户主动校验 / 真正运行时执行）
# --------------------------------------------------------------------------- #
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


def _depict(smiles: str, width: int, height: int) -> bytes:
    from rdkit import Chem
    from rdkit.Chem.Draw import rdMolDraw2D

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError("SMILES 解析失败")
    drawer = rdMolDraw2D.MolDraw2DCairo(int(width), int(height))
    # rdkit 的 `drawOptions()` 存根把属性读成只读，显式 Any 以免与运行时行为不符
    opts: Any = drawer.drawOptions()
    opts.bondLineWidth = 2
    opts.minFontSize = 12
    opts.maxFontSize = 16
    drawer.DrawMolecule(mol)
    drawer.FinishDrawing()
    return drawer.GetDrawingText()


# --------------------------------------------------------------------------- #
# 运行完整度与序列化（也被 tests 直接引用）
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
