"""运行记录：每次运行的全部中间数据与产物清单。

目录结构（`var/runs/<run_id>/`）::

    run.json          # 运行元信息（参数/状态/耗时/产物清单/日志）
    request.json      # 本次请求参数
    molecules.json    # 解析后的分子库
    properties.json   # 理化性质
    docking.json      # 对接明细（含每个分子的能量项与位姿路径）
    binding.json      # 结合模式分析
    ranking.csv       # 排序结果
    report.md         # 报告（agent 模式为协调 Agent 报告，流水线模式为自动生成）
    charts/*.png      # 图表
    poses/*.pdbqt     # 每个分子的最佳位姿（开启保存时）

`manifest` 中的每个产物都可以通过 `GET /api/runs/{id}/artifacts/{name}` 单下载，
或通过 `GET /api/runs/{id}/download.zip` 整体打包下载。
"""
from __future__ import annotations

import json
import threading
import os
import logging
import re
import shutil
import time
import zipfile
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from docking_agent import run_context as _run_context
from docking_agent.config import env_int
from docking_agent.paths import runs_dir
from docking_agent.reporting.store import content_type_for

logger = logging.getLogger(__name__)

RUN_META = "run.json"

# 完整排序结果的进程内缓存：{(run_id): (mtime, rows)}，避免分页请求反复解析大 JSON
_RANKING_CACHE: Dict[str, Any] = {}
_RANKING_CACHE_MAX = 4

# 当前正在执行的运行（多 Agent 模式下由 API 层注入，工具据此把中间数据写入运行目录）
current_run: ContextVar[Optional["Run"]] = ContextVar("current_run", default=None)

# 注册给下层：`core/` 读「当前运行」只走 run_context，不再 import 本模块（审计 V2）
_run_context.set_run_provider(lambda: current_run.get())


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + datetime.now().strftime("%f")[:4]


#: 运行 / 线程标识的合法字符集（首字符必须是字母或数字，其余允许 `._-`）。
#: 它同时是**单层路径片段**，所以必须严格：未校验时 `run_id="../x"` 会让
#: `root / run_id` 逃出运行目录 —— 真实漏洞：`POST /run` 的 `x-run-id` 头可在工作区
#: 任意位置建目录并写入攻击者可控内容；`POST /threads` 的 `thread_id` 可覆盖任意 `*.json`。
RUN_COMPONENT_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def safe_run_component(value: Any, *, field: str = "run_id") -> str:
    """校验并返回可安全用作**单层路径片段**的标识符；不合法直接抛 `ValueError`。

    三道门：① 非空；② 字符集白名单（拒绝 `/`、`\\`、`..` 与控制字符）；
    ③ 解析后必须仍在 `root` 之下（`ensure_inside`）—— 最后一道是防未来有人放宽
    字符集时静默回归的兜底，不是主要防线。
    """
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError(f"{field} 不能为空")
    if not RUN_COMPONENT_SAFE.match(text):
        raise ValueError(f"{field} 含非法字符：{text!r}（只允许字母/数字/._-，首字符须为字母或数字）")
    return text


def ensure_inside(root: Path, candidate: Path, *, field: str) -> Path:
    """断言 `candidate` 解析后位于 `root` 之内，返回解析后的路径；否则抛 `ValueError`。"""
    base = Path(root).resolve()
    try:
        resolved = Path(candidate).resolve()
        resolved.relative_to(base)
    except ValueError as e:
        raise ValueError(f"{field} 解析后不在 {base} 之下：{candidate}") from e
    return resolved


