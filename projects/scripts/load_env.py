#!/usr/bin/env python3
"""把 projects/.env 输出为 shell export 语句（`eval $(python scripts/load_env.py)`）。

本地化改造：不再访问 Coze 工作负载身份服务。
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.getenv("ENV_FILE", PROJECT_ROOT / ".env"))

if not ENV_FILE.exists():
    print(f"# 未找到 {ENV_FILE}", file=sys.stderr)
    sys.exit(0)

count = 0
for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key, value = key.strip(), value.strip().strip('"').strip("'")
    if not key:
        continue
    print(f"export {key}={shlex.quote(value)}")
    count += 1

print(f"# 已导出 {count} 个环境变量（来自 {ENV_FILE}）", file=sys.stderr)
