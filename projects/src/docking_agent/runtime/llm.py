"""LLM 构建：读取 config/agent_llm_config.json，并允许用环境变量覆盖。

本地化改造要点：原项目从 Coze 工作负载身份获取 API Key 与网关地址
（COZE_WORKLOAD_IDENTITY_API_KEY / COZE_INTEGRATION_MODEL_BASE_URL），
这里改为标准配置：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL（兼容 OPENAI_API_KEY / OPENAI_BASE_URL）。

**按 Agent 角色配置（每个子 Agent 独立模型实例）**
本模块支持给每个角色单独指定模型/参数，角色名见 ROLES：

1) 配置文件 `config/agent_llm_config.json` 的 `roles` 段：
     {"config": {...全局默认...},
      "roles": {"docking": {"model": "deepseek-v4-pro", "temperature": 0.1}}}
   未列出的字段继承全局 config。

2) 环境变量（优先级高于配置文件），把全局变量名加上 `_<角色大写>` 后缀：
     LLM_MODEL_DOCKING=deepseek-v4-pro
     LLM_TEMPERATURE_PROPERTY=0
     LLM_BASE_URL_BINDING=... / LLM_API_KEY_CRITIC=... / LLM_MAX_TOKENS_DOCKING=...

每次 build_chat_llm 都会**新建一个独立的 ChatOpenAI 实例**，并按角色记录到
进程内登记表（llm_registry），因此不同子 Agent 的模型互不共享、可分别观测。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from langchain_core.callbacks import BaseCallbackHandler

from docking_agent.config import env, load_env
from docking_agent.paths import project_root

logger = logging.getLogger(__name__)

LLM_CONFIG_REL = "config/agent_llm_config.json"
DEFAULT_MODEL = "doubao-seed-2-0-pro-260215"
DISABLED = ("", "none", "null", "disabled", "off", "false")

# 系统内的 LLM 角色：任务受理、协调（编排）、4 个子 Agent。
ROLES = ("intake", "coordinator", "property", "pocket", "docking", "binding")
# 角色可覆盖的字段 → 对应的全局环境变量名
_ROLE_ENV_FIELDS = {
    "model": "LLM_MODEL",
    "base_url": "LLM_BASE_URL",
    "api_key": "LLM_API_KEY",
    "temperature": "LLM_TEMPERATURE",
    "top_p": "LLM_TOP_P",
    "timeout": "LLM_TIMEOUT",
    "thinking": "LLM_THINKING",
    "max_tokens": "LLM_MAX_TOKENS",
    "extra_body": "LLM_EXTRA_BODY",
    "extra_headers": "LLM_EXTRA_HEADERS",
}


class LLMConfigError(RuntimeError):
    """缺少必要的本地 LLM 配置。"""


def config_path():
    return project_root() / LLM_CONFIG_REL


def _role_env_name(role: str, base: str) -> str:
    """角色专属环境变量名：LLM_MODEL_DOCKING / LLM_TEMPERATURE_PROPERTY ..."""
    return f"{base}_{role.upper()}" if role else base


def _read_raw() -> Dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _local_settings() -> Dict[str, Any]:
    """界面设置（config/local_settings.json）；延迟导入避免循环依赖。"""
    try:
        from docking_agent.settings import load_local_settings

        return load_local_settings()
    except Exception:  # noqa: BLE001
        logger.debug("读取本地设置失败", exc_info=True)
        return {}


def _role_overrides(raw: Dict[str, Any], role: str) -> Dict[str, Any]:
    """取配置文件 roles.<role> 段（忽略以 _ 开头的说明键）。"""
    if not role:
        return {}
    section = (raw.get("roles") or {}).get(role)
    if not isinstance(section, dict):
        return {}
    return {k: v for k, v in section.items() if not str(k).startswith("_")}


def _env_layer(role: str) -> Dict[str, Any]:
    """该角色（role 为空则全局）在环境变量里显式给出的字段。"""
    out: Dict[str, Any] = {}
    for field_name, base in _ROLE_ENV_FIELDS.items():
        value = env(_role_env_name(role, base))
        if value not in (None, ""):
            out[field_name] = value
    # 兼容 OPENAI_* 别名
    aliases = {"base_url": "OPENAI_BASE_URL", "api_key": "OPENAI_API_KEY"}
    for field_name, base in aliases.items():
        if field_name in out:
            continue
        value = env(_role_env_name(role, base))
        if value not in (None, ""):
            out[field_name] = value
    return out


# 字段类型（决定最终值的强制转换方式）
_FLOAT_FIELDS = ("temperature", "top_p", "timeout")
_INT_FIELDS = ("max_tokens",)


def _coerce_field(field_name: str, value: Any) -> Any:
    if value is None or value == "":
        return None
    try:
        if field_name in _FLOAT_FIELDS:
            return float(value)
        if field_name in _INT_FIELDS:
            return int(float(value))
        if field_name in ("extra_body", "extra_headers"):
            if isinstance(value, dict):
                return value
            return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value


def _layers(raw: Dict[str, Any], role: str, local: Dict[str, Any]) -> list:
    """按优先级从低到高返回 (来源标签, 该层字段字典)。

    界面设置（local_settings.json）比 .env 更具体：用户在设置页面刚改的值必须生效，
    否则「改了没反应」；角色专属环境变量仍高于界面设置（脚本/CI 场景的显式覆盖）。
    """
    layers = [(f"内置默认 {LLM_CONFIG_REL}", dict(raw.get("config") or {})),
              ("环境变量", _env_layer("")),
              ("界面设置（全局）", dict(local.get("llm") or {}))]
    if role:
        layers.append((f"内置角色默认 roles.{role}", dict(_role_overrides(raw, role))))
        role_ui = (local.get("roles") or {}).get(role)
        layers.append((f"界面设置（{role}）", dict(role_ui) if isinstance(role_ui, dict) else {}))
        layers.append(("角色环境变量", _env_layer(role)))
    return layers


def resolve_role_config(role: str = "") -> Tuple[Dict[str, Any], Dict[str, str]]:
    """解析该角色的最终配置，并给出**每个字段的来源**。"""
    load_env()
    raw = _read_raw()
    local = _local_settings()
    cfg: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    for label, layer in _layers(raw, role, local):
        for field_name, value in (layer or {}).items():
            if field_name not in _ROLE_ENV_FIELDS:
                continue
            coerced = _coerce_field(field_name, value)
            if coerced is None:
                continue
            cfg[field_name] = coerced
            sources[field_name] = label
    # 兜底默认
    cfg.setdefault("model", DEFAULT_MODEL)
    cfg.setdefault("temperature", 0.2)
    cfg.setdefault("top_p", 0.9)
    cfg.setdefault("timeout", 600)
    cfg.setdefault("thinking", "disabled")
    for key, label in (("model", "内置默认"), ("temperature", "内置默认"),
                       ("top_p", "内置默认"), ("timeout", "内置默认"),
                       ("thinking", "内置默认")):
        sources.setdefault(key, label)
    return cfg, sources


def effective_config(role: str = "") -> Dict[str, Any]:
    """返回该角色最终生效的 LLM 配置（兼容既有调用：只取配置本体）。"""
    return resolve_role_config(role)[0]


def load_llm_config(role: str = "") -> Dict[str, Any]:
    """返回完整配置：{"config": {...}, "sp": "...", "tools": [...], "roles": {...}}。

    role 为空时 config 为全局默认配置（向后兼容既有调用）；
    指定 role（intake/coordinator/property/pocket/docking/binding）时 config 为该角色的最终配置。
    """
    raw = _read_raw()
    raw["config"] = effective_config(role)
    if role:
        raw["role"] = role
    return raw


def describe_roles() -> Dict[str, Dict[str, Any]]:
    """各角色最终生效的模型配置（不构建实例，用于健康检查/前端展示）。"""
    out: Dict[str, Dict[str, Any]] = {}
    for role in ROLES:
        cfg = effective_config(role)
        out[role] = {
            "model": cfg.get("model"),
            "temperature": cfg.get("temperature"),
            "top_p": cfg.get("top_p"),
            "base_url": cfg.get("base_url"),
            "timeout": cfg.get("timeout"),
            "thinking": cfg.get("thinking"),
            "max_tokens": cfg.get("max_tokens"),
        }
    return out


# --------------------------------------------------------------------------- #
# 实例登记表：证明「每个角色一个独立实例」，并记录实际使用的模型
# --------------------------------------------------------------------------- #
_REGISTRY: Dict[str, Dict[str, Any]] = {}


def llm_registry() -> Dict[str, Dict[str, Any]]:
    """已构建的 LLM 实例（role → 元信息，含 instance_id）。"""
    return {k: dict(v) for k, v in _REGISTRY.items()}


def reset_llm_registry() -> None:
    _REGISTRY.clear()


def reset_registry_counters() -> None:
    """只清空「调用次数/服务端确认模型」，保留实例信息（模型、instance_id）。

    每次运行开始时调用：否则 ``calls`` 会随进程累计，运行记录里的「各 Agent 调用次数」
    会把上一次运行的量算进来，无法用来判断本次到底调了多少次。
    """
    for entry in _REGISTRY.values():
        entry.pop("calls", None)
        entry.pop("actual_models", None)
        entry.pop("actual_model", None)
        entry.pop("last_error", None)


def registry_entry(role: str) -> Optional[Dict[str, Any]]:
    """取某个角色的登记条目副本（None = 尚未登记）。"""
    entry = _REGISTRY.get(role)
    return dict(entry) if entry else None


def restore_registry_entry(role: str, entry: Optional[Dict[str, Any]]) -> None:
    """把某个角色的登记条目恢复成给定值（None = 删除该条目）。

    用于「连通性测试」这类临时构建：测试完必须还原，
    否则运行记录/报告里的「各 Agent 模型」溯源会失真。
    """
    if entry is None:
        _REGISTRY.pop(role, None)
    else:
        _REGISTRY[role] = dict(entry)


class _ModelUsageHandler(BaseCallbackHandler):
    """记录每个角色**实际**被服务端确认的模型名与调用次数。

    仅看配置不足以证明「不同角色真的用了不同模型」，因此这里从响应元数据
    （response_metadata.model_name）回读服务端回执的模型名。
    """

    def __init__(self, role: str):
        super().__init__()
        self.role = role or "default"

    def on_llm_start(self, serialized, prompts, **kwargs):  # noqa: ANN001
        entry = _REGISTRY.setdefault(self.role, {"role": self.role})
        entry["calls"] = int(entry.get("calls") or 0) + 1

    def on_llm_end(self, response, **kwargs):  # noqa: ANN001
        names: list = []
        try:
            for group in getattr(response, "generations", None) or []:
                for gen in group:
                    msg = getattr(gen, "message", None)
                    meta = getattr(msg, "response_metadata", None) or {}
                    name = meta.get("model_name") or meta.get("model")
                    if name:
                        names.append(str(name))
            llm_output = getattr(response, "llm_output", None) or {}
            if llm_output.get("model_name"):
                names.append(str(llm_output["model_name"]))
        except Exception:  # noqa: BLE001
            return
        if not names:
            return
        entry = _REGISTRY.setdefault(self.role, {"role": self.role})
        seen = list(entry.get("actual_models") or [])
        for n in names:
            if n not in seen:
                seen.append(n)
        entry["actual_models"] = seen
        entry["actual_model"] = names[-1]

    def on_llm_error(self, error, **kwargs):  # noqa: ANN001
        entry = _REGISTRY.setdefault(self.role, {"role": self.role})
        entry["last_error"] = str(error)[:300]


def _extra_body(cfg: Dict[str, Any], role: str = "") -> Dict[str, Any]:
    """构造 extra_body：支持 LLM_EXTRA_BODY / LLM_EXTRA_BODY_<ROLE>（JSON），
    以及对豆包模型保持 thinking 语义。"""
    body: Dict[str, Any] = {}
    # 环境变量先铺底，界面设置（cfg）覆盖 —— 与「界面设置优先于 .env」的顺序一致
    for name in ("LLM_EXTRA_BODY", _role_env_name(role, "LLM_EXTRA_BODY")):
        raw_extra = env(name)
        if not raw_extra:
            continue
        try:
            body.update(json.loads(raw_extra))
        except json.JSONDecodeError as e:
            raise LLMConfigError(f"{name} 不是合法 JSON: {e}") from e
    if isinstance(cfg.get("extra_body"), dict):
        body.update(cfg["extra_body"])

    model = str(cfg.get("model") or "")
    thinking = str(cfg.get("thinking") or "disabled")
    if model.startswith("doubao"):
        # 豆包需要显式关闭/开启思考，否则默认行为会拖慢或改变输出格式
        body.setdefault("thinking", {"type": thinking})
    elif thinking.lower() not in DISABLED:
        body.setdefault("thinking", {"type": thinking})
    return body


def build_chat_llm(ctx: Any = None, *, role: str = ""):
    """为指定角色构建**独立**的 ChatOpenAI 实例。

    role ∈ intake/coordinator/property/pocket/docking/binding：各角色模型与采样参数
    可分别配置（见模块文档），互不共享实例。
    """
    from langchain_openai import ChatOpenAI  # 延迟导入，便于测试替换

    cfg = load_llm_config(role)["config"]
    api_key = cfg.get("api_key")
    base_url = cfg.get("base_url")
    if not api_key:
        raise LLMConfigError(
            "未配置 LLM API Key。请在 projects/.env 中设置 LLM_API_KEY（或 OPENAI_API_KEY）；"
            "同时可用 LLM_BASE_URL / LLM_MODEL，或按角色设置 LLM_MODEL_<ROLE> 指向任意 OpenAI 兼容端点。"
            "可运行 `bash start.sh --check` 做一次不消耗额度的环境自检"
        )

    headers: Dict[str, str] = {}
    for name in ("LLM_EXTRA_HEADERS", _role_env_name(role, "LLM_EXTRA_HEADERS")):
        raw_headers = env(name)
        if not raw_headers:
            continue
        try:
            headers.update(json.loads(raw_headers))
        except json.JSONDecodeError as e:
            raise LLMConfigError(f"{name} 不是合法 JSON: {e}") from e
    if isinstance(cfg.get("extra_headers"), dict):   # 界面设置优先
        headers.update({str(k): str(v) for k, v in cfg["extra_headers"].items()})

    kwargs: Dict[str, Any] = {
        "model": cfg.get("model"),
        "api_key": api_key,
        "temperature": cfg.get("temperature", 0.2),
        "top_p": cfg.get("top_p", 0.9),
        "streaming": True,
        "timeout": cfg.get("timeout", 600),
        "callbacks": [_ModelUsageHandler(role)],
    }
    if base_url:
        kwargs["base_url"] = base_url
    if cfg.get("max_tokens"):
        kwargs["max_tokens"] = cfg["max_tokens"]
    extra = _extra_body(cfg, role)
    if extra:
        kwargs["extra_body"] = extra
    if headers:
        kwargs["default_headers"] = headers

    llm = ChatOpenAI(**kwargs)   # 每次调用都是新实例：角色之间不共享模型对象
    label = role or "default"
    _REGISTRY[label] = {
        "role": label,
        "model": cfg.get("model"),
        "temperature": cfg.get("temperature"),
        "top_p": cfg.get("top_p"),
        "base_url": base_url,
        "timeout": cfg.get("timeout"),
        "built_at": time.time(),
        "instance_id": id(llm),
    }
    logger.info("构建 LLM role=%s model=%s base_url=%s instance=%s",
                label, cfg.get("model"), base_url or "(default)", id(llm))
    return llm


