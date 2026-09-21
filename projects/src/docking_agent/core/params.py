"""对接参数的自动规划（运行级 / 漏斗阶段级，**绝不逐分子**）。

## 为什么必须自动规划、又必须"阶段一致"

对接耗时 ≈ 盒子体积 × exhaustiveness，而 `exhaustiveness` 直接决定采样是否充分。
给小分子配高强度、给大分子配低强度看似"省时间"，实际会把**参数效应混进排序**：
实测（`docs/architecture.md` §15.7b B0）同一配体仅换盒子大小就能差 **1.34 kcal/mol**，
采样强度的影响更大。因此本项目的第一原则是：

    **参数是运行级 / 漏斗阶段级的：同一阶段（pass）内所有分子的 exhaustiveness 必须完全一致。**

本模块只回答"这一次运行该用什么参数"，输出一份可追溯的 `param_plan`：
每个数值都对应 `decisions` 里的一条可读理由；用户显式指定的参数一律不改（`source="user"`）。

## 规划规则（精度优先）

| 规则 | 公式 |
| --- | --- |
| 基准强度 | `screening` → 16（与表单默认一致）；`binding_only`/姿态分析 → 16；`properties_only` → 不规划 |
| 柔性系数 | `f_rot = clamp(P90(库内可旋转键) / 5, 0.75, 2.5)`（2D 描述符；大库抽样最重的 N 个） |
| 盒体积系数 | `f_box = clamp((V_box / 22³)^(1/3), 1.0, 2.0)`（保持单位体积采样密度） |
| 搜索强度 | `exhaustiveness = clamp(round(base × f_rot × f_box), 2, 32)` |
| 两阶段漏斗 | `N ≥ AGENT_FUNNEL_MIN(500)`：粗筛 `max(1, round(exh/4))` 全库 + 精算 `exh` 前 `refine_top_n` |
| pilot 护栏 | `N ≥ 50`：最贵的 3 个分子以 `exhaustiveness=1` 真实试跑，外推总耗时；超预算按精度优先降级 |

pilot 只是**测量**：不写位姿、不进 ranking/黑板，失败就退回静态规划（只 warning）。

阈值全部来自 `settings.py`/环境变量（`AUTO_PARAM_*`），本模块不写死魔法数。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import Any, Callable, Dict, List, Optional, Sequence

from docking_agent.config import env_bool, env_float, env_int
from docking_agent.core.pockets import (
    _heavy_atom_count,
    _library_smiles,
    _percentile_nearest_rank,
)
from docking_agent.paths import cache_dir

logger = logging.getLogger(__name__)

# 系统对接默认值（与 `core/docking.py` 保持一致：规划关闭/不适用时的回退）
FALLBACK_EXHAUSTIVENESS = 6
FALLBACK_N_POSES = 1

# 姿态/结合模式分析的 n_poses 与硬上限（任务规则，也可由 AUTO_PARAM_N_POSES_* 覆盖）
N_POSES_DEFAULT = 1
N_POSES_BINDING = 3
N_POSES_MAX = 5

# 引擎与随机种子（写进计划便于复算；与 docking.py 的默认一致）
PLAN_ENGINE = "vina"
PLAN_SEED = 42

PilotCallback = Callable[[List[Dict[str, Any]]], Optional[Any]]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _round_half_up(value: float) -> int:
    """四舍五入（不用 Python 的银行家舍入，保证规则可预测、可复现）。"""
    return int(math.floor(float(value) + 0.5))


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rotatable_bonds(smiles: str) -> Optional[int]:
    """便宜的 2D 描述符：可旋转键数（失败返回 None，由调用方降级）。"""
    try:
        from rdkit import Chem  # noqa: PLC0415（重依赖，函数内导入）
        from rdkit.Chem import Lipinski  # noqa: PLC0415

        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        return int(Lipinski.NumRotatableBonds(mol))
    except Exception:  # noqa: BLE001
        return None


def _stats_defaults(count: int) -> Dict[str, Any]:
    """统计失败时的保守默认：p90 = 柔性基准值 → 柔性系数 1.0。"""
    return {"count": int(count), "p90_rotatable": env_float("AUTO_PARAM_FLEX_DIVISOR", 5.0),
            "heaviest": [], "cached": False, "degraded": True,
            "sample_n": 0, "library_n": 0,
            "note": "2D 描述符统计失败 → 柔性系数按 1.0（保守默认）"}


# --------------------------------------------------------------------------- #
# 库统计（2D 描述符 + 内容哈希缓存）
# --------------------------------------------------------------------------- #
def _stats_digest(library: Sequence[str], k: int, pool: int) -> str:
    """库内容 + 抽样规模哈希（sha1 前 12 位）。"""
    joined = "\n".join(library)
    raw = f"{joined}|k={k}|pool={pool}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _stats_cache_path(digest: str) -> Any:
    return cache_dir() / "param_stats" / f"library_{digest}.json"


def _compute_stats(library: Sequence[str], k: int, pool: int,
                   name_by_smiles: Dict[str, str]) -> Dict[str, Any]:
    """真实计算：重原子数（全库，便宜）→ 前 k 个最重的算可旋转键 → P90 + 最贵分子。"""
    heavy = {smi: _heavy_atom_count(smi) for smi in library}
    ranked = sorted(library, key=lambda s: (-heavy.get(s, 0), s))[:k]
    rots: Dict[str, int] = {}
    for smi in ranked:
        value = _rotatable_bonds(smi)
        if value is not None:
            rots[smi] = value
    degraded = not rots
    p90 = (round(_percentile_nearest_rank(list(rots.values()), 0.90), 2)
           if rots else env_float("AUTO_PARAM_FLEX_DIVISOR", 5.0))
    entries: List[Dict[str, Any]] = []
    for smi in ranked:
        r = rots.get(smi)
        if r is None:
            continue
        entries.append({"name": name_by_smiles.get(smi, smi), "smiles": smi,
                        "heavy_atoms": heavy.get(smi, 0), "rotatable_bonds": r,
                        "cost": r * 4 + heavy.get(smi, 0)})
    entries.sort(key=lambda e: (-e["cost"], e["smiles"]))
    note = (f"抽样 {len(ranked)}/{len(library)} 个最重分子（2D 描述符）"
            + (f"；{len(ranked) - len(rots)} 个分子描述符失败已跳过" if degraded else ""))
    return {"p90_rotatable": p90, "heaviest": entries[:pool], "sample_n": len(ranked),
            "library_n": len(library), "k": k, "degraded": degraded, "note": note}


def library_stats(molecules: Sequence[Dict[str, Any]], *,
                  use_cache: bool = True) -> Dict[str, Any]:
    """库级 2D 统计：`{count, p90_rotatable, heaviest}`（外加抽样/缓存元信息）。

    - **成本控制**：不为全库算可旋转键。先用最便宜的重原子数降序取前
      `AUTO_PARAM_STATS_SAMPLE`(500) 个，只为这些算可旋转键（任务规则：大库可抽样最重的 500 个）。
    - **缓存**：按库内容 + 抽样规模的 sha1 缓存到 `cache_dir()/param_stats/`，同一库第二次调用直接命中。
    - **降级**：任何解析/描述符失败都返回保守默认（柔性系数 = 1.0）并 `logger.warning`，
      绝不让规划因为统计失败而中断运行。

    `heaviest` 是**最贵的 N 个分子**（`cost = 可旋转键×4 + 重原子`，即 `_ligand_cost` 口径），
    供 pilot 试跑选取。
    """
    molecules = list(molecules or [])
    count = len(molecules)
    try:
        library = _library_smiles(molecules)
    except Exception as e:  # noqa: BLE001
        logger.warning("库统计失败（SMILES 归一化）：%s", e)
        return _stats_defaults(count)
    if not library:
        return {**_stats_defaults(0), "degraded": False, "note": "库为空"}

    pool = max(3, env_int("AUTO_PARAM_PILOT_N", 3))
    sample = max(1, env_int("AUTO_PARAM_STATS_SAMPLE", 500))
    k = min(sample, len(library))
    digest = _stats_digest(library, k, pool)
    path = _stats_cache_path(digest)
    if use_cache and path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.update({"count": count, "cached": True, "degraded": bool(data.get("degraded"))})
            logger.info("库统计命中缓存：%s 个分子 / P90=%.2f", count, data.get("p90_rotatable", 0.0))
            return data
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.debug("库统计缓存不可用（%s），改为现场计算", e)

    name_by_smiles: Dict[str, str] = {}
    for m in molecules:
        smi = str((m or {}).get("smiles") or "").strip()
        if smi and smi not in name_by_smiles:
            name_by_smiles[smi] = str((m or {}).get("name") or smi)
    try:
        stats = _compute_stats(library, k, pool, name_by_smiles)
    except Exception as e:  # noqa: BLE001
        logger.warning("库统计计算失败（退回保守默认）：%s", e)
        return _stats_defaults(count)

    stats.update({"count": count, "cached": False})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.debug("库统计缓存写入失败（忽略）：%s", e)
    logger.info("库统计：%s 个分子 / P90 可旋转键=%.2f / %s", count, stats["p90_rotatable"],
                stats.get("note", ""))
    return stats


# --------------------------------------------------------------------------- #
# 预算与耗时外推
# --------------------------------------------------------------------------- #
def _budget(budget_sec: Optional[float]) -> float:
    """预算 = `AUTO_PARAM_BUDGET_RATIO` × `RUN_TIMEOUT_SECONDS`（默认 0.6 × 900 = 540 s）。"""
    if budget_sec is not None:
        return max(0.0, float(budget_sec))
    ratio = env_float("AUTO_PARAM_BUDGET_RATIO", 0.6)
    timeout = max(1, env_int("RUN_TIMEOUT_SECONDS", 900))
    return max(0.0, ratio) * timeout


def _estimate_eta(mean_sec: float, library_n: int, exhaustiveness: int,
                  coarse: Optional[int], refine_top_n: Optional[int],
                  two_stage: bool) -> float:
    """由 pilot 单分子耗时外推总耗时（时间与 exhaustiveness 近似线性）。"""
    if two_stage and coarse and refine_top_n:
        return mean_sec * library_n * coarse + mean_sec * refine_top_n * exhaustiveness
    return mean_sec * library_n * exhaustiveness


def _coarse_of(exhaustiveness: int) -> int:
    return max(1, _round_half_up(exhaustiveness / 4.0))


def _largest_exh_within_budget(mean_sec: float, library_n: int, budget: float, *,
                               floor: int, ceiling: int, refine_top_n: int,
                               two_stage: bool) -> int:
    """在 `[floor, ceiling]` 里取**最大**的、外推仍在预算内的 exhaustiveness。"""
    for candidate in range(int(ceiling), int(floor) - 1, -1):
        coarse = _coarse_of(candidate) if two_stage else None
        eta = _estimate_eta(mean_sec, library_n, candidate, coarse, refine_top_n, two_stage)
        if eta <= budget:
            return candidate
    return int(floor)


# --------------------------------------------------------------------------- #
# 主入口：纯函数 + 可选 pilot 回调
# --------------------------------------------------------------------------- #
def plan_docking_params(*, task_type: str = "screening",
                        molecules: Optional[Sequence[Dict[str, Any]]] = None,
                        box_size: Optional[Sequence[float]] = None,
                        user_params: Optional[Dict[str, Any]] = None,
                        budget_sec: Optional[float] = None,
                        pilot: Optional[PilotCallback] = None) -> Dict[str, Any]:
    """规划一次运行的对接参数，返回可落盘的 `param_plan`（纯函数 + 可选 pilot 回调）。

    - `molecules`：候选分子（含 `smiles`），用于库规模与柔性统计；
    - `box_size`：主组对接盒边长（Å）；未知时盒体积系数取 1.0 并如实记录；
    - `user_params`：用户显式指定的参数（`exhaustiveness` / `n_poses`）；
      **一旦给出就冻结，自动规划不改写**（`source="user"`）；
    - `budget_sec`：预算秒数；None 时按 `AUTO_PARAM_BUDGET_RATIO × RUN_TIMEOUT_SECONDS` 计算；
    - `pilot`：可选回调 `fn(candidates) -> 单分子秒数`（exhaustiveness=1）。
      仅用于**测量**；抛异常/返回 None 一律退回静态规划（不抛、只 warning）。
    """
    task_type = str(task_type or "screening").strip().lower() or "screening"
    molecules = list(molecules or [])
    user_params = dict(user_params or {})
    user_exh = _as_int(user_params.get("exhaustiveness"))
    user_np = _as_int(user_params.get("n_poses"))
    library_n = len(molecules)
    decisions: List[str] = []
    warnings: List[str] = []

    budget = _budget(budget_sec)

    # ---- 关闭自动规划：沿用系统默认（行为与旧版一致）----
    if not env_bool("AUTO_PARAM_ENABLED", True):
        return {
            "source": "disabled", "task_type": task_type,
            "exhaustiveness": user_exh if user_exh is not None else FALLBACK_EXHAUSTIVENESS,
            "coarse_exhaustiveness": None, "refine_top_n": None, "two_stage": False,
            "n_poses": user_np if user_np is not None else FALLBACK_N_POSES,
            "engine": PLAN_ENGINE, "seed": PLAN_SEED, "library_size": library_n,
            "p90_rotatable": None, "flex_factor": None, "box_factor": None,
            "eta_sec": None, "budget_sec": round(budget, 1),
            "pilot": {"status": "skipped", "n": 0, "seconds_per_molecule": None,
                      "note": "AUTO_PARAM_ENABLED=off"},
            "decisions": ["AUTO_PARAM_ENABLED=off：不做自动规划，沿用系统默认 "
                          f"exhaustiveness={FALLBACK_EXHAUSTIVENESS} / n_poses={FALLBACK_N_POSES}"],
            "warnings": warnings,
        }

    # ---- 不涉及对接的任务：不规划对接参数 ----
    if task_type == "properties_only":
        return {
            "source": "skipped", "task_type": task_type,
            "exhaustiveness": None, "coarse_exhaustiveness": None, "refine_top_n": None,
            "two_stage": False, "n_poses": None, "engine": PLAN_ENGINE, "seed": PLAN_SEED,
            "library_size": library_n, "p90_rotatable": None, "flex_factor": None,
            "box_factor": None, "eta_sec": None, "budget_sec": round(budget, 1),
            "pilot": {"status": "skipped", "n": 0, "seconds_per_molecule": None,
                      "note": "properties_only 不需要对接"},
            "decisions": ["任务类型 properties_only：本次不做对接，不规划对接参数"],
            "warnings": warnings,
        }

    binding = task_type == "binding_only"
    base = (env_int("AUTO_PARAM_BASE_BINDING", 16) if binding
            else env_int("AUTO_PARAM_BASE_SCREENING", 16))
    decisions.append(f"任务类型 {task_type} → 基准搜索强度 base={base}"
                     + ("（姿态/结合模式分析）" if binding else "（筛选）"))

    # ---- 库统计：柔性系数 ----
    stats = library_stats(molecules)
    raw_p90 = stats.get("p90_rotatable")
    p90 = float(raw_p90 if raw_p90 is not None
                else env_float("AUTO_PARAM_FLEX_DIVISOR", 5.0))
    flex_div = max(0.001, env_float("AUTO_PARAM_FLEX_DIVISOR", 5.0))
    flex_min = env_float("AUTO_PARAM_FLEX_MIN", 0.75)
    flex_max = max(flex_min, env_float("AUTO_PARAM_FLEX_MAX", 2.5))
    flex_factor = _clamp(p90 / flex_div, flex_min, flex_max)
    decisions.append(f"P90 可旋转键 {p90:g}（库 {library_n} 个，{stats.get('note', '')}）"
                     f" → 柔性系数 {flex_factor:.2f}")
    if stats.get("degraded"):
        warnings.append(str(stats.get("note") or "库统计降级为保守默认"))

    # ---- 盒体积系数：保持单位体积采样密度 ----
    box_ref = max(0.001, env_float("AUTO_PARAM_BOX_REF", 22.0))
    box_factor_max = max(1.0, env_float("AUTO_PARAM_BOX_FACTOR_MAX", 2.0))
    if box_size and len([x for x in box_size if x]) == 3:
        try:
            volume = 1.0
            for edge in box_size:
                volume *= float(edge)
            box_factor = _clamp((volume / (box_ref ** 3)) ** (1.0 / 3.0), 1.0, box_factor_max)
            box_text = "×".join(f"{float(x):.0f}" for x in box_size)
            decisions.append(f"主盒 {box_text} Å → 体积系数 {box_factor:.2f}"
                             f"（参考边长 {box_ref:g} Å，保持单位体积采样密度）")
        except (TypeError, ValueError):
            box_factor = 1.0
            decisions.append(f"主盒尺寸无法解析（{box_size!r}）→ 体积系数按 1.0")
    else:
        box_factor = 1.0
        decisions.append("主盒尺寸尚未确定 → 体积系数按 1.0")

    exh_min = max(1, env_int("AUTO_PARAM_EXH_MIN", 2))
    exh_max = max(exh_min, env_int("AUTO_PARAM_EXH_MAX", 32))
    planned_exh = int(_clamp(_round_half_up(base * flex_factor * box_factor), exh_min, exh_max))
    decisions.append(f"exhaustiveness = clamp(round({base} × {flex_factor:.2f} × "
                     f"{box_factor:.2f}), {exh_min}, {exh_max}) = {planned_exh}")

    # ---- 用户显式参数：冻结，不自动改 ----
    source = "auto"
    if user_exh is not None:
        source = "user"
        exhaustiveness = user_exh
        decisions.append(f"用户显式指定 exhaustiveness={user_exh} → 不自动修改（source=user）")
    else:
        exhaustiveness = planned_exh

    np_binding = max(1, env_int("AUTO_PARAM_N_POSES_BINDING", N_POSES_BINDING))
    np_max = max(1, env_int("AUTO_PARAM_N_POSES_MAX", N_POSES_MAX))
    planned_np = min(np_binding if binding else N_POSES_DEFAULT, np_max)
    if user_np is not None:
        n_poses = user_np
        decisions.append(f"用户显式指定 n_poses={user_np} → 不自动修改")
    else:
        n_poses = planned_np
        decisions.append(f"n_poses={n_poses}"
                         + ("（姿态/结合模式分析需要多个位姿）" if binding else "（筛选默认 1）")
                         + f"，上限 {np_max}")

    # ---- 两阶段漏斗 ----
    funnel_min = max(0, env_int("AGENT_FUNNEL_MIN", 500))
    refine_top_n = max(1, min(5000, env_int("AGENT_REFINE_TOP_N", 200)))
    two_stage = bool(funnel_min) and library_n >= funnel_min
    coarse: Optional[int] = _coarse_of(exhaustiveness) if two_stage else None
    if two_stage:
        decisions.append(f"库 {library_n} ≥ {funnel_min} → 两阶段漏斗"
                         f"（粗筛 exh={coarse} / 精算 exh={exhaustiveness}，精算前 {refine_top_n}）")
    else:
        decisions.append(f"库 {library_n} < {funnel_min} → 单阶段（exh={exhaustiveness} 跑全库）")

    # ---- pilot 预算护栏（只测量，不改结果）----
    pilot_n = max(1, env_int("AUTO_PARAM_PILOT_N", 3))
    pilot_min = max(0, env_int("AUTO_PARAM_PILOT_MIN", 50))
    pilot_info: Dict[str, Any] = {"status": "skipped", "n": 0,
                                  "seconds_per_molecule": None, "note": ""}
    eta: Optional[float] = None
    if source == "user":
        pilot_info["note"] = "用户显式指定参数 → 只记录、不降级，pilot 跳过"
        decisions.append("用户显式指定参数 → 跳过 pilot 预算降级（不自动改用户参数）")
    elif pilot is None:
        pilot_info["note"] = "未注入 pilot 回调 → 静态规划"
    elif not env_bool("AUTO_PARAM_PILOT", True):
        pilot_info["note"] = "AUTO_PARAM_PILOT=off → 跳过预算护栏"
        decisions.append("AUTO_PARAM_PILOT=off → 跳过 pilot 预算护栏")
    elif library_n < pilot_min:
        pilot_info["note"] = f"库 {library_n} < AUTO_PARAM_PILOT_MIN={pilot_min} → 跳过 pilot"
    else:
        candidates = [{"name": e.get("name"), "smiles": e.get("smiles"),
                       "cost": e.get("cost")}
                      for e in (stats.get("heaviest") or [])[:pilot_n] if e.get("smiles")]
        if not candidates:
            pilot_info = {"status": "failed", "n": 0, "seconds_per_molecule": None,
                          "note": "取不到 pilot 候选分子（2D 描述符失败）"}
            warnings.append("pilot 候选分子为空 → 退回静态规划")
            logger.warning("pilot 候选分子为空，退回静态规划")
        else:
            measured: Optional[float] = None
            try:
                raw = pilot(candidates)
                if isinstance(raw, dict):
                    raw = raw.get("seconds_per_molecule")
                measured = None if raw is None else float(raw)
                if measured is not None and not math.isfinite(measured):
                    measured = None
            except Exception as e:  # noqa: BLE001
                warnings.append(f"pilot 失败（{type(e).__name__}: {e}）→ 退回静态规划")
                logger.warning("pilot 对接失败，退回静态规划：%s", e)
            if measured is None:
                pilot_info = {"status": "failed", "n": len(candidates),
                              "seconds_per_molecule": None, "note": "pilot 未返回有效耗时"}
                if not any("pilot" in w for w in warnings):
                    warnings.append("pilot 未返回有效耗时 → 退回静态规划")
                    logger.warning("pilot 未返回有效耗时，退回静态规划")
            else:
                pilot_info = {"status": "ok", "n": len(candidates),
                              "seconds_per_molecule": round(measured, 4),
                              "note": f"实测 {measured:.2f} s/分子（exhaustiveness=1）"}
                decisions.append(f"pilot 实测 {measured:.2f} s/分子（最贵 {len(candidates)} 个，"
                                 "exhaustiveness=1 真实对接）")

    if pilot_info["status"] == "ok":
        mean = float(pilot_info["seconds_per_molecule"])
        eta = _estimate_eta(mean, library_n, exhaustiveness, coarse, refine_top_n, two_stage)
        decisions.append(f"预估总耗时 {eta:.0f} s vs 预算 {budget:.0f} s"
                         f"（{env_float('AUTO_PARAM_BUDGET_RATIO', 0.6):g} × RUN_TIMEOUT_SECONDS）")

        # 库很大且预算允许：精度优先，把精算头部从 200 上调到 300
        large_lib = max(0, env_int("AUTO_PARAM_LARGE_LIB", 5000))
        refine_large = max(1, min(5000, env_int("AUTO_PARAM_REFINE_TOP_LARGE", 300)))
        if two_stage and library_n >= large_lib and refine_top_n < refine_large:
            eta_large = _estimate_eta(mean, library_n, exhaustiveness, coarse,
                                      refine_large, two_stage)
            if eta_large <= budget:
                decisions.append(f"库 {library_n} ≥ {large_lib} 且预算允许"
                                 f"（{eta_large:.0f} s ≤ {budget:.0f} s）：精算头部 "
                                 f"{refine_top_n}→{refine_large}（精度优先）")
                refine_top_n = refine_large
                eta = eta_large
            else:
                decisions.append(f"库 {library_n} ≥ {large_lib} 但预算不允许"
                                 f"（{eta_large:.0f} s > {budget:.0f} s）：精算头部维持 "
                                 f"{refine_top_n}")

        # 超预算 → 精度优先的降级顺序：① 先降精算头部（下限 100）② 再降强度（不低于 base/2）
        if eta > budget:
            decisions.append(f"pilot 预估 {eta:.0f} s > 预算 {budget:.0f} s → 触发精度优先降级")
            refine_min = max(1, min(5000, env_int("AUTO_PARAM_REFINE_TOP_MIN", 100)))
            if two_stage and refine_top_n > refine_min:
                old_top = refine_top_n
                refine_top_n = refine_min
                eta = _estimate_eta(mean, library_n, exhaustiveness, coarse,
                                    refine_top_n, two_stage)
                decisions.append(f"精度优先：精算头部 {old_top}→{refine_top_n}"
                                 "（先降头部，不先降强度）")
            floor = max(exh_min, int(math.ceil(base / 2.0)))
            if eta > budget and exhaustiveness > floor:
                new_exh = _largest_exh_within_budget(mean, library_n, budget, floor=floor,
                                                     ceiling=exhaustiveness,
                                                     refine_top_n=refine_top_n,
                                                     two_stage=two_stage)
                old_exh = exhaustiveness
                exhaustiveness = new_exh
                if two_stage:
                    coarse = _coarse_of(exhaustiveness)
                eta = _estimate_eta(mean, library_n, exhaustiveness, coarse,
                                    refine_top_n, two_stage)
                decisions.append(f"仍超预算：exhaustiveness {old_exh}→{exhaustiveness}"
                                 f"（下限=基准的一半 {floor}，精度优先）")
            if eta > budget:
                warnings.append("降级到下限后仍超预算，已按精度优先保留当前强度")
                decisions.append(f"已到降级下限仍超预算（预估 {eta:.0f} s > {budget:.0f} s）："
                                 "按精度优先保留当前强度；如需按时完成请缩小库或调大 "
                                 "RUN_TIMEOUT_SECONDS")

    return {
        "source": source,
        "task_type": task_type,
        "exhaustiveness": exhaustiveness,
        "coarse_exhaustiveness": coarse,
        "refine_top_n": refine_top_n if two_stage else None,
        "two_stage": two_stage,
        "n_poses": n_poses,
        "engine": PLAN_ENGINE,
        "seed": PLAN_SEED,
        "library_size": library_n,
        "p90_rotatable": p90,
        "flex_factor": round(flex_factor, 3),
        "box_factor": round(box_factor, 3),
        "box_size": [float(x) for x in box_size] if box_size else None,
        "eta_sec": round(eta, 1) if eta is not None else None,
        "budget_sec": round(budget, 1),
        "pilot": pilot_info,
        "decisions": decisions,
        "warnings": warnings,
        "stats_cached": bool(stats.get("cached")),
    }
