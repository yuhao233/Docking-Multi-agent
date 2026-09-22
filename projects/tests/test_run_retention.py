"""运行记录保留策略回归（审计：`var/runs` 2.2 GB / 4900+ 目录，且启动要遍历全部）。

看护三件事：
1. `RunStore.prune()` **默认只报告**（dry-run），`dry_run=False` 才删；
2. 删除条件保守 —— 「不属于最近 N 个」**且**「超过 M 天」两条同时满足；
3. `status == "running"` 永不删除；删除后检索索引失效（不会继续列出已删运行）。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()


def _make_run(store, run_id: str, *, status: str = "ok", age_days: float = 0.0) -> Path:
    """建一个带 run.json 的运行目录，并把 mtime 拨到 `age_days` 天前。"""
    run = store.new("agent", {"message": "x"}, run_id=run_id)
    run.finish(status)
    run_dir = run.dir
    stamp = time.time() - age_days * 86400.0
    os.utime(run_dir / "run.json", (stamp, stamp))
    os.utime(run_dir, (stamp, stamp))
    return run_dir


def test_prune_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    from docking_agent.runs import RunStore

    store = RunStore(tmp_path)
    from docking_agent.runs import RUN_META

    old = [_make_run(store, f"R-OLD-{i}", age_days=40.0) for i in range(3)]
    new = [_make_run(store, f"R-NEW-{i}", age_days=0.5) for i in range(2)]

    report = store.prune(keep_last=2, max_age_days=30.0, dry_run=True)
    assert report["dry_run"] is True
    assert report["deleted"] == []
    assert report["freed_bytes"] > 0, "报告应给出可释放的字节数"
    assert sorted(item["run_id"] for item in report["candidates"]) == \
        ["R-OLD-0", "R-OLD-1", "R-OLD-2"]
    assert all(path.is_dir() for path in old + new), "dry-run 不得动磁盘"
    assert (old[0] / RUN_META).is_file()


def test_prune_apply_deletes_only_old_and_excess(tmp_path: Path) -> None:
    from docking_agent.runs import RunStore

    store = RunStore(tmp_path)
    old = [_make_run(store, f"R-OLD-{i}", age_days=40.0) for i in range(3)]
    new = [_make_run(store, f"R-NEW-{i}", age_days=0.5) for i in range(2)]

    report = store.prune(keep_last=2, max_age_days=30.0, dry_run=False)
    assert sorted(report["deleted"]) == ["R-OLD-0", "R-OLD-1", "R-OLD-2"]
    assert all(not path.exists() for path in old)
    assert all(path.is_dir() for path in new), "最近 N 个 / 未超龄的运行必须保留"


def test_prune_keeps_recent_runs_even_if_many(tmp_path: Path) -> None:
    """数量阈值独立生效：都很旧时，最近 N 个仍然保留。"""
    from docking_agent.runs import RunStore

    store = RunStore(tmp_path)
    old = [_make_run(store, f"R-OLD-{i}", age_days=90.0) for i in range(3)]

    assert store.prune(keep_last=3, max_age_days=1.0, dry_run=False)["deleted"] == []
    assert all(path.is_dir() for path in old)

    report = store.prune(keep_last=1, max_age_days=1.0, dry_run=False)
    assert len(report["deleted"]) == 2
    assert len([p for p in old if p.is_dir()]) == 1


def test_prune_never_deletes_running_runs(tmp_path: Path) -> None:
    from docking_agent.runs import RunStore

    store = RunStore(tmp_path)
    running = _make_run(store, "R-RUNNING", status="running", age_days=400.0)
    stale = _make_run(store, "R-STALE", status="ok", age_days=400.0)

    report = store.prune(keep_last=0, max_age_days=1.0, dry_run=False)
    assert report["deleted"] == ["R-STALE"]
    assert running.is_dir(), "正在运行的目录绝不能被清理"
    assert not stale.exists()


def test_prune_invalidates_search_index(tmp_path: Path) -> None:
    """清理后检索不得再列出已删除的运行（索引签名可能不变，必须显式失效）。"""
    from docking_agent.runs import RunStore

    store = RunStore(tmp_path)
    _make_run(store, "R-KEEP-RECENT", age_days=0.1)
    _make_run(store, "R-DROP-OLD", age_days=45.0)
    assert {row["run_id"] for row in store.search(q="")["runs"]} >= {"R-KEEP-RECENT", "R-DROP-OLD"}

    store.prune(keep_last=1, max_age_days=30.0, dry_run=False)
    hits = {row["run_id"] for row in store.search(q="")["runs"]}
    assert "R-DROP-OLD" not in hits and "R-KEEP-RECENT" in hits