def _dir_size(path: Path) -> int:
    """目录内容总字节数（清理报告用；单文件不可读按 0 计，不因权限问题中断清理）。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


# 下载文件名里只允许 ASCII 安全字符：中文受体名/空格/斜杠一律降级为下划线，
# 避免 Content-Disposition 出现引号或路径分隔符，也方便用户在下载目录里辨认。
_DOWNLOAD_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _download_token(text: Any, *, fallback: str, limit: int = 0, strip_edges: bool = False) -> str:
    """把任意文本压成 ASCII 安全片段；limit>0 时截断。"""
    token = _DOWNLOAD_SAFE.sub("_", str(text if text is not None else "").strip())
    if strip_edges:
        token = token.strip("._-")
    if limit > 0:
        token = token[:limit]
    return token or fallback


def download_prefix(run_id: str, *, receptor: str = "", molecules: int = 0) -> str:
    """规范化下载名的公共前缀：`dock_{run_id}_{receptor}[_{N}mols]`。

    receptor 取运行的受体标签（中文/空格/斜杠会降级为下划线，空则用 `receptor`）；
    分子数已知时带上 `_{N}mols`，未知时**整段省略**（不写 `namols` 这种读不通的名字）。
    """
    rid = _download_token(run_id, fallback="run", limit=64)
    rec = _download_token(receptor, fallback="receptor", limit=24, strip_edges=True)
    prefix = f"dock_{rid}_{rec}"
    if isinstance(molecules, int) and molecules > 0:
        prefix += f"_{molecules}mols"
    return prefix


def download_name(run_id: str, kind: str, *, receptor: str = "", molecules: int = 0,
                  ext: str = "") -> str:
    """规范、可读、ASCII 安全的下载文件名：`dock_{run_id}_{receptor}[_{N}mols]_{kind}.{ext}`。

    例：`download_name("20260914-151116-3404", "report", receptor="thrombin",
    molecules=8, ext="pdf")` → `dock_20260914-151116-3404_thrombin_8mols_report.pdf`；
    分子数未知时省略计数段 → `dock_20260914-151116-3404_thrombin_report.pdf`。
    """
    prefix = download_prefix(run_id, receptor=receptor, molecules=molecules)
    name = _download_token(kind, fallback="data", limit=24, strip_edges=True)
    suffix = _download_token(str(ext or "").lstrip("."), fallback="", limit=16, strip_edges=True)
    return f"{prefix}_{name}.{suffix}" if suffix else f"{prefix}_{name}"


def download_names(run_id: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """一次算出本次运行所有规范下载名（前端不再自己拼文件名，避免两处规则漂移）。"""
    info = meta or {}
    molecules = info.get("molecule_count") or 0
    try:
        count = int(molecules)
    except (TypeError, ValueError):
        count = 0
    kwargs = {"receptor": str(info.get("receptor_label") or info.get("receptor") or ""),
              "molecules": count}
    return {
        "report_pdf": download_name(run_id, "report", ext="pdf", **kwargs),
        "report_md": download_name(run_id, "report", ext="md", **kwargs),
        "ranking_csv": download_name(run_id, "ranking", ext="csv", **kwargs),
        "data_zip": download_name(run_id, "data", ext="zip", **kwargs),
        "poses_zip": download_name(run_id, "poses", ext="zip", **kwargs),
        "prefix": download_prefix(run_id, **kwargs),
    }


@dataclass
class Artifact:
    name: str
    label: str
    path: str            # 相对 run 目录
    size: int = 0
    content_type: str = "application/octet-stream"

    def to_dict(self, run_id: str) -> Dict[str, Any]:
        d = {
            "name": self.name,
            "label": self.label,
            "path": self.path,
            "size": self.size,
            "content_type": self.content_type,
            "download_url": f"/api/runs/{run_id}/artifacts/{self.name}",
        }
        if self.content_type.startswith("image/"):
            d["inline_url"] = d["download_url"] + "?inline=1"
        return d


class Run:
    """单次运行的中间数据容器。"""

    def __init__(self, root: Path, run_id: str, kind: str, request: Dict[str, Any]):
        # 标识符同时是路径片段：先过白名单，再断言解析后仍在 root 之内。
        self.id = safe_run_component(run_id)
        self.kind = kind
        self.dir = ensure_inside(root, Path(root) / self.id, field="run_id")
        self.dir.mkdir(parents=True, exist_ok=True)
        self._artifacts: Dict[str, Artifact] = {}
        self.logs: List[str] = []
        self.data: Dict[str, Any] = {
            "run_id": run_id,
            "kind": kind,
            "status": "running",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "duration_sec": None,
            "request": request or {},
            "receptor": None,
            "receptor_label": None,
            "site": None,
            "engine": None,
            "exhaustiveness": None,
            "n_poses": None,
            "molecule_count": 0,
            "top": [],
            "has_report": False,
            "artifact_count": 0,
            "error": None,
            "notes": [],
        }
        self._t0 = datetime.now()
        self.write_json("request", request or {}, label="请求参数", artifact=True)
        self.save()

    # ---------------- 写入 ----------------
    def path(self, rel: str) -> Path:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def rel(self, path: Any) -> str:
        """把运行目录内的路径换算成相对路径（登记产物用）。

        三处调用点原先各写一遍同样的表达式；统一在这里，且**失败时不抛异常**——
        返回原路径，由调用方决定是否登记（大库位姿可能不在运行目录内）。
        """
        try:
            return str(Path(path).resolve().relative_to(self.dir.resolve()))
        except (ValueError, OSError):
            return str(path)

    def add_artifact(self, rel_path: str, name: str, label: str,
                     content_type: Optional[str] = None) -> Artifact:
        full = self.dir / rel_path
        art = Artifact(
            name=name,
            label=label,
            path=rel_path,
            size=full.stat().st_size if full.is_file() else 0,
            content_type=content_type or content_type_for(full),
        )
        self._artifacts[name] = art
        self.data["artifact_count"] = len(self._artifacts)
        return art

    def write_bytes(self, rel_path: str, data: bytes, *, name: str, label: str,
                    content_type: Optional[str] = None, artifact: bool = True) -> Path:
        p = self.path(rel_path)
        p.write_bytes(data)
        if artifact:
            self.add_artifact(rel_path, name, label, content_type)
        return p

    def write_json(self, name: str, obj: Any, *, rel_path: Optional[str] = None,
                   label: Optional[str] = None, artifact: bool = True) -> Path:
        rel = rel_path or f"{name}.json"
        p = self.path(rel)
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        if artifact:
            self.add_artifact(rel, name, label or name, "application/json; charset=utf-8")
        return p

    def write_text(self, rel_path: str, text: str, *, name: str, label: str,
                   content_type: str = "text/plain; charset=utf-8") -> Path:
        p = self.path(rel_path)
        p.write_text(text, encoding="utf-8")
        self.add_artifact(rel_path, name, label, content_type)
        return p

    def write_ranking(self, rows: List[Dict[str, Any]]) -> Path:
        """写入完整排序结果（分页接口据此读取，避免每次解析巨大的 result.json）。"""
        p = self.path("ranking.json")
        p.write_text(json.dumps(rows, ensure_ascii=False, default=str), encoding="utf-8")
        self.add_artifact("ranking.json", "ranking_json", "排序结果（完整 JSON）",
                          "application/json; charset=utf-8")
        return p

    def log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        self.logs.append(line)
        logger.info("run %s: %s", self.id, message)

    def set(self, **kwargs: Any) -> None:
        self.data.update(kwargs)

    def finish(self, status: str = "ok", error: Optional[str] = None, **kwargs: Any) -> None:
        self.data.update(kwargs)
        self.data["status"] = status
        self.data["error"] = error
        self.data["finished_at"] = datetime.now().isoformat(timespec="seconds")
        self.data["duration_sec"] = round((datetime.now() - self._t0).total_seconds(), 2)
        self._write_report_artifact()
        self.save()

    # ---------------- 持久化 ----------------
    def _write_report_artifact(self) -> None:
        report = self.dir / "report.md"
        if report.is_file():
            self.add_artifact("report.md", "report_md", "分析报告（Markdown）", "text/markdown; charset=utf-8")
            self.data["has_report"] = True

    def save(self) -> None:
        """落盘 run.json。

        **原子写**：先写同目录临时文件再 `os.replace` —— 否则并发读取
        （`GET /api/runs/{id}`、运行列表、外部脚本）可能读到只写了一半/空文件
        （真实踩到过：`json.loads` 报 "Expecting value: line 1 column 1"）。
        """
        payload = dict(self.data)
        payload["artifacts"] = [a.to_dict(self.id) for a in self._artifacts.values()]
        payload["log"] = self.logs
        target = self.dir / RUN_META
        tmp = self.dir / f".{RUN_META}.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
        os.replace(tmp, target)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.data)

    def artifacts(self) -> List[Dict[str, Any]]:
        """当前已登记的产物清单（带下载 URL）。"""
        return [a.to_dict(self.id) for a in self._artifacts.values()]


class RunStore:
    """运行记录仓库。"""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else runs_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        # 历史检索索引（惰性建立，进程内缓存；运行目录变化时重建）
        self._index_cache: Optional[List[Dict[str, Any]]] = None
        self._index_signature: Optional[Any] = None
        self._index_lock = threading.Lock()

    def new(self, kind: str, request: Dict[str, Any], run_id: Optional[str] = None) -> Run:
        # 外部传入的 run_id（如兼容入口 `POST /run` 的 `x-run-id` 头）必须先过白名单；
        # 不合法直接抛 ValueError，由 API 层转成 400 —— 绝不落到 `root / run_id` 上。
        rid = safe_run_component(run_id) if str(run_id or "").strip() else new_run_id()
        while (self.root / rid).exists():
            rid = new_run_id()
        return Run(self.root, rid, kind, request)

    def exists(self, run_id: str) -> bool:
        return (self.root / run_id / RUN_META).is_file()

    def delete(self, run_id: str) -> bool:
        """删除一条运行记录（整棵运行目录）。不存在返回 False；非法 id 抛 ValueError。

        用途有两类：① 门禁脚本清理**自己创建**的运行记录（`scripts/ui_e2e.js` /
        `scripts/browser_check.py` 每跑一次会真实创建十几条，长期会挤满用户的历史列表）；
        ② 界面/接口按需删除。删除后索引签名（目录数 / mtime）自变，检索自动重建。
        """
        rid = safe_run_component(run_id)
        target = self.root / rid
        if not target.is_dir():
            return False
        shutil.rmtree(target)
        self._index_cache = None
        self._index_signature = None
        logger.info("已删除运行记录：%s", rid)
        return True

    def meta(self, run_id: str) -> Optional[Dict[str, Any]]:
        meta_path = self.root / run_id / RUN_META
        if not meta_path.is_file():
            return None
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("run.json 解析失败: %s", meta_path)
            return None

    # --------------------------------------------------------------------- #
    # 历史检索：关闭页面后仍可按关键词 / 状态 / 受体 / 时间找回旧运行
    # --------------------------------------------------------------------- #
    def _index_entry(self, run_id: str) -> Optional[Dict[str, Any]]:
        """一条索引记录：运行元数据 + 排序表前若干行的分子名/ID（用于按分子检索）。

        只读 `run.json` 与排序文件的前 64 KB —— 3 千多条运行也能在数秒内建成索引，
        且不受大库产物体积影响。
        """
        meta = self.meta(run_id)
        if not meta:
            return None
        run_dir = self.root / run_id
        parts: List[str] = [run_id, str(meta.get("kind") or ""), str(meta.get("status") or ""),
                            str(meta.get("receptor_label") or ""), str(meta.get("receptor") or "")]
        request = meta.get("request") or {}
        if isinstance(request, dict):
            for key in ("message", "goal", "molecule_file", "ligands_text", "receptor_file"):
                value = request.get(key)
                if isinstance(value, str):
                    parts.append(value[:500])
        for note in (meta.get("notes") or [])[:10]:
            parts.append(str(note)[:200])
        molecules: List[str] = []
        for name in ("ranking.csv", "ranking.json"):
            path = run_dir / name
            if not path.is_file():
                continue
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    chunk = fh.read(65536)
            except OSError as exc:
                logger.debug("读取排序文件失败（跳过分子索引）：%s", exc)
                continue
            for line in chunk.splitlines()[:200]:
                cells = [cell.strip() for cell in line.split(",")[:3]]
                for cell in cells:
                    if cell and cell.lower() not in ("rank", "id", "name", "smiles"):
                        molecules.append(cell[:64])
            break
        parts.extend(molecules)
        return {"run_id": run_id,
                "created_at": str(meta.get("created_at") or ""),
                "status": str(meta.get("status") or ""),
                "kind": str(meta.get("kind") or ""),
                "receptor": str(meta.get("receptor_label") or meta.get("receptor") or ""),
                "molecule_count": int(meta.get("molecule_count") or 0),
                "blob": " ".join(parts).lower()}

    def _ensure_index(self) -> List[Dict[str, Any]]:
        """惰性建立/刷新检索索引（进程内缓存；运行目录变化时重建）。"""
        if not self.root.is_dir():
            return []
        dirs = [d for d in self.root.iterdir() if d.is_dir()]
        signature = (str(self.root), len(dirs), max((d.stat().st_mtime for d in dirs), default=0.0))
        with self._index_lock:
            if self._index_cache is not None and self._index_signature == signature:
                return self._index_cache
            entries: List[Dict[str, Any]] = []
            for d in sorted(dirs, reverse=True):
                entry = self._index_entry(d.name)
                if entry:
                    entries.append(entry)
            self._index_cache = entries
            self._index_signature = signature
            logger.info("运行检索索引已建立：%d 条", len(entries))
            return entries

    def search(self, *, q: str = "", status: str = "", kind: str = "", receptor: str = "",
               since: str = "", until: str = "", offset: int = 0,
               limit: int = 20) -> Dict[str, Any]:
        """按关键词/状态/类型/受体/时间检索历史运行，返回分页结果。

        `q` 会在 run_id、受体、状态、任务描述与**排序表里的分子名/ID**上做子串匹配
        （大小写不敏感，空格分隔的多个词按 AND 处理）。`since`/`until` 接受
        `YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM:SS`，按运行创建时间比较。
        """
        entries = self._ensure_index()
        terms = [t for t in str(q or "").lower().split() if t]
        status_l = str(status or "").strip().lower()
        kind_l = str(kind or "").strip().lower()
        receptor_l = str(receptor or "").strip().lower()
        since_s, until_s = str(since or "").strip(), str(until or "").strip()
        if len(until_s) == 10:                     # 只给日期 → 含当天整天
            until_s += "T23:59:59"
        if len(since_s) == 10:
            since_s += "T00:00:00"

        def _keep(entry: Dict[str, Any]) -> bool:
            if status_l and entry["status"].lower() != status_l:
                return False
            if kind_l and entry["kind"].lower() != kind_l:
                return False
            if receptor_l and receptor_l not in entry["receptor"].lower():
                return False
            created = entry["created_at"]
            if since_s and created and created < since_s:
                return False
            if until_s and created and created > until_s:
                return False
            blob = entry["blob"]
            return all(term in blob for term in terms)

        hits = [e for e in entries if _keep(e)]
        total = len(hits)
        start = max(0, int(offset))
        rows = [{k: v for k, v in e.items() if k != "blob"} for e in hits[start:start + max(0, limit)]]
        return {"runs": rows, "total": total, "offset": start, "limit": max(0, limit),
                "query": {"q": q, "status": status, "kind": kind, "receptor": receptor,
                          "since": since, "until": until}}

    def list(self, limit: int = 20) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        if not self.root.is_dir():
            return items
        for d in sorted(self.root.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            meta = self.meta(d.name)
            if meta:
                items.append({k: v for k, v in meta.items() if k not in ("artifacts", "log")})
            if len(items) >= limit:
                break
        return items

    # --------------------------------------------------------------------- #
    # 保留策略：运行目录不能无限增长（审计：3 882 个目录 / 2.0 GB，且启动扫描全部目录）
    # --------------------------------------------------------------------- #
    def prune(self, *, keep_last: int = 200, max_age_days: float = 30.0,
              dry_run: bool = True) -> Dict[str, Any]:
        """清理过旧的运行目录，返回报告（**默认只报告不删除**）。

        删除条件（**两条都满足才删**，保守，避免误删刚产生的运行）：
          1. 按目录 mtime 倒序排名 ≥ `keep_last`（不属于最近 N 个）；
          2. `max_age_days > 0` 且目录年龄 > `max_age_days`。
        另外 `status == "running"` 的运行**永不删除**（进程内可能正在跑）。

        目录 mtime 取目录自身与其 `run.json` 的较大者：两者都会被写入更新。
        """
        report: Dict[str, Any] = {"dry_run": bool(dry_run), "keep_last": int(keep_last),
                                  "max_age_days": float(max_age_days), "candidates": [],
                                  "deleted": [], "freed_bytes": 0, "errors": []}
        if not self.root.is_dir():
            return report
        now = time.time()
        entries: List[Any] = []
        for d in self.root.iterdir():
            if not d.is_dir():
                continue
            try:
                mtime = d.stat().st_mtime
                meta_path = d / RUN_META
                if meta_path.is_file():
                    mtime = max(mtime, meta_path.stat().st_mtime)
            except OSError:
                continue
            entries.append((mtime, d))
        entries.sort(key=lambda item: item[0], reverse=True)

        for rank, (mtime, d) in enumerate(entries):
            if rank < max(0, int(keep_last)):
                continue
            age_days = (now - mtime) / 86400.0
            if max_age_days > 0 and age_days <= max_age_days:
                continue
            meta = self.meta(d.name) or {}
            if str(meta.get("status") or "") == "running":
                continue
            size = _dir_size(d)
            report["candidates"].append({"run_id": d.name, "age_days": round(age_days, 1),
                                         "size_bytes": size})
            if dry_run:
                report["freed_bytes"] += size
                continue
            try:
                shutil.rmtree(d)
            except OSError as exc:
                report["errors"].append(f"{d.name}: {exc}")
                logger.warning("清理运行目录失败（%s）：%s", d.name, exc)
                continue
            report["deleted"].append(d.name)
            report["freed_bytes"] += size
            logger.info("已清理旧运行目录：%s（%.1f 天前，%.1f MB）",
                        d.name, age_days, size / (1024 * 1024))
        if not dry_run and report["deleted"]:
            # 目录变化后索引必须重建（签名含目录数与 mtime，这里显式失效更稳）
            self._index_cache = None
            self._index_signature = None
        return report

    def reconcile_interrupted(self) -> List[str]:
        """把残留的 `running` 运行标记为 `interrupted`，返回被收尾的 run_id 列表。

        运行只存在于本进程内（checkpointer 在内存、运行注册表在内存），因此**进程重启后
        不可能还有正在运行的运行**：残留的 `running` 一定是进程被杀/崩溃留下的。
        不处理会让它们永久显示"运行中"（真实缺陷：历史列表一直转圈、`finished_at` 为空）。
        这里如实标记并留下说明，而不是假装完成；`choices` 等既有字段原样保留，
        用户仍可点选候选（点选会以同一会话发起新的运行）。
        """
        fixed: List[str] = []
        if not self.root.is_dir():
            return fixed
        now = datetime.now()
        for d in sorted(self.root.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            meta = self.meta(d.name)
            if not meta or str(meta.get("status") or "") != "running":
                continue
            meta["status"] = "interrupted"
            meta["finished_at"] = meta.get("finished_at") or now.isoformat(timespec="seconds")
            if not meta.get("duration_sec"):
                created = str(meta.get("created_at") or "")
                try:
                    started = datetime.fromisoformat(created)
                    meta["duration_sec"] = round((now - started).total_seconds(), 2)
                except ValueError:
                    meta["duration_sec"] = None
            meta["error"] = meta.get("error") or "进程重启，运行被中断"
            meta["log"] = list(meta.get("log") or []) + [
                f"[{now.strftime('%H:%M:%S')}] 进程重启：该运行已中断（原状态 running）"]
            tmp = d / (RUN_META + ".tmp")
            try:
                tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp, d / RUN_META)
            except OSError as exc:
                logger.warning("收尾中断运行失败（%s）：%s", d.name, exc)
                continue
            fixed.append(d.name)
            logger.info("已将中断的运行标记为 interrupted：%s", d.name)
        return fixed

    def detail(self, run_id: str) -> Optional[Dict[str, Any]]:
        """返回 {run, artifacts, report_markdown, log, result}。"""
        meta = self.meta(run_id)
        if meta is None:
            return None
        run_dir = self.root / run_id

        def _read_json(name: str) -> Any:
            p = run_dir / name
            if p.is_file():
                try:
                    return json.loads(p.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    return None
            return None

        report_md = ""
        report_path = run_dir / "report.md"
        if report_path.is_file():
            report_md = report_path.read_text(encoding="utf-8")

        full_ranking = self.ranking_rows(run_id)
        inline_limit = env_int("RESULT_INLINE_LIMIT", 200)

        # 上万分子时，内联的分子库/性质/对接明细同样必须截断；
        # 完整数据以产物文件（molecules.json / properties.json / docking.json）提供下载。
        molecules = _read_json("molecules.json") or []
        properties = _read_json("properties.json") or []
        docking = _read_json("docking.json") or {}
        capped_receptors = []
        for block in (docking.get("receptors") or []):
            rows = block.get("results") or []
            capped_receptors.append({**block, "results": rows[:inline_limit],
                                     "results_total": len(rows)})
        docking = {**docking, "receptors": capped_receptors} if docking else {}
        result_json = _read_json("result.json") or {}
        result = {
            "molecules": molecules[:inline_limit],
            "molecules_total": len(molecules),
            "properties": properties[:inline_limit],
            "properties_total": len(properties),
            "docking": docking,
            "binding": _read_json("binding.json") or {},
            # 上万分子时不能整包内联：只给前 N 条，其余走 /ranking 分页接口
            "ranking": full_ranking[:inline_limit],
            "ranking_total": len(full_ranking),
            "ranking_inline_limit": inline_limit,
            "aggregates": self.aggregates(run_id),
            "positive_control": result_json.get("positive_control", {}),
            "receptors": result_json.get("receptors", []),
            # 结合口袋预测与对接盒溯源（v0.9）
            "pockets": result_json.get("pockets", []),
            "pocket_analysis": result_json.get("pocket_analysis", {}),
            # 流程备注（含「受体丢掉了哪些金属/辅因子」「配体盐被拆掉」这类化学事实）
            # 必须随详情一起返回：否则用户只看到分数，不知道体系里少了什么。
            "notes": (result_json.get("notes") or [])[:40],
            "task_spec": result_json.get("task_spec", {}),
            "param_plan": result_json.get("param_plan", {}),
            # 受体溯源（数据库 / accession / 物种 / PDB 号 / 方法与分辨率）：界面与报告都要能独立追溯
            "receptor_provenance": result_json.get("receptor_provenance", {}),
            "data_sources": result_json.get("data_sources", {}),
            "inline_limit": inline_limit,
        }
        return {
            "run": {k: v for k, v in meta.items() if k not in ("artifacts", "log")},
            "artifacts": meta.get("artifacts", []),
            "report_markdown": report_md,
            "log": meta.get("log", []),
            "result": result,
            # 规范下载名：前端只用这里的值，不再自行拼文件名
            "downloads": download_names(run_id, meta),
            # 多 Agent 协作痕迹：共享黑板统计与各 Agent 的备注
            "collaboration": {
                "stats": meta.get("blackboard_stats") or {},
                "notes": (_read_json("blackboard.json") or {}).get("notes", []),
            },
        }

    # ---------------- 结果分页与聚合 ----------------
    SORTABLE = ("rank", "name", "affinity_kcal_mol", "molecular_weight", "logP", "tpsa",
                "hbd", "hba", "rotatable_bonds", "lipinski_violations",
                "similarity_to_positive_control", "maccs_tanimoto", "combined_similarity",
                "exhaustiveness")

    def ranking_rows(self, run_id: str) -> List[Dict[str, Any]]:
        """读取完整排序结果（带 mtime 缓存，避免每次请求重复解析大 JSON）。"""
        path = self.root / run_id / "ranking.json"
        if path.is_file():
            mtime = path.stat().st_mtime
            cached = _RANKING_CACHE.get(run_id)
            if cached and cached[0] == mtime:
                return cached[1]
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                rows = []
            if len(_RANKING_CACHE) >= _RANKING_CACHE_MAX:
                _RANKING_CACHE.pop(next(iter(_RANKING_CACHE)))
            _RANKING_CACHE[run_id] = (mtime, rows)
            return rows
        # 兼容旧运行：回退到 result.json
        meta_path = self.root / run_id / "result.json"
        if meta_path.is_file():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8")).get("ranking") or []
            except json.JSONDecodeError:
                return []
        return []

    def positive_control_affinity(self, run_id: str) -> Optional[float]:
        path = self.root / run_id / "result.json"
        if path.is_file():
            try:
                pc = json.loads(path.read_text(encoding="utf-8")).get("positive_control") or {}
                value = pc.get("affinity_kcal_mol")
                return float(value) if isinstance(value, (int, float)) else None
            except (json.JSONDecodeError, TypeError, ValueError):
                return None
        return None

    def aggregates(self, run_id: str) -> Dict[str, Any]:
        """结果聚合统计（供大库场景的概览展示，避免前端自行遍历上万行）。"""
        import statistics

        rows = self.ranking_rows(run_id)
        affinities = [float(r["affinity_kcal_mol"]) for r in rows
                      if isinstance(r.get("affinity_kcal_mol"), (int, float))]
        pc = self.positive_control_affinity(run_id)
        hits = len([a for a in affinities if pc is not None and a < pc])
        stats: Dict[str, Any] = {}
        if affinities:
            stats = {"min": round(min(affinities), 2), "max": round(max(affinities), 2),
                     "mean": round(statistics.fmean(affinities), 2),
                     "median": round(statistics.median(affinities), 2)}
        meta = self.meta(run_id) or {}
        return {"total": len(rows), "hits": hits, "affinity": stats,
                "positive_control_affinity": pc,
                "engine": meta.get("engine"), "exhaustiveness": meta.get("exhaustiveness"),
                "with_errors": len([r for r in rows if r.get("error")])}

    def ranking_page(self, run_id: str, *, offset: int = 0, limit: int = 100,
                     sort: str = "affinity_kcal_mol", order: str = "asc",
                     q: str = "", hits_only: bool = False) -> Dict[str, Any]:
        """服务端分页 + 排序 + 搜索 + 「只看优于阳性对照」。"""
        rows = list(self.ranking_rows(run_id))
        pc = self.positive_control_affinity(run_id)

        if hits_only and pc is not None:
            rows = [r for r in rows
                    if isinstance(r.get("affinity_kcal_mol"), (int, float))
                    and r["affinity_kcal_mol"] < pc]
        keyword = (q or "").strip().lower()
        if keyword:
            rows = [r for r in rows
                    if keyword in str(r.get("name") or "").lower()
                    or keyword in str(r.get("smiles") or "").lower()]

        sort_key = sort if sort in self.SORTABLE else "affinity_kcal_mol"
        reverse = (order or "asc").strip().lower() == "desc"
        if sort_key == "rank":
            if reverse:
                rows.reverse()
        else:
            def _key(r: Dict[str, Any]):
                v = r.get(sort_key)
                if isinstance(v, bool):
                    return (0, int(v))
                if isinstance(v, (int, float)):
                    return (0, float(v))
                return (1, str(v or ""))

            rows.sort(key=_key, reverse=reverse)

        total = len(rows)
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 1000))
        return {"total": total, "offset": offset, "limit": limit,
                "rows": rows[offset:offset + limit],
                "sort": sort_key, "order": "desc" if reverse else "asc",
                "aggregates": self.aggregates(run_id)}

    def artifact_path(self, run_id: str, name: str) -> Optional[Path]:
        meta = self.meta(run_id)
        if meta is None:
            return None
        for art in meta.get("artifacts", []):
            if art.get("name") == name:
                rel = art.get("path") or ""
                if ".." in rel or rel.startswith("/"):
                    return None
                p = self.root / run_id / rel
                if p.is_file():
                    return p
                break
        # 位姿回退：大库场景不逐个登记到清单（避免清单与响应膨胀），
        # 但文件名与产物名一一对应，可直接按名解析。
        if name.startswith("pose_") and ".." not in name and "/" not in name:
            run_dir = self.root / run_id / "poses"
            for candidate in (run_dir / f"{name}.pdbqt", run_dir / f"{name}.dlg"):
                if candidate.is_file():
                    return candidate
            if run_dir.is_dir():  # 多受体时位姿在子目录中
                for sub in run_dir.iterdir():
                    if sub.is_dir():
                        for candidate in (sub / f"{name}.pdbqt", sub / f"{name}.dlg"):
                            if candidate.is_file():
                                return candidate
        return None

    def zip(self, run_id: str, *, only_poses: bool = False, dest: Optional[Path] = None) -> Optional[Path]:
        """打包运行目录（或仅位姿）。返回 zip 路径；无内容时返回 None。"""
        run_dir = self.root / run_id
        if not run_dir.is_dir():
            return None
        out_dir = runs_dir().parent / "cache" / "zip"
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = "poses" if only_poses else "full"
        dest = dest or (out_dir / f"{run_id}_{suffix}.zip")

        targets: List[Path] = []
        if only_poses:
            targets = [p for p in (run_dir / "poses").rglob("*") if p.is_file()] if (run_dir / "poses").is_dir() else []
        else:
            targets = [p for p in run_dir.rglob("*") if p.is_file() and p.name != dest.name]
        if not targets:
            return None

        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in targets:
                zf.write(p, arcname=str(p.relative_to(run_dir)))
        return dest


_store: Optional[RunStore] = None


def get_run_store() -> RunStore:
    global _store
    if _store is None:
        _store = RunStore()
    return _store


def slug(text: str) -> str:
    """生成可安全用于文件名/产物名的标识（统一实现在 core.files，避免上下层重复）。"""
    from docking_agent.core.files import slug as _slug

    return _slug(text)
