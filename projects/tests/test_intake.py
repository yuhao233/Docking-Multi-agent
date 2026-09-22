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


def test_missing_library_is_recorded_and_blocks() -> None:
    """分子库是**必需项**：没给就只提问（零工具调用），并如实记进 missing。

    2026-09-17 起：内置示例库/示例受体只在用户明确要求时使用（产品要求）；
    2026-09-21 起：契约收紧为「必需信息不齐 → 只提问、零工具调用」，
    必需项 = 受体 / 分子库 / 任务类型，因此缺分子库同样是 ask。
    """
    spec = intake.build_task_spec(_req(ligands_text=""))
    assert spec["ligands"]["source"] == "library"
    assert spec["missing"] == ["ligands"], "分子库缺失必须如实记录"
    assert spec["decision"] == "ask", "缺必需项 → 只提问，不得开跑"
    assert any("候选分子库" in q for q in spec["questions"]), spec["questions"]
    joined = " ".join(spec["assumptions"])
    assert "默认不使用内置示例库" in joined, joined
    assert intake.user_requested_example_library("") is False


def test_explicit_example_library_counts_as_a_ligand_source() -> None:
    """用户**明确同意**用示例库 = 分子库来源已确定 → 不因缺分子而 ask。"""
    spec = intake.build_task_spec(_req(ligands_text="", message="就用药物的示例分子库跑一遍"))
    assert spec["ligands"]["source"] == "library"
    assert intake._resolvable_ligands(spec) is True
    assert spec["decision"] == "run", "用户已明确同意示例库 → 分子库来源已确定，不应再问"


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
    spec = intake.build_task_spec(_req(mode="chat", advanced=True,
                                       message="请用 trypsin 做筛选", ligands_text="A:CCO"))
    assert spec["receptor"]["name"] == "trypsin" and spec["receptor"]["source"] == "user"
    assert spec["decision"] == "run"

    default = intake.build_task_spec(_req(mode="chat", advanced=True,
                                          message="帮我筛选一下", ligands_text="A:CCO"))
    assert default["receptor"]["source"] == "default"
    # 未指定受体 → 受理层只提问、不计算（已无任何「默认受体」兜底）
    assert default["decision"] == "ask"
    assert not any("默认受体" in a for a in default["assumptions"])
    assert "receptor" in default["missing"]


def test_chat_default_receptor_is_not_rendered_as_user_specified() -> None:
    """真实缺陷回归：chat 折叠时受理层判定「未指定受体」并标了 source=default，
    但渲染层却给出「受体：thrombin」的权威口吻，主管 Agent 于是把示例默认受体当成用户指定。

    修复后（用户明确要求：彻底删除默认受体回退）：未指定受体 → decision=ask，
    渲染成「只提问、零工具调用」，且**任何预置受体名都不得出现在消息里**。"""
    spec = intake.build_task_spec(_req(mode="chat", advanced=False, ligands_text="",
                                       message="帮我筛一下这两个分子 CC(=O)O"))
    assert spec["receptor"]["source"] == "default"
    assert spec["decision"] == "ask"
    msg = intake.render_agent_message(spec)
    assert "thrombin" not in msg and "1DWC" not in msg and "trypsin" not in msg, msg
    assert "未指定受体" in msg
    assert "不要回退任何默认受体" in msg
    assert "不要调用任何计算工具" in msg
    assert "decision=ask" in msg

    # 用户明确指定 trypsin 时：仍按用户指定渲染，行为不变
    user = intake.build_task_spec(_req(mode="chat", advanced=False, ligands_text="",
                                       message="用 trypsin 做一次筛选：A:CCO"))
    assert user["receptor"] == {"name": "trypsin", "file": "", "source": "user"}
    assert "受体：trypsin" in intake.render_agent_message(user)
    assert "未指定受体" not in intake.render_agent_message(user)


def test_unspecified_receptor_is_asked_not_defaulted(monkeypatch) -> None:
    """未指定受体 → 停下来提问，**不得**替用户挑默认受体（旧行为已删除）。

    旧实现把这个默认名还原成空串，交给对接工具回退内建默认受体并写「未指定受体，已默认使用…」的
    note；用户明确要求彻底删除这条回退，因此现在：受理层判 ask，工具层直接拒绝执行。
    """
    from docking_agent.tools.docking import molecular_docking

    class _Run:
        data = {"task_spec": {"receptor": {"name": "thrombin", "source": "default"}}}

    monkeypatch.setattr("docking_agent.tools.docking.active_run", lambda runtime=None: _Run())
    out = molecular_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]',
                                 receptor_sources="thrombin")
    payload = json.loads(out)
    assert payload["status"] == "needs_user_input"
    assert payload.get("missing") == ["receptor"]
    assert "未指定受体" in payload["message"]
    # 预置受体只供内部测试，不再作为用户可选来源下发
    assert not payload.get("choices")

