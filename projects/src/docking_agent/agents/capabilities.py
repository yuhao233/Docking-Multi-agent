"""供应商能力记忆：避免每次运行都先撞一次 400 再降级。

真实问题（用户反馈的运行日志）::

    [15:25:17] pocket 子 Agent：供应商拒绝结构化输出（thinking 模式不支持强制 tool_choice），
               已降级为文本 JSON 契约（结果仍经必需字段校验）

实测根因：该端点/模型在 thinking 语义下**只拒绝"强制" tool_choice**，而
`ToolStrategy` 正是靠强制 tool_choice 工作的；自动 tool_choice 与 JSON 模式都正常。
因此把「这个模型不支持强制 tool_choice」当成**已探明的能力**记下来：

1. 第一次被拒时写入 `var/state/llm_capabilities.json`（按 base_url + model + thinking 区分）；
2. 之后构建子 Agent 时**直接不挂 ToolStrategy**，不再产生 400、不再每次运行刷降级日志；
3. 降级路径优先用 **JSON 模式**（`response_format={"type":"json_object"}`，需要提示词提到
   JSON —— 子 Agent 的契约提示词本来就要求输出 JSON），拿不到 JSON 模式才退回纯文本契约。

可用设置项：`AGENT_STRUCTURED_OUTPUT=auto|on|off`（全局）与设置页的
`roles.<角色>.structured_output=auto|on|off`（按角色，`on` 表示无视记忆强制试一次）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

STATE_DIR_NAME = "state"
STATE_FILE_NAME = "llm_capabilities.json"
DEFAULT_TTL_DAYS = 30

_lock = threading.Lock()


def _state_file() -> Path:
    from docking_agent.paths import workspace_dir  # 延迟导入，避免与 paths 形成环

    path = workspace_dir() / "var" / STATE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path / STATE_FILE_NAME


def _read_state() -> Dict[str, Any]:
    path = _state_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("供应商能力状态读取失败（按空处理）：%s", exc)
        return {}


def _write_state(state: Dict[str, Any]) -> None:
    path = _state_file()
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:  # 记忆失败不影响本次运行
        logger.warning("供应商能力状态写入失败（忽略）：%s", exc)


def capability_key(role: str) -> str:
    """按「端点 + 模型 + thinking」区分能力：换模型会重新探测一次，而不是永久禁用。"""
    try:
        from docking_agent.runtime.llm import resolve_role_config

        cfg, _src = resolve_role_config(role)
        base = str(cfg.get("base_url") or "")
        model = str(cfg.get("model") or "")
        thinking = str(cfg.get("thinking") or "disabled")
        return "|".join((base.rstrip("/"), model, thinking))
    except Exception as exc:  # noqa: BLE001 - 取不到配置时退化为按角色记
        logger.debug("解析角色配置失败（能力键退化为角色名）：%s", exc)
        return f"role:{role}"


def role_setting(role: str, field: str) -> str:
    """读取设置页里的按角色设置（取不到返回空串）。"""
    try:
        from docking_agent.settings import load_local_settings

        local = load_local_settings() or {}
        value = ((local.get("roles") or {}).get(role) or {}).get(field)
        return str(value or "").strip().lower()
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取角色设置 %s.%s 失败：%s", role, field, exc)
        return ""


def forced_tool_choice_supported(role: str) -> bool:
    """该角色的模型是否支持「强制 tool_choice」。未知 → True（先试一次，被拒后记住）。"""
    from docking_agent.config import env

    if str(env("AGENT_STRUCTURED_OUTPUT", "auto") or "auto").strip().lower() != "auto":
        return True                                    # on/off 由调用方另行处理
    if role_setting(role, "structured_output") == "on":
        return True                                    # 用户显式要求试一次
    with _lock:
        entry = (_read_state().get(capability_key(role)) or {})
    if not entry:
        return True
    age_days = (time.time() - float(entry.get("at") or 0)) / 86400.0
    if age_days > DEFAULT_TTL_DAYS:
        return True                                    # 记忆过期 → 重新探测（供应商随时可能升级）
    return bool(entry.get("forced_tool_choice", True))


def mark_forced_tool_choice_unsupported(role: str, reason: str = "") -> None:
    """记录「该模型拒绝强制 tool_choice」，供后续构建期直接跳过。"""
    key = capability_key(role)
    with _lock:
        state = _read_state()
        state[key] = {"forced_tool_choice": False, "reason": str(reason)[:200],
                      "role": role, "at": time.time()}
        _write_state(state)
    logger.info("已记录供应商能力：%s 不支持强制 tool_choice（后续不再重试该路径）", key)


def structured_output_decision(role: str) -> str:
    """该角色用哪种结构化输出：`tool`（框架强制）/ `json_mode`（供应商 JSON 模式）/ `text`。

    优先级：环境变量 off → text；按角色 off → text；能力记忆/按角色 auto → json_mode；
    其余（含 on、未知）→ tool。
    """
    from docking_agent.config import env

    mode = str(env("AGENT_STRUCTURED_OUTPUT", "auto") or "auto").strip().lower()
    if mode == "off":
        return "text"
    role_mode = role_setting(role, "structured_output")
    if role_mode == "off":
        return "text"
    if mode == "on" or role_mode == "on":
        return "tool"
    return "tool" if forced_tool_choice_supported(role) else "json_mode"


def summary() -> Dict[str, Any]:
    """当前记忆内容（供 doctor / 设置页展示）。"""
    with _lock:
        state = _read_state()
    return {"file": str(_state_file()), "entries": state}
