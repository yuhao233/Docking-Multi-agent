"""`python -m docking_agent` 命令行入口：调用 `cli.main` 并以其返回值作为退出码。"""
from __future__ import annotations

import sys

from docking_agent.cli import main

if __name__ == "__main__":
    sys.exit(main())
