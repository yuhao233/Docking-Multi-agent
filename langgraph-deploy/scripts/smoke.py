#!/usr/bin/env python
"""部署冒烟：对**已启动的** LangGraph Agent Server 做一次真实调用。

    .venv/bin/python scripts/smoke.py [base_url]

流程（全部走 HTTP，不依赖 SDK 版本）：
  1) `GET /ok`                 服务存活；
  2) `POST /assistants/search` 7 个图都在；
  3) `POST /runs/wait`         用 `pipeline` 图跑一次**真实对接**（2 个分子，exhaustiveness=1）；
  4) 核对产物：run 目录里有 docking.json / report.md / ranking.csv，且图输出带 run_id。

只读项目目录、只往 `../projects/var/runs/` 写运行产物（与网页端同一处）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
_FLAGS = {a for a in sys.argv[1:] if a.startswith("--")}
BASE = (_ARGS[0] if _ARGS else "http://127.0.0.1:2024").rstrip("/")
WITH_COORDINATOR = "--with-coordinator" in _FLAGS
EXPECTED = {"coordinator", "pipeline", "intake", "property", "pocket", "docking", "binding"}

FAIL: list[str] = []


def call(path: str, payload: dict | None = None, method: str = "POST", timeout: int = 300):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"_raw": body}


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  →  {detail}" if detail else ""))
    if not ok:
        FAIL.append(label)


def main() -> int:
    print(f"目标 Agent Server：{BASE}\n")
    print("1) 服务存活")
    try:
        ok_body = call("/ok", None, "GET", timeout=10)
    except urllib.error.URLError as e:
        check(False, "GET /ok", f"{type(e).__name__}: {e}（服务没起？先 bash scripts/dev.sh）")
        return 1
    check(ok_body.get("ok") is True, "GET /ok", json.dumps(ok_body, ensure_ascii=False))

    print("\n2) 图清单")
    rows = call("/assistants/search", {"limit": 50})
    got = {r.get("graph_id") for r in rows}
    check(EXPECTED <= got, f"7 个图都已加载", ", ".join(sorted(got)))

    print("\n3) 真实调用 pipeline 图（2 个分子 / exhaustiveness=1）")
    site = {"site_center": [31.50, 13.74, 24.36], "site_size": [22.0, 22.0, 22.0]}
    payload = {
        "assistant_id": "pipeline",
        "input": {
            "ligands_text": "乙醇,CCO\n苯酚,Oc1ccccc1",
            "receptor": "thrombin",
            "pocket_engine": "known_site",
            "exhaustiveness": 1,
            "n_poses": 1,
            "save_poses": True,
            **site,
        },
    }
    t0 = time.time()
    try:
        out = call("/runs/wait", payload, "POST", timeout=1800)
    except urllib.error.HTTPError as e:
        check(False, "POST /runs/wait", f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
        return 1
    elapsed = time.time() - t0
    check(out.get("status") == "ok", f"流水线状态 ok（{elapsed:.0f}s）", str(out.get("status")))
    run_id = str(out.get("run_id") or "")
    check(bool(run_id), "图输出带 run_id", run_id)
    if out.get("message"):
        print(f"        message: {out['message']}")
    check(len(out.get("top") or []) >= 2, "排行前 5 有结果",
          json.dumps(out.get("top"), ensure_ascii=False)[:200])

    print("\n4) 产物落盘")
    run_dir = Path(out.get("run_dir") or "")
    check(run_dir.is_dir(), "run_dir 存在", str(run_dir))
    for name in ("run.json", "docking.json", "ranking.csv", "report.md"):
        p = run_dir / name
        check(p.is_file(), f"{name} 已生成", f"{p.stat().st_size} bytes" if p.is_file() else "缺失")
    arts = [a.get("name") for a in (out.get("artifacts") or [])]
    need = {"report_md", "report_pdf", "ranking_csv", "studio_result"}
    check(need <= set(arts), f"产物清单 {len(arts)} 项（含报告/PDF/CSV/Studio 摘要）",
          ", ".join(arts[:10]))

    print()
    if FAIL:
        print(f"\033[31m结果：{len(FAIL)} 项失败\033[0m")
        return 1
    print("\033[32m结果：全部通过\033[0m")
    print(f"产物目录：{run_dir}")
    return 0


def coordinator_step() -> None:
    """可选：真跑一次多 Agent 主入口（真实 LLM + 4 个子 Agent + 真实对接）。"""
    print("\n5) 真实调用 coordinator 图（多 Agent，真实 LLM + 真实对接）")
    payload = {
        "assistant_id": "coordinator",
        "config": {"recursion_limit": 60},
        "input": {"messages": [{"role": "user", "content":
                  "用预置受体 thrombin、对接盒 center=31.5,13.74,24.36 / size=22,22,22，"
                  "对下面两个分子执行完整筛选：乙醇 CCO、苯酚 Oc1ccccc1。"
                  "步骤：导入分子库 → 属性评估 → 直接对接（exhaustiveness=1、n_poses=1、"
                  "pocket_engine=known_site）→ 生成报告。不要再向我确认，直接执行完。"}]},
    }
    t0 = time.time()
    try:
        out = call("/runs/wait", payload, "POST", timeout=3600)
    except urllib.error.HTTPError as e:
        check(False, "coordinator 运行", f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
        return
    elapsed = time.time() - t0
    msgs = out.get("messages") or []
    final = ""
    for m in reversed(msgs):
        content = m.get("content") if isinstance(m, dict) else None
        if m.get("type") == "ai" and content:
            final = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            break
    check(bool(msgs), f"返回消息 {len(msgs)} 条（{elapsed:.0f}s）",
          f"最终回答 {len(final)} 字")
    run_id = str(out.get("run_id") or "")
    run_dir = Path(out.get("run_dir") or "")
    check(run_dir.is_dir(), "coordinator 运行目录存在", str(run_dir))
    for name in ("docking.json", "report.md"):
        check((run_dir / name).is_file(), f"{name} 已生成",
              f"{(run_dir / name).stat().st_size} bytes" if (run_dir / name).is_file() else "缺失")
    if run_id:
        print(f"        run_id={run_id}")
    if final:
        print(f"        最终回答节选：{final[:160].replace(chr(10), ' ')}…")


if __name__ == "__main__":
    if WITH_COORDINATOR:
        rc = main()
        coordinator_step()
        if FAIL:
            print(f"\n\033[31m（含 coordinator 步骤）结果：{len(FAIL)} 项失败\033[0m")
            raise SystemExit(1)
        print("\033[32m（含 coordinator 步骤）结果：全部通过\033[0m")
        raise SystemExit(rc)
    raise SystemExit(main())
