"""候选选择（choices）的生命周期：**同一个问题只问一次、只显示一份**。

真实反馈（2026-09-22，用户实测）：
  「配体选择出现了两次：一个很快很短，直接让选配体；另一个慢，会说受体解析完成、
   配体需要选择，并且会覆盖之前的输出。如果点了先出现的那个选项，后面的选项就出不来了。」

根因是两处：
  1. `publish_choices` 只要 kind+id 相同就算「同一个问题」，但旧实现仍会用**新的**
     label/prompt 改写 `run.data["choices"]` —— 于是 SSE 又发一份（`_tick` 比的是内容），
     前端把已有的选项**覆盖**掉；
  2. 前端 `clearChoicesEverywhere()` 清空**所有**消息的候选、`applyChoice` 运行中点选又被
     busy 判定静默丢弃。前端的看护在 `scripts/browser_check.py`，这里看住服务端的两条不变式。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from docking_agent.tools.choices import clear_choices, publish_choices


def _molecule_choices(query: str) -> list:
    """两套写法解析到同一个 CID（真实场景：代森锰锌 / Mancozeb）。"""
    return [
        {"id": "molecule:3034368:raw", "kind": "molecule",
         "label": "Mancozeb（CID 3034368） · PubChem 原始多组分结构",
         "value": "CCO.[Zn+2]", "prompt": f"{query} CCO.[Zn+2] （按原始多组分继续对接）"},
        {"id": "molecule:3034368:organic", "kind": "molecule",
         "label": "Mancozeb · 最大有机片段 EBDC",
         "value": "CCO", "prompt": f"{query}-EBDC CCO （按最大有机片段继续对接）"},
    ]


def test_republishing_the_same_question_keeps_the_first_options(run_ctx: Any) -> None:
    """同一问题重复发布 → `choices` 保持第一次那份（第二次的 prompt 不得覆盖）。"""
    run, _board = run_ctx
    publish_choices("molecule", _molecule_choices("代森锰锌"), note="需要确认取法", runtime=None)
    first = [dict(c) for c in run.data["choices"]]
    publish_choices("molecule", _molecule_choices("Mancozeb"), note="需要确认取法", runtime=None)

    assert run.data["choices"] == first, "第二次发布改写了已下发的候选（界面会被覆盖）"
    assert len(run.data["_choices_signatures"]) == 1, run.data["_choices_signatures"]
    logs = [line for line in run.logs if "可选项" in line]
    assert len(logs) == 1, logs


def test_clear_then_republish_really_asks_again(run_ctx: Any) -> None:
    """清掉候选（解析成功后）→ 同一问题若再次真的需要，必须能重新下发。

    幂等标记不能把「先问 → 已答 → 又问」这条路一起吞掉。
    """
    run, _board = run_ctx
    publish_choices("molecule", _molecule_choices("代森锰锌"), runtime=None)
    clear_choices("molecule", runtime=None)
    assert not run.data.get("choices"), "clear 之后不应还留着候选"

    publish_choices("molecule", _molecule_choices("代森锰锌"), runtime=None)
    assert run.data.get("choices"), "清空后再次发布被幂等标记吞掉了"


def test_clear_one_kind_keeps_the_other_question(run_ctx: Any) -> None:
    """受体与分子是两问：清掉一类不得把另一类也抹掉（同一次运行里这是正常组合）。"""
    run, _board = run_ctx
    publish_choices("molecule", _molecule_choices("代森锰锌"), runtime=None)
    publish_choices("receptor", [{"id": "receptor:P00734", "kind": "receptor",
                                 "label": "凝血酶 P00734", "value": "P00734",
                                 "prompt": "用 P00734"}], runtime=None)
    clear_choices("molecule", runtime=None)

    kinds = [c.get("kind") for c in run.data.get("choices") or []]
    assert kinds == ["receptor"], kinds
    assert all(s[0] != "molecule" for s in run.data["_choices_signatures"]), \
        run.data["_choices_signatures"]


def test_pending_choice_run_is_needs_user_input_not_no_op(tmp_path: Path) -> None:
    """停下来等用户点选 ≠ 「没有任何工具产出」：状态与原因都必须如实。

    真实缺陷：受体已解析成功、工具确实跑过，只因配体侧要用户确认就被标成 `no_op`，
    界面日志说「未执行计算（未调用任何工具）」，历史列表显示 [ SKIP ]。
    """
    from docking_agent.agents.persistence import persist_agent_run
    from docking_agent.runs import Run

    run = Run(tmp_path, "R-ASK", "agent", {"mode": "chat"})
    run.set(task_spec={"task_type": "docking_only", "decision": "run"})
    run.data["choices"] = _molecule_choices("代森锰锌")
    run.data["choices_note"] = "该名称是多组分/聚合物：代表结构的取法需要用户确认"
    result = persist_agent_run(run, [], "已解析受体，但分子侧必须由用户确认。")

    assert result.get("status") == "needs_user_input", result.get("status")
    assert result.get("needs_user_input") is True
    assert not result.get("no_op"), "等用户选择被当成了 no_op"
    reason = str(run.data.get("no_report_reason") or "")
    assert "等待" in reason and "确认" in reason, reason
    # 没有真实计算 → 仍然不产规范报告（避免空报告噪声）
    assert not (tmp_path / "R-ASK" / "report.md").is_file()


def test_empty_run_without_choices_is_still_no_op(tmp_path: Path) -> None:
    """对照组：真的没受理、也没候选项时，仍然必须如实标成 no_op。"""
    from docking_agent.agents.persistence import persist_agent_run
    from docking_agent.runs import Run

    run = Run(tmp_path, "R-NOOP", "agent", {"mode": "chat"})
    run.set(task_spec={"task_type": "other", "decision": "reject"})
    result = persist_agent_run(run, [], "你好！请告诉我候选分子与受体目标。")

    assert result.get("no_op") is True and result.get("status") == "no_op"
    assert not result.get("needs_user_input")
