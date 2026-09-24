"""报告：结构固定、内容由 Agent 写（自定义小节 + ID/CAS/备注 列）。"""
from __future__ import annotations


def _ctx(result):
    from docking_agent.reporting.report_context import ReportContext

    return ReportContext(result)


def test_agent_sections_render_after_section_8() -> None:
    ctx = _ctx({"report_customization": {"sections": [
        {"title": "本次特别说明", "body": "用户要求带上 ID，已按输入文件 ID 列输出。"},
        {"title": "", "body": "补充一句。"},
    ]}})
    text = "\n".join(ctx.agent_section_lines())
    assert "### 8.1 本次特别说明" in text
    assert "用户要求带上 ID" in text
    assert "### 8.2 补充说明" in text


def test_no_sections_means_no_extra_chapter() -> None:
    assert _ctx({}).agent_section_lines() == []
    assert _ctx({"report_customization": {"sections": []}}).agent_section_lines() == []


def test_binding_section_lists_recommended_molecules() -> None:
    """报告里"展示哪些分子"以**推荐排行**为准（用户要求），不再另按亲和力截取一批。"""
    from docking_agent.reporting.report_context import ReportContext
    from docking_agent.reporting.report_sections_results import section_5_binding

    ranking = [{"name": f"M{i}", "smiles": f"C{'C' * i}O", "affinity_kcal_mol": -9.0 + i,
                "molecular_weight": 100.0, "drug_likeness_pass": True} for i in range(1, 6)]
    result = {
        "ranking": ranking,
        "recommendations": {"rows": [{"rank": 1, "name": "M3", "smiles": "CCCCO"},
                                     {"rank": 2, "name": "M5", "smiles": "CCCCCCO"}]},
        "binding": {"rows": [{"smiles": "CCCCO", "morgan_tanimoto": 0.31},
                             {"smiles": "CCCCCCO", "morgan_tanimoto": 0.22},
                             {"smiles": "CCO", "morgan_tanimoto": 0.90}]},
    }
    ctx = ReportContext(result)
    assert [m["name"] for m in ctx.recommended] == ["M3", "M5"]
    text = "\n".join(section_5_binding(ctx))
    assert "推荐分子" in text
    assert "M3" in text and "M5" in text
    # 未被推荐的 M1（CCO，亲和力最高）不应出现在这张表里
    table = text.split("结合模式与阳性对照比较")[-1]
    assert "M1" not in table, table[:400]


def test_agent_text_is_polished_without_rewriting_facts() -> None:
    """报告文字只做"减法"：去过渡词/过程句/重复句，事实与数字一个不动。"""
    from docking_agent.reporting.content import extract_agent_conclusions, polish_agent_text

    raw = """## 结论
综上所述，本次筛选中 Hyodeoxycholic acid 亲和力 -12.07 kcal/mol，最优。
值得注意的是，该分子配体效率 0.43。
以上为本次分析的结论。
该分子配体效率 0.43。
"""
    text = extract_agent_conclusions(raw)
    assert "综上所述" not in text and "值得注意的是" not in text
    assert "以上为本次分析的结论" not in text, text
    assert text.count("0.43") == 1, text          # 重复句只留一次
    assert "-12.07 kcal/mol" in text, text        # 数字与事实不得改动
    assert polish_agent_text("首先，A 是首选。") == "A 是首选。"
