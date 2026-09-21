"""C 方案回归：库级配体感知下限 + 超限分子分组 + **主组盒子一致性**。

为什么值得一组测试：实测（`docs/architecture.md` §15.7b 的 B0 表）表明 Vina 分数对盒子大小
**敏感且非单调**（同一配体在 18³/22³/28³/34³ 间最大差 1.34 kcal/mol），所以「同一运行内主组所有
分子必须共用同一个盒子」是第一原则；本文件的第 1 条用例就是它的守护。大配体不靠逐分子自适应，
而是划进 `box_group="large"`，用**同一中心**的更大盒子单独重跑并如实标注，不跨组比较。

全部用例离线可跑：对接用本地 Vina（不联网、不调用模型），其余用 monkeypatch 或纯函数验证。
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

THROMBIN = "thrombin"

# 5 个小分子：跨度都远小于 12 Å（不会触发分组）
SMALL: List[Dict[str, str]] = [
    {"name": "乙醇", "smiles": "CCO"},
    {"name": "阿司匹林", "smiles": "CC(=O)Oc1ccccc1C(=O)O"},
    {"name": "咖啡因", "smiles": "Cn1cnc2c1c(=O)n(C)c(=O)n2C"},
    {"name": "布洛芬", "smiles": "CC(C)Cc1ccc(cc1)C(C)C(=O)O"},
    {"name": "对乙酰氨基酚", "smiles": "CC(=O)Nc1ccc(O)cc1"},
]
# 更小的三分子库（跨度都 < 7 Å）：P95+10 仍小于 min_size，用来验证「小分子库不被抬高」
TINY: List[Dict[str, str]] = [SMALL[0], SMALL[1], SMALL[2]]
# 六肽 Gly6（3D 跨度约 15 Å）：跨度 + 10 Å 仍在上限 30 Å 以内 → 用来验证「库级下限抬高盒子」
HEX_GLY6: Dict[str, str] = {
    "name": "六肽(Gly6)",
    "smiles": "NCC(=O)NCC(=O)NCC(=O)NCC(=O)NCC(=O)NCC(=O)O",
}
# 六肽 Phe-Ala-Phe-Gly-Phe-Gly（3D 跨度约 21 Å）：跨度 + 10 Å 超过 MAX(30) → 验证分组兜底
HEX_FAFGFG: Dict[str, str] = {
    "name": "六肽(FAFGFG)",
    "smiles": ("C[C@H](NC(=O)[C@@H](N)Cc1ccccc1)C(=O)N[C@@H](Cc1ccccc1)C(=O)NCC(=O)"
               "N[C@@H](Cc1ccccc1)C(=O)NCC(=O)O"),
}


@pytest.fixture(autouse=True)
def _isolate_box_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把库级跨度缓存指向临时目录，避免污染仓库缓存、也保证用例之间互不串扰。"""
    from docking_agent.core import pockets as P

    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(P, "cache_dir", lambda: cache)
    monkeypatch.setenv("BOX_SPAN_ENABLED", "on")


def _span_max(smiles: str) -> float:
    """用与对接相同的种子算该分子的 3D 最大跨度（断言基于真实计算值，不硬编码）。"""
    from docking_agent.core.pockets import _span_from_smiles

    span = _span_from_smiles(smiles)
    assert span, f"3D 跨度计算失败：{smiles}"
    return max(span)


def _thrombin_spec() -> Dict[str, Any]:
    """注册表里的凝血酶受体 spec（含实验位点，盒子 22³）——用于 select_site 单测。"""
    from docking_agent.core import resolve_receptor_specs

    specs, _notes = resolve_receptor_specs(THROMBIN)
    return specs[0]


def _dock(molecules: List[Dict[str, str]], **kwargs: Any) -> Dict[str, Any]:
    """统一的真实 Vina 小批量对接（exhaustiveness=1，只验证盒子逻辑）。

    显式用 `protonation="keep"`：本文件验证的是**盒子/分组**逻辑，化学形式必须固定，
    否则默认的 pH 质子化会改变分子 3D 跨度，让分组断言随化学策略漂移（真实踩到过）。
    """
    from docking_agent.core import dock_library

    kwargs.setdefault("pocket_engine", "known_site")
    kwargs.setdefault("protonation", "keep")
    return dock_library(molecules, receptor=THROMBIN, exhaustiveness=1, n_poses=1,
                        engine="vina", **kwargs)


