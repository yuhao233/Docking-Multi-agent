"""实时逐分子结果（`molecules` 事件）的服务端回归测试。

真实缺陷：多 Agent（对话）模式下 `run_docking` 只把 `live_progress` 计数写进运行记录，
API 心跳也只会转成 `progress` 事件，于是前端「实时逐分子结果」表在整段运行里一直是空的
（只有确定性流水线 `/api/pipeline/stream` 会发 `molecules`）。修复后工具逐条上报
`live_molecules`，心跳把它们转成与流水线同构的 `molecules` 事件。

这里用一个假协调图替代真实 LLM：图里直接调用工具侧的行构造器并写运行缓冲，
其余（心跳、SSE 编码、运行记录）全走真实代码路径。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _sse_events(text: str):
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                yield json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue


class _FakeState:
    values = {"messages": []}


class _LiveMoleculeGraph:
    """假协调图：模拟「子 Agent 正在逐个上报对接结果」，每个结果之间跨一个心跳窗口。"""

    async def astream(self, payload: Any, config: Any = None,
                      stream_mode: Any = None, context: Any = None) -> AsyncIterator[Any]:
        from docking_agent.runs import current_run
        from docking_agent.tools.docking import _live_molecule_row

        run = current_run.get()
        assert run is not None, "假图必须在运行上下文里执行，才能走真实的 run.data 通路"
        rows = [
            {"name": "乙醇", "smiles": "CCO", "affinity_kcal_mol": -4.2,
             "engine": "vina", "exhaustiveness": 1},
            {"name": "甲醇", "smiles": "CO", "affinity_kcal_mol": -3.1,
             "engine": "vina", "exhaustiveness": 1},
        ]
        for i, row in enumerate(rows):
            run.data.setdefault("live_molecules", []).append(_live_molecule_row(row, i, len(rows)))
            await asyncio.sleep(1.15)      # 跨过 _interleave 的 1s 心跳 → 触发 _tick 排空
        yield ("updates", {"docking": {}})

    async def aget_state(self, config: Any = None) -> Any:
        return _FakeState()


@pytest.fixture()
def client() -> Any:
    from docking_agent.api.app import app

    with TestClient(app) as c:
        yield c


def test_agent_stream_emits_molecules_events_during_run(client: Any, monkeypatch: Any) -> None:
    """多 Agent 运行过程中必须至少发出 1 条 `molecules` 事件，且含 name / affinity 字段。"""
    import docking_agent.api.app as app_mod
    from docking_agent.agents import persistence as persistence_mod

    fake = _LiveMoleculeGraph()
    monkeypatch.setattr(app_mod.state, "graph", fake)
    # 假图没有真实工具消息，产物持久化不是本测试的对象 → 用空结果替代
    monkeypatch.setattr(persistence_mod, "persist_agent_run",
                        lambda run, messages, final_text: {"ranking": [], "molecules": []})

    r = client.post("/api/agent/stream", json={
        "mode": "chat", "advanced": False,
        "message": "帮我筛一下这两个分子 CC(=O)O 和 CCO",
        "conversation_id": "live-molecules-test",
    })
    assert r.status_code == 200
    events = list(_sse_events(r.text))
    kinds = [e.get("type") for e in events]
    assert kinds[0] == "start" and kinds[-1] == "done"

    batches = [e for e in events if e.get("type") == "molecules"]
    rows = [item for e in batches for item in (e.get("items") or [])]
    assert len(rows) >= 1, f"运行过程中应发出 molecules 事件（实际事件类型：{kinds}）"
    assert {"name", "affinity_kcal_mol"} <= set(rows[0]), rows[0]
    names = {row.get("name") for row in rows}
    assert {"乙醇", "甲醇"} <= names, names

    # 实时缓冲不得写进运行元数据（否则 meta 会被逐分子明细撑大）
    run_id = events[0]["run_id"]
    detail = client.get(f"/api/runs/{run_id}").json()
    assert "live_molecules" not in detail["run"]
    assert "live_molecules_seen" not in detail["run"]
