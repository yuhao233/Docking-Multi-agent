"""运行目录**损坏注入**：残缺产物不得把接口打成 500。

`var/runs/` 是唯一的持久化事实源，实测有 3 800+ 个历史目录，且写入并非全部原子
（只有 `run.json` 用 tmp+replace，`result.json` / `ranking.json` 是原地写）。
因此"半写文件"是**会发生**的状态：进程被杀、磁盘写满、并发读。

真实风险不是"数据坏了"（那是事实），而是**坏了以后接口 500 / 页面白屏**——
用户连"这次运行出问题了"都看不到。这里把当前的**优雅降级契约**钉死：

| 损坏形态 | 期望行为 |
| --- | --- |
| `run.json` 截断 / 缺字段 | 该运行视为不可读：`meta/detail` 返回 `None`；历史列表跳过它；HTTP 404（不是 500） |
| `ranking.json` 截断 | 排序为空列表（不抛异常），接口 200 |
| `result.json` 截断 | 同上（回退路径也不抛） |
| 半写 `ranking.csv` | 原样作为下载内容提供（不解析、不报错） |

坏文件都会被 `logger.warning` 记一笔（不静默），见 `runs.py:RunStore.meta`。
"""
from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path
from typing import Iterator

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()


# --------------------------------------------------------------------------- #
# 1) 存储层：损坏不抛异常
# --------------------------------------------------------------------------- #
def test_truncated_run_json_is_skipped_not_crashed(tmp_path: Path) -> None:
    """截断的 run.json → meta/detail 为 None，历史列表跳过它但保留正常记录。"""
    from docking_agent.runs import RunStore

    store = RunStore(tmp_path / "runs")
    good = store.new("agent", {"mode": "manual"})
    good.write_json("result", {"ranking": [], "molecules": []})
    bad = store.new("agent", {"mode": "manual"})
    (bad.dir / "run.json").write_text('{"run_id": "x", "sta', encoding="utf-8")

    assert store.meta(good.id) is not None
    assert store.meta(bad.id) is None, "截断的 run.json 必须按不可读处理"
    assert store.detail(bad.id) is None
    listed = [row["run_id"] for row in store.list()]
    assert good.id in listed and bad.id not in listed


def test_missing_run_json_is_treated_as_absent(tmp_path: Path) -> None:
    from docking_agent.runs import RunStore

    store = RunStore(tmp_path / "runs")
    run = store.new("agent", {"mode": "manual"})
    (run.dir / "run.json").unlink()
    assert store.meta(run.id) is None
    assert store.detail(run.id) is None
    assert [r["run_id"] for r in store.list()] == []


def test_run_json_with_valid_json_but_missing_fields_does_not_crash(tmp_path: Path) -> None:
    """合法 JSON 但字段不全（例如只写了一半的键）也要能读出来，不能 KeyError。"""
    from docking_agent.runs import RunStore

    store = RunStore(tmp_path / "runs")
    run = store.new("agent", {"mode": "manual"})
    (run.dir / "run.json").write_text(json.dumps({"run_id": run.id}), encoding="utf-8")
    meta = store.meta(run.id)
    assert meta is not None and meta["run_id"] == run.id
    detail = store.detail(run.id)
    assert detail is not None and "downloads" in detail


def test_truncated_ranking_json_yields_empty_rows(tmp_path: Path) -> None:
    from docking_agent.runs import RunStore

    store = RunStore(tmp_path / "runs")
    run = store.new("agent", {"mode": "manual"})
    (run.dir / "ranking.json").write_text('[{"name": "x", "smiles": "CC', encoding="utf-8")
    assert store.ranking_rows(run.id) == []


def test_truncated_result_json_yields_empty_rows(tmp_path: Path) -> None:
    """没有 ranking.json 时会回退读 result.json —— 那条路径同样不能抛。"""
    from docking_agent.runs import RunStore

    store = RunStore(tmp_path / "runs")
    run = store.new("agent", {"mode": "manual"})
    (run.dir / "result.json").write_text('{"ranking": [{"name": "x"', encoding="utf-8")
    assert store.ranking_rows(run.id) == []


# --------------------------------------------------------------------------- #
# 2) HTTP 层：损坏运行不得打成 500
# --------------------------------------------------------------------------- #
@pytest.fixture()
def corrupted_run() -> Iterator[str]:
    """在真实运行目录里造一个"半写"运行，测试结束即删除。"""
    from docking_agent.paths import runs_dir

    root = runs_dir()
    rid = f"corrupt-probe-{uuid.uuid4().hex[:8]}"
    d = root / rid
    d.mkdir(parents=True)
    (d / "run.json").write_text(f'{{"run_id": "{rid}", "status": "ok", "sta', encoding="utf-8")
    (d / "result.json").write_text('{"ranking": [{"name": "x", "smiles": "CCO"', encoding="utf-8")
    (d / "ranking.json").write_text('[{"name": "x", "smiles": "CCO", "affinity', encoding="utf-8")
    (d / "ranking.csv").write_text("rank,name,smiles\n1,乙醇,CCO\n2,甲醇", encoding="utf-8")
    (d / "report.md").write_text("# 报告\n\n正文", encoding="utf-8")
    try:
        yield rid
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_corrupted_run_never_returns_500(corrupted_run: str) -> None:
    """逐个体检读接口：只允许 200 / 404，绝不允许 500。"""
    from fastapi.testclient import TestClient

    from docking_agent.api.app import app

    paths = [f"/api/runs/{corrupted_run}", f"/api/runs/{corrupted_run}/ranking",
             f"/api/runs/{corrupted_run}/export.csv", f"/api/runs/{corrupted_run}/report.pdf",
             f"/api/runs/{corrupted_run}/download.zip", f"/api/runs/{corrupted_run}/poses.zip"]
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/runs?limit=5").status_code == 200, "历史列表不能被坏记录打挂"
        for path in paths:
            resp = client.get(path)
            assert resp.status_code in (200, 404), (
                f"{path} 返回 {resp.status_code}：损坏运行不得打成 5xx\n{resp.text[:200]}")
