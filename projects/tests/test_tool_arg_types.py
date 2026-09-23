"""小参数类型化（P1）：坐标与枚举的 schema + 向后兼容。

口径（用户已确认）：**只类化小参数**（位点盒坐标 / 引擎枚举），
`molecules_json` / `*_file` 的「路径即总线」契约保持字符串不变。

覆盖：
1. `floats_to_text` 的规范化与严格性（个数不符/脏数据 → 空串，绝不产出半个盒子）；
2. 工具 schema 里坐标是 **array(number)** 而不是 string（模型看到的是结构化类型）；
3. 旧调用方传字符串仍然工作（不破坏 CLI / 既有提示词 / 测试）；
4. 非法坐标会得到**如实的提示**，而不是被静默当成"没给位点"。
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from docking_agent.tools.schemas import floats_to_text
from support.fake_llm import parse_agent_params


def test_floats_to_text_normalizes_all_accepted_forms() -> None:
    assert floats_to_text([31.5, 13.74, 24.36], expect=3) == "31.5,13.74,24.36"
    assert floats_to_text("31.5,13.74,24.36", expect=3) == "31.5,13.74,24.36"
    assert floats_to_text("31.5 13.74 24.36", expect=3) == "31.5,13.74,24.36"
    assert floats_to_text("31.5;13.74;24.36", expect=3) == "31.5,13.74,24.36"
    assert floats_to_text([22, 22, 22], expect=3) == "22,22,22"
    assert floats_to_text((22.0, 22.0, 22.0), expect=3) == "22,22,22"
    assert floats_to_text([31.0, 13.0]) == "31,13"      # 不校验个数时按原样规范化


def test_floats_to_text_is_strict_when_count_is_required() -> None:
    for bad in (None, "", "   ", "abc", [1, 2], [1, 2, 3, 4], "1,2", "1,2,x"):
        assert floats_to_text(bad, expect=3) == "", bad
    assert floats_to_text([], expect=3) == ""


@pytest.mark.parametrize("tool_name,field", [
    ("run_docking", "site_center"), ("run_docking", "site_size"),
    ("molecular_docking", "site_center"), ("molecular_docking", "site_size"),
    ("set_docking_site", "center"), ("set_docking_site", "size"),
])
def test_site_fields_are_array_typed_in_schema(tool_name: str, field: str) -> None:
    import importlib

    module = {"run_docking": "docking_agent.agents.dispatch",
              "molecular_docking": "docking_agent.tools.docking",
              "set_docking_site": "docking_agent.tools.pockets"}[tool_name]
    tool = getattr(importlib.import_module(module), tool_name)
    # 注意：这些工具带注入的 ToolRuntime，`get_input_jsonschema()` 会因 dataclass 里的
    # 可调用字段报 PydanticInvalidForJsonSchema；模型侧真正的 schema 是 tool_call_schema
    prop = tool.tool_call_schema.model_json_schema()["properties"][field]
    variants = prop.get("anyOf") or [prop]
    assert any(v.get("type") == "array" and v.get("items", {}).get("type") == "number"
               for v in variants), f"{tool_name}.{field} 应为 number 数组：{prop}"


def test_engine_enums_are_literal_in_schema() -> None:
    from docking_agent.agents.dispatch import run_docking
    from docking_agent.tools.docking import molecular_docking

    prop = run_docking.tool_call_schema.model_json_schema()["properties"]["engine"]
    assert set(prop.get("enum") or []) == {"", "auto", "vina", "autodock", "external"}, prop
    assert "" in (prop.get("enum") or []), "留空 = 跟随设置页「对接引擎（默认）」"
    sub = molecular_docking.tool_call_schema.model_json_schema()["properties"]["engine"]
    assert set(sub.get("enum") or []) == {"", "auto", "vina", "autodock", "external"}, sub


def test_run_docking_accepts_both_array_and_legacy_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧调用方传字符串、新调用方传数组，最终发给子 Agent 的坐标文本完全一致。"""
    from docking_agent.agents import dispatch

    seen: List[str] = []

    def _capture(agent: Any, msg: str, **kwargs: Any) -> str:
        seen.append(msg)
        return "{}"

    monkeypatch.setattr(dispatch, "_invoke_checked", _capture)
    monkeypatch.setattr(dispatch, "get_docking_agent", lambda: object())

    dispatch.run_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]',
                              site_center=[31.5, 13.74, 24.36], site_size=[22, 22, 22])
    dispatch.run_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]',
                              site_center="31.5,13.74,24.36", site_size="22,22,22")

    assert len(seen) == 2
    for msg in seen:
        # 下发给子 Agent 的坐标用**数组形态**（子 Agent 走 schema 校验，字符串会被拒）：
        # 现在坐标在结构化参数块里，直接解析 JSON 断言（不再用正则反解散文）
        params = parse_agent_params(msg)
        assert params["site_center"] == [31.5, 13.74, 24.36], params
        assert params["site_size"] == [22.0, 22.0, 22.0], params
    assert seen[0] == seen[1], "两种入参形态必须产生完全相同的下发消息"


