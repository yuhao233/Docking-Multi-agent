"""对接分数的**健全性**回归（`engine` 组：需要本机真实对接引擎）。

**为什么需要它**：门禁里对接分数原本只被断言成 `isinstance(affinity, float)` 或 `< 0`
（见 `tests/test_api.py`、`tests/test_box_sizing.py`、`tests/test_pockets.py`）——
于是把 `dock_library` 换成「永远返回 −1.0」也能全绿：分数是否真的来自对接、
是否真的区分强弱、是否可复现，一个都没被钉住。

**为什么不是"黄金值"**：曾经这里写死了实测窗口（苯甲脒 −5.63±1.0 之类），
但对接受引擎与版本**不固定**（Vina / AutoDock4 的绝对值不同，同引擎跨版本也会有偏移），
锁定绝对值会在换引擎时变成假红。因此这里只保留**与引擎实现无关**的三类不变量：

1. **量级健全**：分数是有限负数、不是 0.0（0.0 是「盒子内没有受体原子」的典型症状）；
2. **区分度**：性质迥异的两个分子必须拉开差距（拦"常数打分"）；
3. **可复现 + 参数生效**：同参数重跑逐位一致；提高搜索强度只微调、不翻转强弱关系。

探针分子用化学性质已知的一对：苯甲脒（凝血酶 S1 口袋经典探针）与乙醇（极小分子）。
窗口/阈值只要求"方向正确、量级合理"，不绑定任何一次实测的末位小数。
"""
from __future__ import annotations

import math
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

pytest.importorskip("vina")

BENZAMIDINE = {"name": "Benzamidine", "smiles": "NC(=N)c1ccccc1"}
ETHANOL = {"name": "Ethanol", "smiles": "CCO"}

#: 物理量级窗口（kcal/mol）：只要求"像真实对接分数"，不绑定具体引擎的绝对值。
#: 参考：Vina 在凝血酶注册位点盒上约 −5.6 / −2.8；AutoDock4 量级相近但不同。
SCORE_WINDOW = (-14.0, -0.5)
#: 必须拉开的最小区分度（弱→强）。实测 Vina 约 2.8，取 1.0 留足跨引擎/跨版本余量：
#: 常数打分差值为 0，一定被拦下；真实引擎的合理波动远小于 1.0。
MIN_DISCRIMINATION = 1.0


def _run(molecules: List[Dict[str, str]], exhaustiveness: int = 1) -> Dict[str, Any]:
    from docking_agent.core import dock_library

    out = dock_library(molecules, receptor="thrombin", exhaustiveness=exhaustiveness,
                       n_poses=1, engine="vina")
    assert out.get("status") == "ok", out
    return out


def _scores(out: Dict[str, Any]) -> Dict[str, float]:
    rows = [r for block in (out.get("receptors") or []) for r in (block.get("results") or [])]
    assert rows, out
    scores: Dict[str, float] = {}
    for row in rows:
        assert not row.get("error"), f"探针分子对接失败：{row}"
        value = row.get("affinity_kcal_mol")
        assert isinstance(value, (int, float)) and math.isfinite(float(value)), row
        scores[str(row.get("name"))] = float(value)
    return scores


@pytest.fixture(scope="module")
def probe_run() -> Dict[str, Any]:
    """一次真实对接跑出两个探针分子的分数（约 1 s）。"""
    return _run([BENZAMIDINE, ETHANOL])


def test_probe_scores_are_physically_sane(probe_run: Dict[str, Any]) -> None:
    """分数必须是有限的、量级合理的负值，且不是 0.0。"""
    scores = _scores(probe_run)
    assert set(scores) == {"Benzamidine", "Ethanol"}, scores
    low, high = SCORE_WINDOW
    for name, value in scores.items():
        assert value < 0, f"{name} 的亲和力应为负（结合为放热约定）：{value}"
        assert value != 0.0, f"{name} 得到 0.0 —— 这是「盒子内没有受体原子」的典型症状"
        assert low <= value <= high, (
            f"{name} 的亲和力 {value} 超出合理量级 {SCORE_WINDOW} kcal/mol"
            "（引擎/盒子/受体可能拿错了，或单位不是 kcal/mol）")


def test_ligand_strength_is_discriminated(probe_run: Dict[str, Any]) -> None:
    """区分度：苯甲脒必须**明显**强于乙醇。

    这一条专门拦「打分退化成常数」——把 `dock_library` 换成 `return -1.0` 时，
    上面的量级断言可能仍然通过，但这里一定失败。
    """
    scores = _scores(probe_run)
    delta = scores["Ethanol"] - scores["Benzamidine"]        # 越负越强 → 差值应为正
    assert scores["Benzamidine"] < scores["Ethanol"], scores
    assert delta > MIN_DISCRIMINATION, (
        f"两个性质迥异的分子分数几乎相同（差值 {delta:.2f} ≤ {MIN_DISCRIMINATION}）："
        f"苯甲脒 {scores['Benzamidine']} vs 乙醇 {scores['Ethanol']} —— 打分疑似退化成常数")


def test_scores_are_reproducible_for_same_parameters(probe_run: Dict[str, Any]) -> None:
    """同参数重跑必须逐位一致（会话级固定种子；否则报告不可复现、排序不稳定）。"""
    first = _scores(probe_run)
    second = _scores(_run([BENZAMIDINE, ETHANOL]))
    assert first == second, f"同参数两次对接分数不一致：{first} vs {second}"


def test_exhaustiveness_is_actually_passed_through(probe_run: Dict[str, Any]) -> None:
    """提高搜索强度只应微调分数（不改变数量级）—— 也间接证明参数真的传到了引擎。"""
    low = _scores(probe_run)
    high = _scores(_run([BENZAMIDINE, ETHANOL], exhaustiveness=8))
    for name in ("Benzamidine", "Ethanol"):
        assert abs(low[name] - high[name]) < 2.0, (
            f"{name}: exhaustiveness 1 → 8 分数变化过大（{low[name]} → {high[name]}）")
    assert high["Benzamidine"] < high["Ethanol"], "高搜索强度下强弱关系不应翻转"
