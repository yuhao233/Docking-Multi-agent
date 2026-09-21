"""历史运行检索：关掉页面后仍能找回之前的运行结果。

用户需求原话：「增加一下历史任务查询机制，让用户在关闭页面后也可以再查询之前的运行结果」。
运行记录本来就持久化在 `var/runs/<run_id>/`，这里补的是**检索能力**：关键词（run_id / 受体 /
任务描述 / 排序表里的分子名与 ID）、状态、类型、受体、时间范围、分页。

真实数据上实测（本机 3.4k 条运行）：首次建索引约 0.4 s，之后走进程内缓存（毫秒级）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from docking_agent.runs import get_run_store

# --------------------------------------------------------------------------- #
# 脚手架：在临时工作区里造 4 条真实结构的运行目录
# --------------------------------------------------------------------------- #
def _write_run(root: Path, run_id: str, *, status: str, receptor: str, created: str,
               molecules: str = "", goal: str = "") -> None:
    run_dir = root / "var" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {"run_id": run_id, "kind": "agent", "status": status, "created_at": created,
            "receptor_label": receptor, "molecule_count": 2,
            "request": {"message": goal or f"对接受体 {receptor}"},
            "notes": [f"本次使用受体 {receptor}"], "artifacts": [], "log": []}
    (run_dir / "run.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    if molecules:
        rows = ["rank,id,name,smiles,affinity_kcal_mol"]
        for i, name in enumerate(molecules.split(","), start=1):
            rows.append(f"{i},PGR{i:03d},{name},CCO,-5.0")
        (run_dir / "ranking.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")


@pytest.fixture()
def populated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("DOCKING_WORKSPACE", str(tmp_path))
    import docking_agent.runs as R

    R._store = None                       # 每个用例独立工作区：清掉 store 单例
    root = tmp_path
    _write_run(root, "20260921-100000-aaaa", status="ok", receptor="thrombin(1DWC)",
               created="2026-09-21T10:00:00", molecules="阿司匹林,布洛芬", goal="对接受体 thrombin")
    _write_run(root, "20260920-090000-bbbb", status="ok", receptor="trypsin(1PTU)",
               created="2026-09-20T09:00:00", molecules="姜黄素", goal="试试 trypsin")
    _write_run(root, "20260919-080000-cccc", status="no_op", receptor="",
               created="2026-09-19T08:00:00", goal="你好")
    _write_run(root, "20260918-070000-dddd", status="error", receptor="thrombin(1DWC)",
               created="2026-09-18T07:00:00", goal="受体文件缺失")
    return root


# --------------------------------------------------------------------------- #
# 1) 关键词：run_id / 受体 / 分子名 / 分子 ID / 多词 AND
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("query,expected", [
    ("20260921-100000-aaaa", ["20260921-100000-aaaa"]),
    ("阿司匹林", ["20260921-100000-aaaa"]),
    # 排序表里的分子 ID 也参与匹配（索引只读排序表前若干行，故各运行的首行 ID 都会命中）
    ("PGR001", ["20260921-100000-aaaa", "20260920-090000-bbbb"]),
    ("trypsin", ["20260920-090000-bbbb"]),
    ("thrombin 布洛芬", ["20260921-100000-aaaa"]),
    ("thrombin 姜黄素", []),
])
def test_keyword_search(populated: Path, query: str, expected: list) -> None:
    out = get_run_store().search(q=query, limit=10)
    assert [r["run_id"] for r in out["runs"]] == expected, out


# --------------------------------------------------------------------------- #
# 2) 状态 / 类型 / 受体 / 时间范围
# --------------------------------------------------------------------------- #
def test_filters(populated: Path) -> None:
    store = get_run_store()
    assert store.search(status="ok")["total"] == 2
    assert store.search(status="no_op")["total"] == 1
    assert store.search(receptor="trypsin")["total"] == 1
    assert store.search(since="2026-09-20")["total"] == 2
    assert store.search(until="2026-09-19")["total"] == 2
    assert store.search(since="2026-09-19", until="2026-09-20")["total"] == 2
    assert store.search(kind="agent")["total"] == 4


def test_pagination_and_shape(populated: Path) -> None:
    store = get_run_store()
    page1 = store.search(limit=2, offset=0)
    page2 = store.search(limit=2, offset=2)
    assert page1["total"] == page2["total"] == 4
    assert len(page1["runs"]) == 2 and len(page2["runs"]) == 2
    assert {r["run_id"] for r in page1["runs"]} & {r["run_id"] for r in page2["runs"]} == set()
    assert page1["offset"] == 0 and page1["limit"] == 2
    assert set(page1["runs"][0]) == {"run_id", "created_at", "status", "kind", "receptor",
                                     "molecule_count"}, "索引行不应把内部检索文本暴露给前端"
    assert page1["runs"][0]["run_id"] == "20260921-100000-aaaa", "默认按时间倒序"


def test_search_results_are_loadable_by_detail_api(populated: Path) -> None:
    """检索命中的 run_id 必须能直接用于 `/api/runs/{id}` 载入（用户点「载入」的路径）。"""
    store = get_run_store()
    hit = store.search(q="阿司匹林")["runs"][0]["run_id"]
    detail = store.detail(hit)
    assert detail is not None and detail["run"]["status"] == "ok"


# --------------------------------------------------------------------------- #
# 3) HTTP 层：带检索参数返回分页结构，不带参数保持旧结构
# --------------------------------------------------------------------------- #
def test_api_runs_search_and_backward_compat(populated: Path) -> None:
    from fastapi.testclient import TestClient

    from docking_agent.api.app import app

    with TestClient(app) as client:
        legacy = client.get("/api/runs", params={"limit": 2}).json()
        assert "runs" in legacy and "total" not in legacy, "不带检索参数时保持旧结构"
        assert len(legacy["runs"]) == 2

        found = client.get("/api/runs", params={"q": "阿司匹林", "limit": 5}).json()
        assert found["total"] == 1
        assert found["runs"][0]["run_id"] == "20260921-100000-aaaa"
        assert found["query"]["q"] == "阿司匹林"

        empty = client.get("/api/runs", params={"q": "不存在的分子"}).json()
        assert empty["total"] == 0 and empty["runs"] == []


# --------------------------------------------------------------------------- #
# 4) 被中断的运行必须收尾：进程重启后不可能还有运行在执行
# --------------------------------------------------------------------------- #
def test_reconcile_marks_stale_running_runs_as_interrupted(populated: Path) -> None:
    """残留的 running 只能来自进程被杀/崩溃 —— 收尾为 interrupted 并留下说明。

    真实缺陷：用户看到历史里某次运行一直「运行中」、`finished_at` 为空，实际早已中断。
    """
    from docking_agent.runs import get_run_store

    store = get_run_store()
    stale = populated / "var" / "runs" / "20260921-100000-aaaa" / "run.json"
    meta = json.loads(stale.read_text(encoding="utf-8"))
    meta["status"] = "running"
    meta["finished_at"] = None
    meta["duration_sec"] = None
    meta["choices"] = [{"id": "x", "kind": "molecule", "label": "候选", "prompt": "继续"}]
    stale.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    fixed = store.reconcile_interrupted()
    assert fixed == ["20260921-100000-aaaa"], fixed
    after = json.loads(stale.read_text(encoding="utf-8"))
    assert after["status"] == "interrupted"
    assert after["finished_at"] and after["duration_sec"] is not None
    assert "进程重启" in str(after["error"])
    assert any("已中断" in line for line in after["log"]), after["log"][-2:]
    # 候选必须保留：用户仍需从中点选（点选会以同一会话发起新运行）
    assert len(after["choices"]) == 1

    # 已完成/失败的运行不受影响；重复调用是幂等的
    assert store.reconcile_interrupted() == []
    assert json.loads((populated / "var" / "runs" / "20260920-090000-bbbb" / "run.json")
                      .read_text(encoding="utf-8"))["status"] == "ok"


def test_app_startup_reconciles_interrupted_runs(populated: Path) -> None:
    """启动钩子必须真的调用收尾（否则重启后仍会显示「运行中」）。"""
    from fastapi.testclient import TestClient

    from docking_agent.api.app import app

    stale = populated / "var" / "runs" / "20260919-080000-cccc" / "run.json"
    meta = json.loads(stale.read_text(encoding="utf-8"))
    meta["status"] = "running"
    stale.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    with TestClient(app):
        pass
    assert json.loads(stale.read_text(encoding="utf-8"))["status"] == "interrupted"
