"""任务受理层（intake）的回归测试。

这一层是「输入 → 任务规约(JSON)」的可测试环节：确定性规则必须零模型、可复现；
受理模型只能补白名单字段、绝不能编造分子或决定参数；任何失败都要退回确定性规约。

设计动机见 `src/docking_agent/intake.py` 文档：
旧协调 Agent 把「受理」与「执行」两套价值观塞进同一个提示词，导致
「含糊就先问用户」与「分子库非空就必须跑完、不得询问」在库有但意图含糊时直接矛盾。
拆分后「跑 / 问 / 拒」由受理层显式判定（decision），编排层只执行。
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

from docking_agent import intake  # noqa: E402
from docking_agent.api.schemas import AgentRequest  # noqa: E402


def _req(**kwargs):
    base = {"mode": "manual", "message": "", "receptor": "thrombin", "ligands_text": "A:CCO"}
    base.update(kwargs)
    return AgentRequest(**base)


# --------------------------------------------------------------------------- #
# 确定性规约（零模型）
# --------------------------------------------------------------------------- #
def test_manual_spec_is_authoritative_and_needs_no_llm():
    spec = intake.build_task_spec(_req(mode="manual", message="随便说点什么",
                                       exhaustiveness=9, engine="vina"))
    assert spec["authority"] == "manual" and spec["decision"] == "run"
    assert spec["needs_llm"] is False, "参数模式由表单驱动，不应为一个模型来回付延迟"
    assert spec["params"]["exhaustiveness"] == 9
    assert spec["ligands"] == {"source": "text", "count": 1, "text": "A:CCO", "file": ""}
    assert spec["source"] == "rules"


def test_chat_collapsed_uses_system_defaults_but_asks_llm_to_understand():
    spec = intake.build_task_spec(_req(mode="chat", advanced=False,
                                       message="用示例库筛一下", ligands_text="A:CCO",
                                       exhaustiveness=9))
    assert spec["authority"] == "chat"
    assert spec["params"]["exhaustiveness"] == 16, "折叠高级设置时应使用系统默认值（v0.24 起默认 16）"
    assert spec["ligands"]["source"] == "library", "折叠时表单里的分子库不生效"
    assert spec["needs_llm"] is True, "对话模式需要模型理解自然语言"


def test_chat_advanced_uses_form_values_as_defaults():
    spec = intake.build_task_spec(_req(mode="chat", advanced=True,
                                       message="帮我筛一下", ligands_text="A:CCO",
                                       exhaustiveness=9))
    assert spec["authority"] == "chat+advanced"
    assert spec["params"]["exhaustiveness"] == 9
    assert spec["ligands"]["text"] == "A:CCO"


def test_explicit_site_and_receptor_file_are_marked_as_user_input():
    spec = intake.build_task_spec(_req(site_center=[31.5, 13.74, 24.36], site_size=[22, 22, 22],
                                       receptor_file="assets/x.pdb"))
    assert spec["site"]["source"] == "user" and spec["site"]["center"][0] == 31.5
    assert spec["receptor"]["file"] == "assets/x.pdb" and spec["receptor"]["source"] == "user"


def test_positive_control_three_states():
    provided = intake.build_task_spec(_req(positive_control="NC(=N)c1ccccc1"))
    assert provided["positive_control"] == {"smiles": "NC(=N)c1ccccc1", "provided_by": "user"}
    skipped = intake.build_task_spec(_req(skip_positive_control=True, positive_control="X"))
    assert skipped["positive_control"]["provided_by"] == "skipped"
    none = intake.build_task_spec(_req())
    assert none["positive_control"]["provided_by"] == "none"
    assert any("阳性对照" in a for a in none["assumptions"])


def test_library_fallback_is_not_treated_as_missing():
    """没给分子仍不阻断（decision=run），但**默认不自动用示例库** —— 示例库需用户明确要求。

    2026-09-17 起：内置示例库/示例受体只在用户明确调用时使用（产品要求），
    因此这里断言「不擅自使用」的表述，而不是旧的「已使用示例分子库」。
    """
    spec = intake.build_task_spec(_req(ligands_text=""))
    assert spec["ligands"]["source"] == "library"
    assert spec["missing"] == [] and spec["decision"] == "run"
    joined = " ".join(spec["assumptions"])
    assert "默认不使用内置示例库" in joined, joined
    assert intake.user_requested_example_library("") is False


@pytest.mark.parametrize("text,expected", [
    ("帮我看看这些分子的类药性和物化性质", "properties_only"),
    ("做一次分子对接，给出亲和力排序", "docking_only"),
    ("比较一下结合模式和阳性对照的相似度", "binding_only"),
    ("只要报告和图表", "report_only"),
    ("", "screening"),
    ("对接 + 性质 + 结合模式都做", "screening"),
])
def test_task_type_keyword_inference(text, expected):
    assert intake._guess_task_type(text) == expected


def test_registry_receptor_named_in_message_is_recognized():
    spec = intake.build_task_spec(_req(mode="chat", message="请用 trypsin 做筛选",
                                       ligands_text="A:CCO"))
    assert spec["receptor"]["name"] == "trypsin" and spec["receptor"]["source"] == "user"
    assert not any("默认受体" in a for a in spec["assumptions"])

    default = intake.build_task_spec(_req(mode="chat", message="帮我筛选一下", ligands_text="A:CCO"))
    assert default["receptor"]["source"] == "default"
    assert any("默认受体" in a for a in default["assumptions"])


def test_chat_default_receptor_is_not_rendered_as_user_specified() -> None:
    """真实缺陷回归：chat 折叠时受理层判定「未指定受体」并标了 source=default，
    但渲染层却给出「受体：thrombin」的权威口吻，主管 Agent 于是把示例默认受体当成
    用户指定。修复后：默认受体只能弱表述，且要求把 receptor_sources 留空交给工具回退
    （工具回退时会写下「未指定受体」的 note，报告里才有「这是假设」的说明）。"""
    spec = intake.build_task_spec(_req(mode="chat", advanced=False, ligands_text="",
                                       message="帮我筛一下这两个分子 CC(=O)O"))
    assert spec["receptor"]["source"] == "default"
    msg = intake.render_agent_message(spec)
    assert "受体：thrombin" not in msg, "默认受体不得再渲染成权威的『受体：thrombin』"
    assert "指令未指定" in msg
    assert "receptor_sources" in msg and "留空" in msg
    assert "系统默认" in msg

    # 用户明确指定 trypsin 时：仍按用户指定渲染，行为不变
    user = intake.build_task_spec(_req(mode="chat", advanced=False, ligands_text="",
                                       message="用 trypsin 做一次筛选：A:CCO"))
    assert user["receptor"] == {"name": "trypsin", "file": "", "source": "user"}
    assert "受体：trypsin" in intake.render_agent_message(user)
    assert "指令未指定" not in intake.render_agent_message(user)


def test_unspecified_default_receptor_is_restored_to_empty_for_tool() -> None:
    """受理层判定 default 时，主管 Agent 即便显式传了默认受体名，也要还原为空，
    让 dock_library 走空值回退并写下「未指定受体，已默认使用 …」的 note。"""
    import tempfile

    from docking_agent.runs import Run
    from docking_agent.tools.docking import _drop_unspecified_default_receptor

    with tempfile.TemporaryDirectory() as tmp:
        run = Run(Path(tmp), "R-DEF", "agent", {"mode": "chat"})
        run.data["task_spec"] = {"receptor": {"name": "thrombin", "source": "default"}}
        assert _drop_unspecified_default_receptor("thrombin", run) == ""
        assert _drop_unspecified_default_receptor("1DWC", run) == ""  # 默认受体的别名同样还原
        assert _drop_unspecified_default_receptor("trypsin", run) == "trypsin"  # 其它受体不动
        assert _drop_unspecified_default_receptor("/tmp/u.pdbqt", run) == "/tmp/u.pdbqt"
        # 用户明确指定（source=user）时绝不还原
        run.data["task_spec"] = {"receptor": {"name": "thrombin", "source": "user"}}
        assert _drop_unspecified_default_receptor("thrombin", run) == "thrombin"
        # 没有 task_spec（如流水线）时不动
        run.data.pop("task_spec")
        assert _drop_unspecified_default_receptor("thrombin", run) == "thrombin"


def test_default_receptor_goes_to_tool_as_empty_and_notes_keep_the_assumption(monkeypatch) -> None:
    """端到端口径：受理层判定 default 时，`molecular_docking` 必须以**空受体**调用
    `dock_library`（回退默认），因此结果 notes 里会出现「未指定受体 → 系统默认」的说明；
    用户显式指定的受体（trypsin）则原样透传，绝不被护栏吞掉。"""
    import tempfile

    from docking_agent.core.receptors import resolve_receptor_specs
    from docking_agent.runs import Run, current_run
    from docking_agent.tools import docking as docking_tool

    captured: dict = {}

    def _fake_dock(molecules, *, receptor=None, **kwargs):
        captured["receptor"] = receptor
        specs, notes = resolve_receptor_specs(receptor)
        return {"status": "ok", "notes": notes,
                "receptors": [{"receptor_key": specs[0].get("key"), "receptor": specs[0].get("name"),
                               "box_center": [0, 0, 0], "box_size": [22, 22, 22],
                               "results": [{"name": "A", "smiles": "CCO",
                                            "affinity_kcal_mol": -4.0, "engine": "vina",
                                            "exhaustiveness": 1}]}]}

    monkeypatch.setattr(docking_tool, "dock_library", _fake_dock)
    call = docking_tool.molecular_docking.func
    kwargs = dict(molecules_json='[{"name":"A","smiles":"CCO"}]', receptor_sources="thrombin",
                  exhaustiveness=1, n_poses=1, engine="vina")

    with tempfile.TemporaryDirectory() as tmp:
        run = Run(Path(tmp), "R-NOTE", "agent", {"mode": "chat"})
        token = current_run.set(run)
        try:
            # ① 受理层判定「未指定」→ 主管即便传了 thrombin 也要还原为空，notes 保留假设说明
            run.data["task_spec"] = {"receptor": {"name": "thrombin", "source": "default"}}
            out = json.loads(call(**kwargs))
            assert captured["receptor"] == "", "default 时必须以空受体交给 dock_library 回退"
            assert any("未指定受体" in n for n in (out.get("notes") or [])), out.get("notes")
            # ② 用户显式指定 trypsin → 原样透传，护栏不得吞掉
            run.data["task_spec"] = {"receptor": {"name": "trypsin", "source": "user"}}
            call(**{**kwargs, "receptor_sources": "trypsin"})
            assert captured["receptor"] == "trypsin", "用户显式指定的受体必须原样透传"
        finally:
            current_run.reset(token)




# --------------------------------------------------------------------------- #
# 渲染：把 decision 讲清楚（这一步消解了旧提示词的自相矛盾）
# --------------------------------------------------------------------------- #
def test_rendered_message_states_decision_and_role_boundary():
    spec = intake.build_task_spec(_req(mode="chat", advanced=True, message="对接并排序"))
    msg = intake.render_agent_message(spec)
    assert "--- 任务规约（受理层产出；编排层只读，不要修改）---" in msg
    assert "decision=run" in msg
    assert "不得在中途停下来询问用户" in msg
    assert "不要重新解析用户语言" in msg
    assert msg.count("allow_example_fallback") >= 0


def test_ask_decision_message_forbids_tool_calls():
    # 只有在「用户没给出任何可识别分子」时 ask 才成立
    spec = intake._finalize({**intake.build_task_spec(_req(ligands_text="")),
                             "needs_user_input": True, "missing": ["可识别的分子"]})
    msg = intake.render_agent_message(spec)
    assert "decision=ask" in msg and "不要调用任何工具" in msg
    assert "不得在中途停下来询问用户" not in msg


def test_reject_decision_message_forbids_tool_calls():
    spec = intake._finalize({**intake.build_task_spec(_req(message="帮我订一份午饭")),
                             "out_of_scope": True})
    msg = intake.render_agent_message(spec)
    assert "decision=reject" in msg and "不要调用任何工具" in msg


def test_mode_specific_param_headers_are_preserved():
    """三种模式的措辞是既有契约（docs/api.md §2/§7），受理层必须原样保留。"""
    collapsed = intake.compose_agent_message(_req(mode="chat", advanced=False))
    assert "系统默认" in collapsed and "以指令为准" in collapsed
    expanded = intake.compose_agent_message(_req(mode="chat", advanced=True))
    assert "来自高级设置" in expanded and "以指令为准" in expanded
    manual = intake.compose_agent_message(_req(mode="manual"))
    assert "权威参数" in manual and "以本节为准" in manual


# --------------------------------------------------------------------------- #
# 受理模型（离线桩）：只补白名单、不得编造、失败即回退
# --------------------------------------------------------------------------- #
class _Reply:
    def __init__(self, content):
        self.content = content
        self.response_metadata = {"model_name": "stub"}


class _StubLLM:
    def __init__(self, payload, *, raise_error=False):
        self.payload = payload
        self.raise_error = raise_error
        self.calls = []

    def invoke(self, messages, *a, **k):
        self.calls.append(messages)
        if self.raise_error:
            raise RuntimeError("模型不可用")
        text = self.payload if isinstance(self.payload, str) else json.dumps(
            self.payload, ensure_ascii=False)
        return _Reply(text)


@pytest.fixture()
def llm_spy(monkeypatch):
    """替换 build_chat_llm，记录 role 并返回桩模型。"""
    from docking_agent.runtime import llm as llm_mod

    def _factory(ctx=None, role="", payload=None, raise_error=False):
        stub = _StubLLM(payload or {}, raise_error=raise_error)
        stub.role = role
        return stub

    holder = {}

    def _install(payload, *, raise_error=False):
        stub = _StubLLM(payload, raise_error=raise_error)
        captured = {}

        def _builder(ctx=None, role=""):
            captured["role"] = role
            return stub

        monkeypatch.setattr(llm_mod, "build_chat_llm", _builder)
        holder.update({"stub": stub, "captured": captured})
        return stub

    holder["install"] = _install
    holder["factory"] = _factory
    return holder


def test_llm_cannot_invent_molecules(llm_spy, monkeypatch):
    monkeypatch.setenv("INTAKE_LLM", "on")
    llm_spy["install"]({
        "task_type": "docking_only", "goal": "对接阿司匹林",
        "mentioned_molecules": ["阿司匹林", "布洛芬"],   # 布洛芬不在原文里
        "confidence": 0.8,
    })
    req = _req(mode="chat", advanced=True, message="帮我对接一下阿司匹林", ligands_text="")
    spec = intake.build_task_spec(req)
    refined = intake.refine_task_spec(spec)
    assert refined["mentioned_molecules"] == ["阿司匹林"], "只接受原文中逐字出现的分子"
    assert refined["ligands"]["source"] == "mentioned"
    assert any("布洛芬" in n for n in refined.get("llm_notes") or [])
    assert any("fetch_molecule_record" in a for a in refined["assumptions"])
    assert refined["task_type"] == "docking_only"
    assert llm_spy["captured"]["role"] == "intake", "受理必须用自己的角色（独立模型实例）"


def test_llm_cannot_set_run_parameters(llm_spy, monkeypatch):
    monkeypatch.setenv("INTAKE_LLM", "on")
    llm_spy["install"]({
        "goal": "筛选", "exhaustiveness": 32, "engine": "autodock",
        "params": {"exhaustiveness": 32}, "site": {"center": [1, 2, 3]},
        "positive_control": {"smiles": "CCO"}, "decision": "ask",
    })
    spec = intake.build_task_spec(_req(mode="chat", advanced=True, message="筛一下"))
    refined = intake.refine_task_spec(spec)
    # v0.26：留空(None) = 自动规划，不再把默认值当成"用户指定"
    assert refined["params"]["exhaustiveness"] is None, "参数只能来自表单/系统默认；未给值就是留空"
    # v0.26：engine 留空("") = 跟随系统默认（不再把 "vina" 当成用户指定）
    assert not str(refined["params"]["engine"]).strip()
    assert refined["site"]["center"] == []
    assert refined["positive_control"]["provided_by"] == "none"
    assert refined["decision"] == "run", "decision 由受理层推导，模型不能直接指定"
    assert refined.get("needs_user_input") is None, "受限字段不得被采纳"
    assert any("受限字段" in n for n in refined.get("llm_notes") or [])


def test_llm_out_of_scope_becomes_reject(llm_spy, monkeypatch):
    monkeypatch.setenv("INTAKE_LLM", "on")
    llm_spy["install"]({"out_of_scope": True, "scope_reason": "用户在问天气",
                        "confidence": 0.95})
    req = _req(mode="chat", advanced=True, message="今天天气怎么样", ligands_text="")
    _, spec = intake.build_message(req)
    assert spec["decision"] == "reject" and spec["out_of_scope"] is True
    assert "不要调用任何工具" in intake.render_agent_message(spec)


def test_llm_missing_alone_does_not_block_the_run(llm_spy, monkeypatch):
    """回归：模型把「没给分子/受体」写进 missing 时**不得**判成 ask。

    系统对受体/阳性对照/参数都有默认值，缺这些不构成阻断；
    实时评估（scripts/intake_eval.py --live）曾抓到模型用 missing 让 5 个应跑的用例变成 ask。
    """
    monkeypatch.setenv("INTAKE_LLM", "on")
    llm_spy["install"]({"missing": ["候选分子库", "目标受体文件"],
                        "questions": ["请问用哪个受体？"]})
    req = _req(mode="chat", advanced=True, message="帮我筛一下这个库", ligands_text="")
    _, spec = intake.build_message(req)
    assert spec["decision"] == "run", "missing 只是记录，不能阻断"


def test_llm_explicit_needs_user_input_becomes_ask(llm_spy, monkeypatch):
    """只有模型明确要求补充、且用户没给出任何可识别分子来源时才 ask。"""
    monkeypatch.setenv("INTAKE_LLM", "on")
    llm_spy["install"]({"needs_user_input": True,
                        "questions": ["请问您想分析哪个分子？（可给名称或 SMILES）"]})
    req = _req(mode="chat", advanced=True, message="帮我分析一下那个化合物", ligands_text="")
    _, spec = intake.build_message(req)
    assert spec["decision"] == "ask"
    assert "请问您想分析哪个分子" in intake.render_agent_message(spec)


def test_needs_user_input_is_ignored_when_ligands_are_known(llm_spy, monkeypatch):
    """用户已经给了分子/文件时，即使模型要求补充也必须跑（不能凭空阻断）。"""
    monkeypatch.setenv("INTAKE_LLM", "on")
    llm_spy["install"]({"needs_user_input": True,
                        "mentioned_molecules": ["阿司匹林"]})
    req = _req(mode="chat", advanced=True, message="对接阿司匹林", ligands_text="")
    _, spec = intake.build_message(req)
    assert spec["ligands"]["source"] == "mentioned"
    assert spec["decision"] == "run"


def test_llm_invalid_json_falls_back_to_rules(llm_spy, monkeypatch):
    monkeypatch.setenv("INTAKE_LLM", "on")
    llm_spy["install"]("抱歉，我不太确定你的意思")
    req = _req(mode="chat", advanced=True, message="筛一下", ligands_text="A:CCO")
    _, spec = intake.build_message(req)
    assert spec["llm_status"] == "invalid_json"
    assert spec["decision"] == "run" and spec["source"] == "rules"


def test_llm_error_falls_back_to_rules(llm_spy, monkeypatch):
    monkeypatch.setenv("INTAKE_LLM", "on")
    llm_spy["install"]({}, raise_error=True)
    req = _req(mode="chat", advanced=True, message="筛一下", ligands_text="A:CCO")
    _, spec = intake.build_message(req)
    assert str(spec["llm_status"]).startswith("error:")
    assert spec["decision"] == "run", "受理模型挂掉不能让整次运行失败"


def test_llm_disabled_by_env(llm_spy, monkeypatch):
    monkeypatch.setenv("INTAKE_LLM", "off")
    llm_spy["install"]({"out_of_scope": True})
    req = _req(mode="chat", advanced=True, message="筛一下", ligands_text="A:CCO")
    _, spec = intake.build_message(req)
    assert spec["source"] == "rules" and spec.get("llm_status") is None
    assert llm_spy["stub"].calls == [], "关闭时不得调用模型"


def test_manual_mode_never_calls_intake_llm(llm_spy, monkeypatch):
    monkeypatch.setenv("INTAKE_LLM", "on")
    llm_spy["install"]({"out_of_scope": True})
    _, spec = intake.build_message(_req(mode="manual", message="做一次筛选"))
    assert spec["source"] == "rules"
    assert llm_spy["stub"].calls == [], "参数模式必须零额外延迟"


def test_intake_is_registered_as_a_role():
    from docking_agent.runtime.llm import ROLES

    assert "intake" in ROLES
    from docking_agent import settings as S

    assert "intake" in S.ROLES and S.ROLE_LABEL.get("intake")


# --------------------------------------------------------------------------- #
# 端到端可观测性：受理结论要进运行记录 / 报告 / start 事件
# --------------------------------------------------------------------------- #
def test_task_spec_is_visible_in_report_and_result():
    """受理结论必须落进报告与运行结果（否则没人知道任务被理解成了什么）。"""
    from docking_agent.reporting import build_markdown_report

    spec = {"task_type": "docking_only", "authority": "chat+advanced", "decision": "run",
            "source": "rules+llm", "assumptions": ["未指定受体 → 使用默认受体 thrombin"]}
    md = build_markdown_report({"ranking": [], "molecules": [], "binding": {},
                                "task_spec": spec}, kind="agent", run_id="R1")
    assert "任务受理" in md
    assert "docking_only" in md and "chat+advanced" in md and "rules+llm" in md
    assert "未指定受体" in md


def test_start_event_payload_shape_has_task_spec():
    """受理结论要能随 SSE start 事件下发（API 层直接取 run.data['task_spec']）。"""
    from docking_agent.intake import compose_agent_message  # noqa: F401
    from docking_agent.api.schemas import AgentRequest

    req = AgentRequest(mode="manual", ligands_text="A:CCO")
    _, spec = intake.build_message(req)
    assert spec["decision"] == "run" and spec["authority"] == "manual"
    assert spec["params"]["exhaustiveness"] is None, "未给值 → 留空（自动规划）"
    # start 事件结构（见 api/app.py）：{"type":"start","run_id":...,"request":...,"task_spec":...}
    assert set(spec) >= {"task_type", "authority", "decision", "source"}


# 原「确定性流水线结果带 task_spec」用例已随流水线移除；Agent 路径的等价事实由上两个用例覆盖。


# --------------------------------------------------------------------------- #
# 流程控制权：服务端不再接管补齐 / 复核（回归：这几项曾被误删或反复引入）
# --------------------------------------------------------------------------- #
def test_server_no_longer_takes_over_the_flow():
    """控制权归主管 Agent：服务端不应再有「自动补齐」与「独立复核」的接管点。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "src/docking_agent/api/app.py").read_text(
        encoding="utf-8")
    assert "_needs_completion" not in src, "自动补齐的判定应已移除"
    assert "_auto_complete" not in src, "服务端不应再接管补齐"
    assert '"stage": "verify"' not in src and "'stage': 'verify'" not in src, "复核阶段应已移除"
    assert "_interleave" in src, "心跳插帧（_interleave）必须在：它是多 Agent 实时进度的唯一来源"


