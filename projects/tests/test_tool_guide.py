"""工具用法文档：Agent 按需查阅（而不是把纪律堆进系统提示词）。"""
from __future__ import annotations


def test_guide_lists_topics_and_docking_preconditions() -> None:
    from docking_agent.agents.tool_docs import tool_guide

    catalog = tool_guide.func("")
    for topic in ("docking", "report", "choices", "parameters", "receptor"):
        assert topic in catalog, catalog
    docking = tool_guide.func("docking")
    assert "前置条件" in docking and "共晶配体" in docking


def test_guide_reports_unknown_topic() -> None:
    from docking_agent.agents.tool_docs import tool_guide

    out = tool_guide.func("不存在的主题")
    assert "没有主题" in out and "docking" in out


def test_report_guide_mentions_coverage_honesty() -> None:
    from docking_agent.agents.tool_docs import tool_guide

    text = tool_guide.func("report")
    assert "coverage" in text and "customize_report" in text
