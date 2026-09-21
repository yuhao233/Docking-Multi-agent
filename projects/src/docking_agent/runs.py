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
import os
import logging
import re
import zipfile
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from docking_agent.config import env_int
from docking_agent.paths import runs_dir
from docking_agent.reporting.store import content_type_for, safe_name

logger = logging.getLogger(__name__)

RUN_META = "run.json"

# 完整排序结果的进程内缓存：{(run_id): (mtime, rows)}，避免分页请求反复解析大 JSON
_RANKING_CACHE: Dict[str, Any] = {}
_RANKING_CACHE_MAX = 4

# 当前正在执行的运行（多 Agent 模式下由 API 层注入，工具据此把中间数据写入运行目录）
current_run: ContextVar[Optional["Run"]] = ContextVar("current_run", default=None)


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + datetime.now().strftime("%f")[:4]


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
        self.id = run_id
        self.kind = kind
        self.dir = root / run_id
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

    def new(self, kind: str, request: Dict[str, Any], run_id: Optional[str] = None) -> Run:
        rid = run_id or new_run_id()
        while (self.root / rid).exists():
            rid = new_run_id()
        return Run(self.root, rid, kind, request)

    def exists(self, run_id: str) -> bool:
        return (self.root / run_id / RUN_META).is_file()

    def meta(self, run_id: str) -> Optional[Dict[str, Any]]:
        meta_path = self.root / run_id / RUN_META
        if not meta_path.is_file():
            return None
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("run.json 解析失败: %s", meta_path)
            return None

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
