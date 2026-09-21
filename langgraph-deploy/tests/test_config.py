"""部署配置契约：langgraph.json、依赖一致性、图工厂可加载且不带 checkpointer。"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.offline

DEPLOY_DIR = Path(__file__).resolve().parents[1]
LANGGRAPH_JSON = DEPLOY_DIR / "langgraph.json"
DEPLOY_PYPROJECT = DEPLOY_DIR / "pyproject.toml"
PROJECT_PYPROJECT = DEPLOY_DIR.parent / "projects" / "pyproject.toml"

# langgraph.json 的 6 个入口：图名 -> graphs.py 里的工厂函数名
EXPECTED_GRAPH_FACTORIES = {
    "coordinator": "coordinator",
    "intake": "intake",
    "property": "property_agent",
    "pocket": "pocket_agent",
    "docking": "docking_agent",
    "binding": "binding_agent",
}


def _load_langgraph_json() -> dict:
    return json.loads(LANGGRAPH_JSON.read_text(encoding="utf-8"))


def test_langgraph_json_parses_and_declares_six_graphs():
    cfg = _load_langgraph_json()
    assert isinstance(cfg.get("graphs"), dict)
    assert set(cfg["graphs"]) == set(EXPECTED_GRAPH_FACTORIES)
    assert len(cfg["graphs"]) == 6


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
