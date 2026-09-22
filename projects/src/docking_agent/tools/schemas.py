"""工具入参的小参数类型化（坐标 / 枚举）。

规范口径（P1，用户已确认）：**只类型化小参数**，不动大库契约。

- 类化：位点盒坐标（`site_center` / `site_size`）、引擎枚举（`engine` / `pocket_engine`）——
  这些是「两三个数」的结构化数据，用字符串传会让模型自己拼格式、也让校验失效
  （真实事故：`molecular_property_assessment` 的 docstring 漂移出并不存在的 `protonation_ph`，
  就是因为没有 schema 兜底）。
- **不**类化：`molecules_json` / `*_file` —— 它们承担的是「大库按文件交接」的既定契约
  （`runtime/tool_io.py`：1 万分子 ≈ 0.5 MB ≈ 13 万 tokens），强类型化会把明细塞回上下文。

为了让**既有调用方（CLI / 测试 / 旧提示词）传字符串也不炸**，这里提供
`floats_to_text()`：`None` / `""` / `"31.5,13.74,24.36"` / `"31.5 13.74 24.36"` /
`[31.5, 13.74, 24.36]` 一律规范化成下游一直使用的 `"31.5,13.74,24.36"` 文本形态。
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Union

NumberList = Optional[Union[str, Sequence[float]]]

#: 工具 schema 里坐标字段的类型（数组；描述里注明也接受逗号/空格分隔字符串）
CoordArray = Optional[List[float]]


def floats_to_text(values: NumberList, *, expect: int = 0) -> str:
    """把坐标值规范化成 `"a,b,c"` 文本；无法解析时返回空串（交给调用方如实报错）。

    `expect>0` 时校验个数（例如位点盒必须 3 个分量），不匹配同样返回空串 —— 宁可让工具
    返回结构化的 `status=error`，也不要把 `[1, 2]` 这种半个盒子塞进对接引擎。
    """
    if values is None:
        return ""
    if isinstance(values, str):
        parts = [p for p in values.replace(",", " ").replace(";", " ").split() if p]
    else:
        try:
            parts = [str(v) for v in values]
        except TypeError:
            return ""
    if not parts:
        return ""
    numbers: List[float] = []
    for part in parts:
        try:
            numbers.append(float(part))
        except (TypeError, ValueError):
            return ""
    if expect and len(numbers) != expect:
        return ""
    return ",".join(_fmt(n) for n in numbers)


def _fmt(value: float) -> str:
    """紧凑输出：整数不带小数点（与既有 `"22,22,22"` 文案一致）。"""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def coord_description(label: str, *, example: str = "31.5,13.74,24.36") -> str:
    """坐标字段的统一描述（数组为主，兼容字符串形态）。"""
    return (f"{label}，形如 [x, y, z]（Å）；也接受逗号分隔字符串 \"{example}\"")


__all__ = ["NumberList", "CoordArray", "floats_to_text", "coord_description"]