def test_rendered_message_states_decision_and_role_boundary():
    spec = intake.build_task_spec(_req(mode="manual", message="对接并排序"))
    msg = intake.render_agent_message(spec)
    assert spec["decision"] == "run"
    assert "--- 任务规约（受理层产出；编排层只读，不要修改）---" in msg
    assert "decision=run" in msg
    assert "不得在中途停下来询问用户" in msg
    assert "不要重新解析用户语言" in msg
    # 用户已经给了分子（manual + ligands_text）→ 不得再指示回退示例库
    # （原先写的是 `msg.count("allow_example_fallback") >= 0`：恒真，什么也没守住）
    assert "allow_example_fallback=true" not in msg, "用户已给出分子，不得再指示回退示例库"


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
    spec = intake.build_task_spec(_req(mode="chat", advanced=True, message="用 1DWC 做一次筛选"))
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
    """回归：模型把「缺什么」写进 missing 只是**记录**，不得改变受理层的判定。

    （必需项缺失由确定性规则判：`_can_ask` 只看受体来源与分子来源，从不读 `missing`。
    实时评估（scripts/intake_eval.py --live）曾抓到模型用 missing 让 5 个应跑的用例变成 ask。）
    """
    monkeypatch.setenv("INTAKE_LLM", "on")
    llm_spy["install"]({"missing": ["候选分子库", "目标受体文件"],
                        "questions": ["请问用哪个受体？"]})
    req = _req(mode="chat", advanced=True, message="用 1DWC 帮我筛一下这个库",
               ligands_text="A:CCO")
    _, spec = intake.build_message(req)
    assert spec["decision"] == "run", "missing 只是记录，不能阻断"
    assert spec["receptor"]["source"] == "user"
    assert spec["missing"], "如实保留模型记录的缺失项"
    # 真实缺陷回归：decision=run 时**不得**把「缺少 / 待向用户确认」下发给编排层。
    # 现场：模型把「用户点名了分子但没给 SMILES」记进 missing 并附「请提供候选分子库」，
    # 渲染层原样转述 → 主管 Agent 当场问一遍分子库，随后又让用户选结构（同轮两次提问）。
    msg = intake.render_agent_message(spec)
    assert "待向用户确认" not in msg and "请问用哪个受体？" not in msg
    assert "缺少：" not in msg


def test_llm_explicit_needs_user_input_becomes_ask(llm_spy, monkeypatch):
    """只有模型明确要求补充、且用户没给出任何可识别分子来源时才 ask。"""
    monkeypatch.setenv("INTAKE_LLM", "on")
    llm_spy["install"]({"needs_user_input": True,
                        "questions": ["请问您想分析哪个分子？（可给名称或 SMILES）"]})
    req = _req(mode="chat", advanced=True, message="帮我分析一下那个化合物", ligands_text="")
    _, spec = intake.build_message(req)
    assert spec["decision"] == "ask"
    msg = intake.render_agent_message(spec)
    assert "请问您想分析哪个分子" in msg
    assert "待向用户确认" in msg, "decision=ask 时问题必须下发给编排层（否则用户看不到）"


def test_needs_user_input_is_ignored_when_ligands_are_known(llm_spy, monkeypatch):
    """用户已经给了分子/文件时，即使模型要求补充也必须跑（不能凭空阻断）。"""
    monkeypatch.setenv("INTAKE_LLM", "on")
    llm_spy["install"]({"needs_user_input": True,
                        "mentioned_molecules": ["阿司匹林"]})
    req = _req(mode="chat", advanced=True, message="用 1DWC 对接阿司匹林", ligands_text="")
    _, spec = intake.build_message(req)
    assert spec["ligands"]["source"] == "mentioned"
    assert spec["decision"] == "run"


def test_llm_invalid_json_falls_back_to_rules(llm_spy, monkeypatch):
    monkeypatch.setenv("INTAKE_LLM", "on")
    llm_spy["install"]("抱歉，我不太确定你的意思")
    req = _req(mode="chat", advanced=True, message="用 1DWC 筛一下", ligands_text="A:CCO")
    _, spec = intake.build_message(req)
    assert spec["llm_status"] == "invalid_json"
    assert spec["decision"] == "run" and spec["source"] == "rules"


