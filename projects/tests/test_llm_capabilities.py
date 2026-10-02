"""供应商能力记忆：避免每次运行都先触发一次 400 再降级。

运行日志片段：

    [15:25:17] pocket 子 Agent：供应商拒绝结构化输出（thinking 模式不支持强制 tool_choice），
               已降级为文本 JSON 契约（结果仍经必需字段校验）

技术原因：该端点与模型在 thinking 语义下只拒绝「强制」tool_choice（`ToolStrategy` 依赖该项），
而自动 tool_choice 与 JSON 模式均正常。因此对策是「预先判定 + 降级」，而不是每轮重试：

* 首次被拒后写入 `var/state/llm_capabilities.json`，按 base_url+model+thinking 区分；
* 之后构建子 Agent 不再挂载 ToolStrategy，也不再输出降级日志；
* 降级优先使用 JSON 模式（供应商保证返回合法 JSON），失败时才退回纯文本契约。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from docking_agent.agents import capabilities as CAP

# --------------------------------------------------------------------------- #
# 脚手架：把状态文件与角色配置都指向临时位置
# --------------------------------------------------------------------------- #
@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """能力状态写进临时目录；角色配置固定为「同一端点 + 同一模型 + thinking 关闭」。"""
    monkeypatch.setenv("DOCKING_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("AGENT_STRUCTURED_OUTPUT", raising=False)
    monkeypatch.setattr(CAP, "role_setting", lambda role, field: "")
    monkeypatch.setattr(CAP, "capability_key", lambda role: "https://api.example|test-model|disabled")
    # conftest 已把状态文件统一指到会话临时目录；这里为本用例再给一个独立文件，便于断言落盘内容
    monkeypatch.setattr(CAP, "_state_file", lambda: tmp_path / CAP.STATE_FILE_NAME)
    return tmp_path


# --------------------------------------------------------------------------- #
# 1) 能力未知时先探测；记入记忆后不再探测
# --------------------------------------------------------------------------- #
def test_unknown_capability_is_tried_first(isolated: Path) -> None:
    assert CAP.forced_tool_choice_supported("pocket") is True
    assert CAP.structured_output_decision("pocket") == "tool"


def test_rejection_is_remembered_and_switches_to_json_mode(isolated: Path) -> None:
    CAP.mark_forced_tool_choice_unsupported("pocket", "Thinking mode does not support this tool_choice")
    assert CAP.forced_tool_choice_supported("pocket") is False
    assert CAP.structured_output_decision("pocket") == "json_mode"
    # 记忆落盘：后续进程不会再次触发同一个 400
    state_file = isolated / CAP.STATE_FILE_NAME
    assert state_file.is_file()
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["https://api.example|test-model|disabled"]["forced_tool_choice"] is False
    assert "tool_choice" in saved["https://api.example|test-model|disabled"]["reason"]


def test_memory_expires_so_capability_is_re_probed(isolated: Path) -> None:
    """供应商可能升级：记忆过期后重新探测一次，不永久禁用该能力。"""
    CAP.mark_forced_tool_choice_unsupported("pocket", "x")
    state_file = isolated / CAP.STATE_FILE_NAME
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    for entry in saved.values():
        entry["at"] = 0.0                       # 很久以前
    state_file.write_text(json.dumps(saved), encoding="utf-8")
    assert CAP.forced_tool_choice_supported("pocket") is True


# --------------------------------------------------------------------------- #
# 2) 设置项优先级：环境变量与按角色设置都能压过记忆
# --------------------------------------------------------------------------- #
def test_env_and_role_settings_override(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    CAP.mark_forced_tool_choice_unsupported("pocket", "x")
    assert CAP.structured_output_decision("pocket") == "json_mode"

    monkeypatch.setenv("AGENT_STRUCTURED_OUTPUT", "on")
    assert CAP.structured_output_decision("pocket") == "tool", "on = 无视记忆强制试一次"

    monkeypatch.setenv("AGENT_STRUCTURED_OUTPUT", "off")
    assert CAP.structured_output_decision("pocket") == "text", "off = 完全不用结构化输出"

    monkeypatch.delenv("AGENT_STRUCTURED_OUTPUT")
    monkeypatch.setattr(CAP, "role_setting", lambda role, field: "off" if field == "structured_output" else "")
    assert CAP.structured_output_decision("pocket") == "text"


def test_setting_field_is_exposed_per_role() -> None:
    from docking_agent.settings import SPEC_BY_PATH

    spec = SPEC_BY_PATH["roles.pocket.structured_output"]
    assert spec.kind == "enum" and set(spec.choices) == {"", "auto", "on", "off"}
    # 四个子 Agent 与协调 Agent 都带该字段
    for role in ("coordinator", "pocket", "property", "docking", "binding"):
        assert f"roles.{role}.structured_output" in SPEC_BY_PATH


# --------------------------------------------------------------------------- #
# 3) 构建期不再挂载 ToolStrategy（这条对应使用者可见的问题）
# --------------------------------------------------------------------------- #
def test_worker_build_skips_forced_tool_choice_after_rejection(
        isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.agents import workers as W

    CAP.mark_forced_tool_choice_unsupported("pocket", "Thinking mode does not support this tool_choice")
    captured: Dict[str, Any] = {}

    def fake_create_agent(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return {"fake": True}

    monkeypatch.setattr(W, "create_agent", fake_create_agent)
    monkeypatch.setattr(W, "build_agent_middleware", lambda *_a, **_k: [])
    monkeypatch.setattr(W, "_build_llm", lambda ctx=None, role="": _FakeLLM())
    W.reset_workers()
    try:
        W._build_role_agent("pocket", None)
        assert "response_format" not in captured, "已记录不支持时不得再挂 ToolStrategy（否则又付一次 400）"
        assert W.worker_structured_decisions().get("pocket") == "json_mode"
    finally:
        W.reset_workers()


def test_worker_build_uses_tool_strategy_when_supported(isolated: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from docking_agent.agents import workers as W

    captured: Dict[str, Any] = {}

    def fake_create_agent(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return {"fake": True}

    monkeypatch.setattr(W, "create_agent", fake_create_agent)
    monkeypatch.setattr(W, "build_agent_middleware", lambda *_a, **_k: [])
    monkeypatch.setattr(W, "_build_llm", lambda ctx=None, role="": _FakeLLM())
    W.reset_workers()
    try:
        W._build_role_agent("pocket", None)
        assert "response_format" in captured, "能力未知时应先走框架强制结构化输出"
        assert W.worker_structured_decisions().get("pocket") == "tool"
    finally:
        W.reset_workers()


class _FakeLLM:
    """最小可绑定对象：`create_agent` 与中间件都被打桩，只需支持绑定类方法。"""

    def bind(self, **_kwargs: Any) -> "_FakeLLM":
        return self

    def with_retry(self, **_kwargs: Any) -> "_FakeLLM":
        return self

    def with_config(self, **_kwargs: Any) -> "_FakeLLM":
        return self