def test_run_docking_warns_on_unparseable_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    """给了值但解析不出 3 个坐标时必须如实提醒（否则会静默换盒子）。"""
    from docking_agent.agents import dispatch

    seen: List[str] = []
    monkeypatch.setattr(dispatch, "_invoke_checked", lambda a, m, **k: seen.append(m) or "{}")
    monkeypatch.setattr(dispatch, "get_docking_agent", lambda: object())

    dispatch.run_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]',
                              site_center="1,2")
    assert "无法解析为 3 个坐标" in seen[0], seen[0]
    # 解析不出的坐标**不得**进入参数块（否则子 Agent 会拿一个半个盒子去对接）
    params = parse_agent_params(seen[0])
    assert "site_center" not in params and "site_size" not in params, params


def test_set_docking_site_accepts_array_and_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """位点提交（写黑板）两种入参形态等价。"""
    import json as _json

    from docking_agent.tools import pockets

    class _Board:
        def __init__(self) -> None:
            self.sites: List[Dict[str, Any]] = []

        def set_site(self, **site: Any) -> Dict[str, Any]:
            self.sites.append(site)
            return site

        def get_pockets(self) -> List[Dict[str, Any]]:
            return []

        def get_site(self) -> Dict[str, Any]:
            return {}

        def get_receptor(self) -> Dict[str, Any]:
            return {}

    board = _Board()
    # 工具现在通过 active_blackboard(runtime) 取黑板（没有 runtime 时回退 ContextVar）
    monkeypatch.setattr(pockets, "active_blackboard", lambda runtime=None: board)

    out_array = _json.loads(pockets.set_docking_site.func(center=[1.0, 2.0, 3.0], size=[10.0, 11.0, 12.0],
                                                         reason="数组"))
    out_text = _json.loads(pockets.set_docking_site.func(center="1,2,3", size="10 11 12",
                                                        reason="字符串"))
    assert out_array["status"] == "ok" and out_text["status"] == "ok", (out_array, out_text)
    assert out_array["site"]["center"] == out_text["site"]["center"] == [1.0, 2.0, 3.0]
    assert out_array["site"]["size"] == out_text["site"]["size"] == [10.0, 11.0, 12.0]
    assert len(board.sites) == 2


def test_form_save_poses_and_max_ligands_reach_run_docking(monkeypatch: pytest.MonkeyPatch) -> None:
    """表单的「保存位姿 / 最大分子数」必须一路传到 run_docking 的参数块。

    真实缺陷：`sp` 要求协调 Agent「原样传给 run_docking」，但 `run_docking` 曾经**没有**这两个参数
    （LangChain 对多余 kwargs 静默忽略），受理层也从不渲染它们 → 用户勾掉保存位姿仍会写位姿文件、
    设了「最大分子数」仍跑全库。这里把「表单 → 规约 → 参数块」整条链路钉住。
    """
    from docking_agent import intake
    from docking_agent.api.schemas import AgentRequest
    from docking_agent.agents import dispatch

    seen: List[str] = []
    monkeypatch.setattr(dispatch, "_invoke_checked", lambda a, m, **k: seen.append(m) or "{}")
    monkeypatch.setattr(dispatch, "get_docking_agent", lambda: object())

    req = AgentRequest(mode="manual", receptor="1DWC", ligands_text="A:CCO",
                       save_poses=False, max_ligands=3)
    msg, spec = intake.build_message(req, allow_llm=False)
    assert "save_poses=False" in msg and "max_ligands=3" in msg, msg      # 受理层渲染

    dispatch.run_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]',
                              receptor_sources="thrombin",
                              save_poses=spec["params"]["save_poses"],
                              max_ligands=spec["params"]["max_ligands"])
    params = parse_agent_params(seen[0])
    assert params.get("save_poses") is False, params
    assert params.get("max_ligands") == 3, params
