"""日志初始化：替代 coze_coding_utils.log.write_log / node_log / config。"""
from __future__ import annotations

import logging
import logging.handlers
import sys

from docking_agent.config import env
from docking_agent.runtime.context import request_context  # noqa: F401  (对外复用)
from docking_agent.paths import logs_dir

LOG_FILE = str(logs_dir() / "app.log")

_CONFIGURED = False


class _RunIdFilter(logging.Filter):
    """将当前 run_id 注入日志记录，用于串联同一次多 Agent 运行的日志。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        ctx = request_context.get()
        record.run_id = getattr(ctx, "run_id", "") if ctx else ""
        return True


def setup_logging(
    log_file: str | None = None,
    max_bytes: int = 100 * 1024 * 1024,
    backup_count: int = 5,
    log_level: str | None = None,
    use_json_format: bool = False,  # 保留原签名以兼容调用方；本地输出统一为文本格式
    console_output: bool = True,
) -> logging.Logger:
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        return root

    # 日志级别在调用时读取：模块导入期求值时 .env 与界面设置可能尚未加载
    configured_level = (log_level or env("LOG_LEVEL", "INFO") or "INFO").upper()
    level = getattr(logging, configured_level, logging.INFO)
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(run_id)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    run_filter = _RunIdFilter()

    if console_output:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(fmt)
        ch.addFilter(run_filter)
        root.addHandler(ch)

    try:
        fh = logging.handlers.RotatingFileHandler(
            log_file or LOG_FILE, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.addFilter(run_filter)
        root.addHandler(fh)
    except OSError as e:
        # logging 尚未配置完成，此处直接写 stderr；规范禁止 src/ 下的非 CLI 代码使用 print
        sys.stderr.write(f"[logging] 文件日志不可写（{e}），仅输出到控制台\n")

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    _CONFIGURED = True
    return root
