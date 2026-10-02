"""坐标字段的入参类型：以数组为主，文档承诺的字符串形态在进入函数体前即被接受。

`coord_description` 与工具 docstring 都写明也接受 `"22,22,22"`，字段类型却是 `List[float]`：
字符串在 pydantic 校验阶段即被拒，函数体中的 `floats_to_text()` 没有机会处理。
调用方按文档传 `site_center="31.5,13.74,24.36"`（如仓库自带的 `scripts/verify_docking.py`）会直接抛
`ValidationError`，模型按描述传字符串同样被拒；用例要求字符串与数组在进入函数体前归一化为同一组数字。
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
    """工具 schema 层：字符串与数组均通过校验，并解析为同一组数字。"""
    from docking_agent.tools.docking import molecular_docking

    schema = molecular_docking.args_schema
    base = {"molecules_json": "[]", "receptor_sources": "thrombin", "engine": "vina"}
    from_str = schema.model_validate({**base, "site_center": "31.5,13.74,24.36", "site_size": "22,22,22"})
    from_list = schema.model_validate({**base, "site_center": [31.5, 13.74, 24.36], "site_size": [22, 22, 22]})
    assert from_str.site_center == from_list.site_center == [31.5, 13.74, 24.36]
    assert from_str.site_size == from_list.site_size == [22.0, 22.0, 22.0]


def test_docking_tool_schema_rejects_garbage() -> None:
    """非法输入仍应报错：接受字符串形态不等于放宽取值范围。"""
    from docking_agent.tools.docking import molecular_docking

    schema = molecular_docking.args_schema
    with pytest.raises(ValidationError):
        schema.model_validate({"molecules_json": "[]", "receptor_sources": "thrombin",
                               "site_center": "不是坐标"})
    with pytest.raises(ValidationError):
        schema.model_validate({"molecules_json": "[]", "receptor_sources": "thrombin",
                               "site_size": ["22", "x", "22"]})
