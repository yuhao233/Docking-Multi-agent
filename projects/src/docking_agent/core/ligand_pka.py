"""配体质子化引擎：优先用**专业 pKa 软件**（Dimorphite-DL），不可用时回退内置规则表。

为什么单独一层：受体侧已经用专业工具（`pdb2pqr` + PROPKA，见 `core/receptor_ph.py`），
而配体侧原先是**内置官能团 pKa 规则表**（`core/protonation.py`，明确标注「近似，不是 pKa 预测」）。
本模块把「用哪个引擎」做成可探测、可溯源、可回退的一层：

    auto（默认）  → 装了专业引擎就用它；否则回退内置规则表（并在报告里如实说明）
    dimorphite    → 强制用 Dimorphite-DL（不可用即回退）
    rules         → 强制内置规则表（复现旧口径 / 离线兜底）

环境变量：
    LIGAND_PKA_ENGINE      auto | dimorphite | rules（默认 auto）
    LIGAND_PKA_WINDOW      靶 pH ± 该值内的微观态都参与选择（默认 0.5；0 = 只取该 pH）
    LIGAND_PKA_PRECISION   Dimorphite 的 pKa 精度因子（默认 0.1：越小越少「临界态」）

**安装说明（重要）**：Dimorphite-DL 2.0.2 的元数据把 RDKit 钉在 `rdkit<2026`，
而本项目需要 `rdkit>=2026.3.6`。实测 2.0.2 在 RDKit 2026.3.6 上功能正常，
因此安装时用 `--no-deps`（只补 loguru 依赖），**避免把 RDKit 降级**：

    uv pip install loguru
    uv pip install --no-deps dimorphite-dl
"""
from __future__ import annotations

import importlib.metadata as md
import logging
from typing import Any, Dict, List, Optional, Sequence

from rdkit import Chem

from docking_agent.config import env, env_float

logger = logging.getLogger(__name__)

#: 引擎选择（`LIGAND_PKA_ENGINE`）
ENGINES = ("auto", "dimorphite", "rules")
#: pH 窗口半宽默认值
DEFAULT_WINDOW = 0.5
#: Dimorphite 的 pKa 精度因子默认值（0.1 = 只把真正临界的位点列为多态）
DEFAULT_PRECISION = 0.1
#: 单分子最多枚举的微观态数（防组合爆炸）
MAX_VARIANTS = 32
#: 安装提示（含 --no-deps 的原因）
INSTALL_HINT = ("uv pip install loguru && uv pip install --no-deps dimorphite-dl"
                "（其元数据把 rdkit 钉在 <2026，而本项目需要 >=2026.3.6；"
                "实测 dimorphite-dl 2.0.2 与 rdkit 2026.3.6 兼容，故用 --no-deps 避免降级 RDKit）")


