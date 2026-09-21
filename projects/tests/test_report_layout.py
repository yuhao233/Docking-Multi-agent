"""报告版式：推荐排行逐个分子附「2D 结构 + 关键指标」卡片，备注精简成一句话。

用户实测反馈两点：
  1. 推荐化合物排行里的分子「长什么样」看不到（只有一张排在后面的网格图），
     希望结构图**就在数据旁边**，数据用表格整理整齐；
  2. 备注太长：网页里汇总表塞了一整段、PDF 里整页都是备注。

约定：
  - `charts/recommend_card_NN.png`（左结构 + 右指标表）由 `write_report_charts` 生成并登记，
    报告第 3.1 节用 `figure_by_rel` 逐分子内嵌 —— 网页 / PDF / 整包 ZIP 三处一致；
  - 报告里的运行笔记、推荐理由、推进建议、规则提示一律先过 `_brief()`（一句话），
    条数也有上限；完整原文仍在 run.json / result.json 里。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from docking_agent.reporting.artifacts import write_report_charts
from docking_agent.reporting.report import _brief, build_markdown_report
from docking_agent.runs import Run


def _ranking(n: int = 3) -> List[Dict[str, Any]]:
    rows = []
    for i in range(n):
        rows.append({
            "rank": i + 1,
            "name": f"分子{i + 1}",
            "smiles": "CCO" if i == 0 else ("Oc1ccccc1" if i == 1 else "NC(=N)c1ccccc1"),
            "affinity_kcal_mol": -3.0 - i,
            "engine": "vina", "exhaustiveness": 1, "n_poses": 1,
            "box_group": "main", "box_size": [22.0, 22.0, 22.0],
            "molecular_weight": 46.1 + i, "logP": -0.1 + i, "tpsa": 20.2 + i,
            "lipinski_violations": 0, "heavy_atoms": 3 + i, "drug_likeness_pass": True,
        })
    return rows


def test_report_puts_structure_card_next_to_each_compound(tmp_path: Path) -> None:
    """3.1 节：每个推荐分子都要有 `recommend_card_NN` 结构卡，且不再内嵌冗余网格图。"""
    run = Run(tmp_path, "R-CARD", "pipeline", {"receptor": "thrombin"})
    result: Dict[str, Any] = {"ranking": _ranking(3), "receptors": [], "positive_control": {},
                              "notes": [], "param_plan": {}, "task_spec": {}}
    written = write_report_charts(run, result["ranking"], {}, result=result)

    cards = [n for n in written if n.startswith("recommend_card_")]
    assert cards == ["recommend_card_01", "recommend_card_02", "recommend_card_03"], written
    for name in cards:
        assert (run.dir / "charts" / f"{name}.png").is_file(), name

    md = build_markdown_report(result, kind="pipeline", run_id="R-CARD",
                               artifacts=run.artifacts())
    assert "#### 逐个分子：2D 结构 + 关键指标" in md
    # 每个分子一节，卡片图就挂在该节里（结构挨着数据），顺序与排行一致
    sections = md.split("##### ")[1:]
    assert len(sections) == 3, len(sections)
    for section in sections:
        rank = int(section[1:].split(" ")[0])
        assert f"charts/recommend_card_{rank:02d}.png" in section, section[:120]
        assert "2D 结构 + 关键指标" in section
    # 排行表仍在（数据用表格整理），且每个分子的卡片图都被登记进产物清单
    assert "| # | 分子 | 综合分 | 等级 |" in md
    assert md.count("recommend_card_") >= 3
    assert "](charts/recommend_chart.png)" not in md, "旧的整张网格图不再内嵌（结构已挨着每个分子）"


def test_report_notes_and_reasons_are_briefed(tmp_path: Path) -> None:
    """长备注 / 长理由必须被压成一句话，报告里不出现原文全段。"""
    long_note = ("受体 thrombin 质子化：受体未按目标 pH 准备（注册表预置 PDBQT：质子化态由"
                 "该文件本身决定，未按运行 pH 重新准备）；其质子化态为 meeko 残基模板默认态"
                 "（≈pH 7 标准态），与配体目标 pH 可能存在系统偏差，如对 His/Asp/Glu 敏感请重做。")
    long_reason = ("该分子与 S1 口袋形成三个氢键并占据疏水亚口袋，" * 6) + "因此建议优先推进。"
    run = Run(tmp_path, "R-BRIEF", "agent", {"mode": "chat"})
    run.set(recommendation_reasons=[{"name": "分子1", "reason": long_reason,
                                     "suggestion": long_reason}])
    result: Dict[str, Any] = {
        "ranking": _ranking(2), "receptors": [], "positive_control": {},
        "notes": [long_note] * 8, "param_plan": {}, "task_spec": {},
        "agent_recommendations": [{"name": "分子1", "reason": long_reason,
                                   "suggestion": long_reason}],
    }
    write_report_charts(run, result["ranking"], {}, result=result)
    md = build_markdown_report(result, kind="agent", run_id="R-BRIEF", artifacts=run.artifacts())

    # 笔记：最多 6 条，每条都短（原文 110+ 字）
    notes_block = md[md.index("**运行笔记**"):]
    notes_block = notes_block[:notes_block.index("\n### ")]
    bullets = [ln for ln in notes_block.splitlines() if ln.startswith("- ")]
    assert 0 < len(bullets) <= 6, bullets
    assert all(len(b) <= 130 for b in bullets), [len(b) for b in bullets]
    assert long_note not in md, "完整长备注不应出现在报告里（原文在 run.json）"

    # 推荐理由：≤130 字
    reason_lines = [ln for ln in md.splitlines() if ln.startswith("- **推荐理由**")]
    assert reason_lines and all(len(ln) <= 140 for ln in reason_lines), reason_lines
    assert long_reason not in md


def test_brief_keeps_first_clause_and_marks_truncation() -> None:
    assert _brief("") == ""
    assert _brief("短句") == "短句"
    # 只有超长才会断句：给一个小 limit 验证「去掉加粗 + 取第一个分句」
    assert _brief("**重点**：甲；乙；丙", limit=6) == "重点：甲"
    assert _brief("**重点**：甲；乙；丙") == "重点：甲；乙；丙"
    long_text = "甲" * 200
    out = _brief(long_text, limit=40)
    assert len(out) <= 41 and out.endswith("…")
    # 全角括号内的分号不应被当成断句点（避免把「（…；…）」切断后语义不清）
    assert _brief("先说明（包含；分号）然后继续", limit=200) == "先说明（包含；分号）然后继续"


def test_cards_registered_as_artifacts_and_in_zip(tmp_path: Path) -> None:
    """卡片要登记成产物：中间数据页签能下载、整包 ZIP 里有、报告 PDF 能内嵌。"""
    from docking_agent.runs import RunStore
    import zipfile

    run = Run(tmp_path / "runs", "R-CARDZIP", "pipeline", {"receptor": "thrombin"})
    result: Dict[str, Any] = {"ranking": _ranking(2), "receptors": [], "positive_control": {},
                              "notes": [], "param_plan": {}, "task_spec": {}}
    write_report_charts(run, result["ranking"], {}, result=result)
    names = {a["name"] for a in run.artifacts()}
    assert {"recommend_card_01", "recommend_card_02"} <= names
    card = [a for a in run.artifacts() if a["name"] == "recommend_card_01"][0]
    assert card["content_type"] == "image/png" and card["size"] > 1000
    assert re.search(r"2D 结构", str(card["label"]))

    store = RunStore(root=tmp_path / "runs")
    dest = store.zip("R-CARDZIP")
    with zipfile.ZipFile(dest) as zf:
        assert "charts/recommend_card_01.png" in zf.namelist()
