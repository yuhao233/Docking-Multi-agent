"""候选选择（choices）的生命周期约束：同一个问题只询问一次，界面上只保留一份候选。

服务端的两条不变式：
  1. `publish_choices` 以 kind+id 判定「同一个问题」，已下发的候选不被新的
     label/prompt 改写；`run.data["choices"]` 若被改写，`_tick` 会因内容变化重发 SSE，
     前端会覆盖已有选项；
  2. 候选清空后，同一问题再次需要时仍可重新下发；幂等标记只作用于未清空的候选，
     不吞掉「先问、已答、又问」这条路径。

前端侧的对应行为：`clearChoicesEverywhere()` 清空所有消息的候选，`applyChoice` 在运行中的
点选被 busy 判定静默丢弃，因此服务端不依赖前端补发候选。
前端的看护在 `scripts/browser_check.py`，本模块看住服务端的两条不变式。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from docking_agent.tools.choices import clear_choices, publish_choices


def _molecule_choices(query: str) -> list:
    """两种写法解析到同一个 CID（示例：代森锰锌 / Mancozeb）。"""
    return [
        {"id": "molecule:3034368:raw", "kind": "molecule",
         "label": "Mancozeb（CID 3034368） · PubChem 原始多组分结构",
         "value": "CCO.[Zn+2]", "prompt": f"{query} CCO.[Zn+2] （按原始多组分继续对接）"},
        {"id": "molecule:3034368:organic", "kind": "molecule",
         "label": "Mancozeb · 最大有机片段 EBDC",
         "value": "CCO", "prompt": f"{query}-EBDC CCO （按最大有机片段继续对接）"},
    ]


def test_republishing_the_same_question_keeps_the_first_options(run_ctx: Any) -> None:
    """同一问题重复发布时，`choices` 保持第一次下发的版本，第二次的 prompt 不覆盖。"""
    run, _board = run_ctx
    publish_choices("molecule", _molecule_choices("代森锰锌"), note="需要确认取法", runtime=None)
    first = [dict(c) for c in run.data["choices"]]
    publish_choices("molecule", _molecule_choices("Mancozeb"), note="需要确认取法", runtime=None)

    assert run.data["choices"] == first, "第二次发布改写了已下发的候选（界面会被覆盖）"
    assert len(run.data["_choices_signatures"]) == 1, run.data["_choices_signatures"]
    logs = [line for line in run.logs if "可选项" in line]
    assert len(logs) == 1, logs


def test_clear_then_republish_really_asks_again(run_ctx: Any) -> None:
    """候选清空（解析成功后）后，同一问题再次出现时需要能重新下发。

    幂等标记不覆盖「先问、已答、又问」这条路径。
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
    """等待选择与「没有任何工具产出」是两种状态，返回的状态与原因分别对应。

    受体已解析成功且工具执行过时，仅因配体侧等待确认就标成 `no_op`，会与界面日志
    「未执行计算（未调用任何工具）」及运行列表里的 [ SKIP ] 冲突。
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
    # 没有实际计算时不产出规范报告，避免空报告噪声
    assert not (tmp_path / "R-ASK" / "report.md").is_file()


def test_empty_run_without_choices_is_still_no_op(tmp_path: Path) -> None:
    """对照组：既没有受理也没有候选项时，状态标成 no_op。"""
    from docking_agent.agents.persistence import persist_agent_run
    from docking_agent.runs import Run

    run = Run(tmp_path, "R-NOOP", "agent", {"mode": "chat"})
    run.set(task_spec={"task_type": "other", "decision": "reject"})
    result = persist_agent_run(run, [], "你好！请告诉我候选分子与受体目标。")

    assert result.get("no_op") is True and result.get("status") == "no_op"
    assert not result.get("needs_user_input")


# --------------------------------------------------------------------------- #
# 阻断式候选：未回答前不继续计算（设计约束）
# --------------------------------------------------------------------------- #
def test_blocking_choice_blocks_until_answered(run_ctx: Any) -> None:
    """`blocking=True` 的候选（共晶配体是否作阳性对照）在清除之前保持待回答状态。

    候选下发后若继续执行，主管 Agent 重试对接工具时会被「一次运行只询问一次」的
    幂等标记放行，在调用方尚未回答时算完 2961 个分子、耗时 40 分钟，
    问题直到运行结束才显示。
    """
    from docking_agent.tools.choices import blocking_choice_pending

    run, _board = run_ctx
    publish_choices("positive_control", _molecule_choices("代森锰锌"), runtime=None, blocking=True)
    assert run.data.get("choices_blocking") == "positive_control"
    assert blocking_choice_pending("positive_control", runtime=None) is True
    assert blocking_choice_pending("molecule", runtime=None) is False, "只拦同一类问题"

    clear_choices("positive_control", runtime=None)
    assert run.data.get("choices_blocking") is None, "回答/清理后必须解除阻断"
    assert blocking_choice_pending("positive_control", runtime=None) is False


def test_non_blocking_choice_does_not_block_docking(run_ctx: Any) -> None:
    """普通候选（非阻断）不得拦住后续工具调用，否则会把正常流程卡死。"""
    from docking_agent.tools.choices import blocking_choice_pending

    run, _board = run_ctx
    publish_choices("molecule", _molecule_choices("代森锰锌"), runtime=None)
    assert blocking_choice_pending(runtime=None) is False


def test_dispatch_refuses_to_start_docking_while_choice_pending(
        run_ctx: Any, monkeypatch) -> None:
    """存在待回答的阻断式问题时，主管的 `run_docking` 原地返回 needs_user_input。

    该护栏在调度子 Agent 之前生效，省掉一整轮 LLM 调用，也不会绕过检查直接开跑。
    """
    import json

    from docking_agent.agents import dispatch
    from docking_agent.tools.choices import publish_choices

    run, _board = run_ctx
    publish_choices("positive_control", _molecule_choices("共晶配体"), runtime=None, blocking=True)

    calls = []
    monkeypatch.setattr(dispatch, "get_docking_agent",
                        lambda: calls.append("agent") or object())
    out = json.loads(dispatch.run_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]'))
    assert out["status"] == "needs_user_input", out
    assert "点选" in out["message"] or "选择" in out["message"]
    assert calls == [], "待回答期间不得调度对接子 Agent"
