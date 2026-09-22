#!/usr/bin/env python
"""运行记录保留策略：清理过旧的 `var/runs/<id>/` 与 `var/threads/*.json`。

用法：
    .venv/bin/python scripts/prune_runs.py                    # 只报告（默认 dry-run）
    .venv/bin/python scripts/prune_runs.py --apply            # 真正删除
    .venv/bin/python scripts/prune_runs.py --apply --keep-last 50 --max-age-days 7
    .venv/bin/python scripts/prune_runs.py --apply --threads  # 同时清理旧线程文件
    .venv/bin/python scripts/prune_runs.py --json             # 机器可读（CI / 定时任务）

默认值可用环境变量覆盖：`RUNS_KEEP_LAST`（默认 200）、`RUNS_MAX_AGE_DAYS`（默认 30）。

为什么要有它：运行目录每次筛选都会落盘（产物 + 位姿 + 图表），本机实测已到
**2.2 GB / 4900+ 个目录**，而 `reconcile_interrupted()` 启动时要遍历全部目录。
没有保留策略时只能人工 `rm -rf`，风险高且没有报告。

安全取向：
  * **默认 dry-run**，必须显式 `--apply` 才删除；
  * 删除条件「不属于最近 N 个」**且**「超过 M 天」两条同时满足（保守）；
  * `status == "running"` 的运行永不删除。

退出码：0 = 成功；2 = 参数非法。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import env_float, env_int, ensure_runtime_env  # noqa: E402

ensure_runtime_env()


def _prune_threads(max_age_days: float, *, dry_run: bool) -> Dict[str, Any]:
    """清理 `var/threads/*.json`（按文件年龄；线程文件很小，只做数量控制）。"""
    from docking_agent.paths import var_dir

    threads_dir = var_dir() / "threads"
    report: Dict[str, Any] = {"dir": str(threads_dir), "candidates": [], "deleted": [],
                              "errors": []}
    if not threads_dir.is_dir():
        return report
    now = time.time()
    for path in sorted(threads_dir.glob("*.json")):
        try:
            age_days = (now - path.stat().st_mtime) / 86400.0
        except OSError:
            continue
        if max_age_days > 0 and age_days <= max_age_days:
            continue
        report["candidates"].append({"name": path.name, "age_days": round(age_days, 1)})
        if dry_run:
            continue
        try:
            path.unlink()
            report["deleted"].append(path.name)
        except OSError as exc:
            report["errors"].append(f"{path.name}: {exc}")
    return report


def _mb(value: float) -> str:
    return f"{value / (1024 * 1024):.1f} MB"


def main() -> int:
    parser = argparse.ArgumentParser(description="运行记录保留策略（默认只报告）")
    parser.add_argument("--keep-last", type=int,
                        default=env_int("RUNS_KEEP_LAST", 200),
                        help="至少保留最近的 N 个运行（默认 RUNS_KEEP_LAST=200）")
    parser.add_argument("--max-age-days", type=float,
                        default=env_float("RUNS_MAX_AGE_DAYS", 30.0),
                        help="只删除超过 M 天的运行（默认 RUNS_MAX_AGE_DAYS=30；0=不限年龄）")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认 dry-run）")
    parser.add_argument("--threads", action="store_true", help="同时按年龄清理 var/threads/*.json")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args()
    if args.keep_last < 0 or args.max_age_days < 0:
        print("--keep-last / --max-age-days 不能为负", file=sys.stderr)
        return 2

    from docking_agent.runs import get_run_store

    dry_run = not args.apply
    runs_report = get_run_store().prune(keep_last=args.keep_last,
                                        max_age_days=args.max_age_days, dry_run=dry_run)
    threads_report: Dict[str, Any] = {}
    if args.threads:
        threads_report = _prune_threads(args.max_age_days, dry_run=dry_run)

    if args.json:
        print(json.dumps({"runs": runs_report, "threads": threads_report,
                          "applied": args.apply}, ensure_ascii=False, indent=1))
        return 0

    mode = "删除" if args.apply else "仅报告（加 --apply 才会删除）"
    print(f"运行目录：{get_run_store().root}  [{mode}]")
    print(f"保留策略：最近 {args.keep_last} 个 且 不超过 {args.max_age_days:g} 天")
    candidates: List[Dict[str, Any]] = runs_report["candidates"]
    print(f"命中 {len(candidates)} 个目录，合计 "
          f"{_mb(runs_report['freed_bytes'])}；已删 {len(runs_report['deleted'])} 个")
    for item in candidates[:20]:
        print(f"  - {item['run_id']}  {item['age_days']:>6.1f} 天  "
              f"{_mb(item['size_bytes'])}")
    if len(candidates) > 20:
        print(f"  … 其余 {len(candidates) - 20} 个见 --json")
    for err in runs_report["errors"]:
        print(f"  ! {err}", file=sys.stderr)
    if args.threads:
        print(f"线程文件：命中 {len(threads_report.get('candidates', []))} 个，"
              f"已删 {len(threads_report.get('deleted', []))} 个")
    if dry_run and candidates:
        print("（dry-run：磁盘未改动；确认无误后加 --apply）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
