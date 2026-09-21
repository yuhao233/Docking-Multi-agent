"""pytest 全局隔离。

`ensure_runtime_env()` 会把 `config/local_settings.json`（设置页面保存的本地覆盖）
里的运行类参数写进环境变量 —— 这对真实运行是对的，但会让测试结果受「开发者本机设置」影响。
因此整个测试会话默认把设置文件指到一个不存在的临时路径；
需要测试设置文件本身的用例（tests/test_settings.py）再自行覆盖 `LOCAL_SETTINGS_PATH`。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_local_settings():
    tmp = Path(tempfile.mkdtemp(prefix="docking-settings-")) / "local_settings.json"
    previous = os.environ.get("LOCAL_SETTINGS_PATH")
    os.environ["LOCAL_SETTINGS_PATH"] = str(tmp)
    # 受理层默认不调用模型（确定性规约已足够；需要测 LLM 路径的用例自行 monkeypatch）
    os.environ["INTAKE_LLM"] = "off"
    # InChIKey 在线反查默认关：测试必须零网络（需要测在线路径的用例自行 monkeypatch）
    os.environ["INCHIKEY_ONLINE"] = "off"
    from docking_agent.config import ensure_runtime_env

    ensure_runtime_env()
    yield
    if previous is None:
        os.environ.pop("LOCAL_SETTINGS_PATH", None)
    else:
        os.environ["LOCAL_SETTINGS_PATH"] = previous
    os.environ.pop("INTAKE_LLM", None)
    os.environ.pop("INCHIKEY_ONLINE", None)

@pytest.fixture(autouse=True)
def _isolate_llm_capabilities(tmp_path_factory: pytest.TempPathFactory,
                             monkeypatch: pytest.MonkeyPatch) -> None:
    """供应商能力记忆（`var/state/llm_capabilities.json`）在测试里写到临时目录。

    真实工作区里可能已经记下"该模型不支持强制 tool_choice"（那是真实探测结果），
    若测试直接读它，构建期行为会随本机历史漂移 —— 测试必须从"能力未知"开始。
    """
    from docking_agent.agents import capabilities as CAP

    state = tmp_path_factory.mktemp("llm_caps") / "llm_capabilities.json"
    monkeypatch.setattr(CAP, "_state_file", lambda: state)
