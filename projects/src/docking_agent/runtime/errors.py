"""错误包装：替代 coze_coding_utils.error.classifier / log.err_trace。"""
from __future__ import annotations

import traceback
from types import SimpleNamespace
from typing import Any, Dict, Optional

# 常见错误 -> (错误码, 分类)
_RULES = [
    (FileNotFoundError, "RESOURCE_NOT_FOUND", "resource"),
    (PermissionError, "PERMISSION_DENIED", "resource"),
    (TimeoutError, "TIMEOUT", "runtime"),
    (ConnectionError, "CONNECTION_ERROR", "network"),
    (ValueError, "INVALID_ARGUMENT", "input"),
    (KeyError, "MISSING_FIELD", "input"),
]


def core_stack(limit: int = 12) -> str:
    """提取精简堆栈（去掉框架内部帧）。"""
    tb = traceback.format_exc()
    if tb.strip() == "NoneType: None":
        tb = "".join(traceback.format_stack()[-limit:])
    lines = [ln for ln in tb.splitlines() if ln.strip()]
    return "\n".join(lines[-limit:])


def classify(e: BaseException, where: Optional[Dict[str, Any]] = None) -> SimpleNamespace:
    code, category = type(e).__name__, "unknown"
    for exc_type, c, cat in _RULES:
        if isinstance(e, exc_type):
            code, category = c, cat
            break
    return SimpleNamespace(code=code, message=str(e), category=SimpleNamespace(name=category), where=where or {})


def error_payload(e: BaseException, where: Any = None) -> Dict[str, Any]:
    info = classify(e, where if isinstance(where, dict) else None)
    return {
        "error_code": info.code,
        "error_message": info.message,
        "category": info.category.name,
        "where": where,
        "stack_trace": core_stack(),
    }


class ErrorClassifier:
    """与平台同名 API 的本地实现。"""

    def classify(self, e: BaseException, where: Optional[Dict[str, Any]] = None) -> SimpleNamespace:
        return classify(e, where)

    def get_error_response(self, e: BaseException, where: Any = None) -> Dict[str, Any]:
        return error_payload(e, where)
