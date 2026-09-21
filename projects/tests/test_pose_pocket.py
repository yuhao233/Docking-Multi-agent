"""姿态–口袋结合分析（真实几何）+ 2D/3D 结合分析图 + 报告呈现。

需求来源（用户）：「让 agent 把口袋也分析说明一下，在写报告与推荐小分子时加上当前对接姿态
与口袋的结合情况分析，2D 与 3D 的结合分析图」。

本文件看护四件事：
  1. 相互作用判定（氢键/盐桥/疏水/π–π/金属配位）在**构造好的真实坐标**上必须判对；
  2. 位姿文件的 SMILES / 原子序号映射（含 meeko 折行写的 `REMARK SMILES IDX`）；
  3. 2D 相互作用图与 3D 姿态图能真的画出来（非空 PNG），且 2D 的锚点按残基逐条给出；
  4. 报告第 5.1（口袋分析）/5.2（姿态–口袋 + 2D/3D 图）与第 3.1 的逐分子姿态行都渲染出来，
     没有位姿时如实说明而不是编造。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Tuple

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _atom(serial: int, name: str, resname: str, chain: str, resnum: int,
          x: float, y: float, z: float, ad4: str, charge: float = 0.0) -> str:
    """按 AutoDock PDBQT 列约定拼一行。

    列位置必须精确：坐标 31-54、电荷 67-76、AD4 类型 78-79（解析器按列取值，
    早先测试里类型被写到 80 列之后，导致 `ad4` 读成电荷数字）。
    """
    # 链为空时必须**占一列空格**（`{chain:1}` 对空串不产生字符，会让整行左移一列）
    return (f"ATOM  {serial:5d} {name:<4} {resname:>3} {(chain or ' '):1}{resnum:4d}{'':4}"
            f"{x:8.3f}{y:8.3f}{z:8.3f}{'1.00':>6}{'0.00':>6}{charge:>10.3f} {ad4:<2}")


def _receptor_pdbqt(path: Path) -> Path:
    """构造受体（坐标精确可控，用于验证每一类相互作用的判定）。

    几何布置（单位 Å，全部写死在坐标里，断言可核对）：
      ASP102 OD1 (0,0,0)  q=-0.6  → 与配体 O1(2.9,0,0) 氢键
      LYS107 NZ  (0,6,0)  q=+0.6  → 与配体 O2(0,3.6,0) 盐桥（异号）
      TYR200 OH  (0,0,12) q=-0.4  → 与配体 O4(0,0,9.0) 氢键
      PHE134 CZ  (12,0,0) 芳香碳   → 与配体 C1(8.2,0,0) 疏水接触
      ZN900      (6,6,6)  金属     → 与配体 O3(6,6,3.6) 金属配位
    配体 N1(0,2.5,0) q=+0.5 与 ASP102 OD1 相距 2.5 Å → 氢键 + 盐桥。
    """
    rows = [
        _atom(1, "OD1", "ASP", "A", 102, 0.0, 0.0, 0.0, "OA", -0.6),
        _atom(2, "NZ", "LYS", "A", 107, 0.0, 6.0, 0.0, "N", 0.6),
        _atom(3, "OH", "TYR", "A", 200, 0.0, 0.0, 12.0, "OA", -0.4),
        _atom(4, "CZ", "PHE", "A", 134, 12.0, 0.0, 0.0, "A", 0.0),
        _atom(5, "ZN", "ZN", "A", 900, 6.0, 6.0, 6.0, "ZN", 1.0),
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _pose_pdbqt(path: Path, smiles: str = "OC(=O)c1ccccc1") -> Path:
    """构造位姿（9 个重原子，与 `OC(=O)c1ccccc1` 的原子数一致）。

    映射故意折成两行 `REMARK SMILES IDX`（meeko 对长映射就是这么折的）。
    """
    rows = [
        "MODEL        1",
        "REMARK SMILES " + smiles,
        "REMARK SMILES IDX 1 1 2 2 3 3 4 4 5 5",
        "REMARK SMILES IDX 6 6 7 7 8 8 9 9",
        "ROOT",
        _atom(1, "O1", "UNL", "", 1, 2.9, 0.0, 0.0, "OA", -0.6),      # 与 ASP102 氢键
        _atom(2, "N1", "UNL", "", 1, 0.0, 2.5, 0.0, "N", 0.5),        # 与 ASP102 盐桥
        _atom(3, "O2", "UNL", "", 1, 0.0, 3.6, 0.0, "OA", -0.5),      # 与 LYS107 盐桥
        _atom(4, "O4", "UNL", "", 1, 0.0, 0.0, 9.0, "OA", -0.4),      # 与 TYR200 氢键
        _atom(5, "O3", "UNL", "", 1, 6.0, 6.0, 3.6, "OA", -0.5),      # 与 ZN900 金属配位
        _atom(6, "C1", "UNL", "", 1, 8.2, 0.0, 0.0, "C", 0.0),        # 与 PHE134 疏水
        _atom(7, "C2", "UNL", "", 1, 10.0, 1.0, 0.0, "A", 0.0),       # 芳香碳
        _atom(8, "C3", "UNL", "", 1, 11.0, 2.0, 0.0, "A", 0.0),       # 芳香碳
        _atom(9, "C4", "UNL", "", 1, 12.0, 3.0, 0.0, "A", 0.0),       # 芳香碳
        "ENDROOT",
        "TORSDOF 2",
        "ENDMDL",
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 1. 相互作用判定
# --------------------------------------------------------------------------- #
def test_interaction_kinds_are_detected_from_real_coordinates(tmp_path: Path) -> None:
    from docking_agent.core.interactions import analyze_pose_pocket

    rec = _receptor_pdbqt(tmp_path / "rec.pdbqt")
    pose = _pose_pdbqt(tmp_path / "pose.pdbqt")
    out = analyze_pose_pocket(str(rec), str(pose))
    assert out["status"] == "ok", out
    counts = out["summary"]["type_counts"]
    assert counts.get("氢键", 0) >= 3, counts
    assert counts.get("盐桥", 0) >= 2, counts
    assert counts.get("疏水接触", 0) >= 1, counts
    assert counts.get("金属配位", 0) >= 1, counts
    labels = {r["residue"] for r in out["residues"]}
    for expected in ("ASP102", "LYS107", "TYR200", "PHE134", "ZN900"):
        assert any(label.startswith(expected) for label in labels), (expected, labels)
    closest = out["summary"]["closest"]
    assert closest["distance"] < 3.0, closest
    # 阈值与口径必须随结果一起交出去（报告要写清"近似判定"）
    assert out["thresholds"]["hbond"] == 3.5


def test_pi_stacking_needs_several_aromatic_pairs(tmp_path: Path) -> None:
    """单个芳香碳偶然靠近不算 π–π 堆叠：必须达到最小对数（否则会误报）。"""
    from docking_agent.core.interactions import PI_MIN_PAIRS, analyze_pose_pocket

    rec = tmp_path / "rec.pdbqt"
    rec.write_text(_atom(1, "CZ", "PHE", "A", 134, 0.0, 0.0, 0.0, "A", 0.0) + "\n", encoding="utf-8")

    def _run(count: int) -> set:
        pose = tmp_path / f"pose_{count}.pdbqt"
        rows = ["MODEL        1", "REMARK SMILES c1ccccc1", "ROOT"]
        # 芳香碳全部落在 4.0 Å 内 → 每一对都满足距离判据
        rows += [_atom(i, f"C{i}", "UNL", "", 1, 3.5 + (i - 1) * 0.1, 0.0, 0.0, "A", 0.0)
                 for i in range(1, count + 1)]
        rows += ["ENDROOT", "TORSDOF 0", "ENDMDL"]
        pose.write_text("\n".join(rows) + "\n", encoding="utf-8")
        out = analyze_pose_pocket(str(rec), str(pose))
        return {k for r in out["residues"] for k in r["types"]}

    assert "π–π 堆叠" not in _run(1), "单对芳香碳靠近不能判成 π–π 堆叠"
    assert "π–π 堆叠" not in _run(PI_MIN_PAIRS - 1)
    assert "π–π 堆叠" in _run(PI_MIN_PAIRS + 2)


def test_missing_pose_is_reported_not_invented(tmp_path: Path) -> None:
    from docking_agent.core.interactions import analyze_pose_pocket

    rec = _receptor_pdbqt(tmp_path / "rec.pdbqt")
    out = analyze_pose_pocket(str(rec), str(tmp_path / "nope.pdbqt"))
    assert out["status"] == "no_pose" and "保存位姿" in out["message"], out


def test_pose_smiles_and_multiline_index_map(tmp_path: Path) -> None:
    from docking_agent.core.interactions import pose_smiles, pose_smiles_index_map

    pose = _pose_pdbqt(tmp_path / "pose.pdbqt", smiles="OC(=O)c1ccccc1")
    assert pose_smiles(str(pose)) == "OC(=O)c1ccccc1"
    mapping = pose_smiles_index_map(str(pose))
    # meeko 会把长映射折成多行 REMARK —— 只看第一行会漏原子（真实踩到过）
    assert mapping[8] == 8 and mapping[9] == 9, mapping


def test_pocket_residues_inventory(tmp_path: Path) -> None:
    from docking_agent.core.interactions import pocket_residues

    rec = _receptor_pdbqt(tmp_path / "rec.pdbqt")
    rows = pocket_residues(str(rec), [3.0, 2.0, 3.0], [10.0, 10.0, 10.0], limit=10)
    assert {r["resname"] for r in rows} >= {"ASP", "LYS", "ZN"}, rows
    assert all(r["atoms_in_box"] >= 1 for r in rows)


# --------------------------------------------------------------------------- #
# 2. 2D / 3D 图
# --------------------------------------------------------------------------- #
def test_binding_figures_render(tmp_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    from docking_agent.core.interactions import analyze_pose_pocket
    from docking_agent.reporting.charts import binding_interaction_map_2d, binding_pose_3d

    rec = _receptor_pdbqt(tmp_path / "rec.pdbqt")
    pose = _pose_pdbqt(tmp_path / "pose.pdbqt")
    out = analyze_pose_pocket(str(rec), str(pose))
    png2d = binding_interaction_map_2d(out, pose_path=str(pose), title="测试分子")
    png3d = binding_pose_3d(str(rec), str(pose), out, title="测试分子",
                            box_center=[3.0, 2.0, 3.0], box_size=[10.0, 10.0, 10.0])
    assert png2d and png2d[:8] == PNG_MAGIC and len(png2d) > 5000, len(png2d or b"")
    assert png3d and png3d[:8] == PNG_MAGIC and len(png3d) > 5000, len(png3d or b"")


def test_interaction_map_anchors_one_per_residue(tmp_path: Path) -> None:
    """2D 图的锚点按**残基**逐条给出：多个残基接触同一原子时不能被合并掉。"""
    from docking_agent.core.interactions import analyze_pose_pocket
    from docking_agent.reporting.charts import _anchor_atoms

    rec = _receptor_pdbqt(tmp_path / "rec.pdbqt")
    pose = _pose_pdbqt(tmp_path / "pose.pdbqt")
    out = analyze_pose_pocket(str(rec), str(pose))
    anchors = _anchor_atoms(out, str(pose), "OC(=O)c1ccccc1")
    assert len(anchors) == len(out["residues"]), (len(anchors), len(out["residues"]))
    assert all(0 <= a["atom_idx"] < 9 for a in anchors), anchors


# --------------------------------------------------------------------------- #
# 3. 报告呈现
# --------------------------------------------------------------------------- #
def _fake_result(rec_pdbqt: str, pose_pdbqt: str, analysis: dict) -> dict:
    block = {"receptor": "测试受体", "receptor_key": "T", "pdbqt": rec_pdbqt,
             "box_center": [3.0, 2.0, 3.0], "box_size": [10.0, 10.0, 10.0],
             "box_source": "口袋分析 Agent 选择 pocket1（p2rank，score=42.0）",
             "box_chosen_by": "pocket_agent", "box_atom_stats": {"atoms": 5, "nearest_atom": "ASP102",
                                                                 "nearest_angstrom": 2.9},
             "box_validation": {"verdict": "一致", "distance_angstrom": 1.2},
             "box_warnings": [], "results": []}
    return {
        "ranking": [{"name": "测试分子", "smiles": "OC(=O)c1ccccc1", "affinity_kcal_mol": -7.5,
                     "pose_file": pose_pdbqt, "engine": "vina", "exhaustiveness": 8, "n_poses": 1,
                     "seed": 42, "seed_policy": "session", "box_group": "main",
                     "box_size": [10.0, 10.0, 10.0], "molecular_weight": 138.1, "logP": 1.4,
                     "tpsa": 37.3, "rotatable_bonds": 1, "lipinski_violations": 0,
                     "heavy_atoms": 10, "drug_likeness_pass": True}],
        "receptors": [block], "positive_control": {}, "notes": [], "param_plan": {}, "task_spec": {},
        "pocket_analysis": {"engine": "p2rank"},
        "pockets": [{"rank": 1, "name": "pocket1", "score": 42.0, "engine": "p2rank",
                     "center": [3.0, 2.0, 3.0], "residues": ["ASP102", "TYR200"]}],
        "pose_analysis": [{"rank": 1, "name": "测试分子", "affinity_kcal_mol": -7.5,
                           "pose_file": Path(pose_pdbqt).name,
                           "summary": analysis["summary"], "residues": analysis["residues"],
                           "pocket_residues": analysis["pocket_residues"], "warnings": [],
                           "figure_2d": "interaction_2d_01", "figure_3d": "pose_3d_01"}],
    }


def test_report_contains_pocket_explanation_and_pose_binding_sections(tmp_path: Path) -> None:
    from docking_agent.core.interactions import analyze_pose_pocket
    from docking_agent.reporting.report import build_markdown_report

    rec = _receptor_pdbqt(tmp_path / "rec.pdbqt")
    pose = _pose_pdbqt(tmp_path / "pose.pdbqt")
    analysis = analyze_pose_pocket(str(rec), str(pose))
    result = _fake_result(str(rec), str(pose), analysis)
    artifacts = [{"name": "interaction_2d_01", "content_type": "image/png",
                  "path": "charts/interaction_2d_01.png", "size": 9000, "label": "2D"},
                 {"name": "pose_3d_01", "content_type": "image/png",
                  "path": "charts/pose_3d_01.png", "size": 9000, "label": "3D"}]
    md = build_markdown_report(result, kind="pipeline", run_id="T", artifacts=artifacts)

    assert "### 5.1 结合口袋分析" in md
    assert "### 5.2 推荐分子的姿态–口袋相互作用（2D / 3D）" in md
    assert "口袋分析 Agent 选择 pocket1" in md, md[:500]
    assert "盒内残基" in md and "组成特征" in md and "芳香残基" in md
    # 逐分子结合分析：残基名 + 相互作用类型 + 距离
    assert "ASP102" in md and "氢键" in md and "盐桥" in md
    # 2D / 3D 图都内嵌，且用相对路径（报告里不得出现裸 URL）
    assert "charts/interaction_2d_01.png" in md and "charts/pose_3d_01.png" in md
    assert "http://" not in md and "https://" not in md
    # 推荐排行处也要带姿态–口袋一行
    assert "姿态–口袋" in md and "残基接触" in md


def test_report_says_so_when_no_pose(tmp_path: Path) -> None:
    """关闭「保存位姿」时必须如实说明，不能假装做过结合分析。"""
    from docking_agent.reporting.report import build_markdown_report

    result = {
        "ranking": [], "receptors": [{"receptor": "R", "pdbqt": "", "box_center": [0, 0, 0],
                                      "box_size": [10, 10, 10], "box_source": "显式指定",
                                      "results": []}],
        "positive_control": {}, "notes": [], "param_plan": {}, "task_spec": {},
        "pose_analysis_note": "有 2 个推荐分子没有可用位姿文件（本次运行关闭了「保存位姿」？）",
    }
    md = build_markdown_report(result, kind="pipeline", run_id="T")
    assert "### 5.2 推荐分子的姿态–口袋相互作用（2D / 3D）" in md
    assert "关闭了「保存位姿」" in md


# --------------------------------------------------------------------------- #
# 4. Agent 工具
# --------------------------------------------------------------------------- #
def test_analyze_pose_pocket_tool_returns_real_contacts(tmp_path: Path) -> None:
    from docking_agent.agents.blackboard import Blackboard, current_blackboard
    from docking_agent.runs import current_run
    from docking_agent.tools.pose import analyze_pose_pocket

    rec = _receptor_pdbqt(tmp_path / "rec.pdbqt")
    pose = _pose_pdbqt(tmp_path / "pose.pdbqt")
    rows = [{"name": "测试分子", "smiles": "OC(=O)c1ccccc1", "affinity_kcal_mol": -7.5,
             "pose_file": str(pose), "box_group": "main"}]

    class _Run:
        id = "POSE-TOOL"

        def __init__(self) -> None:
            self.data = {"request": {}}
            self.dir = tmp_path

        def save(self) -> None:
            return None

        def log(self, *_a: Any, **_k: Any) -> None:
            return None

        def write_json(self, name: str, obj: Any, **kw: Any) -> Path:
            path = self.dir / (kw.get("rel_path") or f"{name}.json")
            path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            return path

    run = _Run()
    board = Blackboard("POSE-BOARD")
    token, btoken = current_run.set(run), current_blackboard.set(board)
    try:
        from docking_agent.agents import tool_io

        tool_io.record("docking", {"status": "ok", "receptors": [
            {"receptor": "测试受体", "receptor_key": "T", "pdbqt": str(rec),
             "box_center": [3.0, 2.0, 3.0], "box_size": [10.0, 10.0, 10.0],
             "box_source": "口袋分析 Agent 选择 pocket1", "results": rows}]})
        payload = json.loads(analyze_pose_pocket.func(top_n=3))
    finally:
        current_run.reset(token)
        current_blackboard.reset(btoken)

    assert payload["status"] == "ok", payload
    assert payload["receptors"][0]["residues_in_box"], payload["receptors"][0]
    molecule = payload["molecules"][0]
    assert molecule["name"] == "测试分子"
    assert molecule["summary"]["type_counts"].get("氢键"), molecule
    assert any(r["residue"].startswith("ASP102") for r in molecule["residues"]), molecule
    assert molecule["figures"] == {"2d": "charts/interaction_2d_01.png",
                                   "3d": "charts/pose_3d_01.png"}
    assert "原样引用" in payload["how_to_write"]


def test_analyze_pose_pocket_tool_without_pose_is_honest(tmp_path: Path) -> None:
    from docking_agent.agents.blackboard import Blackboard, current_blackboard
    from docking_agent.runs import current_run
    from docking_agent.tools.pose import analyze_pose_pocket

    rec = _receptor_pdbqt(tmp_path / "rec.pdbqt")

    class _Run:
        id = "POSE-NONE"

        def __init__(self) -> None:
            self.data = {"request": {}}
            self.dir = tmp_path

        def save(self) -> None:
            return None

        def log(self, *_a: Any, **_k: Any) -> None:
            return None

        def write_json(self, name: str, obj: Any, **kw: Any) -> Path:
            path = self.dir / (kw.get("rel_path") or f"{name}.json")
            path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            return path

    run = _Run()
    board = Blackboard("POSE-BOARD-2")
    token, btoken = current_run.set(run), current_blackboard.set(board)
    try:
        from docking_agent.agents import tool_io

        tool_io.record("docking", {"status": "ok", "receptors": [
            {"receptor": "测试受体", "pdbqt": str(rec), "box_center": [3, 2, 3],
             "box_size": [10, 10, 10], "results": [
                 {"name": "无位姿分子", "smiles": "CCO", "affinity_kcal_mol": -5.0,
                  "pose_file": ""}]}]})
        payload = json.loads(analyze_pose_pocket.func(top_n=1))
    finally:
        current_run.reset(token)
        current_blackboard.reset(btoken)

    assert payload["status"] == "no_pose"
    assert payload["missing_pose"] == ["无位姿分子"]
    assert "保存位姿" in payload["message"]


def test_pdf_embeds_dynamic_figures(tmp_path: Path) -> None:
    """动态命名的结合分析图（`interaction_2d_01.png` / `pose_3d_01.png`）必须真的进 PDF。

    真实缺陷：`pdf._image_name` 对不在 `CHART_FILES` 里的图返回了带 `.png` 的文件名，
    与产物名（无扩展名）对不上 → PDF 静默跳过这些图（报告正文有图、PDF 里没有）。
    """
    from docking_agent.reporting.pdf import _image_name, build_report_pdf

    assert _image_name("charts/interaction_2d_01.png") == "interaction_2d_01"
    assert _image_name("charts/pose_3d_01.png") == "pose_3d_01"
    assert _image_name("charts/docking_chart.png") == "docking_chart"

    png = tmp_path / "interaction_2d_01.png"
    png.write_bytes(_tiny_png())
    markdown = ("## 1. 任务与参数\n\n"
                "![图 1 2D 相互作用图](charts/interaction_2d_01.png)\n\n"
                "**图 1 2D 相互作用图**\n\n"
                "![图 2 3D 姿态](charts/pose_3d_01.png)\n\n"
                "**图 2 3D 姿态**\n")
    pdf = build_report_pdf({"ranking": []}, kind="pipeline", run_id="T", markdown=markdown,
                           chart_paths={"interaction_2d_01": png, "pose_3d_01": png})
    assert len(pdf) > 2000
    assert pdf.count(b"/Subtype /Image") >= 2, "两张动态图都应内嵌"
    assert b"\xe6\x9c\xaa\xe6\x89\xbe\xe5\x88\xb0\xe5\xaf\xb9\xe5\xba\x94\xe4\xba\xa7\xe7\x89\xa9" not in pdf


def _tiny_png() -> bytes:
    """一个能通过 PIL/matplotlib 读取的最小 PNG（红点图）。"""
    import io

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(1.2, 0.8))
    ax.plot([0, 1], [0, 1], color="#1f77b4")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80)
    plt.close(fig)
    return buf.getvalue()
