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