def test_llm_error_falls_back_to_rules(llm_spy, monkeypatch):
    monkeypatch.setenv("INTAKE_LLM", "on")
    llm_spy["install"]({}, raise_error=True)
    req = _req(mode="chat", advanced=True, message="用 1DWC 筛一下", ligands_text="A:CCO")
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

    spec = {"task_type": "docking_only", "authority": "chat+advanced", "decision": "ask",
            "source": "rules+llm",
            "assumptions": ["未指定受体 → 不执行任何计算，先请用户指定受体"],
            "questions": ["请指定受体：① 提供 PDB 编号 / UniProt accession；② 上传结构文件。"]}
    md = build_markdown_report({"ranking": [], "molecules": [], "binding": {},
                                "task_spec": spec}, kind="agent", run_id="R1")
    assert "任务受理" in md
    assert "docking_only" in md and "chat+advanced" in md and "rules+llm" in md
    assert "未指定受体" in md


def test_start_event_payload_shape_has_task_spec():
    """受理结论要能随 SSE start 事件下发（API 层直接取 run.data['task_spec']）。"""
    from docking_agent.intake import compose_agent_message  # noqa: F401
    from docking_agent.api.schemas import AgentRequest

    req = AgentRequest(mode="manual", receptor="1DWC", ligands_text="A:CCO")
    _, spec = intake.build_message(req)
    assert spec["decision"] == "run" and spec["authority"] == "manual"
    assert spec["missing"] == [], "受体与分子都给了 → 必需项齐全"
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

    root = Path(__file__).resolve().parent.parent
    api_dir = root / "src/docking_agent/api"
    # 第 2 波拆分后，SSE 心跳（_interleave）在 api/agent_flow.py，路由在 routers/*，
    # 因此扫**整个 api 包**而不是单个文件。
    src = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted([api_dir / "app.py", api_dir / "support.py", api_dir / "agent_flow.py"]
                        + list((api_dir / "routers").glob("*.py"))))
    assert "_needs_completion" not in src, "自动补齐的判定应已移除"
    assert "_auto_complete" not in src, "服务端不应再接管补齐"
    assert '"stage": "verify"' not in src and "'stage': 'verify'" not in src, "复核阶段应已移除"
    assert "_interleave" in src, "心跳插帧（_interleave）必须在：它是多 Agent 实时进度的唯一来源"


