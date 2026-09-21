"""schema 契约：Studio 输入面板 / API 调用方依赖的字段必须稳定存在。

`CompiledStateGraph.get_input_jsonschema()` 会把节点输入 TypedDict 转成 JSON Schema；
字段用 `total=False` 声明，因此 `required` 通常为空，这里断言的是 **properties**。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.offline

# 文档承诺的 pipeline 输入字段（见 deploy/README.md §3 与 projects/docs）
PIPELINE_INPUT_FIELDS = {
    "ligands_text", "molecule_file", "receptor", "receptor_file", "positive_control",
    "exhaustiveness", "n_poses", "engine", "pocket_engine", "site_center", "site_size",
    "save_poses", "max_ligands", "protonation", "protonation_ph", "allow_example_fallback",
}


def _properties(graph) -> dict:
    schema = graph.get_input_jsonschema()
    assert isinstance(schema, dict)
    return schema.get("properties") or {}


def test_pipeline_input_schema_exposes_documented_fields(graphs_module):
    props = _properties(graphs_module.pipeline())
    missing = PIPELINE_INPUT_FIELDS - set(props)
    assert not missing, f"pipeline 输入 schema 缺字段：{sorted(missing)}"


def test_pipeline_input_schema_output_fields_also_present(graphs_module):
    """单节点图输入/输出同 schema，Studio 输出面板要能看到 run_id / run_dir / artifacts。"""
    props = _properties(graphs_module.pipeline())
    for field in ("run_id", "run_dir", "artifacts", "status", "top"):
        assert field in props, f"pipeline schema 缺输出字段 {field}"


def test_coordinator_input_schema_has_messages(graphs_module):
    props = _properties(graphs_module.coordinator())
    assert "messages" in props
    assert "run_id" in props and "run_dir" in props


def test_intake_input_schema_fields(graphs_module):
    props = _properties(graphs_module.intake())
    for field in ("message", "mode", "use_llm"):
        assert field in props, f"intake schema 缺字段 {field}"
    # 受理层的结构化输入也应在面板可见
    for field in ("receptor", "ligands_text", "advanced", "task_spec", "agent_message"):
        assert field in props, f"intake schema 缺字段 {field}"


@pytest.mark.parametrize("factory", ["coordinator", "property_agent", "pocket_agent",
                                     "docking_agent", "binding_agent"])
def test_agent_wrapper_output_schema_has_messages_and_run_meta(graphs_module, factory):
    props = _properties(getattr(graphs_module, factory)())
    assert "messages" in props
    assert "run_id" in props and "run_dir" in props