# --------------------------------------------------------------------------- #
# 1. 一致性不变量（本次改动的核心守护）
# --------------------------------------------------------------------------- #
def test_main_group_shares_one_box() -> None:
    """6 个小分子的库：所有 main 组行的 box_size / box_center 必须**完全相同**。"""
    out = _dock(SMALL + [HEX_GLY6])
    assert out["status"] == "ok"
    block = out["receptors"][0]
    rows = block["results"]
    assert len(rows) == len(SMALL) + 1
    main = [r for r in rows if (r.get("box_group") or "main") == "main"]
    assert len(main) >= len(SMALL), "小分子都应在主组"
    assert len({tuple(r["box_size"]) for r in main}) == 1, \
        f"主组盒子必须唯一：{[r['box_size'] for r in main]}"
    assert len({tuple(r["box_center"]) for r in main}) == 1, "主组中心也必须唯一"
    assert all(isinstance(r.get("affinity_kcal_mol"), float) for r in main)


# --------------------------------------------------------------------------- #
# 2. 库级配体感知下限
# --------------------------------------------------------------------------- #
def test_library_bound_raises_box_for_big_ligand() -> None:
    """库内含大配体时，盒子被抬到 ≥ 该配体跨度 + 10 Å，并在 warnings/notes 里解释。"""
    from docking_agent.core.pockets import library_span_bound

    hex_span = _span_max(HEX_GLY6["smiles"])
    bound = library_span_bound(SMALL + [HEX_GLY6], min_size=18.0)
    assert bound["enabled"] is True and bound["cached"] is False
    assert bound["p95_span"] == pytest.approx(hex_span, abs=0.05)
    assert bound["bound"] >= hex_span + 10 - 0.05

    out = _dock(SMALL + [HEX_GLY6])
    block = out["receptors"][0]
    assert block["box_size"][0] >= hex_span + 10 - 0.05, \
        f"盒子未被库级下限抬高：{block['box_size']} vs 六肽跨度 {hex_span:.1f}"
    floor = block["box_library_floor"]
    assert floor and floor["enabled"] is True and floor["raised"] is True
    assert floor["size_before"] == [22.0, 22.0, 22.0], "抬高前的盒子应是注册位点的 22³"
    assert any("为容纳库内大配体" in w for w in block["box_warnings"]), block["box_warnings"]
    assert any("库级配体感知下限" in n for n in out["notes"]), out["notes"]
    assert "库级下限" in block["box_source"], block["box_source"]


def test_small_library_is_not_raised() -> None:
    """全是小分子（乙醇/阿司匹林…）的库：下限退回 min_size，盒子保持口袋驱动值。"""
    from docking_agent.core.pockets import library_span_bound, select_site

    bound = library_span_bound(TINY, min_size=18.0)
    assert bound["p95_span"] is not None and bound["p95_span"] < 7.0
    assert bound["bound"] == 18.0, "小分子库的下限不应超过 min_size"

    picked = select_site(_thrombin_spec(), engine="known_site", box_floor=bound, use_cache=False)
    assert picked["size"] == [22.0, 22.0, 22.0], "小分子库不该改变口袋驱动的盒子"
    assert picked["library_floor"]["raised"] is False