def test_verification_module_is_gone():
    """独立复核流程已整体移除（模块、报告章节、角色都不得残留）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    assert not (root / "src/docking_agent/verification.py").exists()
    report = (root / "src/docking_agent/reporting/report.py").read_text(encoding="utf-8")
    assert "## 8. 独立复核" not in report
    assert "verification" not in report
    from docking_agent.runtime.llm import ROLES

    assert "critic" not in ROLES, "复核角色应已从角色表移除"


def test_completeness_is_recorded_but_not_enforced(client=None):
    """完整性只作为**记录**保留（信息），不再驱动服务端行为。"""
    from docking_agent.api.app import _completeness
    from docking_agent.runs import Run
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        run = Run(Path(tmp), "R-C", "agent", {"mode": "manual"})
        state = _completeness(run, {"molecules": [], "ranking": []})
        assert state["docking"] == "not_applicable"
        state2 = _completeness(run, {"molecules": [{"smiles": "CCO"}],
                                     "docking": {"receptors": [{"results": [
                                         {"smiles": "CCO", "affinity_kcal_mol": -3.0}]}]},
                                     "ranking": [{"smiles": "CCO"}]})
        assert state2["docking"] == "agent" and state2["docked"] == 1


# --------------------------------------------------------------------------- #
# 指令里直接写了分子：必须用用户的分子，绝不能静默回退示例库
# --------------------------------------------------------------------------- #
_PROSE_WITH_MOLECULES = ("用这个受体筛一下这两个分子，帮我看看结合情况："
                         "华法林 CC(=O)CC(c1ccccc1)c1c(O)c2ccccc2oc1=O；"
                         "布洛芬 CCC(C)Cc1ccc(cc1)C(C)C(=O)O")


def test_molecules_written_in_prose_are_used_not_example_library():
    """真实踩过的坑：用户在指令里写了名称+SMILES，但表单没填分子，
    系统于是静默退回**示例分子库**，用户看到的却是别人的分子（benzamidine…）被对接。
    现在必须确定性抽取指令中的 SMILES 并直接使用。"""
    spec = intake.build_task_spec(_req(mode="manual", message=_PROSE_WITH_MOLECULES,
                                       ligands_text=""))
    assert spec["ligands"]["source"] == "message", spec["ligands"]
    assert spec["ligands"]["count"] == 2, spec["ligands"]
    assert spec["decision"] == "run"
    assert not any("示例分子库" in a for a in spec["assumptions"]), spec["assumptions"]
    extracted = "；".join(spec["ligands"]["molecules"])
    assert "华法林:CC(=O)CC(c1ccccc1)c1c(O)c2ccccc2oc1=O" in extracted
    assert "布洛芬:" in extracted

    msg = intake.render_agent_message(spec, None)
    assert "华法林:CC(=O)" in msg, "指令里必须给出可直接导入的 名称:SMILES 清单"
    assert "使用示例分子库" not in msg


def test_prose_without_any_molecule_does_not_auto_use_example_library() -> None:
    """没有任何分子信息时：不阻断，但也不再自动使用示例库（改为请用户补充）。"""
    spec = intake.build_task_spec(_req(mode="manual", message="帮我看看这个受体的成药性",
                                       ligands_text=""))
    assert spec["ligands"]["source"] == "library"
    assert spec["decision"] == "run", "缺分子不构成阻断"
    assert any("默认不使用内置示例库" in a for a in spec["assumptions"]), spec["assumptions"]


def test_explicit_example_library_request_is_honored() -> None:
    """用户**明确**要求用示例库时，渲染的指令必须让它传 allow_example_fallback=true。"""
    spec = intake.build_task_spec(_req(mode="chat", advanced=False,
                                       message="用示例库筛一下", ligands_text=""))
    assert intake.user_requested_example_library("用示例库筛一下") is True
    assert any("allow_example_fallback=true" in a for a in spec["assumptions"]), spec["assumptions"]
    msg = intake.render_agent_message(spec)
    assert "明确要求使用内置示例库" in msg and "allow_example_fallback=true" in msg, msg


def test_chat_mode_prose_molecules_also_extracted():
    spec = intake.build_task_spec(_req(mode="chat", advanced=False, message=_PROSE_WITH_MOLECULES,
                                       ligands_text=""))
    assert spec["ligands"]["source"] == "message"
    assert spec["ligands"]["count"] == 2
