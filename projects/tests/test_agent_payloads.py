"""Agent 之间的信息传递：**给模型的视图**必须精简，完整明细留在产物里。

用户要求「每一个 agent 的输出信息也要精简高效、专业」。这里的口径是**受众分离**：

- 工具产物（`*_tool.json`）保留**全量**明细 —— 报告、排序、审计都读它；
- 回给模型（子 Agent）的载荷只带**决策与转述所需**的字段，逐分子的溯源散文压成聚合口径。

为什么不是单纯「删字段」：`protonation.method` 这类字符串（"dimorphite-dl 2.0.2（专业 pKa 引擎；
pH 7.4 ± 0.5）"）完全是 `engine`+`engine_version`+`ph`+`engine_window` 的重复描述，逐分子重复
一遍能占整行 80% 的字节，还最容易被模型照抄进正文。

本文件**不属于** `ENGINE_TEST_FILES`：这些是纯载荷形状用例（引擎全部被 monkeypatch），
CI（`DOCKING_ENGINE_TESTS=0`）必须跑到。
"""
from __future__ import annotations

import json
from typing import Any

import pytest


def _payload_size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# Agent 之间的信息传递：**给模型的视图**必须精简，完整明细留在产物里
# --------------------------------------------------------------------------- #
def _payload_size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


def test_property_payload_is_agent_sized_and_artifact_keeps_full_detail(run_ctx) -> None:
    """属性评估回给模型的行只带数据字段；逐分子质子化散文留在产物里。

    为什么值得钉住：`protonation` 的 `method`/`note`/`variants` 逐分子重复一遍能占整行 80% 的
    字节，而报告要的只是 engine/charge 这些数据字段 —— 模型照着长文复述还会把报告撑肿。
    """
    from docking_agent.tools import properties

    run, _board = run_ctx
    mols = [{"name": f"M{i}", "smiles": "NC(=N)c1ccccc1"} for i in range(5)]
    raw = properties.molecular_property_assessment.func(molecules_json=json.dumps(mols))
    out = json.loads(raw)
    assert out["status"] == "ok" and len(out["assessment"]) == 5
    row = out["assessment"][0]
    # 视图里只有数据字段 + 压成 policy/applied 的质子化口径
    assert set(row["protonation"]) <= {"policy", "applied"}, row["protonation"]
    for forbidden in ("method", "note", "variants", "variant_rule", "engine_window", "rules"):
        assert forbidden not in row["protonation"], forbidden
    assert row["molecular_weight"] and row["logP"] is not None
    # 完整明细仍在产物里（报告与审计读它）
    saved = json.loads((run.dir / "properties_tool.json").read_text(encoding="utf-8"))
    assert "variants" in saved[0]["protonation"] and saved[0]["protonation"].get("engine")
    assert _payload_size(out) < _payload_size(saved), "给模型的载荷必须小于产物"


def test_docking_payload_drops_report_only_detail(run_ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """对接视图：保留盒子溯源与结论字段，压掉逐分子溯源长文与「报告才需要」的块字段。"""
    from docking_agent.tools import docking as dock_tool

    run, _board = run_ctx

    def _fake_dock(molecules, **kwargs):
        return {"status": "ok", "notes": ["受体准备：丢弃 5CM×20"],
                "receptors": [{"receptor_key": "R", "receptor": "R", "pdbqt": "/x.pdbqt",
                               "box_center": [1, 2, 3], "box_size": [22, 22, 22],
                               "box_source": "实验位点", "box_validation": {"status": "consistent"},
                               "box_atom_stats": {"atoms": 542, "nearest_atom": "ASP611"},
                               "pockets": [{"rank": 1, "name": "p1", "score": 28.9,
                                            "residues": ["A_1", "A_2"]}],
                               "results": [{"name": "M1", "smiles": "CCO", "affinity_kcal_mol": -5.0,
                                            "engine": "vina", "exhaustiveness": 19, "n_poses": 1,
                                            "pose_file": "poses/p0.pdbqt",
                                            "ligand_facts": {"num_fragments": 1, "has_metal": False,
                                                             "formal_charge": 0,
                                                             "protonation": {
                                                                 "policy": "ph", "applied": True,
                                                                 "engine": "dimorphite-dl",
                                                                 "engine_version": "2.0.2",
                                                                 "method": "dimorphite-dl 2.0.2（专业 pKa 引擎；pH 7.4 ± 0.5）",
                                                                 "note": "目标 pH 7.4：净电荷 +0 → +0",
                                                                 "variants": ["CCO"], "variant_rule": "|净电荷| 最小",
                                                                 "rules": [{"name": "carboxylic"}]}},
                                            "ligand_warnings": ["w1", "w2", "w3"]}]}]}

    monkeypatch.setattr(dock_tool, "dock_library", _fake_dock)
    # 用注册表里的内部测试受体（`thrombin`）：本用例只关心「给模型的视图」，
    # 而**不可用/无法识别的受体现在会抛 ReceptorInputError**（见
    # tests/test_receptor_ext_upload.py），所以不能随便编一个名字。
    out = json.loads(dock_tool.molecular_docking.invoke(
        {"molecules_json": json.dumps([{"name": "M1", "smiles": "CCO"}]),
         "receptor_sources": "thrombin"}))
    block = out["receptors"][0]
    assert "box_atom_stats" not in block, "报告才需要的字段不该塞给模型"
    assert block["box_source"] == "实验位点" and block["pockets"], "盒子溯源必须保留（要转述）"
    row = block["results"][0]
    assert row["affinity_kcal_mol"] == -5.0 and row["pose_file"]
    assert set(row["ligand_facts"]["protonation"]) <= {"policy", "applied", "engine",
                                                       "engine_version", "ph", "charge_before",
                                                       "charge_after"}
    assert "method" not in row["ligand_facts"]["protonation"]
    assert len(row["ligand_warnings"]) == 2, "告警最多两条（其余在产物里）"
    # 完整明细仍在产物里
    saved = json.loads((run.dir / "docking_tool.json").read_text(encoding="utf-8"))
    full = saved["receptors"][0]
    assert "box_atom_stats" in full
    assert full["results"][0]["ligand_facts"]["protonation"].get("variants")


def test_binding_payload_does_not_repeat_control_pharmacophore(run_ctx) -> None:
    """结合模式：对照的药效团只在顶层给一次，逐行不再重复（按分子数线性膨胀）。"""
    from docking_agent.tools import binding

    _run, _board = run_ctx
    out = json.loads(binding.binding_mode_analysis.func(
        molecules_json=json.dumps([{"name": "M1", "smiles": "CCO"},
                                   {"name": "M2", "smiles": "NC(=N)c1ccccc1"}]),
        positive_control_smiles="NC(=N)c1ccccc1"))
    assert out["control_pharmacophore"], "顶层必须给出对照药效团"
    for row in out["results"]:
        assert "control_pharmacophore" not in row, row
        assert "anchor_match" in row and row["pharmacophore"], "锚定匹配与自身药效团仍要保留"
