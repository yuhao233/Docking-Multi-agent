"""部署层离线测试的全局隔离。

测试必须满足三条硬约束（见 deploy/README.md 与本轮任务书）：

1. **零网络 / 零 LLM / 零真实对接**：所有会联网或真算的入口都在用例里 monkeypatch 掉；
   这里只提供一组确定性的假 LLM 配置，让「构建图」这种必须发生的动作不会因为缺 key 失败。
2. **产物重定向到临时目录**：`docking_agent.paths.workspace_dir()` 每次调用都读
   `DOCKING_WORKSPACE`，但 `runs.RunStore` 在**构造时**就把 `runs_dir()` 固定下来了，
   且 `docking_agent.runs.get_run_store()` 用模块级 `_store` 做单例缓存。
   因此每个用例都：先 `monkeypatch.setenv("DOCKING_WORKSPACE", tmp_path)`，
   再把 `docking_agent.runs._store` 置回 None（下一次 `get_run_store()` 才会用新工作区）。
   本文件顶层还会把会话级工作区指到系统临时目录，防止 collection 阶段（导入
   `docking_graphs` / `reporting` / matplotlib）在 `projects/` 下写入任何东西。
3. **不污染项目**：`projects/` 只读（比对 pyproject、读 config），不写入。

`blockbuster` 回归用例见 `test_blockbuster.py`。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# 必须在导入 docking_agent / docking_graphs **之前**完成的环境隔离
# --------------------------------------------------------------------------- #
DEPLOY_DIR = Path(__file__).resolve().parents[1]
PROJECTS_DIR = DEPLOY_DIR.parent / "projects"

# 会话级兜底工作区：collection 阶段就会 import graphs.py → reporting → matplotlib，
# 此时 per-test fixture 还没运行；把工作区与 matplotlib 缓存都指到系统临时目录，
# 避免在 projects/ 或 ~/.config 下创建目录。
_SESSION_WORKSPACE = Path(tempfile.mkdtemp(prefix="docking-deploy-tests-"))
os.environ["DOCKING_WORKSPACE"] = str(_SESSION_WORKSPACE)
os.environ["MPLCONFIGDIR"] = str(_SESSION_WORKSPACE / "mplcache")
# 确定性假 LLM 配置：ChatOpenAI 只构造、不调用，所以不需要真实端点/密钥。
os.environ.setdefault("LLM_API_KEY", "offline-test-key")
os.environ.setdefault("LLM_MODEL", "offline-test-model")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9/v1")
# 与 projects/tests/conftest.py 同一套离线开关。
os.environ["INTAKE_LLM"] = "off"
os.environ["INCHIKEY_ONLINE"] = "off"
os.environ["LOCAL_SETTINGS_PATH"] = str(_SESSION_WORKSPACE / "local_settings.json")


# --------------------------------------------------------------------------- #
# fixture
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把本次用例的运行产物锁进 `tmp_path`（并清掉 RunStore 单例）。"""
    monkeypatch.setenv("DOCKING_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "mplcache"))
    import docking_agent.runs as runs

    # 关键：RunStore() 构造时确定 root；不清空单例就会沿用上一个工作区。
    monkeypatch.setattr(runs, "_store", None, raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def fresh_graph_caches() -> None:
    """清掉模块级图缓存与子 Agent 缓存，避免用例之间相互串味。"""
    from docking_agent.agents import workers as workers_mod
    from docking_graphs import graphs as graphs_mod

    workers_mod.reset_workers()
    graphs_mod._coordinator = None
    graphs_mod._workers.clear()
    yield
    workers_mod.reset_workers()
    graphs_mod._coordinator = None
    graphs_mod._workers.clear()


@pytest.fixture
def graphs_module():
    """导入并返回 `docking_graphs.graphs`（延迟导入，确保上面的环境变量先生效）。"""
    from docking_graphs import graphs

    return graphs