# --------------------------------------------------------------------------- #
# 3. 超限分子分组 + CSV 列
# --------------------------------------------------------------------------- #
def test_large_group_rerun_and_csv_columns() -> None:
    """显式 22³ 主盒下，跨度 15 Å 的六肽必须进 large 组并用更大的同中心盒重跑。"""
    from docking_agent.core import merge_and_rank
    from docking_agent.reporting.tables import build_ranking_csv

    hex_span = _span_max(HEX_GLY6["smiles"])
    site = {"center": [31.5, 13.74, 24.36], "size": [22.0, 22.0, 22.0],
            "source": "测试：显式指定 22³ 主盒"}
    out = _dock(SMALL + [HEX_GLY6], site=site)
    block = out["receptors"][0]
    rows = block["results"]
    assert block["box_size"] == [22.0, 22.0, 22.0]

    main = [r for r in rows if (r.get("box_group") or "main") == "main"]
    large = [r for r in rows if r.get("box_group") == "large"]
    assert len(main) == len(SMALL) and [r["name"] for r in large] == [HEX_GLY6["name"]]
    assert large[0]["box_size"][0] > 22.0
    assert large[0]["box_size"][0] >= hex_span + 12 - 0.05, "大配体组盒子应容得下组内最大跨度"
    assert tuple(large[0]["box_center"]) == tuple(block["box_center"]), "中心不随分组改变"
    assert "已单独分组重跑" in str(large[0].get("box_fit_warning"))
    assert block["box_group_counts"] == {"main": len(SMALL), "large": 1}

    # 默认排序榜只含 main 组
    ranking = merge_and_rank([], rows, {})
    assert ranking and all((r.get("box_group") or "main") == "main" for r in ranking)
    assert all(r["name"] != HEX_GLY6["name"] for r in ranking)

    # ranking.csv 含 box_group / box_size 两列，box_size 为紧凑字符串
    text = build_ranking_csv(ranking, None)
    header = text.splitlines()[0].split(",")
    assert "box_group" in header and "box_size" in header
    assert header.index("box_group") == header.index("exhaustiveness") + 1
    assert header.index("box_size") == header.index("box_group") + 1
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert parsed and all(r["box_group"] == "main" for r in parsed)
    assert all(r["box_size"] == "22.0x22.0x22.0" for r in parsed)


# --------------------------------------------------------------------------- #
# 4. 抽样缓存：第二次不重新生成 3D
# --------------------------------------------------------------------------- #
def test_library_span_cache_hits_without_regeneration(monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path: Path) -> None:
    from docking_agent.core import ligands, pockets as P

    calls = {"n": 0}
    real = ligands.smiles_to_pdbqt

    def counting(smiles: str, seed: int = 42) -> str:
        calls["n"] += 1
        return real(smiles, seed=seed)

    monkeypatch.setattr(ligands, "smiles_to_pdbqt", counting)
    library = SMALL + [HEX_GLY6]
    first = P.library_span_bound(library, min_size=18.0)
    assert first["cached"] is False
    assert calls["n"] == len(library), "第一次应为每个抽样分子生成一次 3D"

    second = P.library_span_bound(library, min_size=18.0)
    assert second["cached"] is True and second["bound"] == first["bound"]
    assert calls["n"] == len(library), "第二次命中缓存，不得重新生成 3D"
    cache_files = list((tmp_path / "cache" / "box_span").glob("library_*.json"))
    assert cache_files, "应写出按库 SMILES 哈希命名的缓存文件"


# --------------------------------------------------------------------------- #
# 5. BOX_SPAN_ENABLED=off：完全退回旧行为
# --------------------------------------------------------------------------- #
def test_box_span_disabled_falls_back_to_old_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.core import docking as D

    monkeypatch.setenv("BOX_SPAN_ENABLED", "off")
    seen: List[List[float]] = []

    def fake_dock_batch(spec: Dict[str, Any], molecules: List[Dict[str, str]],
                        **kwargs: Any) -> List[Dict[str, Any]]:
        seen.append([float(x) for x in spec["size"]])
        on_result = kwargs.get("on_result")
        rows = []
        for m in molecules:
            row = {"name": m.get("name"), "smiles": m["smiles"], "affinity_kcal_mol": -3.0,
                   "box_center": list(spec["center"]), "box_size": list(spec["size"]),
                   "box_group": "main", "ligand_span": [2.0, 2.0, 2.0], "engine": "vina",
                   "exhaustiveness": 1}
            rows.append(row)
            if on_result:
                on_result(row)
        return rows

    monkeypatch.setattr(D, "dock_batch", fake_dock_batch)
    out = D.dock_library(SMALL + [HEX_GLY6], receptor=THROMBIN, exhaustiveness=1,
                         protonation="keep",
                         pocket_engine="known_site")
    block = out["receptors"][0]
    assert block["box_size"] == [22.0, 22.0, 22.0], "关闭后盒子不得被库级下限抬高"
    assert seen == [[22.0, 22.0, 22.0]], "传给对接的盒子应是口袋驱动的 22³"
    floor = block["box_library_floor"]
    assert floor and floor["enabled"] is False and floor["raised"] is False
    assert all((r.get("box_group") or "main") == "main" for r in block["results"])


