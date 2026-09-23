"""大库参数规划与两阶段漏斗：**库解析完成后再规划一次**（库来自上传文件时受理层拿不到）。

真实缺口（2026-09-23，运行 20260923-224338-3779）：分子来自上传的 SDF，受理层规划时库里
还没有分子 → `param_plan` 整份为空 → notes 写"本次无规划值"，2961 个分子全部按系统默认
exhaustiveness=16 精算 40 分钟，粗筛/精算两阶段一次都没跑。
"""
from __future__ import annotations

from typing import Any, Dict, List


def _fake_run(tmp_path: Any = None):
    """最小 run 替身：只记录 data / log / dir（工具层只用到这三样）。"""
    from pathlib import Path

    class _Run:
        def __init__(self) -> None:
            self.data: Dict[str, Any] = {}
            self.logs: List[str] = []
            self.dir = Path(tmp_path) if tmp_path else Path(".")
            self.id = "test-run"

        def log(self, message: str) -> None:
            self.logs.append(str(message))

    return _Run()


def _patch_runtime(monkeypatch: Any, run: Any) -> None:
    from docking_agent.tools import docking as D

    monkeypatch.setattr(D, "active_run", lambda runtime=None: run)
    monkeypatch.setattr(D, "active_blackboard", lambda runtime=None: None)


def test_large_library_gets_a_late_plan_and_two_stage_funnel(monkeypatch: Any, tmp_path: Any) -> None:
    """2961 条库：工具内部必须补规划，并按「粗筛全库 → 精算头部」跑两遍。"""
    import os

    from docking_agent.tools import docking as D

    funnel_min = int(os.environ.get("AGENT_FUNNEL_MIN") or 500)
    # 每条必须**化学身份不同**：同 SMILES 会被身份去重折叠成一条（这正是"库级去重"的正确行为）
    molecules = [{"name": f"M{n}", "smiles": f"{'C' * n}O"} for n in range(1, funnel_min + 51)]
    run = _fake_run(tmp_path)
    _patch_runtime(monkeypatch, run)

    calls: List[Dict[str, Any]] = []

    def _fake_dock_library(mols, **kwargs):
        calls.append({"n": len(mols), "exh": kwargs.get("exhaustiveness")})
        rows = [{"name": m["name"], "smiles": m["smiles"],
                 "affinity_kcal_mol": -5.0 - (i % 100) * 0.01, "engine": "vina"}
                for i, m in enumerate(mols)]
        return {"status": "ok", "receptors": [{"receptor_key": "r", "results": rows}], "notes": []}

    monkeypatch.setattr(D, "dock_library", _fake_dock_library)
    monkeypatch.setattr(D, "active_run", lambda runtime=None: run)
    out = D.molecular_docking.func(
        molecules_json=__import__("json").dumps(molecules), receptor_sources="thrombin",
        exhaustiveness=0, n_poses=1)

    assert run.data.get("param_plan", {}).get("two_stage") is True, run.data.get("param_plan")
    assert len(calls) == 2, f"应跑两遍（粗筛 + 精算），实际 {calls}"
    assert calls[0]["n"] == len(molecules) and calls[1]["n"] < calls[0]["n"]
    assert calls[0]["exh"] < calls[1]["exh"], f"粗筛强度必须小于精算强度：{calls}"
    assert any("两阶段漏斗" in note for note in out_notes(out))


def out_notes(raw: str) -> List[str]:
    import json

    data = json.loads(raw)
    return list(data.get("notes") or (data.get("summary") or {}).get("notes") or [])


def test_small_library_is_not_funneled(monkeypatch: Any, tmp_path: Any) -> None:
    """小库（< 漏斗门槛）不该被拆成两遍，也不该出现两阶段说明。"""
    from docking_agent.tools import docking as D

    run = _fake_run(tmp_path)
    _patch_runtime(monkeypatch, run)
    calls: List[int] = []

    def _fake_dock_library(mols, **kwargs):
        calls.append(len(mols))
        return {"status": "ok", "receptors": [{"receptor_key": "r", "results": [
            {"name": "A", "smiles": "CCO", "affinity_kcal_mol": -4.0, "engine": "vina"}]}],
            "notes": []}

    monkeypatch.setattr(D, "dock_library", _fake_dock_library)
    D.molecular_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]',
                             receptor_sources="thrombin", exhaustiveness=0, n_poses=1)
    assert calls == [1], calls
    assert not (run.data.get("param_plan") or {}).get("two_stage")