def configured_engine(explicit: Optional[str] = None) -> str:
    raw = str(explicit or env("LIGAND_PKA_ENGINE") or "auto").strip().lower()
    return raw if raw in ENGINES else "auto"


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(env_float(name, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def configured_window() -> float:
    return _env_float("LIGAND_PKA_WINDOW", DEFAULT_WINDOW, 0.0, 2.0)


def configured_precision() -> float:
    return _env_float("LIGAND_PKA_PRECISION", DEFAULT_PRECISION, 0.01, 2.0)


def dimorphite_engine() -> Optional[Dict[str, Any]]:
    """Dimorphite-DL 可用时返回引擎信息，否则 None。"""
    try:
        from dimorphite_dl import protonate_smiles  # noqa: F401
    except Exception as e:  # noqa: BLE001 - 未安装/依赖冲突都按「不可用」处理
        logger.debug("Dimorphite-DL 不可用：%s", e)
        return None
    version = ""
    try:
        version = md.version("dimorphite-dl")
    except Exception:  # noqa: BLE001 - 元数据缺失不影响功能
        version = "未知"
    return {"engine": "dimorphite-dl", "version": version,
            "note": "专业 pKa 规则库（RDKit）：按目标 pH 枚举可电离官能团的微观态"}


def engine_status() -> Dict[str, Any]:
    """给 doctor / 设置页 / 报告用的引擎状态（含安装提示）。"""
    info = dimorphite_engine()
    if info:
        return {**info, "available": True, "install": "", "active": configured_engine() != "rules"}
    return {"engine": "", "version": "", "available": False, "active": False,
            "note": "未安装专业 pKa 引擎（Dimorphite-DL），配体 pH 处理回退内置规则表（近似）",
            "install": INSTALL_HINT}


def _variant_row(smiles: str) -> Optional[Dict[str, Any]]:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    return {"smiles": Chem.MolToSmiles(mol),
            "charge": int(Chem.GetFormalCharge(mol)),
            "charged_atoms": sum(1 for atom in mol.GetAtoms() if atom.GetFormalCharge())}


def pick_variant(variants: Sequence[str]) -> Dict[str, Any]:
    """多微观态里选一个用于对接：|净电荷| 最小 → 带电原子最少 → 字典序（可复现）。

    为什么这样选：Dimorphite 只给「窗口内可能的微观态」，**不给布居数**；对接只能取一个形式。
    在它已经把 pKa 明显偏离 pH 的位点固定之后（`precision` 控制），剩下的多态都是**临界态**，
    此时取净电荷最小的形式是 docking 界的常规取法，且完全可复现。选择规则会写进溯源与报告，
    用户可据此判断是否需要自行提供已准备好的质子化态（策略 `keep`）。
    """
    rows: List[Dict[str, Any]] = []
    for smiles in variants or []:
        row = _variant_row(str(smiles))
        if row is not None:
            rows.append(row)
    if not rows:
        return {}
    rows.sort(key=lambda r: (abs(r["charge"]), r["charged_atoms"], r["smiles"]))
    best = dict(rows[0])
    best["rule"] = "|净电荷| 最小 → 带电原子最少 → 字典序"
    return best


def protonate(smiles: str, ph: float, *, engine: Optional[str] = None) -> Dict[str, Any]:
    """用专业引擎按目标 pH 处理配体，返回单一位点形式的 SMILES。

    返回字典：
      ok=True  → `smiles` / `charge` / `engine` / `version` / `variants` / `rule` / `window`
      ok=False → `reason`（调用方据此回退内置规则表；`install` 给出补齐方法）
    """
    chosen = configured_engine(engine)
    if chosen == "rules":
        return {"ok": False, "reason": "已配置 LIGAND_PKA_ENGINE=rules（强制内置规则表）"}
    info = dimorphite_engine()
    if info is None:
        return {"ok": False, "reason": "未安装专业 pKa 引擎（Dimorphite-DL）",
                "install": INSTALL_HINT}

    text = str(smiles or "").strip()
    if not text or Chem.MolFromSmiles(text) is None:
        return {"ok": False, "reason": "SMILES 无法解析，专业引擎未执行"}

    window = configured_window()
    precision = configured_precision()
    try:
        from dimorphite_dl import protonate_smiles

        raw_variants = protonate_smiles(text, ph_min=max(0.0, ph - window),
                                        ph_max=min(14.0, ph + window),
                                        precision=precision, max_variants=MAX_VARIANTS)
    except Exception as e:  # noqa: BLE001 - 引擎异常一律回退，不让配体准备失败
        logger.warning("Dimorphite-DL 处理失败（%s: %s），回退内置规则表", type(e).__name__, e)
        return {"ok": False, "reason": f"引擎异常（{type(e).__name__}: {e}）",
                "install": INSTALL_HINT}

    best = pick_variant(raw_variants)
    if not best:
        return {"ok": False, "reason": "引擎未返回任何可解析的质子化态", "install": INSTALL_HINT}
    return {"ok": True, "smiles": best["smiles"], "charge": best["charge"],
            "charged_atoms": best["charged_atoms"], "rule": best["rule"],
            "variants": [row["smiles"] for row in
                         (r for r in (_variant_row(v) for v in raw_variants) if r)],
            "engine": info["engine"], "version": info["version"],
            "window": window, "precision": precision, "ph": ph}
