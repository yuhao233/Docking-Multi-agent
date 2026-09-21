"""schema 契约：Studio 输入面板 / API 调用方依赖的字段必须稳定存在。

`CompiledStateGraph.get_input_jsonschema()` 会把节点输入 TypedDict 转成 JSON Schema；
字段用 `total=False` 声明，因此 `required` 通常为空，这里断言的是 **properties**。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.offline

def _properties(graph) -> dict:
    schema = graph.get_input_jsonschema()
    assert isinstance(schema, dict)
    return schema.get("properties") or {}


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
