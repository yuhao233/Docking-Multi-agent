"""配体质子化的专业 pKa 引擎层（Dimorphite-DL）回归。

设计要点：
  * **默认优先专业引擎**（装了就用），`LIGAND_PKA_ENGINE=rules` 可强制回退内置规则表（复现旧口径）；
  * 引擎不可用时**如实回退**并记录原因 + 安装提示，绝不让配体准备失败；
  * 窗口内多微观态时按可复现规则取一个形式对接，规则写进溯源；
  * 本文件在**装了/没装 Dimorphite** 的环境都必须通过（CI 不装）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"
#: 组胺在 pH 7.4 附近有多个微观态（+1 / -1 / 0 / 0），用于验证选择规则
HISTAMINE_VARIANTS = ["[NH3+]CCc1c[nH]cn1", "NCCc1c[n-]cn1",
                      "[NH3+]CCc1c[n-]cn1", "NCCc1c[nH]cn1"]


def _engine_available() -> bool:
    from docking_agent.core import ligand_pka

    return bool(ligand_pka.engine_status().get("available"))


def test_engine_status_shape() -> None:
    from docking_agent.core import ligand_pka

    status = ligand_pka.engine_status()
    assert set(status) >= {"engine", "available", "install", "active"}
    if not status["available"]:
        assert status["install"], "不可用时必须给出安装提示（含 --no-deps 的原因）"
        assert "no-deps" in status["install"]
        assert "rdkit" in status["install"]


def test_pick_variant_prefers_lowest_absolute_charge() -> None:
    """选择规则：|净电荷| 最小 → 带电原子最少 → 字典序（可复现，且写进 rule）。"""
    from docking_agent.core.ligand_pka import pick_variant

    best = pick_variant(HISTAMINE_VARIANTS)
    assert best["charge"] == 0, best
    assert best["smiles"] in ("[NH3+]CCc1c[n-]cn1", "NCCc1c[nH]cn1")
    assert "净电荷" in best["rule"]
    # 只有一种形态时直接选它
    assert pick_variant(["CC(=O)[O-]"])["smiles"] == "CC(=O)[O-]"
    # 全部不可解析 → 空结果（调用方据此回退）
    assert pick_variant(["not_a_smiles"]) == {}


def test_forced_rules_engine_skips_professional_engine() -> None:
    from docking_agent.core import ligand_pka

    result = ligand_pka.protonate(ASPIRIN, 7.4, engine="rules")
    assert result["ok"] is False and "rules" in result["reason"]


def test_apply_protonation_records_engine_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """走正式入口 `apply_protonation`：溯源里必须能看出用了哪个引擎。"""
    from docking_agent.core.protonation import apply_protonation

    monkeypatch.setenv("LIGAND_PKA_ENGINE", "rules")
    _, info = apply_protonation(ASPIRIN, policy="ph", ph=7.4)
    assert info["policy"] == "ph" and info["ph"] == 7.4
    assert info.get("engine_fallback_reason"), "回退必须写明原因"
    assert "规则表" in info["method"]
    assert "规则表" in info["note"] or "专业引擎不可用" in info["note"]


@pytest.mark.skipif(not _engine_available(), reason="未安装 Dimorphite-DL")
def test_professional_engine_used_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """装了专业引擎时默认就用它，并把引擎/版本/微观态写进溯源。"""
    from docking_agent.core.protonation import apply_protonation

    monkeypatch.delenv("LIGAND_PKA_ENGINE", raising=False)
    out, info = apply_protonation(ASPIRIN, policy="ph", ph=7.4)
    assert info.get("engine") == "dimorphite-dl"
    assert info.get("engine_version")
    assert info.get("variants")
    assert out.endswith("[O-]"), f"pH 7.4 下羧酸应去质子化：{out}"
    assert info["charge_before"] == 0 and info["charge_after"] == -1
    # 中性分子在酸性 pH 下保持不变
    acid_out, acid_info = apply_protonation(ASPIRIN, policy="ph", ph=1.5)
    assert acid_info["charge_after"] == 0 and acid_out == ASPIRIN


@pytest.mark.skipif(not _engine_available(), reason="未安装 Dimorphite-DL")
def test_multi_variant_run_is_traceable(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.core.protonation import apply_protonation

    monkeypatch.delenv("LIGAND_PKA_ENGINE", raising=False)
    _, info = apply_protonation("NCCc1c[nH]cn1", policy="ph", ph=7.4)
    assert len(info.get("variants") or []) >= 1
    assert info.get("variant_rule")
    if len(info["variants"]) > 1:
        assert "微观态" in info["note"], info["note"]


def test_protonation_summary_counts_engines() -> None:
    from docking_agent.reporting.content import protonation_summary

    rows = [
        {"name": "A", "smiles": "CCO", "ligand_facts": {"protonation": {
            "policy": "ph", "ph": 7.4, "applied": True, "charge_before": 0, "charge_after": -1,
            "engine": "dimorphite-dl", "engine_version": "2.0.2"}}},
        {"name": "B", "smiles": "CCO", "ligand_facts": {"protonation": {
            "policy": "ph", "ph": 7.4, "applied": False, "charge_before": 0, "charge_after": 0,
            "engine_fallback_reason": "未安装专业 pKa 引擎（Dimorphite-DL）"}}},
    ]
    summary = protonation_summary(rows)
    assert summary["engine_counts"] == {"dimorphite-dl": 1, "内置规则表（回退）": 1}
    assert summary["engine_versions"]["dimorphite-dl"] == "2.0.2"
    assert summary["observed"] == 2 and len(summary["applied"]) == 1


def test_missing_engine_falls_back_and_explains(monkeypatch: pytest.MonkeyPatch) -> None:
    """没装专业引擎的环境（CI）必须：如实回退内置规则表 + 给出安装提示，绝不报错。"""
    from docking_agent.core import ligand_pka
    from docking_agent.core.protonation import apply_protonation

    monkeypatch.setattr(ligand_pka, "dimorphite_engine", lambda: None)
    monkeypatch.delenv("LIGAND_PKA_ENGINE", raising=False)

    status = ligand_pka.engine_status()
    assert status["available"] is False and status["active"] is False
    assert "no-deps" in status["install"] and "rdkit" in status["install"]

    result = ligand_pka.protonate(ASPIRIN, 7.4)
    assert result["ok"] is False and "未安装" in result["reason"]

    # 正式入口仍然给出可用结果（规则表），并把回退原因与安装提示写进溯源
    out, info = apply_protonation(ASPIRIN, policy="ph", ph=7.4)
    assert out.endswith("[O-]"), f"规则表也应把羧酸处理成羧酸根：{out}"
    assert "未安装" in info["engine_fallback_reason"]
    assert "no-deps" in info.get("engine_install", "")
    assert info["charge_before"] == 0 and info["charge_after"] == -1
