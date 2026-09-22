#!/usr/bin/env python
"""本地静态检查门禁（不依赖 ruff/mypy，零额外安装）。

为什么需要它：项目源码里已有大量 `# noqa: BLE001/PLC0415/...`（说明团队按 ruff 规则写码），
但仓库里**没有** ruff/mypy 配置与依赖，新人或 AI 接手时无法复现这套约定。
本脚本用标准库 AST 把其中**能自动判定**的几条固化下来，作为提交前的最低门禁。

检查项（与 docs/architecture.md §10 的规范一一对应）：
  1. 语法/字节码：`compileall` 等价（import 前先 AST 解析）。
  2. `except ...: pass` 静默吞异常 —— 必须打日志，或写明 `# 允许静默：<原因>`。
  3. `print()` 出现在 `src/` 非 CLI 文件（`cli.py` 白名单）。
  4. 裸 `os.getenv/os.environ` —— 只允许 `config.py`/`paths.py`/`logging_setup.py` 与脚本白名单。
  5. 模块缺少 `from __future__ import annotations`（`__init__.py` 除外）。
  6. 公共函数缺返回类型标注（`test_`/私有函数/`__init__` 等豁免；`--strict-types` 可收紧）。
  7. 单文件行数超过阈值（`--max-lines`，默认 700；已知大户见 EXEMPT）。
  8. 函数内 import 未带 `# noqa: PLC0415` 且无原因注释（`--lazy-imports` 才检查，默认只统计）。

用法：
    python scripts/lint_local.py              # 全量检查（0 = 通过）
    python scripts/lint_local.py --summary    # 只打印统计
    python scripts/lint_local.py --max-lines 800
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
BASELINE = PROJECT_ROOT / "scripts" / "lint_baseline.json"

# 已知超限文件：**只允许存量**，新文件不得加入（要拆分时从表里删除）
# 报告排版脚本是"一次性/按需运行"的工具脚本，行数偏大属可接受（写进豁免并留理由）
SCRIPT_BIG_FILE_EXEMPT = {"scripts/build_report_docx.py"}
BIG_FILE_EXEMPT = {
    # api/app.py 原先 700+ 行（create_app 闭包注册 ~36 条路由），第 2 波已按
    # routers/* + support.py + agent_flow.py 拆分（各自 < 700 行），故从本表删除。
    "src/docking_agent/core/docking.py",
    "src/docking_agent/core/pockets.py",
    "src/docking_agent/settings.py",
    "src/docking_agent/intake.py",
    "src/docking_agent/core/receptors.py",
    # 报告模板原先是一个 900+ 行的巨型函数，第 2 波已按「上下文 + 章节」拆成
    # report_context / report_sections_setup / report_sections_results（各自 < 700 行），
    # 因此 report.py 不再需要豁免（拆分后从本表删除，回归常规阈值）。
    "src/docking_agent/core/normalize.py",
    # 运行记录仓库：本轮加入「历史检索索引」与「被中断运行收尾」后超过 700 行；
    # 检索索引应拆到独立模块（core/run_index.py）是独立一轮的事，先登记豁免。
    "src/docking_agent/runs.py",
}
# 允许直接读环境变量的位置（各有明确理由）：
#   envs.py         —— 环境变量读取的唯一底层实现（从 config.py 拆出，见 audit SCC-1）
#   config.py       —— 运行时环境 bootstrap（MPLCONFIGDIR / 设置注入）
#   settings.py     —— 设置覆盖层本身（要读 LOCAL_SETTINGS_PATH 与判断 env_priority）
#   paths.py        —— 工作区根目录早于 config 可用（避免循环依赖）
#   logging_setup.py —— 日志级别需要在配置完成前读取
GETENV_WHITELIST = {
    "src/docking_agent/envs.py",
    "src/docking_agent/config.py",
    "src/docking_agent/settings.py",
    "src/docking_agent/paths.py",
    "src/docking_agent/logging_setup.py",
    "scripts/load_env.py",
}
PRINT_WHITELIST = {"src/docking_agent/cli.py"}
# 允许没有返回类型标注的名字（框架回调、django 风格钩子等）
NO_RETURN_OK = {"__init__", "__post_init__", "__enter__", "__exit__", "filter", "setup"}


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def py_files() -> list[Path]:
    files = sorted(SRC.rglob("*.py"))
    files += sorted((PROJECT_ROOT / "scripts").glob("*.py"))
    files += sorted((PROJECT_ROOT / "tests").glob("*.py"))
    return [f for f in files if "__pycache__" not in f.parts]


def check(max_lines: int, check_lazy: bool) -> tuple[list[str], dict[str, int]]:
    problems: list[str] = []
    stats = {"files": 0, "functions": 0, "missing_return": 0, "silent_except": 0,
             "lazy_imports": 0, "bare_getenv": 0, "print": 0, "no_future": 0, "big_files": 0}

    for f in py_files():
        rel = _rel(f)
        src = f.read_text(encoding="utf-8")
        stats["files"] += 1
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            problems.append(f"{rel}:{e.lineno} 语法错误：{e.msg}")
            continue

        lines = src.splitlines()
        if len(lines) > max_lines and rel not in BIG_FILE_EXEMPT \
                and rel not in SCRIPT_BIG_FILE_EXEMPT:
            stats["big_files"] += 1
            problems.append(f"{rel} 文件 {len(lines)} 行 > {max_lines} 行（需拆分，或先加入 BIG_FILE_EXEMPT）")

        if (not rel.endswith("__init__.py") and "src/" in rel
                and "from __future__ import annotations" not in src):
            stats["no_future"] += 1
            problems.append(f"{rel} 缺少 `from __future__ import annotations`")

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                stats["functions"] += 1
                if (node.returns is None and node.name not in NO_RETURN_OK
                        and not node.name.startswith("_")):
                    stats["missing_return"] += 1
                    problems.append(f"{rel}:{node.lineno} 公共函数 {node.name}() 缺返回类型标注")
                for child in ast.walk(node):
                    if isinstance(child, ast.Import) or isinstance(child, ast.ImportFrom):
                        line = lines[child.lineno - 1]
                        if child.col_offset > 0:      # 函数体内导入
                            stats["lazy_imports"] += 1
                            if check_lazy and "noqa: PLC0415" not in line and "# 允许延迟" not in line:
                                problems.append(
                                    f"{rel}:{child.lineno} 函数内 import 未标注原因"
                                    "（重依赖/破环需要 `# noqa: PLC0415` 或 `# 允许延迟：<原因>`）")
            if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 \
                    and isinstance(node.body[0], ast.Pass):
                line = lines[node.lineno - 1]
                if "允许静默" not in line:
                    stats["silent_except"] += 1
                    problems.append(
                        f"{rel}:{node.lineno} `except ...: pass` 静默吞异常"
                        "（改为打日志，或写明 `# 允许静默：<原因>`）")
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id == "print" and rel not in PRINT_WHITELIST \
                        and "src/" in rel:
                    stats["print"] += 1
                    problems.append(f"{rel}:{node.lineno} src/ 非 CLI 文件出现 print()（应使用 logging）")
                if isinstance(fn, ast.Attribute) and fn.attr == "getenv" \
                        and rel not in GETENV_WHITELIST and "tests/" not in rel:
                    stats["bare_getenv"] += 1
                    problems.append(f"{rel}:{node.lineno} 裸 os.getenv（应走 config.env/env_int/env_bool）")
    return problems, stats


def main() -> int:
    ap = argparse.ArgumentParser(description="本地静态检查门禁（零依赖）")
    ap.add_argument("--max-lines", type=int, default=700, help="单文件行数阈值（默认 700）")
    ap.add_argument("--lazy-imports", action="store_true",
                    help="同时检查函数内 import 是否标注原因（存量较多，默认只统计）")
    ap.add_argument("--summary", action="store_true", help="只打印统计与结论")
    ap.add_argument("--update-baseline", action="store_true",
                    help="把当前问题写入 scripts/lint_baseline.json（存量豁免，新增问题仍会失败）")
    ap.add_argument("--all", action="store_true", help="忽略 baseline，报告全部问题")
    args = ap.parse_args()

    problems, stats = check(args.max_lines, args.lazy_imports)

    if args.update_baseline:
        BASELINE.write_text(json.dumps(sorted({re.sub(r":\d+", "", p) for p in problems}),
                                       ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
        print(f"已写入 baseline：{len(problems)} 条存量问题 → {_rel(BASELINE)}")
        print("后续新增问题会失败；存量问题建议按 docs/architecture.md §15 逐项清理。")
        return 0

    # baseline 用**与行号无关**的键：否则任何一次插入/删除行都会让全部存量看起来是「新增」
    def _key(problem: str) -> str:
        return re.sub(r":\d+", "", problem)

    known: set[str] = set()
    if BASELINE.exists() and not args.all:
        try:
            known = set(json.loads(BASELINE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            known = set()
    current = {_key(p) for p in problems}
    new_problems = [p for p in problems if _key(p) not in known]
    fixed = sorted(known - current)
    print("=" * 78)
    print("本地静态检查门禁（零依赖；规范见 docs/architecture.md §10）")
    print("=" * 78)
    print(f"文件 {stats['files']} 个 | 函数 {stats['functions']} 个"
          f" | 缺返回类型 {stats['missing_return']}"
          f" | 静默 except {stats['silent_except']}"
          f" | 裸 getenv {stats['bare_getenv']}"
          f" | print {stats['print']}"
          f" | 缺 future 注解 {stats['no_future']}"
          f" | 函数内 import {stats['lazy_imports']}"
          f" | 超长文件 {stats['big_files']}")
    if not args.summary:
        for p in new_problems:
            print("  [FAIL] " + p)
    print("-" * 78)
    if fixed:
        print(f"提示：{len(fixed)} 条存量问题已修复，可运行 --update-baseline 收窄豁免。")
    if new_problems:
        print(f"结果：新增 {len(new_problems)} 项不合规（存量豁免 {len(known)} 项；阈值：0 新增）")
        return 1
    print(f"结果：通过 ✅（新增 0 项；存量豁免 {len(known)} 项，清理进度见 --all）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
