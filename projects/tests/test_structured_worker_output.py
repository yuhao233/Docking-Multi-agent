"""P2：子 Agent 结构化输出（`response_format=ToolStrategy(<Role>Report)`）。

覆盖：
1. 4 个报告模型覆盖既有 `_REQUIRED_KEYS`，且**额外字段原样保留**（`extra="allow"`）；
2. 4 个子 Agent 构建时都带上了对应角色的 `ToolStrategy`；
3. `invoke_worker` 优先返回 `structured_response`（序列化成 JSON 字符串，对外契约不变）；
4. 没有结构化输出时**回退到消息文本**（旧路径 / 模型不调用结构化工具时仍然工作）；
5. `_invoke_checked` 对结构化结果**一次通过、不触发重试**，而"文本路径仍会重试并在两次都坏时
   返回 `agent_output_invalid`"——后者是既有语义，必须保留。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from docking_agent.agents.reports import (BindingReport, DockingReport, PocketReport,
                                          PropertyReport, ROLE_REPORTS, is_structured_rejection,
                                          report_model, structured_output_mode,
                                          uses_structured_output)
from docking_agent.tools.dispatch import _REQUIRED_KEYS


def test_report_models_cover_required_keys() -> None:
    for role, model in ROLE_REPORTS.items():
        fields = set(model.model_fields)
        required = set(_REQUIRED_KEYS.get(role, ()))
        assert required <= fields, f"{role} 模型缺关键字段：{sorted(required - fields)}"
        assert fields >= {"status"}, role


def test_report_models_preserve_extra_fields() -> None:
    """子 Agent 的真实报告里还有 artifacts / top / summary 等字段，绝不能被 schema 吃掉。"""
    rep = PropertyReport.model_validate({
        "status": "ok", "assessment": [{"name": "乙醇"}],
        "artifacts": {"properties_file": "/tmp/p.json"}, "assessment_total": 1,
    })
    dumped = rep.model_dump()
    assert dumped["assessment"] == [{"name": "乙醇"}]
    assert dumped["artifacts"] == {"properties_file": "/tmp/p.json"}
    assert dumped["assessment_total"] == 1

    dock = DockingReport.model_validate({
        "status": "ok", "receptors": [{"receptor": "thrombin"}], "summary": {"count": 2},
        "concurrency": {"workers": 4}, "poses_saved": True,
    }).model_dump()
    assert dock["summary"] == {"count": 2} and dock["concurrency"] == {"workers": 4}
    assert dock["poses_saved"] is True

    pocket = PocketReport.model_validate({"status": "ok", "pockets": [], "engine": "p2rank"}).model_dump()
    assert pocket["engine"] == "p2rank"
    binding = BindingReport.model_validate({"status": "ok", "results": [],
                                            "positive_control": "NC(=N)c1ccccc1"}).model_dump()
    assert binding["positive_control"] == "NC(=N)c1ccccc1"


def test_report_model_lookup() -> None:
    assert report_model("property") is PropertyReport
    assert report_model("DOCKING") is DockingReport       # 大小写不敏感
    assert report_model("coordinator") is None            # 协调 Agent 不做结构化输出
    assert report_model("") is None


def test_worker_agents_pass_role_structured_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.agents import workers as W

    captured: List[Dict[str, Any]] = []

    def _capture(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return {"name": kwargs.get("name")}

    monkeypatch.setattr(W, "build_chat_llm",
                        lambda ctx=None, role="": GenericFakeChatModel(messages=iter(["ok"])))
    monkeypatch.setattr(W, "create_agent", _capture)
    W.reset_workers()
    try:
        W.init_workers(None)
    finally:
        W.reset_workers()

    assert [c["name"] for c in captured] == ["property", "pocket", "docking", "binding"]
    for c in captured:
        rf = c.get("response_format")
        assert isinstance(rf, ToolStrategy), f"{c['name']} 缺 response_format"
        assert rf.schema is ROLE_REPORTS[c["name"]], c["name"]


def test_invoke_worker_prefers_structured_response() -> None:
    from docking_agent.agents.workers import invoke_worker

    class _Agent:
        def invoke(self, payload: Any, config: Any = None, context: Any = None) -> Any:
            return {"structured_response": PropertyReport(status="ok", assessment=[{"name": "乙醇"}],
                                                          artifacts={"properties_file": "/tmp/p.json"}),
                    "messages": [AIMessage(content="这段文本不应被采用")]}

    out = json.loads(invoke_worker(_Agent(), "评估", "t1"))
    assert out["status"] == "ok" and out["assessment"] == [{"name": "乙醇"}]
    assert out["artifacts"] == {"properties_file": "/tmp/p.json"}


def test_invoke_worker_falls_back_to_text_when_no_structured_response() -> None:
    from docking_agent.agents.workers import invoke_worker

    class _Agent:
        def invoke(self, payload: Any, config: Any = None, context: Any = None) -> Any:
            return {"messages": [AIMessage(content='{"status":"ok","assessment":[]}')]}

    assert invoke_worker(_Agent(), "评估", "t1") == '{"status":"ok","assessment":[]}'


def test_invoke_checked_passes_structured_result_without_retry(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.tools import dispatch

    calls: List[str] = []
    notes: List[str] = []

    class _Board:
        def add_note(self, text: str) -> None:
            notes.append(text)

    def _fake_invoke(agent: Any, message: str, thread_id: str) -> str:
        calls.append(thread_id)
        return json.dumps({"status": "ok", "assessment": [{"name": "乙醇"}]})

    monkeypatch.setattr(dispatch, "invoke_worker", _fake_invoke)
    monkeypatch.setattr(dispatch, "active_blackboard", lambda runtime=None: _Board())

    out = json.loads(dispatch._invoke_checked(object(), "请评估", "t-property", "property"))
    assert out["assessment"] == [{"name": "乙醇"}]
    assert len(calls) == 1 and not any("retry" in c for c in calls), calls
    assert not notes, "结构化结果一次通过，不该记录重试 note"


def test_invoke_checked_still_retries_and_reports_invalid_text(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """既有语义（文本路径）必须保留：两次都不合法 → agent_output_invalid。"""
    from docking_agent.tools import dispatch

    calls: List[str] = []

    def _bad_invoke(agent: Any, message: str, thread_id: str) -> str:
        calls.append(thread_id)
        return "我无法给出 JSON"

    monkeypatch.setattr(dispatch, "invoke_worker", _bad_invoke)
    monkeypatch.setattr(dispatch, "active_blackboard", lambda runtime=None: None)

    out = json.loads(dispatch._invoke_checked(object(), "请评估", "t-property", "property"))
    assert out["status"] == "agent_output_invalid"
    assert out["required_keys"] == ["assessment"]
    assert len(calls) == 2 and calls[1].endswith("-retry"), calls


# --------------------------------------------------------------------------- #
# 6) 供应商兼容：结构化输出「能上就上」，被拒绝时必须优雅降级（真实案例：DeepSeek thinking mode）
# --------------------------------------------------------------------------- #
class _ProviderRejection(Exception):
    """模拟 OpenAI 兼容端点的 400：'Thinking mode does not support this tool_choice'。"""

    def __init__(self) -> None:
        super().__init__("Error code: 400 - {'error': {'message': "
                         "'Thinking mode does not support this tool_choice'}}")
        self.message = "Error code: 400 - Thinking mode does not support this tool_choice"


def test_structured_rejection_detection_is_narrow() -> None:
    assert is_structured_rejection(_ProviderRejection())
    assert is_structured_rejection(RuntimeError("response_format is not supported"))
    # 无关错误绝不降级（限流/超时必须照常抛出，否则会掩盖真实故障）
    assert not is_structured_rejection(RuntimeError("429 rate limit exceeded"))
    assert not is_structured_rejection(TimeoutError("timed out"))


def test_structured_output_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_STRUCTURED_OUTPUT", raising=False)
    assert structured_output_mode() == "auto" and uses_structured_output()
    monkeypatch.setenv("AGENT_STRUCTURED_OUTPUT", "off")
    assert structured_output_mode() == "off" and not uses_structured_output()
    monkeypatch.setenv("AGENT_STRUCTURED_OUTPUT", "莫名其妙")
    assert structured_output_mode() == "auto", "非法值按 auto 处理"


def _init_fake_workers(monkeypatch: pytest.MonkeyPatch) -> Any:
    from docking_agent.agents import workers as W

    monkeypatch.setattr(W, "build_chat_llm",
                        lambda ctx=None, role="": GenericFakeChatModel(messages=iter(["ok"])))
    W.reset_workers()
    W.init_workers(None)
    return W


def test_invoke_worker_falls_back_when_provider_rejects_structured(
        monkeypatch: pytest.MonkeyPatch) -> None:
    W = _init_fake_workers(monkeypatch)
    try:
        agent = W.get_property_agent()
        calls: List[str] = []

        class _Fallback:
            def invoke(self, payload: Any, config: Any = None, context: Any = None) -> Any:
                calls.append("fallback")
                return {"messages": [AIMessage(content='{"status":"ok","assessment":[]}')]}

        monkeypatch.setattr(W, "_text_fallback_agent", lambda a, role: _Fallback())
        monkeypatch.setattr(type(agent), "invoke", lambda self, *a, **k: (_ for _ in ()).throw(
            _ProviderRejection()), raising=False)

        out = W.invoke_worker(agent, "评估", "t1")
        assert out == '{"status":"ok","assessment":[]}', out
        assert calls == ["fallback"], "被拒绝后必须走文本契约图"
    finally:
        W.reset_workers()


def test_structured_rejection_is_remembered_per_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一角色的第二次调用必须**直接用文本契约图**，不再重复付一次 400 的代价。"""
    W = _init_fake_workers(monkeypatch)
    try:
        attempts: List[str] = []

        class _Structured:
            def invoke(self, payload: Any, config: Any = None, context: Any = None) -> Any:
                attempts.append("structured")
                raise _ProviderRejection()

        class _Fallback:
            def invoke(self, payload: Any, config: Any = None, context: Any = None) -> Any:
                attempts.append("text")
                return {"messages": [AIMessage(content='{"status":"ok","assessment":[]}')]}

        agent = _Structured()
        # 用**真实**的缓存逻辑：登记角色 + 该角色的文本重建器
        W._agent_roles[id(agent)] = "property"
        W._WORKER_BUILDERS["property"] = lambda **kw: _Fallback()

        assert W.invoke_worker(agent, "第一次", "t1").startswith("{")
        assert W.invoke_worker(agent, "第二次", "t2").startswith("{")
        assert attempts == ["structured", "text", "text"], attempts
    finally:
        W.reset_workers()


