"""pipeline 图（显式 StateGraph）：打桩 run_pipeline，验证运行目录 / 状态 / 返回体。

不跑真实对接：`docking_graphs.graphs.run_pipeline` 被替换成同步桩函数。
真实 `docking_agent.pipeline.run_pipeline` 的契约是「自己收尾运行状态」
（成功 `run.finish("ok")`、失败 `run.finish("error", ...)` 后再抛），因此桩函数
分两类：

* **契约桩**（`_ContractStub`）：模拟真实内层函数的行为 → 断言部署层管道全绿；
* **裸桩**（只返回字典 / 直接抛异常）：暴露部署层自身不收尾状态的健壮性缺口，
  见文件末尾两个 `xfail` 用例。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.offline


def _stub_result(**overrides):
    result = {
        "status": "ok",
        "message": "stub-pipeline",
        "molecules": [{"name": "ethanol", "smiles": "CCO"}],
        "ranking": [{"name": "ethanol", "smiles": "CCO", "affinity_kcal_mol": -1.23}],
        "param_plan": {"exhaustiveness": 4, "source": "stub"},
        "notes": ["stub note"],
    }
    result.update(overrides)
    return result


def _runs(workspace: Path):
    return sorted((workspace / "var" / "runs").iterdir())


@pytest.fixture
def recorded_calls():
    return []


# --------------------------------------------------------------------------- #
# 契约桩：模拟真实 run_pipeline（自己 finish）
# --------------------------------------------------------------------------- #
async def test_pipeline_success_creates_run_and_studio_result(
        workspace, graphs_module, monkeypatch, recorded_calls):
    def stub(**kwargs):
        recorded_calls.append(kwargs)
        run = kwargs["run"]
        run.write_text("report.md", "# report", name="report_md",
                       label="分析报告", content_type="text/markdown")
        run.finish("ok")
        return _stub_result()

    monkeypatch.setattr(graphs_module, "run_pipeline", stub)
    graph = graphs_module.pipeline()

    out = await graph.ainvoke({"ligands_text": "CCO", "engine": "vina",
                               "save_poses": True, "receptor": "thrombin"})

    # 返回体
    assert out["status"] == "ok"
    assert out["run_id"]
    assert Path(out["run_dir"]).is_dir()
    assert Path(out["run_dir"]).name == out["run_id"]
    assert isinstance(out["artifacts"], list)
    assert out["molecule_count"] == 1
    assert out["top"][0]["name"] == "ethanol"
    assert out["param_plan"] == {"exhaustiveness": 4, "source": "stub"}
    assert out["notes"] == ["stub note"]

    # run 目录 + run.json
    run_dir = Path(out["run_dir"])
    assert run_dir.parent == workspace / "var" / "runs"
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "ok", f"run.json 状态应为 ok，实际 {meta['status']!r}"
    assert meta["kind"] == "studio"
    assert meta["request"]["graph"] == "pipeline"
    assert meta["request"]["engine"] == "vina"
    assert meta["finished_at"] is not None

    # studio_result.json 落盘且是摘要
    studio_path = run_dir / "studio_result.json"
    assert studio_path.is_file()
    studio = json.loads(studio_path.read_text(encoding="utf-8"))
    assert studio["status"] == "ok"
    assert studio["run_id"] == out["run_id"]
    assert studio["molecule_count"] == 1
    # studio_result 自身也是产物：写完后刷新清单，artifacts 里应能看到它
    names = {a["name"] for a in out["artifacts"]}
    assert "studio_result" in names
    assert "report_md" in names

    # 桩函数拿到的参数与图输入一致（确认部署层正确转发）
    assert len(recorded_calls) == 1
    call = recorded_calls[0]
    assert call["run"] is not None
    assert call["ligands_text"] == "CCO"
    assert call["engine"] == "vina"
    assert call["receptor"] == "thrombin"
    assert call["save_poses"] is True


async def test_pipeline_error_finish_then_raise(workspace, graphs_module, monkeypatch):
    """内层抛错（并如实 finish error）→ run.json 为 error，异常继续向上抛。"""

    def stub(**kwargs):
        run = kwargs["run"]
        run.finish("error", error="boom")
        raise RuntimeError("boom")

    monkeypatch.setattr(graphs_module, "run_pipeline", stub)
    graph = graphs_module.pipeline()

    with pytest.raises(RuntimeError, match="boom"):
        await graph.ainvoke({"ligands_text": "CCO"})

    run_dirs = _runs(workspace)
    assert len(run_dirs) == 1
    meta = json.loads((run_dirs[0] / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "error"
    assert meta["error"] == "boom"


async def test_pipeline_site_center_becomes_site_dict(workspace, graphs_module, monkeypatch):
    """site_center/site_size 应被组装成 site={center,size} 再交给内层。"""
    seen = {}

    def stub(**kwargs):
        seen.update(kwargs)
        kwargs["run"].finish("ok")
        return _stub_result()

    monkeypatch.setattr(graphs_module, "run_pipeline", stub)
    await graphs_module.pipeline().ainvoke(
        {"ligands_text": "CCO", "site_center": [1.0, 2.0, 3.0], "site_size": [10.0, 11.0, 12.0]})

    assert seen["site"] == {"center": [1.0, 2.0, 3.0], "size": [10.0, 11.0, 12.0]}


async def test_pipeline_site_size_defaults_when_only_center(workspace, graphs_module, monkeypatch):
    seen = {}

    def stub(**kwargs):
        seen.update(kwargs)
        kwargs["run"].finish("ok")
        return _stub_result()

    monkeypatch.setattr(graphs_module, "run_pipeline", stub)
    await graphs_module.pipeline().ainvoke({"ligands_text": "CCO", "site_center": [1.0, 2.0, 3.0]})

    assert seen["site"] == {"center": [1.0, 2.0, 3.0], "size": [22.0, 22.0, 22.0]}


# --------------------------------------------------------------------------- #
# 裸桩：部署层必须**自行收尾**运行状态（曾是不收尾的真实缺陷，现已修复并回归保护）
# --------------------------------------------------------------------------- #
async def test_pipeline_success_without_inner_finish_is_finalized(
        workspace, graphs_module, monkeypatch):
    """回归保护（真实缺陷，已修）：内层不 finish 时，节点自己兜底 finish，不留 running。"""
    monkeypatch.setattr(graphs_module, "run_pipeline",
                        lambda **kwargs: _stub_result())
    out = await graphs_module.pipeline().ainvoke({"ligands_text": "CCO"})
    meta = json.loads((Path(out["run_dir"]) / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "ok", f"裸桩返回后 run.json 仍是 {meta['status']!r}"


async def test_pipeline_error_without_inner_finish_is_marked_error(
        workspace, graphs_module, monkeypatch):
    """回归保护（真实缺陷，已修）：内层裸抛异常时，节点落 error 后再抛。"""
    def bare(**kwargs):
        raise RuntimeError("bare boom")

    monkeypatch.setattr(graphs_module, "run_pipeline", bare)
    with pytest.raises(RuntimeError, match="bare boom"):
        await graphs_module.pipeline().ainvoke({"ligands_text": "CCO"})

    run_dirs = _runs(workspace)
    meta = json.loads((run_dirs[-1] / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] == "error", f"裸桩抛错后 run.json 仍是 {meta['status']!r}"
