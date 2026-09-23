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

`CoordArray` 用 `BeforeValidator` 把**字符串形态在校验前**折成数组：字段描述里承诺了
「也接受 "22,22,22"」，但 `List[float]` 会在进函数体前就拒掉字符串 —— 文档承诺与真实校验
不一致（真实缺陷：仓库自带的 `scripts/verify_docking.py` 按文档传 `"31.5,13.74,24.36"`
直接抛 ValidationError；模型按描述传字符串同样会被拒）。
"""
from __future__ import annotations

from typing import Annotated, Any, List, Optional, Sequence, Union

from pydantic import BeforeValidator

NumberList = Optional[Union[str, Sequence[float]]]


def normalize_coord(value: Any) -> Any:
    """把 `"31.5,13.74,24.36"` / `"31.5 13.74 24.36"` 折成 `[31.5, 13.74, 24.36]`。

    已经是数组/None 的原样返回；无法解析的字符串原样返回，交给 pydantic 报错
    （错误信息比这里自己抛更具体）。空串按未提供处理（返回 None）—— 与 `floats_to_text`
    的「空 = 未提供」口径一致。
    """
    if not isinstance(value, str):
        return value
    parts = [p for p in value.replace(",", " ").replace(";", " ").split() if p]
    if not parts:
        return None
    try:
        return [float(p) for p in parts]
    except ValueError:
        return value


#: 工具 schema 里坐标字段的类型：数组为主，**字符串形态在校验前折成数组**（见模块 docstring）
CoordArray = Annotated[Optional[List[float]], BeforeValidator(normalize_coord)]


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


__all__ = ["NumberList", "CoordArray", "floats_to_text", "coord_description",
           "normalize_coord"]
