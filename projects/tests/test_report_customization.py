"""协调 Agent 的「按用户要求定制报告」与「ID 全链路可追溯」回归。

用户实测反馈两点：
  1. 「报告是按格式的，但也不能死板 —— 用户要求带上小分子 ID 或其它信息时，
     要能正确从文件里取到，并按用户要求输出；主管 Agent 到底在干什么，似乎什么都没干」；
  2. 事实是：ID 只在 `molecules.json` 里活过一次，**对接/性质/排序/报告/CSV 全丢了**。

约定：
  - `core.docking.carry_identity()` 把输入分子的 `id/source_file/source_index` 带进结果行，
    对接、性质评估、排序、CSV 每一环都保留；
  - 协调 Agent 用 `customize_report` 声明「这次报告要什么」（标题/附加列/要点/要求与响应），
    白名单 + 覆盖率校验（取不到就如实回绝，不许编造）；
  - 报告固定骨架不变，但标题、3.1 排行表附加列、第 0 节「本次要求与响应」按定制输出。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from docking_agent.reporting.report import build_markdown_report
from docking_agent.runs import Run


def _ranking(n: int = 3, *, with_id: bool = True) -> List[Dict[str, Any]]:
    rows = []
    for i in range(n):
        row: Dict[str, Any] = {
            "rank": i + 1, "name": f"分子{i + 1}",
            "smiles": "CCO" if i == 0 else ("Oc1ccccc1" if i == 1 else "NC(=N)c1ccccc1"),
            "affinity_kcal_mol": -3.0 - i, "engine": "vina", "exhaustiveness": 1,
            "box_group": "main", "molecular_weight": 46.1 + i, "logP": -0.1 + i,
            "tpsa": 20.2 + i, "lipinski_violations": 0, "heavy_atoms": 3 + i,
            "drug_likeness_pass": True, "formula": f"C{i + 1}H2O",
        }
        if with_id:
            row.update({"id": f"PGR{i + 1:03d}", "source_index": i + 1,
                        "source_file": "/data/library.sdf"})
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# 1) ID 全链路：输入 → 对接结果行 → CSV
# --------------------------------------------------------------------------- #
def test_carry_identity_keeps_id_on_result_rows() -> None:
    from docking_agent.core.docking import carry_identity

    row: Dict[str, Any] = {"name": "A", "smiles": "CCO"}
    carry_identity(row, {"id": "PGR001", "source_index": 7, "source_file": "/lib.sdf",
                         "name": "A", "smiles": "CCO"})
    assert row["id"] == "PGR001" and row["source_index"] == 7 and row["source_file"] == "/lib.sdf"
    # 不覆盖已有值；源里没有的键不凭空造
    carry_identity(row, {"id": "OTHER", "source_file": ""})
    assert row["id"] == "PGR001"
    assert "unrelated" not in row


def test_dock_batch_attaches_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实走一遍 dock_batch（worker 打桩）：结果行必须带上输入分子的 ID/序号/来源。"""
    from docking_agent.core import docking as D

    def fake_worker(item: Any) -> Dict[str, Any]:  # 打桩 worker，避免真跑 Vina
        name, smiles = item
        return {"name": name, "smiles": smiles, "affinity_kcal_mol": -2.5, "engine": "vina"}

    monkeypatch.setattr(D, "_worker_dock", fake_worker)
    spec = D.resolve_receptor_specs("thrombin")[0][0]
    molecules = [{"id": "PGR001", "name": "乙醇", "smiles": "CCO", "source_index": 1,
                  "source_file": "/lib.sdf"}]
    rows = D.dock_batch(spec, molecules, engine="vina", exhaustiveness=1)
    assert rows and rows[0]["id"] == "PGR001"
    assert rows[0]["source_index"] == 1 and rows[0]["source_file"] == "/lib.sdf"


def test_ranking_csv_has_id_columns() -> None:
    from docking_agent.reporting.tables import CSV_FIELDS, build_ranking_csv

    assert "id" in CSV_FIELDS and "source_index" in CSV_FIELDS and "source_file" in CSV_FIELDS
    csv_text = build_ranking_csv(_ranking(2), {})
    header = csv_text.splitlines()[0].split(",")
    assert header[:3] == ["rank", "id", "name"], header[:4]
    assert "PGR001" in csv_text


def test_report_ranking_table_honours_requested_columns() -> None:
    result = {"ranking": _ranking(2), "receptors": [], "positive_control": {}, "notes": [],
              "task_spec": {}, "report_customization": {"extra_columns": ["id", "formula"]}}
    md = build_markdown_report(result, kind="agent", run_id="T", artifacts=[])
    assert "| 分子 ID |" in md and "| 分子式 |" in md
    assert "PGR001" in md and "C1H2O" in md


# --------------------------------------------------------------------------- #
# 2) customize_report 工具：白名单 / 覆盖率 / 如实回绝
# --------------------------------------------------------------------------- #
def _run_with_ranking(tmp_path: Path, rows: List[Dict[str, Any]]) -> Run:
    run = Run(tmp_path, "R-CUSTOM", "agent", {"mode": "chat"})
    run.set(recommendations=rows)
    return run


def _call_tool(run: Run, spec: Dict[str, Any]) -> Dict[str, Any]:
    from docking_agent.runs import current_run

    token = current_run.set(run)
    try:
        from docking_agent.tools.recommend import customize_report

        out = customize_report.func(json.dumps(spec, ensure_ascii=False))
    finally:
        current_run.reset(token)
    return json.loads(out)


