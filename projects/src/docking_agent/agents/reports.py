"""子 Agent 的结构化输出契约（`response_format` 与 `ToolStrategy`）。

输出契约由 LangChain 1.x 的 `response_format=ToolStrategy(<Role>Report)` 实现：框架要求
模型调用结构化输出工具，返回值落在图的 `structured_response` 通道里，并经过 pydantic 校验。
采用该方式的原因是纯提示词契约在模型不遵循时会产生 `agent_output_invalid` 一类错误。

设计取舍：
  - `extra="allow"`：子 Agent 的报告里除了下面的关键字段，还带 `artifacts` / `top` /
    `summary` / `positive_control` 等字段，需要原样保留，不能被 schema 丢弃；
  - 关键字段给出默认值（`status="ok"` 等），与既有 `_REQUIRED_KEYS` 对齐；
  - 协调 Agent 侧拿到的仍是 JSON 字符串（`invoke_worker` 负责序列化），
    因此 `persistence`、报告链路与既有测试的对外契约不变（降低影响面）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


#: `agent_note` 的上限（一句、≤80 字）：它会被主管 Agent 直接消费，也可能被原样带进报告
AGENT_NOTE_MAX = 80


def _one_sentence(value: Any, limit: int = AGENT_NOTE_MAX) -> Optional[str]:
    """把 `agent_note` 压成一句、至多 limit 字（机制上保证「输出经济」，不只靠提示词）。

    只取第一个句子（按句号、感叹号、问号、分号或换行切分），超长再截断加省略号；
    子 Agent 的判断要点就是这一句，长论证属于报告正文，不属于 Agent 之间的回执。
    """
    text = " ".join(str(value or "").split())
    if not text:
        return None
    for sep in ("。", "！", "？", "；"):
        head = text.split(sep, 1)[0]
        if head and len(head) < len(text):
            text = head + sep
            break
    return text if len(text) <= limit else text[:limit].rstrip("，,、 ") + "…"


class _ExtraAllowed(BaseModel):
    """允许携带额外字段（子 Agent 的报告字段原样透传）。"""

    model_config = ConfigDict(extra="allow")


class _NoteContract(_ExtraAllowed):
    """所有子 Agent 共用的回执长度约束：`agent_note` 限制为一句、至多 80 字。"""

    @field_validator("agent_note", check_fields=False)
    @classmethod
    def _trim_agent_note(cls, value: Any) -> Optional[str]:
        return _one_sentence(value)


class PropertyReport(_NoteContract):
    """分子属性评估 Agent 的输出。"""

    status: str = Field(default="ok", description="ok / no_molecules / error")
    assessment: List[Dict[str, Any]] = Field(default_factory=list,
                                             description="逐分子的理化性质与类药性")
    agent_note: Optional[str] = Field(default=None, description="一句话说明规范化/判断")


class PocketReport(_NoteContract):
    """口袋分析 Agent 的输出。"""

    status: str = Field(default="ok", description="ok / error")
    pockets: List[Dict[str, Any]] = Field(default_factory=list,
                                          description="预测到的口袋（含评分/坐标/尺寸）")
    engine: Optional[str] = Field(default=None, description="实际使用的口袋引擎")
    agent_note: Optional[str] = Field(default=None, description="选盒依据与一致性结论")


class DockingReport(_NoteContract):
    """Docking 执行 Agent 的输出。"""

    status: str = Field(default="ok", description="ok / error / cancelled")
    receptors: List[Dict[str, Any]] = Field(default_factory=list, description="按受体分组的对接结果")
    summary: Dict[str, Any] = Field(default_factory=dict, description="本次对接汇总（计数/参数/耗时）")
    agent_note: Optional[str] = Field(default=None, description="一句话说明")


class BindingReport(_NoteContract):
    """结合模式检测 Agent 的输出。"""

    status: str = Field(default="ok", description="ok / error")
    results: List[Dict[str, Any]] = Field(default_factory=list, description="逐分子结合模式对比结果")
    positive_control: Optional[str] = Field(default=None, description="本次使用的阳性对照")
    agent_note: Optional[str] = Field(default=None, description="一句话说明")


#: 角色到结构化输出模型的映射（与 `agents/dispatch._REQUIRED_KEYS` 的关键字段一一对应）
ROLE_REPORTS: Dict[str, type[BaseModel]] = {
    "property": PropertyReport,
    "pocket": PocketReport,
    "docking": DockingReport,
    "binding": BindingReport,
}


def report_model(role: str) -> Optional[type[BaseModel]]:
    """取某个子 Agent 的结构化输出模型（未知角色返回 None，此时不加 `response_format`）。"""
    return ROLE_REPORTS.get((role or "").strip().lower())


# --------------------------------------------------------------------------- #
# 供应商兼容：结构化输出可用时启用，供应商拒绝时降级为文本 JSON 契约
# --------------------------------------------------------------------------- #
#: `AGENT_STRUCTURED_OUTPUT`：auto（默认，供应商拒绝时降级为文本 JSON 契约）/ on（不降级）/
#: off（不使用结构化输出）
STRUCTURED_MODES = ("auto", "on", "off")

#: 供应商拒绝「强制 tool_choice」类错误的关键词（仅用于决定是否降级，其它错误照常抛出）
_REJECTION_MARKERS = ("tool_choice", "thinking mode", "response_format", "json_schema",
                      "structured output", "does not support this tool")


def structured_output_mode() -> str:
    """读取 `AGENT_STRUCTURED_OUTPUT`（非法值按 auto 处理）。"""
    from docking_agent.config import env  # 延迟导入，避免与 config 形成环

    value = (env("AGENT_STRUCTURED_OUTPUT", "auto") or "auto").strip().lower()
    return value if value in STRUCTURED_MODES else "auto"


def uses_structured_output() -> bool:
    return structured_output_mode() != "off"


def is_structured_rejection(exc: BaseException) -> bool:
    """判断异常是否属于「供应商不支持强制结构化输出」这一类（案例：DeepSeek thinking mode）。

    典型报文：`Error code: 400 ... 'Thinking mode does not support this tool_choice'`。
    只匹配关键词，不把无关错误（限流、超时、参数错）当成可降级错误，这类错误照常抛出。
    """
    text = f"{type(exc).__name__}: {getattr(exc, 'message', '') or exc}".lower()
    return any(marker in text for marker in _REJECTION_MARKERS)


__all__ = ["PropertyReport", "PocketReport", "DockingReport", "BindingReport",
           "ROLE_REPORTS", "report_model", "structured_output_mode",
           "uses_structured_output", "is_structured_rejection", "STRUCTURED_MODES"]