# --------------------------------------------------------------------------- #
# 6. 跨度计算失败必须降级、不抛异常
# --------------------------------------------------------------------------- #
def test_span_failure_degrades_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.core import ligands, pockets as P

    def boom(smiles: str, seed: int = 42) -> str:
        raise RuntimeError("模拟 3D 构象生成失败")

    monkeypatch.setattr(ligands, "smiles_to_pdbqt", boom)
    out = P.library_span_bound(SMALL + [HEX_GLY6], min_size=18.0)
    assert out["bound"] == 18.0, "全部失败时应退回 min_size"
    assert out["p95_span"] is None
    assert "失败" in out["message"]

    # 降级后的下限传给 select_site 也不能抛异常，盒子退回口袋驱动值
    picked = P.select_site(_thrombin_spec(), engine="known_site", box_floor=out, use_cache=False)
    assert picked["size"] == [22.0, 22.0, 22.0]
    assert picked["library_floor"]["raised"] is False


# --------------------------------------------------------------------------- #
# 7. 库级下限被 MAX 夹住时，超限分子仍要进 large 组（两个机制的衔接）
# --------------------------------------------------------------------------- #
def test_bound_capped_by_max_size_sends_overflow_to_large_group() -> None:
    hex_span = _span_max(HEX_FAFGFG["smiles"])
    out = _dock(SMALL + [HEX_FAFGFG])
    block = out["receptors"][0]
    floor = block["box_library_floor"]
    assert floor and floor["raised"] is True
    assert floor["bound"] >= hex_span + 10 - 0.05
    assert max(block["box_size"]) <= 30.0 + 1e-9, "主盒仍受 POCKET_MAX_SIZE 约束"

    main = [r for r in block["results"] if (r.get("box_group") or "main") == "main"]
    large = [r for r in block["results"] if r.get("box_group") == "large"]
    assert len({tuple(r["box_size"]) for r in main}) == 1
    if hex_span + 10 > max(block["box_size"]) + 1e-9:
        assert [r["name"] for r in large] == [HEX_FAFGFG["name"]]
        assert large[0]["box_size"][0] > block["box_size"][0]
        assert tuple(large[0]["box_center"]) == tuple(block["box_center"])
    else:  # 极端情况下（该六肽跨度变小）至少不应出现错误的 large 分组
        assert not large


# --------------------------------------------------------------------------- #
# 8. 融合/拆分语义（纯函数，不依赖对接）
# --------------------------------------------------------------------------- #
def test_merge_and_rank_keeps_main_only() -> None:
    """merge_and_rank 只返回 main 组；large 组仍保留在原始结果行里。"""
    from docking_agent.core import merge_and_rank

    rows = [
        {"name": "小", "smiles": "CCO", "affinity_kcal_mol": -3.0, "box_group": "main"},
        {"name": "大", "smiles": "CCCCCCCCCCCCCCCCCCCC", "affinity_kcal_mol": -9.0,
         "box_group": "large"},
        {"name": "旧数据", "smiles": "CC", "affinity_kcal_mol": -2.0},  # 无 box_group → 视为 main
    ]
    ranked = merge_and_rank([], rows, {})
    assert [r["name"] for r in ranked] == ["小", "旧数据"]


def test_split_box_groups_helper() -> None:
    from docking_agent.core.ranking import split_box_groups

    main, large = split_box_groups([
        {"name": "a", "box_group": "main"},
        {"name": "b", "box_group": "large"},
        {"name": "c"},
    ])
    assert [r["name"] for r in main] == ["a", "c"]
    assert [r["name"] for r in large] == ["b"]