def test_customize_report_accepts_whitelist_and_reports_coverage(tmp_path: Path) -> None:
    run = _run_with_ranking(tmp_path, _ranking(3))
    out = _call_tool(run, {"title": "EGFR 筛选（含 ID）", "extra_columns": ["id", "formula"],
                           "highlights": ["优先推进 分子1"],
                           "requirements": [{"ask": "带上 ID", "response": "已带（3/3）"}]})
    assert out["status"] == "ok"
    assert out["accepted"]["extra_columns"] == ["id", "formula"]
    assert out["coverage"]["id"] == "3/3"
    assert run.data["report_customization"]["title"] == "EGFR 筛选（含 ID）"


def test_customize_report_rejects_unknown_and_empty_fields(tmp_path: Path) -> None:
    """不在白名单、或本次数据里没有值的字段必须被拒（并给出原因），不许假装生效。"""
    rows = _ranking(2, with_id=False)
    run = _run_with_ranking(tmp_path, rows)
    out = _call_tool(run, {"extra_columns": ["id", "别乱写", "formula"]})
    rejected = {r["field"]: r["reason"] for r in out["rejected"]}
    assert "id" in rejected and "覆盖率" in rejected["id"]
    assert "别乱写" in rejected and "白名单" in rejected["别乱写"]
    assert out["accepted"]["extra_columns"] == ["formula"]


def test_customize_report_clamps_long_text(tmp_path: Path) -> None:
    run = _run_with_ranking(tmp_path, _ranking(1))
    out = _call_tool(run, {"highlights": ["要点" * 200] * 9, "notes": "备注" * 900,
                           "requirements": [{"ask": "a", "response": "b"}] * 9,
                           "title": "标" * 200})
    accepted = out["accepted"]
    assert len(accepted.get("highlights") or []) <= 5
    assert len(accepted.get("requirements") or []) <= 6
    assert len(accepted.get("notes") or "") <= 600
    assert "title" not in accepted and any(r["field"] == "title" for r in out["rejected"])


def test_report_section0_shows_requirements_and_dispatch(tmp_path: Path) -> None:
    """第 0 节要把「用户要求 → 实际处理」和协调 Agent 的调度记录摆出来（它到底干了什么）。"""
    result = {
        "ranking": _ranking(2), "receptors": [], "positive_control": {}, "notes": [],
        "task_spec": {},
        "report_customization": {
            "title": "带分子 ID 的筛选报告",
            "extra_columns": ["id"],
            "highlights": ["优先推进 分子1：亲和力最优"],
            "requirements": [{"ask": "报告里带上小分子的 ID",
                              "response": "已按输入文件 ID 列展示（覆盖 2/2）"}],
            "notes": "本次按用户要求附加了 ID 列。",
        },
    }
    md = build_markdown_report(result, kind="agent", run_id="T", artifacts=[],
                               agent_models={"coordinator": {"actual_model": "m-a", "calls": 9},
                                             "docking": {"actual_model": "m-b", "calls": 3}})
    assert md.splitlines()[0] == "# 带分子 ID 的筛选报告"
    assert "## 0. 本次要求与响应（协调 Agent）" in md
    assert "报告里带上小分子的 ID" in md and "覆盖 2/2" in md
    assert "优先推进 分子1" in md
    assert "协调 Agent 的调度记录" in md and "对接执行" in md and "m-b" in md


def test_report_without_customization_is_unchanged() -> None:
    """没有定制时不能凭空冒出第 0 节（骨架保持原样）。"""
    result = {"ranking": _ranking(2), "receptors": [], "positive_control": {}, "notes": [],
              "task_spec": {}}
    md = build_markdown_report(result, kind="agent", run_id="T", artifacts=[])
    assert md.splitlines()[0] == "# 分子对接筛选报告"
    assert "## 0. 本次要求与响应" not in md


# --------------------------------------------------------------------------- #
# 3) 从文件里正确取 ID：表格的独立 ID 列 / SDF 标题行
# --------------------------------------------------------------------------- #
def test_tabular_id_column_becomes_molecule_id() -> None:
    """`id,name,smiles` 这类文件：ID 列必须成为分子 ID（与名称分开），而不是被当成名称。"""
    from docking_agent.core.normalize import normalize_ligand_text

    mols, meta = normalize_ligand_text("id,name,smiles\nPGR001,乙醇,CCO\nPGR002,苯酚,Oc1ccccc1\n")
    assert [(m["id"], m["name"], m["smiles"]) for m in mols] == [
        ("PGR001", "乙醇", "CCO"), ("PGR002", "苯酚", "Oc1ccccc1")]
    assert meta.get("header", {}).get("name") == "name", meta.get("header")

    # 中文表头「编号」同样识别；没有名称列时 name 回落到占位名
    mols2, _ = normalize_ligand_text("编号,SMILES\nA-12,CCO\n")
    assert mols2[0]["id"] == "A-12" and mols2[0]["name"] != "A-12"

    # 只有 name 列时 id 与 name 同值（不制造无信息量的“ID 列”）
    mols3, _ = normalize_ligand_text("name,smiles\n乙醇,CCO\n")
    assert mols3[0]["id"] == mols3[0]["name"] == "乙醇"


def test_report_adds_id_column_only_when_id_is_informative() -> None:
    """ID 与名称不同（文件里真有编号）→ 报告自动带 ID 列；相同则不重复占列。"""
    informative = {"ranking": _ranking(2), "receptors": [], "positive_control": {},
                   "notes": [], "task_spec": {}}
    md = build_markdown_report(informative, kind="agent", run_id="T", artifacts=[])
    assert "| 分子 ID |" in md and "PGR001" in md

    same = _ranking(2)
    for r in same:
        r["id"] = r["name"]
    md2 = build_markdown_report({"ranking": same, "receptors": [], "positive_control": {},
                                 "notes": [], "task_spec": {}},
                                kind="agent", run_id="T", artifacts=[])
    assert "| 分子 ID |" not in md2
