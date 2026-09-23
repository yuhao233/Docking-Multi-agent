"""坐标字段的入参类型：数组为主，**文档承诺的字符串形态必须在进函数体前就被接受**。

真实缺陷（2026-09-23）：`coord_description()` 与工具 docstring 都写着「也接受 "22,22,22"」，
但字段类型是 `List[float]` —— 字符串在 pydantic 校验阶段就被拒，函数体里的 `floats_to_text()`
永远没机会处理。表现：仓库自带的 `scripts/verify_docking.py`（按文档传
`site_center="31.5,13.74,24.36"`）直接抛 `ValidationError`；模型照描述传字符串同样会被拒。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from docking_agent.tools.schemas import normalize_coord


def test_string_forms_are_normalized_before_validation() -> None:
    assert normalize_coord("31.5,13.74,24.36") == [31.5, 13.74, 24.36]
    assert normalize_coord("31.5 13.74 24.36") == [31.5, 13.74, 24.36]
    assert normalize_coord("22;22;22") == [22.0, 22.0, 22.0]
    assert normalize_coord(" 22, 22 ,22 ") == [22.0, 22.0, 22.0]


def test_arrays_and_none_pass_through() -> None:
    assert normalize_coord([1, 2, 3]) == [1, 2, 3]
    assert normalize_coord(None) is None
    assert normalize_coord("") is None, "空串 = 未提供（与 floats_to_text 口径一致）"


def test_unparsable_string_is_left_for_pydantic_to_report() -> None:
    assert normalize_coord("abc") == "abc"


def test_docking_tool_schema_accepts_both_forms() -> None:
    """工具 schema 层：字符串与数组都必须通过校验，且解析成同一组数字。"""
    from docking_agent.tools.docking import molecular_docking

    schema = molecular_docking.args_schema
    base = {"molecules_json": "[]", "receptor_sources": "thrombin", "engine": "vina"}
    from_str = schema.model_validate({**base, "site_center": "31.5,13.74,24.36", "site_size": "22,22,22"})
    from_list = schema.model_validate({**base, "site_center": [31.5, 13.74, 24.36], "site_size": [22, 22, 22]})
    assert from_str.site_center == from_list.site_center == [31.5, 13.74, 24.36]
    assert from_str.site_size == from_list.site_size == [22.0, 22.0, 22.0]


def test_docking_tool_schema_rejects_garbage() -> None:
    """真正非法的输入仍要报错（不能因为放宽字符串而放过垃圾）。"""
    from docking_agent.tools.docking import molecular_docking

    schema = molecular_docking.args_schema
    with pytest.raises(ValidationError):
        schema.model_validate({"molecules_json": "[]", "receptor_sources": "thrombin",
                               "site_center": "不是坐标"})
    with pytest.raises(ValidationError):
        schema.model_validate({"molecules_json": "[]", "receptor_sources": "thrombin",
                               "site_size": ["22", "x", "22"]})
