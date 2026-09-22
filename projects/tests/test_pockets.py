"""结合口袋预测与「工具定盒」的回归测试。

关键质量门：内置几何法在凝血酶（1DWC）上必须把**共晶配体所在的位点**排进 top-3。
本文件用 **1.5 Å 粗网格**（快，≈0.3 s）跑质量门：实测 top-1 距共晶配体 MIT 中心
31.5/13.74/24.36 为 **3.60 Å**（默认 1.0 Å 网格为 2.6 Å，见 `core/pockets.py` 的实测表）——
精度与网格间距相关，引用数字要带 spacing。这是「盒子不是猜的」的客观证据。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

THROMBIN_PDBQT = str(PROJECT_ROOT / "assets/receptors/registry/thrombin_1DWC.pdbqt")
THROMBIN_PDB = str(PROJECT_ROOT / "assets/receptors/structures/thrombin.pdb")
COCRYSTAL_CENTER = [31.5, 13.74, 24.36]


@pytest.fixture(scope="module")
def thrombin_spec():
    from docking_agent.core import read_receptor_file

    return read_receptor_file(THROMBIN_PDBQT)


# --------------------------------------------------------------------------- #
# 内置几何法（真实计算 + 客观质量门）
# --------------------------------------------------------------------------- #
def test_geometric_detector_finds_cocrystal_site():
    """top-1/top-3 必须命中共晶配体所在的结合位点。"""
    from docking_agent.core import pockets as P

    atoms = P.read_receptor_atoms(THROMBIN_PDBQT)
    out = P.detect_geometric(atoms, top_n=5, spacing=1.5)   # 测试用粗网格，快
    assert out["status"] == "ok" and out["pockets"]
    ligand = P.cocrystal_ligand(THROMBIN_PDB)
    assert ligand and ligand["resname"] == "MIT"
    distances = [P.distance(p["center"], ligand["center"]) for p in out["pockets"]]
    assert distances[0] <= 8.0, f"top-1 未命中实验位点：{distances[0]:.2f} Å（{distances}）"
    assert min(distances[:3]) <= 8.0, f"top-3 未命中实验位点：{distances[:3]}"
    top = out["pockets"][0]
    for key in ("score", "center", "extent", "residues", "burial", "enclosure"):
        assert key in top, f"口袋缺少字段 {key}"
    assert top["residues"], "口袋应给出附近残基"


def test_geometric_detector_is_deterministic():
    from docking_agent.core import pockets as P

    atoms = P.read_receptor_atoms(THROMBIN_PDBQT)
    a = P.detect_geometric(atoms, top_n=3, spacing=1.5)["pockets"]
    b = P.detect_geometric(atoms, top_n=3, spacing=1.5)["pockets"]
    assert [p["center"] for p in a] == [p["center"] for p in b], "口袋预测必须可复现"


def test_cocrystal_ligand_skips_ions_and_additives():
    from docking_agent.core import pockets as P

    ligand = P.cocrystal_ligand(THROMBIN_PDB)
    assert ligand and ligand["n_atoms"] >= 6
    assert ligand["resname"] not in ("HOH", "SO4", "GOL", "EDO")


# --------------------------------------------------------------------------- #
# 口袋 → 盒子
# --------------------------------------------------------------------------- #
def test_pocket_to_box_padding_and_clamping():
    from docking_agent.core import pockets as P

    small = {"center": [0.0, 0.0, 0.0], "extent": [4.0, 5.0, 6.0]}
    center, size = P.pocket_to_box(small, padding=4.0, min_size=18.0, max_size=30.0)
    assert center == [0.0, 0.0, 0.0]
    assert size == [18.0, 18.0, 18.0], "过小的口袋要被抬到下限"

    big = {"center": [1.5, 2.5, 3.5], "extent": [40.0, 40.0, 40.0]}
    _center, size = P.pocket_to_box(big, padding=4.0, min_size=18.0, max_size=30.0)
    assert size == [30.0, 30.0, 30.0], "过大的口袋要被压到上限"


# --------------------------------------------------------------------------- #
# 盒子决策规则
# --------------------------------------------------------------------------- #
def test_select_site_prefers_experimental_site_with_tool_validation(thrombin_spec):
    from docking_agent.core import pockets as P

    spec = dict(thrombin_spec)
    spec["center"] = list(COCRYSTAL_CENTER)
    spec["size"] = [22.0, 22.0, 22.0]
    spec["site"] = {"center": list(COCRYSTAL_CENTER), "size": [22.0, 22.0, 22.0],
                    "source": "共晶配体质心"}
    picked = P.select_site(spec, engine="geometric", use_cache=False)
    assert picked["chosen_by"] == "experimental_site"
    assert picked["center"] == COCRYSTAL_CENTER, "实验位点优先，中心不被预测结果替换"
    assert picked["validation"]["status"] == "consistent"
    assert "预测一致" in picked["source"] and "Å" in picked["source"]
    assert picked["pockets"], "同时要保留工具预测的口袋清单"


def test_select_site_replaces_low_trust_centroid_fallback(thrombin_spec):
    """只有「蛋白质心」这种兜底位点时，必须改用工具预测的口袋。"""
    from docking_agent.core import pockets as P

    spec = dict(thrombin_spec)
    spec["center"] = [10.0, 10.0, 10.0]
    spec["size"] = [22.0, 22.0, 22.0]
    spec["site"] = {"center": [10.0, 10.0, 10.0], "size": [22.0, 22.0, 22.0],
                    "source": "受体原子质心（未找到位点信息，建议显式指定位点）"}
    picked = P.select_site(spec, engine="geometric", use_cache=False)
    assert picked["chosen_by"] == "pocket_prediction"
    assert P.distance(picked["center"], COCRYSTAL_CENTER) <= 8.0, "应落到工具预测的真实口袋"
    assert "几何法" in picked["source"] or "geometric" in picked["source"]


def test_select_site_honors_explicit_override(thrombin_spec):
    from docking_agent.core import pockets as P

    picked = P.select_site(thrombin_spec, engine="geometric", override={
        "center": [1.0, 2.0, 3.0], "size": [20.0, 20.0, 20.0], "source": "用户指定"})
    assert picked["center"] == [1.0, 2.0, 3.0]
    assert picked["chosen_by"] == "request"
    assert picked["pockets"] == []


def test_select_site_known_site_engine_skips_prediction(thrombin_spec):
    from docking_agent.core import pockets as P

    spec = dict(thrombin_spec)
    spec["center"] = list(COCRYSTAL_CENTER)
    spec["size"] = [22.0, 22.0, 22.0]
    spec["site"] = {"center": list(COCRYSTAL_CENTER), "size": [22.0, 22.0, 22.0],
                    "source": "注册表已知位点（1DWC）"}
    picked = P.select_site(spec, engine="known_site", use_cache=False)
    assert picked["chosen_by"] == "known_site"
    assert picked["pockets"] == []
    assert picked["center"] == COCRYSTAL_CENTER


def test_engine_settings_read_env(monkeypatch):
    from docking_agent.core import pockets as P

    monkeypatch.setenv("POCKET_ENGINE", "geometric")
    monkeypatch.setenv("POCKET_PADDING", "6.5")
    monkeypatch.setenv("POCKET_MIN_SIZE", "20")
    settings = P.engine_settings()
    assert settings["engine"] == "geometric"
    assert settings["padding"] == 6.5
    assert settings["min_size"] == 20.0


def test_available_engines_always_has_geometric():
    from docking_agent.core import pockets as P

    engines = P.available_engines()
    assert engines["geometric"] is True and engines["known_site"] is True
    assert set(engines) == {"p2rank", "geometric", "known_site", "centroid"}


def test_p2rank_home_env_override(monkeypatch, tmp_path):
    from docking_agent.core import pockets as P

    fake = tmp_path / "p2rank_fake"
    fake.mkdir()
    script = fake / "prank"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("P2RANK_HOME", str(fake))
    assert P.p2rank_home() == fake
    assert P.p2rank_command() == [str(script)]


def test_p2rank_unavailable_is_reported_honestly(monkeypatch):
    """没有 P2Rank 时必须如实报告 unavailable，并让 auto 回退几何法。"""
    from docking_agent.core import pockets as P

    monkeypatch.setattr(P, "p2rank_home", lambda: None)
    assert P.p2rank_command() is None
    out = P.run_p2rank(THROMBIN_PDB)
    assert out["status"] == "unavailable" and "P2Rank" in out["message"]
    detail = P.detect_pockets(THROMBIN_PDBQT, engine="auto", top_n=3, use_cache=False)
    assert detail["engine"] == "geometric", "P2Rank 缺失时应回退内置几何法"
    assert any(a["engine"] == "p2rank" and a["status"] == "unavailable"
               for a in detail.get("attempts") or []), "回退过程要留痕"


# --------------------------------------------------------------------------- #
# P2Rank 适配层（用合成输出验证解析，不需要真的装 P2Rank）
# --------------------------------------------------------------------------- #
def test_p2rank_prediction_parsing_with_extent(monkeypatch, tmp_path):
    from docking_agent.core import pockets as P

    csv_path = tmp_path / "in.pdb_predictions.csv"
    csv_path.write_text(
        "rank,score,probability,sas_points,surf_atoms,center_x,center_y,center_z,"
        "residue_ids,surf_atom_ids\n"
        "1,12.34,0.812,180,42,31.6,13.9,24.2,\"TRP215 HIS57 SER195\",\"100 101\"\n"
        "2,7.10,0.501,120,30,50.0,20.0,10.0,\"GLU39 LEU40\",\"200 201\"\n",
        encoding="utf-8")
    pockets = P._parse_p2rank_predictions(csv_path, top_n=5)
    assert len(pockets) == 2
    assert pockets[0]["name"] == "pocket_1" and pockets[0]["source"] == "p2rank"
    assert pockets[0]["score"] == 12.34
    assert pockets[0]["center"] == [31.6, 13.9, 24.2]
    assert "TRP215" in pockets[0]["residues"]
    # 中心附近的受体原子范围会被用来估算口袋范围
    atoms = P.read_receptor_atoms(THROMBIN_PDB)
    extent = P._extent_from_atoms(atoms, pockets[0]["center"])
    assert all(6.0 <= e <= 24.0 for e in extent), extent


def test_pdbqt_to_pdb_conversion_keeps_elements(tmp_path):
    from docking_agent.core import pockets as P

    target = tmp_path / "conv.pdb"
    P._pdbqt_to_pdb(THROMBIN_PDBQT, str(target))
    text = target.read_text(encoding="utf-8")
    assert text.count("\n") > 1000, "转换后的 PDB 应包含全部原子"
    atoms = P.read_receptor_atoms(str(target))
    elements = {a["element"] for a in atoms}
    assert {"C", "N", "O"} <= elements, f"元素解析异常：{elements}"
    assert "A" not in elements and "OA" not in elements, "PDBQT 的 AD 类型必须映射成元素"


# --------------------------------------------------------------------------- #
# 黑板交接 + 工具链
# --------------------------------------------------------------------------- #
def test_blackboard_pinned_site_survives_set_receptor():
    from docking_agent.runtime.blackboard import Blackboard

    board = Blackboard("R")
    board.set_receptor({"key": "thrombin", "center": [31.5, 13.74, 24.36],
                        "size": [22, 22, 22], "site": {"source": "共晶配体质心"}})
    board.set_pockets([{"rank": 1, "name": "pocket_1", "score": 4.5,
                        "center": [29.1, 14.7, 24.0], "extent": [13, 11, 7]}], engine="geometric")
    site = board.set_site(center=[29.1, 14.7, 24.0], size=[21, 19, 15],
                          source="口袋分析 Agent 选择 pocket_1", pocket=board.get_pockets()[0])
    assert site["chosen_by"] == "pocket_agent"
    board.set_receptor({"key": "thrombin", "center": [31.5, 13.74, 24.36],
                        "size": [22, 22, 22], "site": {"source": "共晶配体质心"}})
    assert board.get_site()["center"] == [29.1, 14.7, 24.0], "Agent 选定的盒子不得被覆盖"
    assert board.stats()["pockets"] == 1


@pytest.fixture()
def board_ctx():
    from docking_agent.runtime.blackboard import Blackboard, current_blackboard

    board = Blackboard("TEST")
    token = current_blackboard.set(board)
    yield board
    current_blackboard.reset(token)


def test_pocket_tools_predict_compare_commit(thrombin_spec, board_ctx, monkeypatch):
    """三个工具串起来：预测 → 与实验位点比对 → 提交盒子（真实计算）。"""
    from docking_agent.tools.pockets import (
        compare_pocket_with_experiment,
        list_pocket_engines,
        predict_binding_pockets,
        set_docking_site,
    )

    monkeypatch.setenv("POCKET_ENGINE", "geometric")
    out = json.loads(predict_binding_pockets.invoke(
        {"receptor_file": THROMBIN_PDBQT, "top_n": 5}))
    assert out["status"] == "ok"
    block = out["receptors"][0]
    assert block["pockets"] and block["engine"] == "geometric"
    assert block["suggested"]["center"], "必须给出建议盒子"
    assert board_ctx.get_pockets(), "口袋结果要写入共享黑板"

    cmp_result = json.loads(compare_pocket_with_experiment.invoke(
        {"pocket_rank": 1, "receptor_file": THROMBIN_PDBQT}))
    assert cmp_result["status"] == "ok"
    # verdict 必须是 validation 结论的**直接映射**（原先只断言"属于某个枚举"，恒真）
    validation = cmp_result["validation"]
    assert validation["status"] in ("consistent", "inconsistent", "no_reference"), validation
    assert cmp_result["verdict"] == validation["status"], (cmp_result["verdict"], validation)
    if validation["status"] != "no_reference":
        assert isinstance(cmp_result["distance_angstrom"], (int, float)), cmp_result
        assert cmp_result["reference"], "有参考位点时必须回传参考（否则无法核验一致性）"

    committed = json.loads(set_docking_site.invoke(
        {"pocket_rank": 1, "reason": "测试：采纳 top1 口袋"}))
    assert committed["status"] == "ok"
    site = committed["site"]
    assert site["chosen_by"] == "pocket_agent"
    assert site["center"] == block["pockets"][0]["center"]
    assert "pocket_1" in site["source"] and "测试" in site["source"]
    assert board_ctx.get_site()["chosen_by"] == "pocket_agent"

    engines = json.loads(list_pocket_engines.invoke({}))
    assert engines["engines"]["geometric"] is True


def test_set_docking_site_rejects_unknown_rank(board_ctx):
    from docking_agent.tools.pockets import predict_binding_pockets, set_docking_site

    predict_binding_pockets.invoke({"receptor_file": THROMBIN_PDBQT, "top_n": 3})
    out = json.loads(set_docking_site.invoke({"pocket_rank": 99}))
    assert out["status"] == "not_found"

    # 坐标字段已类型化为 number 数组（P1）：走 invoke（带 schema 校验）必须传数组；
    # 字符串形态仅作为旧调用方兼容保留在 `.func(...)` 直调路径
    explicit = json.loads(set_docking_site.invoke(
        {"center": [1, 2, 3], "size": [20, 20, 20], "reason": "手工微调"}))
    assert explicit["status"] == "ok"
    assert explicit["site"]["center"] == [1.0, 2.0, 3.0]


def test_predict_requires_receptor(board_ctx, monkeypatch):
    """未指定受体 → 直接拦下提问，**不执行任何口袋分析**（预置受体仅内部测试用）。"""
    from docking_agent.tools import pockets as T

    monkeypatch.setattr(T, "_resolve_specs",
                        lambda *a, **k: pytest.fail("未指定受体时不得进入受体解析/口袋计算"))
    out = json.loads(T.predict_binding_pockets.invoke({}))
    assert out["status"] == "needs_user_input"
    assert out["missing"] == ["receptor"]
    assert "未指定受体" in out["message"]


def test_predict_reports_no_receptor_when_specs_come_back_empty(board_ctx, monkeypatch) -> None:
    """对照：受体**已指定**但解析不出任何 spec 时，仍如实报 no_receptor（不是提问）。"""
    from docking_agent.tools import pockets as T

    monkeypatch.setattr(T, "_resolve_specs", lambda *a, **k: [])
    out = json.loads(T.predict_binding_pockets.invoke({"receptor_file": THROMBIN_PDBQT}))
    assert out["status"] == "no_receptor"


def test_predict_never_falls_back_to_default_receptor(board_ctx, monkeypatch) -> None:
    """旧行为（未指定受体 → 按系统默认受体处理）已被用户明确删除：
    现在只提问、零工具调用，绝不替用户挑靶点。"""
    from docking_agent.tools import pockets as T

    monkeypatch.setenv("POCKET_ENGINE", "geometric")
    monkeypatch.setattr(T, "_resolve_specs",
                        lambda *a, **k: pytest.fail("未指定受体时不得解析出默认受体"))
    out = json.loads(T.predict_binding_pockets.invoke({}))
    assert out["status"] == "needs_user_input"
    assert "thrombin" not in out["message"] and "1DWC" not in out["message"]
    assert not out.get("choices"), "预置受体不得作为用户可选来源下发"


# --------------------------------------------------------------------------- #
# 对接按口袋 Agent 提交的盒子执行
# --------------------------------------------------------------------------- #
def test_docking_uses_submitted_box():
    """把「口袋 Agent 提交的盒子」传给 dock_library，结果必须如实记录该盒子与来源。"""
    from docking_agent.core import dock_library

    box = {"center": [31.5, 13.74, 24.36], "size": [20.0, 20.0, 20.0],
           "source": "口袋分析 Agent 选择 pocket_1（geometric，score=4.545）",
           "chosen_by": "pocket_agent"}
    out = dock_library([{"name": "乙醇", "smiles": "CCO"}], receptor="thrombin",
                       exhaustiveness=1, n_poses=1, engine="vina", site=box)
    assert out["status"] == "ok"
    block = out["receptors"][0]
    assert block["box_center"] == [31.5, 13.74, 24.36]
    assert block["box_size"] == [20.0, 20.0, 20.0]
    assert "口袋分析 Agent" in block["box_source"]
    assert block["box_chosen_by"] == "pocket_agent"
    assert isinstance(block["results"][0]["affinity_kcal_mol"], float)


def _tool_msg(name, payload):
    from langchain_core.messages import ToolMessage

    return ToolMessage(content=json.dumps(payload, ensure_ascii=False), name=name,
                       tool_call_id="t-" + name)


def test_persist_recovers_box_provenance_from_raw_tool_output(tmp_path):
    """子 Agent 转述时删掉 box_source/pockets，落盘仍必须带上（以工具原始输出为准）。"""
    from docking_agent.agents.persistence import persist_agent_run
    from docking_agent.runs import Run

    raw_block = {
        "receptor_key": "thrombin", "receptor": "thrombin(1DWC)", "box_center": [31.5, 13.74, 24.36],
        "box_size": [22.0, 22.0, 22.0],
        "box_source": "实验位点（共晶配体质心）· geometric 预测一致（相距 2.6 Å）",
        "box_chosen_by": "experimental_site",
        "box_validation": {"status": "consistent", "distance_angstrom": 2.6},
        "pockets": [{"rank": 1, "name": "pocket_1", "score": 4.545,
                     "center": [29.1, 14.7, 24.0], "residues": ["TRP215"]}],
        "results": [{"name": "A", "smiles": "CCO", "affinity_kcal_mol": -2.8}],
    }
    # 模型转述版：只保留最基础的字段（这正是真实运行时发生的情况）
    spoken_block = {"receptor_key": "thrombin", "box_center": [31.5, 13.74, 24.36],
                    "box_size": [22.0, 22.0, 22.0], "site": {"source": "共晶配体质心"},
                    "results": [{"name": "A", "smiles": "CCO", "affinity_kcal_mol": -2.8}]}
    messages = [
        _tool_msg("run_docking", {"status": "ok", "receptors": [spoken_block]}),
        _tool_msg("predict_binding_pockets", {"status": "ok", "engine": "geometric", "receptors": [{
            "receptor_key": "thrombin", "engine": "geometric",
            "pockets": raw_block["pockets"],
            "validation": {"status": "consistent", "distance_angstrom": 2.6},
            "suggested": {"center": [31.5, 13.74, 24.36], "size": [22.0, 22.0, 22.0],
                          "source": "实验位点（共晶配体质心）· geometric 预测一致（相距 2.6 Å）"}}]}),
        _tool_msg("molecular_docking", {"status": "ok", "receptors": [raw_block]}),
        _tool_msg("set_docking_site", {"status": "ok", "site": {
            "center": [31.5, 13.74, 24.36], "size": [22.0, 22.0, 22.0],
            "source": "口袋分析 Agent 选择 pocket_1（geometric，score=4.545）",
            "chosen_by": "pocket_agent"}}),
        _tool_msg("import_molecule_library", {"status": "ok", "molecules": [
            {"name": "A", "smiles": "CCO"}]}),
    ]
    run = Run(tmp_path, "R-POCKET", "agent", {"mode": "manual"})
    result = persist_agent_run(run, messages, "结论")
    block = (result.get("docking") or {}).get("receptors")[0]
    assert block["box_source"], "落盘必须带上盒子来源"
    assert block["pockets"], "落盘必须带上口袋预测结果"
    assert block["box_validation"]["status"] == "consistent"
    assert result.get("pockets"), "result 里也要有口袋列表"
    assert (tmp_path / "R-POCKET" / "pockets.json").is_file(), "应产出 pockets.json 产物"
    saved = json.loads((tmp_path / "R-POCKET" / "pockets.json").read_text(encoding="utf-8"))
    assert saved["selected"]["chosen_by"] == "pocket_agent"
    assert saved["engine"] == "geometric"
    assert saved["pockets"][0]["name"] == "pocket_1"


def test_persist_recovers_pockets_from_docking_block(tmp_path):
    """子 Agent 嵌套调用不进父图历史时，口袋信息仍要从对接块里恢复出来。"""
    from docking_agent.agents.persistence import persist_agent_run
    from docking_agent.runs import Run

    block = {
        "receptor_key": "thrombin", "box_center": [31.5, 13.74, 24.36],
        "box_size": [22.0, 22.0, 22.0],
        "box_source": "实验位点（共晶配体质心）· geometric 预测一致（相距 2.6 Å）",
        "site": {"source": "实验位点（共晶配体质心）", "engine": "geometric"},
        "pockets": [{"rank": 1, "name": "pocket_1", "score": 4.545, "center": [29.1, 14.7, 24.0]}],
        "results": [{"name": "A", "smiles": "CCO", "affinity_kcal_mol": -2.8}],
    }
    messages = [
        _tool_msg("run_docking", {"status": "ok", "receptors": [block]}),
        _tool_msg("import_molecule_library", {"status": "ok", "molecules": [
            {"name": "A", "smiles": "CCO"}]}),
    ]
    run = Run(tmp_path, "R-RECOVER", "agent", {"mode": "manual"})
    result = persist_agent_run(run, messages, "结论")
    assert result.get("pockets"), "从对接块恢复口袋信息"
    assert (tmp_path / "R-RECOVER" / "pockets.json").is_file()
    saved = json.loads((tmp_path / "R-RECOVER" / "pockets.json").read_text(encoding="utf-8"))
    assert saved["engine"] == "geometric" and saved["pockets"][0]["name"] == "pocket_1"


# --------------------------------------------------------------------------- #
# P2Rank（成熟工具）：装了就要跑通；没装则跳过（CI 可无 P2Rank）
# --------------------------------------------------------------------------- #
P2RANK_READY = __import__("docking_agent.core.pockets", fromlist=["x"]).available_engines()["p2rank"]


@pytest.mark.skipif(not P2RANK_READY, reason="本机未部署 P2Rank")
def test_p2rank_predicts_the_cocrystal_site():
    """P2Rank 真实运行：top-2 必须命中共晶配体所在位点，并给出可解释的残基。"""
    from docking_agent.core import pockets as P

    out = P.run_p2rank(THROMBIN_PDB, top_n=6)
    assert out["status"] == "ok", out.get("message")
    assert out["engine"] == "p2rank" and len(out["pockets"]) >= 3
    ligand = P.cocrystal_ligand(THROMBIN_PDB)
    distances = [P.distance(p["center"], ligand["center"]) for p in out["pockets"]]
    assert min(distances[:2]) <= 8.0, f"top-2 未命中实验位点：{distances[:2]}"
    top = out["pockets"][0]
    assert top["score"] > 0 and top.get("probability") is not None
    assert top["residues"], "P2Rank 应给出残基"
    assert all(len(p["center"]) == 3 for p in out["pockets"])


@pytest.mark.skipif(not P2RANK_READY, reason="本机未部署 P2Rank")
def test_auto_engine_prefers_p2rank(thrombin_spec):
    """engine=auto 时应优先使用 P2Rank（成熟工具），而不是内置几何法。"""
    from docking_agent.core import pockets as P

    out = P.detect_pockets(THROMBIN_PDBQT, engine="auto", top_n=5, pdb_hint=THROMBIN_PDB,
                           use_cache=False)
    assert out["engine"] == "p2rank", f"auto 应优先 P2Rank，实际 {out['engine']}"
    assert out["pockets"], "P2Rank 应给出候选口袋"
    spec = dict(thrombin_spec)
    spec["center"] = [0.0, 0.0, 0.0]
    spec["site"] = {"source": "蛋白质质心"}
    picked = P.select_site(spec, engine="auto", use_cache=False)
    assert picked["engine"] == "p2rank"


def test_site_trust_classification():
    """盒子来源的可信度判定：兜底来源优先、未识别一律保守（防「蛋白质心当实验位点」）。"""
    from docking_agent.core import pockets as P

    def trust(source):
        return P.known_site_from_spec(
            {"center": [1, 2, 3], "size": [22, 22, 22], "site": {"source": source}})["trust"]

    assert trust("") == "fallback"
    assert trust("蛋白质质心") == "fallback"
    # 注意：这句话里同时含「共晶」与「蛋白质质心」，必须按兜底来源判定
    assert trust("蛋白质质心（未找到共晶配体，建议由口袋预测工具确定位点）") == "fallback"
    assert trust("受体原子质心（未找到位点信息，建议显式指定位点）") == "fallback"
    assert trust("某个没见过的来源") == "fallback"
    assert trust("共晶配体质心") == "experimental"
    assert trust("注册表已知位点（1DWC）") == "experimental"
    assert trust("用户指定") == "experimental"


def test_apo_receptor_site_is_low_trust_and_tool_takes_over(tmp_path):
    """无共晶配体的受体：位点来源必须标成低可信，并由工具预测接管盒子。"""
    from docking_agent.core import prepare_user_receptor, pockets as P

    # 造一个「删掉若干残基」的 apo 受体：内容与注册受体不同 → 没有已知位点
    source = PROJECT_ROOT / "assets/receptors/structures/thrombin.pdb"
    apo = tmp_path / "apo_thrombin.pdb"
    with source.open() as src, apo.open("w") as dst:
        for line in src:
            if line.startswith("HETATM"):
                continue
            if line.startswith("ATOM") and line[21:22].strip() == "L" and line[22:26].strip().isdigit() \
                    and int(line[22:26]) <= 8:
                continue
            dst.write(line)

    spec = prepare_user_receptor(str(apo))
    reference = P.known_site_from_spec(spec)
    assert reference and reference["trust"] == "fallback", reference.get("source") if reference else None
    picked = P.select_site(spec, engine="geometric", use_cache=False)
    assert picked["chosen_by"] == "pocket_prediction", "无实验位点时必须用工具预测定盒"
    # 工具预测应当仍落在真实活性位点附近（删掉几个残基不改变折叠）
    assert P.distance(picked["center"], COCRYSTAL_CENTER) <= 8.0, picked["center"]


def test_persist_recovers_pockets_from_blackboard(tmp_path):
    """子 Agent 嵌套调用不进父图历史时，口袋结果必须能从共享黑板恢复。"""
    from docking_agent.runtime.blackboard import Blackboard, current_blackboard
    from docking_agent.agents.persistence import persist_agent_run
    from docking_agent.runs import Run

    board = Blackboard("R-BOARD")
    board.set_pockets([{"rank": 1, "name": "pocket1", "score": 10.58,
                        "center": [32.88, 11.85, 19.37], "residues": ["H_195", "H_215"]}],
                      engine="p2rank", receptor_key="apo")
    token = current_blackboard.set(board)
    try:
        messages = [
            _tool_msg("run_docking", {"status": "ok", "receptors": [{
                "receptor_key": "apo", "box_center": [32.88, 11.85, 19.37],
                "box_size": [20.7, 23.0, 20.8], "box_source": "口袋分析 Agent 选择 pocket1（p2rank）",
                "results": [{"name": "A", "smiles": "CCO", "affinity_kcal_mol": -4.1}]}]}),
            _tool_msg("import_molecule_library", {"status": "ok", "molecules": [
                {"name": "A", "smiles": "CCO"}]}),
        ]
        run = Run(tmp_path, "R-BOARD", "agent", {"mode": "manual"})
        result = persist_agent_run(run, messages, "结论")
    finally:
        current_blackboard.reset(token)

    assert result.get("pockets"), "口袋结果应从共享黑板恢复"
    assert result["pockets"][0]["name"] == "pocket1"
    saved = json.loads((tmp_path / "R-BOARD" / "pockets.json").read_text(encoding="utf-8"))
    assert saved["engine"] == "p2rank"
    assert (result["docking"]["receptors"][0].get("pockets") or []), "受体块也要带上口袋列表"


def test_run_detail_exposes_pocket_analysis(tmp_path):
    """接口层的运行详情必须能看到口袋预测与对接盒溯源。"""
    from docking_agent.runs import RunStore

    store = RunStore(tmp_path)
    run = store.new("agent", {"mode": "chat"})
    run.write_json("result", {
        "status": "ok", "ranking": [], "positive_control": {}, "receptors": [],
        "pockets": [{"rank": 1, "name": "pocket1", "score": 10.58}],
        "pocket_analysis": {"engine": "p2rank", "pockets": [{"rank": 1, "name": "pocket1"}]},
    }, label="完整结果（JSON）")
    run.finish("ok")
    detail = store.detail(run.id)
    assert detail["result"]["pocket_analysis"]["engine"] == "p2rank"
    assert detail["result"]["pockets"][0]["name"] == "pocket1"
