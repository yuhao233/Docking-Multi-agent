"""两处真实异常的回归护栏（2026-09-17 实测，报告里的「执行过程异常」1 与 2）。

## 异常 1：首轮对接误用默认受体 → 分数全为 0.0

机制（已复现）：Vina 在**盒子内没有任何受体原子**时不报错、不告警，直接返回**全 0 能量**
（`affinity_kcal_mol = 0.0`）。0.0 不是分数，是「什么都没算」。真实事故里盒子来自另一个蛋白
（盒中心距该受体最近原子 75.2 Å），147 个分子跑了 7.5 分钟得到一堆 0.0。
本文件看护两道护栏：**开跑前**按盒内原子数拒绝 + **行级**把全 0 能量判为失败。

## 异常 2：属性评估 Agent 读到「黑板上无分子」

机制：`import_molecule_library` 从不写共享黑板（全仓只有属性子 Agent 自己的 normalize 写过），
而提示词却承诺「子 Agent 工具留空参数即可用黑板」→ 属性阶段读到 0 条，147 条库只有前 20 条被评估。
本文件看护：导入成功即写黑板，且照该契约留空调用 normalize 能拿到同一批分子。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

#: 8ZE2 的口袋坐标：对上传的 8ZE2 是口袋内，对注册表 thrombin 是**空盒**（相距 75 Å）
BOX_FAR = [86.63, 76.165, 91.953]
SIZE_FAR = [28.9, 28.9, 28.9]


# --------------------------------------------------------------------------- #
# 异常 1：盒子内没有受体原子 → 拒绝对接（不产生 0.0 分）
# --------------------------------------------------------------------------- #
def test_box_atom_stats_counts_atoms_and_nearest_distance() -> None:
    from docking_agent.core.receptors import box_atom_stats

    thrombin = str(PROJECT_ROOT / "assets" / "receptors" / "registry" / "thrombin_1DWC.pdbqt")
    good = box_atom_stats(thrombin, [31.5, 13.74, 24.36], [22.0, 22.0, 22.0])
    assert good["atoms"] and good["atoms"] > 100, good
    assert good["nearest_angstrom"] is not None and good["nearest_angstrom"] < 5, good

    bad = box_atom_stats(thrombin, BOX_FAR, SIZE_FAR)
    assert bad["atoms"] == 0, bad
    assert bad["nearest_angstrom"] and bad["nearest_angstrom"] > 50, bad


def test_dock_library_refuses_box_without_receptor_atoms(monkeypatch) -> None:
    """盒内 0 个受体原子 → 状态 error、零引擎调用、notes 写明差多远。"""
    from docking_agent.core import docking as D

    calls: list = []
    monkeypatch.setattr(D, "dock_batch", lambda *a, **kw: calls.append(kw) or [])

    out = D.dock_library([{"name": "A", "smiles": "CCO"}], receptor="thrombin",
                         exhaustiveness=1, n_poses=1, engine="vina",
                         pocket_engine="known_site",
                         site={"center": BOX_FAR, "size": SIZE_FAR, "source": "测试：来自另一个蛋白的盒子"})
    assert out["status"] == "error", out
    block = out["receptors"][0]
    assert block["status"] == "error" and block["results"] == []
    assert block["box_atom_stats"]["atoms"] == 0
    joined = " ".join(out["notes"])
    assert "没有任何受体原子" in joined and "75" in joined, joined
    assert any("属于这个受体" in w for w in block["box_warnings"]), block["box_warnings"]
    assert not calls, "盒子无效时绝不允许再调用对接引擎"


def test_all_zero_energy_row_is_reported_as_failure() -> None:
    """行级兜底：引擎真的返回全 0 能量时，该行必须是 error，不能当成 0.0 分。"""
    from docking_agent.core import DockingSession, resolve_receptor_specs

    spec = dict(resolve_receptor_specs("thrombin")[0][0])
    spec["center"] = list(BOX_FAR)
    spec["size"] = list(SIZE_FAR)
    session = DockingSession(spec, engine="vina", seed=42, ga_num_evals=2000, threads=2)
    row = session.dock("CCO", 1, 1, None)
    assert row.get("error"), f"全 0 能量必须判为失败：{row}"
    assert "全 0 能量" in str(row["error"])
    assert "affinity_kcal_mol" not in row, "失败行不得带亲和力字段（否则会被排序/CSV 当成真分数）"


def test_valid_box_still_docks_normally() -> None:
    """对照：正常盒子必须照常出分（护栏不得误伤）。"""
    from docking_agent.core import DockingSession, resolve_receptor_specs

    spec = resolve_receptor_specs("thrombin")[0][0]
    session = DockingSession(spec, engine="vina", seed=42, ga_num_evals=2000, threads=2)
    span = session.dock("CCO", 1, 1, None)
    assert not span.get("error"), span
    assert isinstance(span.get("affinity_kcal_mol"), float) and span["affinity_kcal_mol"] < 0, span


# --------------------------------------------------------------------------- #
# 异常 2：导入即写共享黑板 → 属性阶段能拿到同一批分子
# --------------------------------------------------------------------------- #
def _write_sdf(path: Path, count: int = 5) -> Path:
    from rdkit import Chem

    names = ["阿司匹林", "咖啡因", "乙醇", "尿素", "布洛芬", "水杨酸", "苯"]
    smiles = ["CC(=O)Oc1ccccc1C(=O)O", "Cn1c(=O)c2c(ncn2C)n(C)c1=O", "CCO",
              "NC(N)=O", "CC(C)Cc1ccc(C(C)C(=O)O)cc1", "O=C(O)c1ccccc1O", "c1ccccc1"]
    writer = Chem.SDWriter(str(path))
    for i in range(count):
        mol = Chem.MolFromSmiles(smiles[i % len(smiles)])
        mol.SetProp("_Name", names[i % len(names)])
        writer.write(mol)
    writer.close()
    return path


def test_import_seeds_shared_blackboard(tmp_path: Path) -> None:
    """导入成功必须写进共享黑板（否则「留空即用黑板」的承诺是空话）。"""
    from docking_agent.runtime.blackboard import Blackboard, current_blackboard
    from docking_agent.agents.dispatch import import_molecule_library

    path = _write_sdf(tmp_path / "lib.sdf", 5)
    board = Blackboard(run_id="t-board")
    token = current_blackboard.set(board)
    try:
        out = json.loads(import_molecule_library.func(molecule_file=str(path)))
    finally:
        current_blackboard.reset(token)

    assert out["status"] == "ok" and len(out["molecules"]) == 5, out
    board_mols = board.molecules()
    assert len(board_mols) == 5, board_mols
    assert any("共享黑板" in n for n in board.notes), board.notes


def test_property_stage_sees_library_after_import(tmp_path: Path) -> None:
    """端到端契约：导入 → 子 Agent 留空调用 normalize → 拿到**全部**分子（异常 2 的修复）。"""
    from docking_agent.runtime.blackboard import Blackboard, current_blackboard
    from docking_agent.agents.dispatch import import_molecule_library
    from docking_agent.tools.properties import normalize_molecule_library

    path = _write_sdf(tmp_path / "lib.sdf", 5)
    board = Blackboard(run_id="t-board-2")
    token = current_blackboard.set(board)
    try:
        import_molecule_library.func(molecule_file=str(path))
        empty_call = json.loads(normalize_molecule_library.func(molecules_json=""))
    finally:
        current_blackboard.reset(token)

    assert empty_call["status"] == "ok", empty_call
    assert empty_call["count"] == 5, empty_call
    assert len(empty_call["molecules"]) == 5


def test_normalize_payload_is_bounded_for_large_library(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """大库：完整清单留在黑板，只把前 N 条回给子 Agent（否则上下文会被撑爆）。"""
    from docking_agent.runtime.blackboard import Blackboard, current_blackboard
    from docking_agent.tools.properties import normalize_molecule_library

    monkeypatch.setenv("AGENT_TOOL_TOP_N", "2")
    board = Blackboard(run_id="t-board-3")
    token = current_blackboard.set(board)
    try:
        payload = json.dumps([{"name": f"M{i}", "smiles": "C" * (i + 2) + "O"} for i in range(5)])
        out = json.loads(normalize_molecule_library.func(molecules_json=payload))
    finally:
        current_blackboard.reset(token)

    assert out["count"] == 5, out
    assert len(out["molecules"]) == 2 and out.get("detail_omitted") is True, out
    assert "共享黑板" in out.get("notice", ""), out
    assert len(board.molecules()) == 5, "完整清单必须留在黑板上"


# --------------------------------------------------------------------------- #
# 异常 2 的**漏网路径**：协调 Agent 跳过 import，直接把文件交给 run_docking
# （提示词明确允许）→ 黑板一直是空的 → 属性评估拿到 0 条却 status=ok
# --------------------------------------------------------------------------- #
def _run_probe(tmp_path: Path, molecule_file: str) -> Any:
    """最小 Run 替身：带本次运行请求（含 molecule_file）与产物目录。"""
    class _Run:
        id = "PROBE-RUN"

        def __init__(self) -> None:
            self.data = {"request": {"molecule_file": molecule_file}, "status": "running"}
            self.dir = tmp_path

        def log(self, *_a: Any, **_k: Any) -> None:  # pragma: no cover
            return None

        def write_json(self, name: str, obj: Any, **kw: Any) -> Path:
            path = self.dir / (kw.get("rel_path") or f"{name}.json")
            path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            return path

        def add_artifact(self, *_a: Any, **_k: Any) -> None:  # pragma: no cover
            return None

    return _Run()


def test_property_stage_falls_back_to_run_request_when_board_empty(tmp_path: Path) -> None:
    """黑板为空但本次运行请求带分子库文件 → 属性评估必须拿到**全量**（而不是 0 条 + status=ok）。

    真实缺陷路径：协调 Agent 可以直接把文件交给 run_docking（跳过 import），
    此时若没人发布到黑板，属性评估读到 0 条，147 条库只有 20 条被评估甚至全空。
    """
    from docking_agent.runtime.blackboard import Blackboard, current_blackboard
    from docking_agent.runs import current_run
    from docking_agent.tools.properties import normalize_molecule_library

    lib = _write_sdf(tmp_path / "lib.sdf", 5)
    run = _run_probe(tmp_path, str(lib))
    board = Blackboard("PROBE-BOARD")
    token, btoken = current_run.set(run), current_blackboard.set(board)
    try:
        out = json.loads(normalize_molecule_library.func(molecules_json=""))
    finally:
        current_run.reset(token)
        current_blackboard.reset(btoken)

    assert out["count"] == 5, out
    assert len(board.molecules()) == 5, "读到的分子要顺手发布到黑板，供下游子 Agent 使用"


def test_docking_publishes_resolved_library_to_blackboard(monkeypatch: pytest.MonkeyPatch,
                                                          tmp_path: Path) -> None:
    """谁解析到分子谁就发布：molecular_docking 从文件解析后必须写黑板。"""
    from docking_agent.runtime.blackboard import Blackboard, current_blackboard
    from docking_agent.runs import current_run
    from docking_agent.tools import docking as TD

    lib = _write_sdf(tmp_path / "lib.sdf", 4)
    run = _run_probe(tmp_path, str(lib))
    board = Blackboard("PROBE-BOARD-2")
    monkeypatch.setattr(TD, "dock_library", lambda *_a, **_kw: {"status": "ok", "receptors": []})
    token, btoken = current_run.set(run), current_blackboard.set(board)
    try:
        TD.molecular_docking.func(molecule_file=str(lib), receptor_sources="thrombin")
    finally:
        current_run.reset(token)
        current_blackboard.reset(btoken)

    assert len(board.molecules()) == 4, board.molecules()
    assert any("写入共享黑板" in n for n in board.notes), board.notes


# --------------------------------------------------------------------------- #
# 文件优先的 Agent 间交接（v0.19）
# --------------------------------------------------------------------------- #
def test_molecular_property_assessment_reads_molecules_file(tmp_path: Path) -> None:
    """molecules_file 指向运行产物 JSON 也能读（不依赖黑板、不把清单搬进上下文）。"""
    from docking_agent.runtime import tool_io
    from docking_agent.runtime.blackboard import Blackboard, current_blackboard
    from docking_agent.runs import current_run
    from docking_agent.tools.properties import molecular_property_assessment

    run = _run_probe(tmp_path, "")
    board = Blackboard("PROBE-BOARD-3")
    token, btoken = current_run.set(run), current_blackboard.set(board)
    try:
        path = tool_io.record("molecules", [{"name": "乙醇", "smiles": "CCO"},
                                            {"name": "尿素", "smiles": "NC(N)=O"}])
        assert path and tool_io.artifact_path("molecules"), "产物路径要能被下游查到"
        out = json.loads(molecular_property_assessment.func(molecules_file=str(path)))
    finally:
        current_run.reset(token)
        current_blackboard.reset(btoken)

    assert out["status"] == "ok" and len(out["assessment"]) == 2, out
    assert {r["name"] for r in out["assessment"]} == {"乙醇", "尿素"}


def test_check_binding_consistency_reads_docking_file(tmp_path: Path) -> None:
    """对接明细按文件交接：check_binding_consistency(docking_file=…) 应读到文件里的行。"""
    from docking_agent.runtime.blackboard import Blackboard, current_blackboard
    from docking_agent.runs import current_run
    from docking_agent.tools.binding import check_binding_consistency

    payload = {"receptors": [{"receptor_key": "t", "results": [
        {"name": "A", "smiles": "CCO", "affinity_kcal_mol": -9.0, "box_group": "main"}]}]}
    path = tmp_path / "docking_tool.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    run = _run_probe(tmp_path, "")
    board = Blackboard("PROBE-BOARD-4")
    token, btoken = current_run.set(run), current_blackboard.set(board)
    try:
        out = json.loads(check_binding_consistency.func(docking_file=str(path)))
    finally:
        current_run.reset(token)
        current_blackboard.reset(btoken)

    assert out["status"] == "ok", out
    assert out["total"] == 1 and out["rows"][0]["name"] == "A", out


def test_artifact_refs_exposes_absolute_paths_for_handoff(tmp_path: Path) -> None:
    """artifact_refs 必须给出可直接传参的绝对路径（文件交接的发现入口）。"""
    from docking_agent.runtime import tool_io
    from docking_agent.runtime.blackboard import Blackboard, current_blackboard
    from docking_agent.runs import current_run

    run = _run_probe(tmp_path, "")
    board = Blackboard("PROBE-BOARD-5")
    token, btoken = current_run.set(run), current_blackboard.set(board)
    try:
        tool_io.record("molecules", [{"name": "乙醇", "smiles": "CCO"}])
        refs = tool_io.artifact_refs()
    finally:
        current_run.reset(token)
        current_blackboard.reset(btoken)

    assert refs["molecules_file"].startswith(str(tmp_path))
    assert Path(refs["molecules_file"]).is_file(), "交接路径必须是可直接读取的绝对路径"
