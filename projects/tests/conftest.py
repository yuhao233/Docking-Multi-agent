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
from typing import Any, Iterator, List

import pytest


#: 需要**本机真实计算引擎 / 外部二进制 / 完整工作区**的测试文件。
#:
#: 这些用例会调用真实 Vina / P2Rank / pdb2pqr / meeko，或执行 `pack.sh`、`doctor.sh`
#: 并断言本机能力矩阵 —— 在缺引擎的机器上它们会静默跳过大半（覆盖面随机器漂移），
#: 在 CI 容器里则会直接失败。因此显式点名，CI 用 `DOCKING_ENGINE_TESTS=0` 跳过，
#: 本机（有引擎）仍全量跑。
#:
#: 新增引擎相关文件时把它加进来 —— `test_engine_files_still_exist()` 会防止这份名单腐坏。
ENGINE_TEST_FILES = frozenset({
    "test_agent_context.py",
    "test_api.py",
    "test_box_sizing.py",
    "test_docking_sanity.py",
    "test_external_engine.py",
    "test_ligand_receptor_chemistry.py",
    "test_ootb.py",
    "test_param_plan.py",
    "test_pockets.py",
    "test_protonation_ranking.py",
    "test_receptor_ext_upload.py",
    "test_receptor_ph.py",
    "test_receptor_race.py",
    "test_report_pdf.py",
    "test_report_skip.py",
    "test_resilience.py",
    "test_run_anomalies.py",
    "test_upload_report.py",
})

#: 关闭开关的取值（CI 用 `DOCKING_ENGINE_TESTS=0`）
_OFF = ("0", "false", "no", "off", "disabled")


def engine_tests_enabled() -> bool:
    """本机引擎相关用例是否参与运行（默认**开启**，保持既有本机行为）。"""
    return str(os.environ.get("DOCKING_ENGINE_TESTS", "1")).strip().lower() not in _OFF


def pytest_collection_modifyitems(config: pytest.Config, items: List[pytest.Item]) -> None:
    """给引擎相关文件打 `engine` 标记；开关关闭时跳过（而不是静默少跑）。"""
    for item in items:
        if Path(str(getattr(item, "fspath", ""))).name in ENGINE_TEST_FILES:
            item.add_marker(pytest.mark.engine)
    if engine_tests_enabled():
        return
    skip = pytest.mark.skip(reason="DOCKING_ENGINE_TESTS=0：需要本机引擎 / 外部二进制，故跳过")
    for item in items:
        if "engine" in item.keywords:
            item.add_marker(skip)


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


@pytest.fixture()
def run_ctx(tmp_path: Path) -> "Iterator[Any]":
    """造一个真实的 Run 上下文（工具产物写到它的目录里）。

    放在 conftest 里而不是某个用例文件里：**非引擎**用例也要能用它
    （例如搜索强度解析 `tests/test_param_resolution.py` —— 它在
    `DOCKING_ENGINE_TESTS=0` 的 CI 里同样要跑）。
    """
    from docking_agent.runs import Run, current_run
    from docking_agent.runtime.blackboard import Blackboard, current_blackboard

    run = Run(tmp_path, "R-BIG", "agent", {"mode": "manual"})
    run_token = current_run.set(run)
    board = Blackboard("R-BIG")
    board_token = current_blackboard.set(board)
    yield run, board
    current_blackboard.reset(board_token)
    current_run.reset(run_token)
