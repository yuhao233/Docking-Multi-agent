"""部署配置契约：langgraph.json、依赖一致性、图工厂可加载且不带 checkpointer。"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from docking_agent.runtime.llm import ROLES

pytestmark = pytest.mark.offline

DEPLOY_DIR = Path(__file__).resolve().parents[1]
LANGGRAPH_JSON = DEPLOY_DIR / "langgraph.json"
DEPLOY_PYPROJECT = DEPLOY_DIR / "pyproject.toml"
PROJECT_PYPROJECT = DEPLOY_DIR.parent / "projects" / "pyproject.toml"


def _expected_graph_factories() -> dict:
    """图名 → graphs.py 工厂名，**由 `ROLES` 派生**（单一事实来源）。

    原先是一份手写清单：角色表新增/改名后，langgraph.json 与它一起漂移也不会有测试发现
    （真实缺陷：`pipeline` 图删除后，清单与 smoke.py 都还留着它）。
    """
    return {role: (role if role in ("coordinator", "intake") else f"{role}_agent")
            for role in ROLES}


EXPECTED_GRAPH_FACTORIES = _expected_graph_factories()


def _load_langgraph_json() -> dict:
    return json.loads(LANGGRAPH_JSON.read_text(encoding="utf-8"))


def test_langgraph_json_matches_role_table_exactly():
    """langgraph.json 的图清单必须与 `ROLES` **完全一致**（多一个 / 少一个都失败）。"""
    cfg = _load_langgraph_json()
    assert isinstance(cfg.get("graphs"), dict)
    assert set(cfg["graphs"]) == set(EXPECTED_GRAPH_FACTORIES), (
        f"langgraph.json 与 ROLES 漂移："
        f"文件多 {sorted(set(cfg['graphs']) - set(EXPECTED_GRAPH_FACTORIES))} / "
        f"少 {sorted(set(EXPECTED_GRAPH_FACTORIES) - set(cfg['graphs']))}")
    assert len(cfg["graphs"]) == len(ROLES)


@pytest.mark.parametrize("name,factory", sorted(EXPECTED_GRAPH_FACTORIES.items()))
def test_graph_entry_file_exists_and_callable(graphs_module, name, factory):
    spec = _load_langgraph_json()["graphs"][name]
    module_path, sep, callable_name = spec.partition(":")
    assert sep == ":", f"{name} 入口应写成 './module.py:callable'，实际 {spec!r}"
    assert callable_name == factory, f"{name} 指向 {callable_name}，期望 {factory}"

    file_path = DEPLOY_DIR / module_path.lstrip("./")
    assert file_path.is_file(), f"{name} 的入口文件不存在：{file_path}"
    assert file_path == DEPLOY_DIR / "docking_graphs" / "graphs.py"

    assert hasattr(graphs_module, callable_name), f"graphs.py 缺少 {callable_name}"
    assert callable(getattr(graphs_module, callable_name))


def test_dependencies_match_projects_pyproject_exactly():
    """部署包依赖必须与 projects/pyproject.toml 逐条一致（单一事实来源）。"""
    deploy = tomllib.loads(DEPLOY_PYPROJECT.read_text(encoding="utf-8"))
    project = tomllib.loads(PROJECT_PYPROJECT.read_text(encoding="utf-8"))
    deploy_deps = set(deploy["project"]["dependencies"])
    project_deps = set(project["project"]["dependencies"])

    assert deploy_deps == project_deps, (
        "部署包依赖与项目不一致：\n"
        f"  项目有、部署包缺：{sorted(project_deps - deploy_deps)}\n"
        f"  部署包多出：{sorted(deploy_deps - project_deps)}"
    )


def test_env_and_pip_config_files_exist():
    cfg = _load_langgraph_json()
    for key in ("env", "pip_config_file"):
        assert key in cfg, f"langgraph.json 缺少 {key}"
        target = DEPLOY_DIR / cfg[key]
        assert target.is_file(), f"{key} 指向的文件不存在：{target}"
    assert DEPLOY_PYPROJECT.is_file()


@pytest.mark.parametrize("name,factory", sorted(EXPECTED_GRAPH_FACTORIES.items()))
def test_factory_returns_compiled_graph_without_checkpointer(graphs_module, name, factory):
    graph = getattr(graphs_module, factory)()
    assert hasattr(graph, "get_graph"), f"{name} 返回的对象不是已编译图：{type(graph)!r}"
    # langgraph Platform / Studio 自己托管持久化，图自带 checkpointer 会冲突。
    assert graph.checkpointer is None, f"{name} 自带了 checkpointer"


def test_agent_graphs_are_single_node_wrappers(graphs_module):
    """4 个子 Agent + coordinator 都应是「一层运行作用域」的单节点包装图。"""
    for factory in ("coordinator", "property_agent", "pocket_agent",
                    "docking_agent", "binding_agent"):
        graph = getattr(graphs_module, factory)()
        assert "agent" in graph.nodes
    assert "intake" in graphs_module.intake().nodes


def test_intake_state_covers_every_request_field(graphs_module) -> None:
    """受理 State 必须覆盖 `AgentRequest` 的**全部**字段。

    LangGraph 按 State 里声明的 channel 过滤输入：手工清单少写一个字段，
    Studio / 标准面传进来的它就被静默丢掉（真实缺陷：`conversation_id` 与
    `positive_control_decision` 曾经就是这样丢的）。现在由 `AgentRequest.model_fields` 派生，
    这条测试是防回归的兜底。
    """
    from docking_agent.api.schemas import AgentRequest

    declared = set(graphs_module.IntakeState.__annotations__)
    missing = sorted(set(AgentRequest.model_fields) - declared)
    assert missing == [], f"IntakeState 漏了 AgentRequest 的字段（会被静默丢弃）：{missing}"


def test_worker_getters_cover_all_worker_roles(graphs_module) -> None:
    """子 Agent 取用函数清单由 `ROLES` 派生 —— 不得漏角色。"""
    expected = {r for r in ROLES if r not in ("coordinator", "intake")}
    assert set(graphs_module._WORKER_GETTERS) == expected