# --------------------------------------------------------------------------- #
# 9. 「口袋 Agent 提交的盒子」= 工具产物 → 仍受库级下限约束；用户显式盒子不受影响
#    （真实缺陷：120 个农药大分子库被塞进口袋 Agent 给的 22³ 主盒）
# --------------------------------------------------------------------------- #
def test_pocket_agent_box_is_raised_by_library_floor() -> None:
    """中心用口袋 Agent 的，尺寸按库级下限抬高（只抬不缩），并留下可追溯的 library_floor。"""
    hex_span = _span_max(HEX_GLY6["smiles"])
    pinned = {"center": [31.5, 13.74, 24.36], "size": [22.0, 22.0, 22.0],
              "source": "口袋分析 Agent 选定", "chosen_by": "pocket_agent",
              "pocket": {}, "validation": {}}
    out = _dock(SMALL + [HEX_GLY6], site=pinned)
    block = out["receptors"][0]
    assert block["box_center"] == [31.5, 13.74, 24.36], "中心必须沿用口袋 Agent 的"
    assert block["box_chosen_by"] == "pocket_agent"
    floor = block["box_library_floor"]
    assert floor and floor["raised"] is True, f"大配体库必须抬高工具盒子：{floor}"
    assert floor["bound"] >= hex_span + 10 - 0.05
    assert min(block["box_size"]) >= floor["bound"] - 1e-9
    assert floor["size_before"] == [22.0, 22.0, 22.0]
    warnings_text = " ".join(block.get("box_warnings") or [])
    assert "P95 跨度" in warnings_text and "下限提到" in warnings_text, warnings_text
    assert "库级下限" in str(block.get("box_source") or ""), block.get("box_source")


def test_user_specified_explicit_box_is_never_touched() -> None:
    """对照（不变量）：用户显式给的盒子（无 chosen_by）一律不抬高、不加库级下限。"""
    hex_span = _span_max(HEX_GLY6["smiles"])
    user_site = {"center": [31.5, 13.74, 24.36], "size": [22.0, 22.0, 22.0],
                 "source": "用户指定"}
    out = _dock(SMALL + [HEX_GLY6], site=user_site)
    block = out["receptors"][0]
    assert block["box_size"] == [22.0, 22.0, 22.0], "用户显式盒子不得被抬高"
    assert not block["box_library_floor"], "用户显式盒子不得附库级下限"
    assert not any("库级下限" in w for w in (block.get("box_warnings") or []))


