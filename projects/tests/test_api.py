"""API 契约测试（含一次真实的单分子对接，约数秒）。

运行： .venv/bin/python -m pytest tests/test_api.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="module")
def client():
    from docking_agent.api.app import app

    with TestClient(app) as c:
        yield c


def _sse_events(text: str):
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                yield json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue


# --------------------------------------------------------------------------- #
# 元信息
# --------------------------------------------------------------------------- #
def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "llm_configured" in body
    assert body["engine_available"]["vina"] is True


def test_receptors_exposes_known_site(client):
    body = client.get("/api/receptors").json()
    assert body["default"] in {r["key"] for r in body["receptors"]}
    thrombin = next(r for r in body["receptors"] if r["key"] == "thrombin")
    assert len(thrombin["site"]["center"]) == 3
    assert len(thrombin["site"]["size"]) == 3
    assert thrombin["available"] is True


def test_libraries(client):
    body = client.get("/api/libraries").json()
    assert body["libraries"], "示例分子库应存在"
    assert body["libraries"][0]["count"] >= 5
    assert body["positive_control"]["smiles"]


def test_molecule_endpoints(client):
    r = client.get("/api/molecule/depict", params={"smiles": "CCO", "width": 200, "height": 150})
    assert r.status_code == 200
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    r = client.get("/api/molecule/properties", params={"smiles": "CCO"})
    assert r.status_code == 200
    assert r.json()["formula"] == "C2H6O"

    assert client.get("/api/molecule/depict", params={"smiles": "不是SMILES"}).status_code == 400


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()
    assert "/static/app.js" in r.text


def test_unknown_run_404(client):
    assert client.get("/api/runs/does-not-exist").status_code == 404


# --------------------------------------------------------------------------- #
# 执行 + 中间数据闭环
# --------------------------------------------------------------------------- #
def test_pipeline_stream_and_artifacts(client):
    payload = {
        "receptor": "thrombin",
        "ligands_text": "乙醇:CCO",
        "positive_control": "NC(=N)c1ccccc1",
        "exhaustiveness": 1,
        "n_poses": 1,
        "engine": "vina",
        "save_poses": True,
        "max_ligands": 1,
    }
    r = client.post("/api/pipeline/stream", json=payload)
    assert r.status_code == 200
    events = list(_sse_events(r.text))
    kinds = [e.get("type") for e in events]

    assert kinds[0] == "start"
    assert "stage" in kinds and "progress" in kinds
    # 逐分子结果：小库用单条 molecule，大库用批量 molecules，两者都要支持
    assert ("molecule" in kinds) or ("molecules" in kinds)
    assert kinds[-1] == "done"

    run_id = events[0]["run_id"]
    mol_events = [e for e in events if e.get("type") == "molecule"]
    mol_events += [item for e in events if e.get("type") == "molecules" for item in e.get("items", [])]
    assert mol_events, "应至少推送一条逐分子结果"
    assert "affinity_kcal_mol" in mol_events[0]
    assert mol_events[0]["pose_url"], "开启保存位姿时应给出位姿下载地址"

    # ---- 运行记录 ----
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["run"]["status"] == "ok"
    assert detail["run"]["molecule_count"] == 1
    ranking = detail["result"]["ranking"]
    assert ranking and ranking[0]["name"]
    assert isinstance(ranking[0]["affinity_kcal_mol"], (int, float))
    assert detail["report_markdown"].startswith("# ")

    # ---- 产物清单 ----
    names = {a["name"] for a in detail["artifacts"]}
    for expected in ("molecules", "properties", "docking", "binding", "ranking_csv",
                     "docking_chart", "similarity_chart", "report_md"):
        assert expected in names, f"缺少产物 {expected}（实际：{sorted(names)}）"

    # ---- 单项下载 ----
    csv_art = next(a for a in detail["artifacts"] if a["name"] == "ranking_csv")
    csv_resp = client.get(csv_art["download_url"])
    assert csv_resp.status_code == 200
    lines = [ln for ln in csv_resp.text.splitlines() if ln.strip()]
    assert lines[0].startswith("rank,id,name,smiles")
    assert "engine" in lines[0] and "exhaustiveness" in lines[0]

    png_art = next(a for a in detail["artifacts"] if a["name"] == "docking_chart")
    png_resp = client.get(png_art["inline_url"])
    assert png_resp.status_code == 200 and png_resp.content[:8] == b"\x89PNG\r\n\x1a\n"

    # ---- 整体打包 ----
    z = client.get(f"/api/runs/{run_id}/download.zip")
    assert z.status_code == 200 and z.content[:2] == b"PK"

    # ---- 位姿打包 ----
    p = client.get(f"/api/runs/{run_id}/poses.zip")
    assert p.status_code == 200 and p.content[:2] == b"PK"

    # ---- 历史列表包含本次运行 ----
    runs = client.get("/api/runs", params={"limit": 50}).json()["runs"]
    assert run_id in {x["run_id"] for x in runs}


# --------------------------------------------------------------------------- #
# 指令与参数冲突：mode / advanced 语义
# --------------------------------------------------------------------------- #
def test_agent_message_modes_avoid_conflict():
    """manual=参数权威；chat 折叠=系统默认；chat 展开=高级设置值；三种都不会与指令冲突。"""
    from docking_agent.intake import compose_agent_message
    from docking_agent.api.schemas import AgentRequest

    base = dict(message="请用 trypsin 做一次筛选，exhaustiveness 用 4",
                receptor="thrombin", ligands_text="A:CCO", positive_control="NC(=N)c1ccccc1",
                exhaustiveness=16, engine="vina")

    # chat + 折叠：使用系统默认参数，并声明「指令已指定的以指令为准」
    collapsed = compose_agent_message(AgentRequest(mode="chat", advanced=False, **base))
    assert collapsed.startswith(base["message"])
    assert "系统默认" in collapsed and "以指令为准" in collapsed
    # v0.26：留空 = 自动 —— 折叠高级设置时搜索强度按库/盒自动规划，不渲染成"固定 16"
    assert "自动（按库柔性/盒体积规划" in collapsed, collapsed[:400]
    assert "已知结合位点：**未指定坐标**" in collapsed, "位点盒留空 = 由口袋分析自动定盒"
    assert "A:CCO" not in collapsed, "折叠时不应使用高级设置里的分子库（那是系统默认场景）"

    # chat + 展开：使用高级设置里的参数，仍以指令为准
    expanded = compose_agent_message(AgentRequest(mode="chat", advanced=True, **base))
    assert "来自高级设置" in expanded and "以指令为准" in expanded
    assert "A:CCO" in expanded

    # manual：参数是权威来源，且指令可留空
    hard = compose_agent_message(AgentRequest(mode="manual", **base))
    assert "权威参数" in hard and "以本节为准" in hard
    empty = compose_agent_message(AgentRequest(mode="manual"))
    assert "完整的分子筛选" in empty
    assert "自动（按库柔性/盒体积规划" in empty, "未填参数 = 自动，不冒充用户指定"

    # 阳性对照可选：明确跳过时要在指令中说明
    skipped = compose_agent_message(AgentRequest(mode="manual", skip_positive_control=True))
    assert "不使用" in skipped and "跳过" in skipped


def test_agent_request_defaults_to_manual():
    from docking_agent.api.schemas import AgentRequest

    req = AgentRequest()
    assert req.mode == "manual" and req.advanced is False


def test_ranking_api_pagination_and_export(client):
    """结果分页接口与 CSV 导出（真实对接 3 个分子，费用低）。"""
    payload = {
        "receptor": "thrombin",
        "ligands_text": "乙醇:CCO,甲醇:CO,丙醇:CCCO",
        "positive_control": "NC(=N)c1ccccc1",
        "exhaustiveness": 1, "engine": "vina", "save_poses": False,
    }
    r = client.post("/api/pipeline/stream", json=payload)
    assert r.status_code == 200
    run_id = next(e["run_id"] for e in _sse_events(r.text) if e.get("type") == "start")

    detail = client.get(f"/api/runs/{run_id}").json()["result"]
    assert detail["ranking_total"] == 3
    assert "ranking_inline_limit" in detail
    assert detail["aggregates"]["total"] == 3
    assert set(detail["aggregates"]["affinity"]) == {"min", "max", "mean", "median"}

    page = client.get(f"/api/runs/{run_id}/ranking",
                      params={"offset": 0, "limit": 2, "sort": "affinity_kcal_mol",
                              "order": "asc"}).json()
    assert page["total"] == 3 and len(page["rows"]) == 2
    assert page["rows"][0]["affinity_kcal_mol"] <= page["rows"][1]["affinity_kcal_mol"]
    assert page["aggregates"]["total"] == 3

    desc = client.get(f"/api/runs/{run_id}/ranking",
                      params={"limit": 1, "order": "desc"}).json()
    assert desc["rows"][0]["affinity_kcal_mol"] >= page["rows"][0]["affinity_kcal_mol"]

    search = client.get(f"/api/runs/{run_id}/ranking", params={"q": "乙醇"}).json()
    assert search["total"] == 1 and search["rows"][0]["name"] == "乙醇"

    hits = client.get(f"/api/runs/{run_id}/ranking", params={"hits_only": "true"}).json()
    pc = detail["aggregates"]["positive_control_affinity"]
    if pc is not None:
        assert all(row["affinity_kcal_mol"] < pc for row in hits["rows"])

    csv_resp = client.get(f"/api/runs/{run_id}/export.csv")
    assert csv_resp.status_code == 200
    assert csv_resp.text.splitlines()[0].startswith("rank,id,name,smiles")
    # 表头 + 3 个分子 + 阳性对照行 = 5 行
    assert len([ln for ln in csv_resp.text.splitlines() if ln.strip()]) == 5


def test_chat_collapsed_uses_system_defaults(client):
    """chat 折叠时下发系统默认参数（而不是什么都不给）。"""
    from docking_agent.intake import compose_agent_message
    from docking_agent.api.schemas import AgentRequest

    msg = compose_agent_message(AgentRequest(mode="chat", advanced=False,
                                              message="用示例库筛选一下"))
    assert "系统默认" in msg
    assert "自动（按库柔性/盒体积规划" in msg
    assert "engine=auto" in msg
    # 明确要求示例库时才渲染「用示例库」的指令（默认不自动使用）
    assert "明确要求使用内置示例库" in msg and "allow_example_fallback=true" in msg, msg[-200:]


def test_chat_advanced_opens_but_untouched_sends_no_params() -> None:
    """用户反馈：只是**打开过**「高级设置」再关掉，默认值却被当成参数下发。

    语义（v0.26）：只下发**用户真正改动过**的字段；未改动 = 留空/自动 ——
    搜索强度自动规划、位点盒由口袋分析自动确定、受体由指令或系统默认决定、不做阳性对照。
    因此 `advanced=True` + 空表单 必须与"纯对话"等价（除附件外不带任何参数）。
    """
    from docking_agent.api.schemas import AgentRequest
    from docking_agent.intake import build_task_spec, compose_agent_message

    req = AgentRequest(mode="chat", advanced=True, message="用上传文件对接")
    spec = build_task_spec(req)
    params = spec["params"]
    assert params["exhaustiveness"] is None, params
    assert params["n_poses"] is None, params
    assert not str(params["engine"]).strip(), params
    assert params.get("site") in (None, (), "") or not params.get("site"), params
    control = spec.get("positive_control") or {}
    assert not str(control.get("smiles") or "").strip(), control
    assert params.get("positive_control") in (None, ""), params

    msg = compose_agent_message(req)
    assert "自动（按库柔性/盒体积规划" in msg, msg[:400]
    assert "已知结合位点：**未指定坐标**" in msg
    assert "阳性对照：未提供" in msg
