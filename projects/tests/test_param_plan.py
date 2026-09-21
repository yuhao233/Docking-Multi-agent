"""对接参数自动规划回归：规则 / 漏斗 / 预算降级 / **阶段一致性不变量**。

为什么值得一组测试：对接参数（尤其是 `exhaustiveness`）对分数的影响比盒子更大，
一旦"逐分子调参"，参数效应就会混进排序。本文件守护三条底线：

1. 同一 `pass`（coarse/fine）内所有行的 `exhaustiveness`、`box_size`、`box_center` 完全一致；
2. 用户显式指定的参数一律不自动改（`source="user"`）；
3. 预算护栏只降"精算头部"、且降强度绝不低于基准的一半（精度优先）。

全部用例离线可跑：规则/漏斗/预算用纯函数 + 注入的 pilot 回调；只有一致性用例跑一次
小规模**真实** Vina（4 个分子，`exhaustiveness=1`），与 `tests/test_box_sizing.py` 口径一致。
"""
from __future__ import annotations

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
SMALL: List[Dict[str, str]] = [
    {"name": "乙醇", "smiles": "CCO"},
    {"name": "阿司匹林", "smiles": "CC(=O)Oc1ccccc1C(=O)O"},
    {"name": "咖啡因", "smiles": "Cn1cnc2c1c(=O)n(C)c(=O)n2C"},
    {"name": "布洛芬", "smiles": "CC(C)Cc1ccc(cc1)C(C)C(=O)O"},
]