def test_pocket_agent_box_floor_disabled_keeps_tool_size(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """开关关闭时完全退回旧行为：工具盒子原样使用（可追溯地留一条说明）。"""
    monkeypatch.setenv("BOX_SPAN_ENABLED", "off")
    pinned = {"center": [31.5, 13.74, 24.36], "size": [22.0, 22.0, 22.0],
              "source": "口袋分析 Agent 选定", "chosen_by": "pocket_agent"}
    out = _dock(SMALL + [HEX_GLY6], site=pinned)
    block = out["receptors"][0]
    assert block["box_size"] == [22.0, 22.0, 22.0], "关闭后工具盒子不得被抬高"
    floor = block["box_library_floor"]
    assert floor and floor["enabled"] is False and floor["raised"] is False
    assert any("库级配体感知下限已关闭" in n for n in (out.get("notes") or [])), out.get("notes")


# --------------------------------------------------------------------------- #
# 10. 库级跨度抽样的并发路径：结果必须与串行**逐位一致**（否则盒子会随机器核数漂移）
# --------------------------------------------------------------------------- #
def test_parallel_span_sampling_matches_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    """大库走进程池、小库走串行：两条路径的 P95/下限必须完全相同。"""
    from docking_agent.core import pockets as P

    library = [{"name": f"M{n}", "smiles": "C" * n + "O"} for n in range(40, 100, 3)]
    assert len(library) >= P._SPAN_PARALLEL_MIN, "本用例需要触发并发路径"

    parallel = P.library_span_bound(library, min_size=18.0, use_cache=False)
    monkeypatch.setattr(P, "_SPAN_PARALLEL_MIN", 10 ** 9)   # 强制串行
    serial = P.library_span_bound(library, min_size=18.0, use_cache=False)

    assert parallel["sample_n"] == serial["sample_n"] == len(library)
    assert parallel["p95_span"] == serial["p95_span"], "并发与串行不得给出不同跨度"
    assert parallel["bound"] == serial["bound"]


def test_parallel_span_failure_falls_back_to_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程池不可用时必须静默退回串行并给出同样的结果（绝不让整库失败）。"""
    from docking_agent.core import pockets as P

    class _BrokenPool:
        def __init__(self, **_kwargs: Any) -> None:
            raise OSError("模拟无法创建子进程")

    monkeypatch.setattr(P, "ProcessPoolExecutor", _BrokenPool)
    library = [{"name": f"M{n}", "smiles": "C" * n + "O"} for n in range(40, 100, 3)]
    out = P.library_span_bound(library, min_size=18.0, use_cache=False)
    assert out["sample_n"] == len(library) and out["bound"] > 18.0


def test_small_library_stays_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    """小库不得为了「优化」去起进程池（开销大于收益，且会破坏测试里的调用计数）。"""
    from docking_agent.core import ligands, pockets as P

    calls = {"n": 0}
    real = ligands.smiles_to_pdbqt

    def counting(smiles: str, seed: int = 42) -> str:
        calls["n"] += 1
        return real(smiles, seed=seed)

    monkeypatch.setattr(ligands, "smiles_to_pdbqt", counting)
    assert len(SMALL) < P._SPAN_PARALLEL_MIN
    P.library_span_bound(SMALL, min_size=18.0, use_cache=False)
    assert calls["n"] == len(SMALL), "小库应在同进程内逐个计算"


# --------------------------------------------------------------------------- #
# 11. 准备阶段（定盒/库级下限抽样）必须给界面真实反馈，不能长时间静默
# --------------------------------------------------------------------------- #
def test_note_cb_reports_prep_phase_before_docking(monkeypatch: pytest.MonkeyPatch) -> None:
    """库级下限抽样发生在对接之前：这段必须先播报，否则界面看起来卡死。"""
    from docking_agent.core import docking as D

    notes: List[str] = []

    def fake_dock_batch(spec: Dict[str, Any], molecules: List[Dict[str, str]],
                        **kwargs: Any) -> List[Dict[str, Any]]:
        return []

    monkeypatch.setattr(D, "dock_batch", fake_dock_batch)
    pinned = {"center": [31.5, 13.74, 24.36], "size": [22.0, 22.0, 22.0],
              "source": "口袋分析 Agent 选定", "chosen_by": "pocket_agent"}
    D.dock_library(SMALL + [HEX_GLY6], receptor=THROMBIN, exhaustiveness=1,
                   pocket_engine="known_site", site=pinned, note_cb=notes.append)
    assert notes, "必须至少播报一条准备阶段说明"
    assert any("库级下限" in n for n in notes), notes
    assert any("几十秒" in n and "缓存" in n for n in notes), notes


def test_note_cb_absent_is_silent_and_safe() -> None:
    """不传 note_cb（命令行/单测路径）必须照常工作，不得因此报错。"""
    from docking_agent.core import docking as D

    out = D.dock_library(SMALL, receptor=THROMBIN, exhaustiveness=1,
                         pocket_engine="known_site")
    assert out["status"] == "ok"


def test_note_cb_exception_never_breaks_docking(monkeypatch: pytest.MonkeyPatch) -> None:
    """回调抛异常（界面已关/run 已取消）绝不能影响对接本身。"""
    from docking_agent.core import docking as D

    def boom(_message: str) -> None:
        raise RuntimeError("模拟回调异常")

    out = D.dock_library(SMALL, receptor=THROMBIN, exhaustiveness=1,
                         pocket_engine="known_site", note_cb=boom)
    assert out["status"] == "ok"