def test_verification_module_is_gone():
    """独立复核流程已整体移除（模块、报告章节、角色都不得残留）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    assert not (root / "src/docking_agent/verification.py").exists()
    # 报告模板第 2 波已按章节拆到多个模块，因此扫**整个 reporting 包**而不是单个文件
    reporting_dir = root / "src/docking_agent/reporting"
    report = "\n".join(p.read_text(encoding="utf-8") for p in sorted(reporting_dir.glob("*.py")))
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
    """没有任何分子信息时：**只提问**（分子库是必需项），且绝不自动使用示例库。"""
    spec = intake.build_task_spec(_req(mode="manual", message="帮我看看这个受体的成药性",
                                       ligands_text=""))
    assert spec["ligands"]["source"] == "library"
    assert spec["decision"] == "ask", "缺分子库 = 必需信息不齐 → 只提问、零工具调用"
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


# --------------------------------------------------------------------------- #
# 用例集脚本本身必须是被看护的（曾静默失效：期望值停在旧默认 6，只剩 13/14 通过）
# --------------------------------------------------------------------------- #
def test_intake_eval_script_passes_all_its_cases() -> None:
    """`scripts/intake_eval.py` 是受理层的人工取证脚本 —— 它**不在自动门禁里**，
    因此期望值会悄悄过期（真实缺陷：折叠表单那条一直期望 `exhaustiveness=6`，
    而系统默认 v0.24 起已是 16，脚本只报「未命中 1 项」，没人发现）。
    """
    import subprocess

    proc = subprocess.run([sys.executable, "scripts/intake_eval.py"],
                          cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "全部用例符合预期" in proc.stdout, proc.stdout[-2000:]


# --------------------------------------------------------------------------- #
# 界面点选（多组分分子的「代表结构」）必须确定性续跑
#
# 真实反馈：「我选择了，但没有正常工作」——点选只把选项文案当普通消息发回，
# 后端于是按名称重新查询、又是多组分 → 把同一个问题再问一次；追问若丢了受体还会被判 ask。
# --------------------------------------------------------------------------- #
_MANCOZEB = "C(CNC(=S)[S-])NC(=S)[S-].C(CNC(=S)[S-])NC(=S)[S-].[Mn+2].[Zn+2]"


def test_selected_representative_structure_is_deterministic() -> None:
    """带 `molecule_choice` 时：直接按该结构导入，不再查询/不再询问，且能判 run。"""
    # 注意：chat 模式下 `receptor` 表单字段**不是**权威来源（预填值不算用户意图），
    # 受体要么写在指令里、要么由多轮继承 —— 这里按真实追问的写法带上 accession。
    spec = intake.build_task_spec(_req(
        mode="chat", advanced=False, message="用 Q9SJQ6 继续", ligands_text="",
        molecule_choice=_MANCOZEB, molecule_choice_decision="raw-mixture",
        molecule_choice_label="Mancozeb（CID 3034368）· PubChem 原始多组分结构"))
    ligands = spec["ligands"]
    assert ligands["source"] == "choice"
    assert ligands["count"] == 1 and ligands["molecules"][0]["smiles"] == _MANCOZEB
    assert ligands["decision"]["mode"] == "raw-mixture" and ligands["decision"]["by"] == "user"
    assert spec["decision"] == "run", "来源已明确（用户点选）时不得再判 ask"
    assert any("用户在界面选定" in a for a in spec["assumptions"]), spec["assumptions"]

    msg = intake.render_agent_message(spec)
    assert "用户在界面选定" in msg and "不要再询问代表结构" in msg, msg


def test_selected_structure_without_receptor_is_still_ask() -> None:
    """底线不变：分子已选定但受体仍缺失 → 只提问、零计算（不能拿默认受体开跑）。"""
    spec = intake.build_task_spec(_req(
        mode="chat", advanced=False, message="代森锰锌 · 原始多组分结构", ligands_text="",
        receptor="",
        molecule_choice=_MANCOZEB, molecule_choice_decision="organic-fragment"))
    assert spec["decision"] == "ask"
    assert (spec["receptor"] or {}).get("source") == "default"
    assert spec["ligands"]["source"] == "choice", "分子侧仍然如实记录用户的选择"


def test_followup_inherits_receptor_named_earlier_by_gene_symbol() -> None:
    """多轮续跑：上一轮用户只写了「拟南芥ROS1」（没有 酶/蛋白/受体 后缀）也要能继承受体。

    真实缺陷：`_NAMED_RECEPTOR_RE` 只认带后缀的名字，于是「拟南芥ROS1」既不算「点名了受体」，
    也让多轮继承拿不到上一轮已解析出的 accession/PDB → 用户点选分子代表结构后的追问被判 ask。
    """
    prior = [
        {"role": "user", "content": "把拟南芥ROS1和小分子代森锰锌对接"},
        {"role": "assistant", "content": ("**受体已解析成功**：拟南芥 ROS1 = UniProt **Q9SJQ6**"
                                          "，实验结构取自 RCSB **7YHP**。配体侧请点选代表结构。")},
    ]
    spec = intake.build_task_spec(_req(
        mode="chat", advanced=False, message="代森锰锌 · PubChem 原始多组分结构", ligands_text="",
        receptor="", molecule_choice=_MANCOZEB, molecule_choice_decision="raw-mixture",
        molecule_choice_label="PubChem 原始多组分结构"), prior_turns=prior)
    assert spec["decision"] == "run", "受体可从上一轮继承时，续跑必须真的跑起来"
    assert (spec["receptor"] or {}).get("source") == "user"
    assert (spec["receptor"] or {}).get("name") in ("7YHP", "Q9SJQ6")
    assert spec["ligands"]["source"] == "choice"


def test_prior_gene_symbol_does_not_hijack_unrelated_conversations() -> None:
    """反面：上一轮没点名受体、只提到 ADMET/PDB 之类的缩写时，不得继承出受体。"""
    prior = [
        {"role": "user", "content": "帮我看下这些分子的 ADMET 和 CSV 报表"},
        {"role": "assistant", "content": "受体未指定，请提供 PDB 编号（如 3ZBF）。"},
    ]
    spec = intake.build_task_spec(_req(mode="chat", advanced=False, message="继续", ligands_text="",
                                       receptor=""), prior_turns=prior)
    assert (spec["receptor"] or {}).get("source") == "default", spec["receptor"]
    assert spec["decision"] == "ask"