@pytest.fixture(autouse=True)
def _isolate_param_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """库统计缓存指向临时目录，避免污染仓库缓存、也保证用例之间互不串扰。"""
    from docking_agent.core import params as P

    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(P, "cache_dir", lambda: cache)
    # 每个用例都从显式默认出发，避免本机 .env / 界面设置影响断言
    for name in ("AUTO_PARAM_ENABLED", "AUTO_PARAM_PILOT", "AUTO_PARAM_PILOT_N",
                 "AUTO_PARAM_PILOT_MIN", "AUTO_PARAM_BUDGET_RATIO", "AUTO_PARAM_EXH_MIN",
                 "AUTO_PARAM_EXH_MAX", "AUTO_PARAM_BASE_SCREENING", "AUTO_PARAM_BASE_BINDING",
                 "AUTO_PARAM_STATS_SAMPLE", "AUTO_PARAM_FLEX_DIVISOR", "AUTO_PARAM_FLEX_MIN",
                 "AUTO_PARAM_FLEX_MAX", "AUTO_PARAM_BOX_REF", "AUTO_PARAM_BOX_FACTOR_MAX",
                 "AUTO_PARAM_REFINE_TOP_MIN", "AUTO_PARAM_REFINE_TOP_LARGE",
                 "AUTO_PARAM_LARGE_LIB", "AUTO_PARAM_N_POSES_BINDING",
                 "AUTO_PARAM_N_POSES_MAX", "AGENT_FUNNEL_MIN", "AGENT_REFINE_TOP_N",
                 "RUN_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)


def _molecules(n: int) -> List[Dict[str, str]]:
    return [{"name": f"M{i}", "smiles": "CCO"} for i in range(n)]


def _patch_stats(monkeypatch: pytest.MonkeyPatch, p90: float,
                 heaviest_n: int = 3) -> None:
    """把 2D 描述符统计替换为固定值，让规则断言与 RDKit 解耦。"""
    from docking_agent.core import params as P

    def fake(molecules: Any, *, use_cache: bool = True) -> Dict[str, Any]:
        return {"count": len(list(molecules or [])), "p90_rotatable": p90,
                "heaviest": [{"name": f"H{i}", "smiles": "CCO", "cost": 20 - i}
                             for i in range(heaviest_n)],
                "cached": False, "degraded": False, "sample_n": 1, "library_n": 1,
                "note": "test fixture"}

    monkeypatch.setattr(P, "library_stats", fake)


# --------------------------------------------------------------------------- #
# 1. 规则：柔性/盒体积系数、任务类型、上限、确定性
# --------------------------------------------------------------------------- #
def test_exhaustiveness_rules_are_deterministic_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.core.params import plan_docking_params

    _patch_stats(monkeypatch, p90=5.0)
    base = dict(molecules=_molecules(20), box_size=[22.0, 22.0, 22.0], pilot=None)
    first = plan_docking_params(task_type="screening", **base)
    second = plan_docking_params(task_type="screening", **base)
    assert first["exhaustiveness"] == second["exhaustiveness"] == 16, "同输入同输出、基准 16"
    assert first["decisions"] == second["decisions"], "决策理由必须确定"

    binding = plan_docking_params(task_type="binding_only", **base)
    assert binding["exhaustiveness"] == 16, "binding_only 基准应为 16"
    assert binding["n_poses"] == 3, "姿态分析需要 3 个位姿"

    # 柔性 9/5 = 1.8；盒 26³ → (26/22) ≈ 1.18 → 16 × 1.8 × 1.18 ≈ 34 → 上限 32
    _patch_stats(monkeypatch, p90=9.0)
    big = plan_docking_params(task_type="screening", molecules=_molecules(20),
                              box_size=[26.0, 26.0, 26.0], pilot=None)
    assert 20 <= big["exhaustiveness"] <= 32, big["exhaustiveness"]
    assert "clamp(" in " ".join(big["decisions"]), "规划过程必须写明 clamp 截断"
    assert big["flex_factor"] == pytest.approx(1.8, abs=0.01)
    assert big["box_factor"] == pytest.approx(1.18, abs=0.02)

    # 上限 32 生效（柔性 2.5 × 盒系数 2.0 × base 12 = 60 → 夹到 32）
    _patch_stats(monkeypatch, p90=100.0)
    capped = plan_docking_params(task_type="screening", molecules=_molecules(20),
                                 box_size=[60.0, 60.0, 60.0], pilot=None)
    assert capped["exhaustiveness"] == 32
    assert capped["flex_factor"] == pytest.approx(2.5)
    assert capped["box_factor"] == pytest.approx(2.0)

    # properties_only：不规划对接参数
    skipped = plan_docking_params(task_type="properties_only", molecules=_molecules(20),
                                  box_size=[22.0, 22.0, 22.0], pilot=None)
    assert skipped["exhaustiveness"] is None and skipped["n_poses"] is None


# --------------------------------------------------------------------------- #
# 2. 用户显式参数：冻结，不改
# --------------------------------------------------------------------------- #
def test_user_params_are_never_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.core.params import plan_docking_params

    _patch_stats(monkeypatch, p90=100.0)          # 静态规则本会算出 32
    called = {"n": 0}

    def pilot(_candidates: Any) -> float:
        called["n"] += 1
        return 999.0

    plan = plan_docking_params(task_type="screening", molecules=_molecules(1000),
                               box_size=[60.0, 60.0, 60.0],
                               user_params={"exhaustiveness": 20, "n_poses": 5},
                               pilot=pilot)
    assert plan["source"] == "user"
    assert plan["exhaustiveness"] == 20, "用户显式指定的 exhaustiveness 不得被改写"
    assert plan["n_poses"] == 5, "用户显式指定的 n_poses 不得被改写"
    assert plan["refine_top_n"] == 200, "用户参数冻结时不触发预算降级"
    assert called["n"] == 0, "用户参数冻结时 pilot 应跳过"
    assert any("不自动修改" in d for d in plan["decisions"])


# --------------------------------------------------------------------------- #
# 3. 两阶段漏斗：阈值
# --------------------------------------------------------------------------- #
def test_funnel_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.core.params import plan_docking_params

    _patch_stats(monkeypatch, p90=5.0)
    big = plan_docking_params(task_type="screening", molecules=_molecules(1000),
                              box_size=[22.0, 22.0, 22.0], pilot=None)
    assert big["two_stage"] is True
    assert big["coarse_exhaustiveness"] < big["exhaustiveness"]
    assert big["refine_top_n"] == 200

    small = plan_docking_params(task_type="screening", molecules=_molecules(100),
                                box_size=[22.0, 22.0, 22.0], pilot=None)
    assert small["two_stage"] is False
    assert small["coarse_exhaustiveness"] is None and small["refine_top_n"] is None


# --------------------------------------------------------------------------- #
# 4. 预算降级顺序：先降头部、再降强度且不低于 base/2
# --------------------------------------------------------------------------- #
def test_budget_degradation_prefers_head_then_strength(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.core.params import plan_docking_params

    _patch_stats(monkeypatch, p90=5.0)            # base 16 → exh 16，粗筛 ceil(16/4)
    mols, box = _molecules(1000), [22.0, 22.0, 22.0]

    # ① 预估超预算：先把精算头部压到下限 100，再看是否还需要降强度（精度优先）
    mild = plan_docking_params(task_type="screening", molecules=mols, box_size=box,
                               pilot=lambda _c: 0.11)
    assert mild["refine_top_n"] == 100, "应先降精算头部到下限 100"
    assert 8 <= mild["exhaustiveness"] <= 16, mild["exhaustiveness"]   # base=16，下限 base/2=8
    assert mild["eta_sec"] <= mild["budget_sec"]
    assert any("精算头部" in d for d in mild["decisions"]), mild["decisions"][-3:]

    # ② 更贵的试跑（0.30 s/分子）→ 降完头部仍超预算 → 继续降强度，且不低于 base/2 = 8
    harsh = plan_docking_params(task_type="screening", molecules=mols, box_size=box,
                                pilot=lambda _c: 0.30)
    assert harsh["refine_top_n"] == 100
    assert 8 <= harsh["exhaustiveness"] < 16, harsh["exhaustiveness"]
    decisions = list(harsh["decisions"])
    head_idx = next((i for i, d in enumerate(decisions) if "精算头部" in d), None)
    strength_idx = next((i for i, d in enumerate(decisions)
                         if d.startswith("仍超预算：exhaustiveness")), None)
    assert head_idx is not None and strength_idx is not None, decisions
    assert head_idx < strength_idx, "降级顺序必须是先降头部、再降强度"


# --------------------------------------------------------------------------- #
# 5. 一致性不变量：真实 dock_library 的同一 pass 参数完全一致
# --------------------------------------------------------------------------- #
def test_real_dock_rows_share_parameters_within_pass() -> None:
    from docking_agent.core import dock_library

    out = dock_library(SMALL, receptor=THROMBIN, exhaustiveness=1, n_poses=1,
                       engine="vina", pocket_engine="known_site")
    assert out["status"] == "ok"
    rows = out["receptors"][0]["results"]
    assert len(rows) == len(SMALL)

    passes: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        passes.setdefault(str(row.get("pass") or "main"), []).append(row)
    for name, group in passes.items():
        assert len({int(r["exhaustiveness"]) for r in group}) == 1, \
            f"pass={name} 内 exhaustiveness 必须唯一：{[r['exhaustiveness'] for r in group]}"
        assert len({tuple(r["box_size"]) for r in group}) == 1, \
            f"pass={name} 内 box_size 必须唯一：{[r['box_size'] for r in group]}"
        assert len({tuple(r["box_center"]) for r in group}) == 1, \
            f"pass={name} 内 box_center 必须唯一"
        assert all(isinstance(r.get("affinity_kcal_mol"), float) for r in group)


def test_merge_funnel_tags_pass_and_keeps_precision() -> None:
    """漏斗合并：精算行替换粗筛行、标 pass=fine 并保留 affinity_coarse；同 pass 参数一致。"""
    from docking_agent.pipeline import _merge_funnel

    def row(name: str, smiles: str, aff: float, exh: int) -> Dict[str, Any]:
        return {"name": name, "smiles": smiles, "affinity_kcal_mol": aff, "exhaustiveness": exh,
                "box_size": [22.0, 22.0, 22.0], "box_center": [1.0, 2.0, 3.0], "engine": "vina"}

    coarse = {"receptors": [{"receptor_key": "R", "results": [
        row("PC", "PC", -6.0, 3), row("A", "A", -5.0, 3), row("B", "B", -4.0, 3)]}]}
    fine = [("R", {"receptors": [{"receptor_key": "R", "results": [
        row("PC", "PC", -6.5, 12), row("B", "B", -4.4, 12)]}]})]
    merged = _merge_funnel(coarse, fine, 3, 12, 2)["receptors"][0]["results"]
    by = {r["smiles"]: r for r in merged}
    assert by["B"]["pass"] == "fine" and by["B"]["affinity_kcal_mol"] == -4.4
    assert by["B"]["affinity_coarse"] == -4.0, "粗筛分数必须保留在 affinity_coarse"
    assert by["A"]["pass"] == "coarse", "未进精算的分子保留粗筛行"
    for pass_name in ("coarse", "fine"):
        group = [r for r in merged if r["pass"] == pass_name]
        assert group, pass_name
        assert len({r["exhaustiveness"] for r in group}) == 1
        assert len({tuple(r["box_size"]) for r in group}) == 1
        assert len({tuple(r["box_center"]) for r in group}) == 1


# --------------------------------------------------------------------------- #
# 6. pilot 失败：退回静态规划、不抛异常、有 warning
# --------------------------------------------------------------------------- #
def test_pilot_failure_falls_back_to_static_plan(monkeypatch: pytest.MonkeyPatch,
                                                 caplog: pytest.LogCaptureFixture) -> None:
    from docking_agent.core.params import plan_docking_params

    _patch_stats(monkeypatch, p90=5.0)
    mols, box = _molecules(60), [22.0, 22.0, 22.0]

    with caplog.at_level("WARNING", logger="docking_agent.core.params"):
        boom = plan_docking_params(task_type="screening", molecules=mols, box_size=box,
                                   pilot=lambda _c: (_ for _ in ()).throw(RuntimeError("试跑炸了")))
    assert boom["pilot"]["status"] == "failed"
    assert boom["exhaustiveness"] == 16, "pilot 失败必须退回静态规划"
    assert boom["warnings"] and "pilot" in " ".join(boom["warnings"])
    assert any("pilot" in rec.message for rec in caplog.records)

    caplog.clear()
    with caplog.at_level("WARNING", logger="docking_agent.core.params"):
        none_plan = plan_docking_params(task_type="screening", molecules=mols, box_size=box,
                                        pilot=lambda _c: None)
    assert none_plan["pilot"]["status"] == "failed"
    assert none_plan["exhaustiveness"] == 16
    assert none_plan["warnings"]


# --------------------------------------------------------------------------- #
# 7. 库统计缓存：第二次命中、不重算描述符
# --------------------------------------------------------------------------- #
def test_library_stats_cache_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.core import params as P

    calls = {"n": 0}
    real = P._rotatable_bonds

    def counting(smiles: str) -> Any:
        calls["n"] += 1
        return real(smiles)

    monkeypatch.setattr(P, "_rotatable_bonds", counting)
    library = [{"name": f"M{i}", "smiles": s} for i, s in enumerate(
        ["CCO", "CCCO", "CCCCO", "c1ccccc1", "CC(=O)O", "CCN", "CCCN"])]

    first = P.library_stats(library)
    assert first["cached"] is False
    assert first["count"] == len(library)
    assert first["p90_rotatable"] is not None and first["heaviest"]
    before = calls["n"]
    assert before == len(library), "第一次应为每个抽样分子算一次可旋转键"

    second = P.library_stats(library)
    assert second["cached"] is True
    assert second["p90_rotatable"] == first["p90_rotatable"]
    assert calls["n"] == before, "第二次必须命中缓存，不得重算描述符"
    assert list((P.cache_dir() / "param_stats").glob("library_*.json")), "应写出内容哈希缓存文件"