def test_degradation_is_recorded_in_the_run_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """降级必须**如实写进运行记录**（用户/报告能看到本次为什么没用结构化输出）。"""
    from docking_agent.runs import current_run

    W = _init_fake_workers(monkeypatch)
    logs: List[str] = []

    class _Run:
        def log(self, message: str) -> None:
            logs.append(message)

    class _Structured:
        def invoke(self, payload: Any, config: Any = None, context: Any = None) -> Any:
            raise _ProviderRejection()

    class _Fallback:
        def invoke(self, payload: Any, config: Any = None, context: Any = None) -> Any:
            return {"messages": [AIMessage(content='{"status":"ok","assessment":[]}')]}

    agent = _Structured()
    W._agent_roles[id(agent)] = "property"
    W._WORKER_BUILDERS["property"] = lambda **kw: _Fallback()

    token = current_run.set(_Run())
    try:
        W.invoke_worker(agent, "评估", "t1")
    finally:
        current_run.reset(token)
    assert logs and "降级为文本 JSON 契约" in logs[0], logs


def test_invoke_worker_reraises_unrelated_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    W = _init_fake_workers(monkeypatch)
    try:
        agent = W.get_property_agent()

        class _Boom:
            def invoke(self, payload: Any, config: Any = None, context: Any = None) -> Any:
                raise RuntimeError("429 rate limit exceeded")

        with pytest.raises(RuntimeError, match="429"):
            W.invoke_worker(_Boom(), "评估", "t1")
    finally:
        W.reset_workers()


def test_structured_output_can_be_disabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_STRUCTURED_OUTPUT", "off")
    from docking_agent.agents import workers as W

    captured: List[Dict[str, Any]] = []
    monkeypatch.setattr(W, "build_chat_llm",
                        lambda ctx=None, role="": GenericFakeChatModel(messages=iter(["ok"])))
    monkeypatch.setattr(W, "create_agent", lambda **kw: captured.append(kw) or {"name": kw.get("name")})
    W.reset_workers()
    try:
        W.init_workers(None)
    finally:
        W.reset_workers()
    assert captured and all("response_format" not in c for c in captured), captured
