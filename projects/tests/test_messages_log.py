"""模型可见消息日志：气泡/载荷争议事后可查（不落全量正文）。

真实困扰（2026-09-23，运行 20260923-224338-3779）：用户反馈"对话气泡把大量数据也对话了出去"，
但运行产物里只有最终结论（`agent_report.md`，3 KB），模型当时看到/输出了什么**无从查证**。
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from docking_agent.agents.persistence import (MESSAGES_LOG_HEAD, MESSAGES_LOG_LIMIT,
                                              build_messages_log)


def test_log_records_shape_not_full_payload() -> None:
    big = "X" * 5000
    messages = [
        HumanMessage(content="筛一下这两个分子"),
        AIMessage(content="", tool_calls=[{"name": "molecular_docking",
                                           "args": {"molecules_json": big}, "id": "c1"}]),
        ToolMessage(content=big, name="molecular_docking", tool_call_id="c1"),
        AIMessage(content="结论：……"),
    ]
    log = build_messages_log(messages, final_text="结论：……")
    assert [e["role"] for e in log] == ["HumanMessage", "AIMessage", "ToolMessage",
                                        "AIMessage", "FinalAnswer"]
    docking_call = log[1]["tool_calls"][0]
    assert docking_call["name"] == "molecular_docking"
    assert docking_call["args_chars"] > 5000, "入参大小必须如实记录（这是判断是否倒数据的关键）"
    tool_entry = log[2]
    assert tool_entry["tool"] == "molecular_docking"
    assert tool_entry["chars"] == 5000 and len(tool_entry["head"]) == MESSAGES_LOG_HEAD
    assert "X" * 5000 not in json_dump(log), "日志不得保存全量正文"


def json_dump(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def test_log_keeps_only_the_tail_of_long_runs() -> None:
    messages = [HumanMessage(content=f"第 {i} 步") for i in range(MESSAGES_LOG_LIMIT + 25)]
    log = build_messages_log(messages)
    assert len(log) == MESSAGES_LOG_LIMIT
    assert log[-1]["head"].startswith(f"第 {MESSAGES_LOG_LIMIT + 24} 步")
