"""默认对接引擎：设置页的选择必须真的生效，且「未指定」与「显式 auto」不能混为一谈。

背景（真实缺口）：2026-09-23 加入 GPU 外部引擎后，设置页新增「对接引擎（默认）」。
如果工具参数继续用 `"auto"` 当默认值，用户把默认改成 `external`（或 `autodock`）也不会生效 ——
模型不传这个参数时永远是 `auto`。这与 `exhaustiveness` 曾经踩过的坑同型：
**工具层硬编码默认值会静默盖掉用户在设置页做的选择**。

优先级固定为：工具参数 > 运行请求（表单/高级设置）> 设置页默认 > `auto`。
"""
from __future__ import annotations

import pytest

from docking_agent.core import params as P


def test_choices_cover_external_engine() -> None:
    assert "external" in P.ENGINE_CHOICES
    assert set(P.ENGINE_CHOICES) == {"auto", "vina", "autodock", "external"}
    from docking_agent.settings import SPEC_BY_PATH

    spec = SPEC_BY_PATH["docking.engine"]
    assert tuple(spec.choices) == P.ENGINE_CHOICES, "设置页枚举必须与代码枚举同源"
    assert spec.default == "auto", "出厂默认仍是 auto（优先 Vina、不可用回退 AD4）"


def test_resolve_engine_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P, "system_default_engine", lambda: "external")
    assert P.resolve_engine("autodock", {"engine": "vina"}) == "autodock", "显式参数最优先"
    assert P.resolve_engine("", {"engine": "vina"}) == "vina", "其次看运行请求（表单）"
    assert P.resolve_engine("", {}) == "external", "都没有才用设置页默认"
    assert P.resolve_engine(None, {"engine": ""}) == "external"
    # 非法值一律忽略，不能把未知字符串当引擎传下去
    assert P.resolve_engine("gpu", {"engine": "cuda"}) == "external"


def test_blank_engine_follows_settings_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P, "system_default_engine", lambda: "autodock")
    # 参数默认值必须是空串：否则「模型没给」与「模型明确要 auto」无法区分
    from docking_agent.tools.docking import molecular_docking

    assert molecular_docking.args_schema.model_fields["engine"].default in ("", None)


def test_system_default_engine_reads_settings(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path) -> None:
    """`system_default_engine()` 读的是设置页当前生效值（非法值回退 auto）。"""
    from docking_agent import settings as S

    spec = S.SPEC_BY_PATH["docking.engine"]
    for value, expected in (("external", "external"), ("AUTODOCK", "autodock"),
                            ("gpu", "auto"), ("", "auto"), (None, "auto")):
        monkeypatch.setattr(S, "runtime_effective", lambda _spec, v=value: v)
        assert P.system_default_engine() == expected, value
    assert spec.path == "docking.engine"


def test_run_docking_forwards_settings_default_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """协调层不传 engine 时，下发给对接子 Agent 的必须是设置页默认值（而不是硬编码 auto）。"""
    from typing import Any, List

    from docking_agent.agents import dispatch
    from docking_agent.runtime.payload import parse_json_object

    monkeypatch.setattr(P, "system_default_engine", lambda: "external")
    seen: List[str] = []

    def _capture(agent: Any, msg: str, **kwargs: Any) -> str:
        seen.append(msg)
        return "{}"

    monkeypatch.setattr(dispatch, "_invoke_checked", _capture)
    monkeypatch.setattr(dispatch, "get_docking_agent", lambda: object())
    dispatch.run_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]')
    dispatch.run_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]', engine="vina")
    assert len(seen) == 2
    first = parse_json_object(seen[0])
    second = parse_json_object(seen[1])
    assert first["engine"] == "external", first
    assert "engine=external" in seen[0]
    assert second["engine"] == "vina", "显式参数优先于设置页默认"


def test_param_plan_reports_the_engine_actually_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    """报告 1.5 节的「引擎」必须是本次要用的引擎：用户/设置页选了 external 就不能仍写 vina。"""
    monkeypatch.setattr(P, "system_default_engine", lambda: "external")
    default_plan = P.plan_docking_params(task_type="screening",
                                         molecules=[{"smiles": "CCO"}])
    assert default_plan["engine"] == "external", default_plan
    assert any("external" in d for d in default_plan["decisions"]), default_plan["decisions"]

    explicit = P.plan_docking_params(task_type="screening", molecules=[{"smiles": "CCO"}],
                                     user_params={"engine": "autodock"})
    assert explicit["engine"] == "autodock"
    assert any("autodock" in d for d in explicit["decisions"]), explicit["decisions"]
