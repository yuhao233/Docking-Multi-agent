#!/usr/bin/env python
"""受理层（intake）评估：用固定用例集量「任务理解」的准确率、越界拒绝与延迟。

为什么需要它：拆分受理层的目的之一就是**让"理解用户要什么"变得可测**。
本脚本把一批典型输入（明确 / 含糊 / 越界 / 多意图 / 参数冲突 / 只给名称）跑一遍，
给出确定性与（可选）真实模型两条路径的结果对照与耗时。

用法：
    .venv/bin/python scripts/intake_eval.py            # 只跑确定性规则（零模型、毫秒级）
    .venv/bin/python scripts/intake_eval.py --live     # 额外跑真实受理模型（需要 .env 里的 LLM 配置）
    .venv/bin/python scripts/intake_eval.py --live --only-llm-cases
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

from docking_agent import intake  # noqa: E402
from docking_agent.api.schemas import AgentRequest  # noqa: E402

# (用例名, 请求字段, 期望：{字段路径: 期望值}, 是否需要模型理解)
CASES: List[Tuple[str, Dict[str, Any], Dict[str, Any], bool]] = [
    ("参数模式·明确分子", {"mode": "manual", "ligands_text": "A:CCO,B:CCC"},
     {"authority": "manual", "decision": "run", "needs_llm": False,
      "ligands.source": "text", "task_type": "screening"}, False),
    ("参数模式·只做性质", {"mode": "manual", "ligands_text": "A:CCO", "message": "只看类药性"},
     {"authority": "manual", "decision": "run", "needs_llm": False}, False),
    ("参数模式·给了位点", {"mode": "manual", "ligands_text": "A:CCO",
                     "site_center": [31.5, 13.74, 24.36], "site_size": [22, 22, 22]},
     {"site.source": "user", "receptor.source": "user"}, False),
    ("对话折叠·示例库", {"mode": "chat", "advanced": False, "message": "用示例库筛选一下"},
     {"authority": "chat", "ligands.source": "library", "needs_llm": True}, True),
    ("对话折叠·表单被忽略", {"mode": "chat", "advanced": False, "message": "筛一下",
                      "ligands_text": "A:CCO", "exhaustiveness": 9},
     # 折叠时表单**完全不生效**：分子来源回落到示例库，搜索强度取系统默认 16
     # （v0.24 起默认值由 6 改为 16；这里曾长期留在 6，导致用例静默失效 —— 见 tests/test_intake.py）
     {"ligands.source": "library", "params.exhaustiveness": 16}, True),
    ("对话展开·指令提到受体", {"mode": "chat", "advanced": True, "message": "用 trypsin 做筛选",
                       "ligands_text": "A:CCO"},
     {"receptor.name": "trypsin", "receptor.source": "user"}, True),
    ("对话展开·只给分子名称", {"mode": "chat", "advanced": True, "message": "帮我对接阿司匹林和布洛芬"},
     {"needs_llm": True}, True),
    ("对话展开·多维度意图", {"mode": "chat", "advanced": True,
                      "message": "对接排序，同时看类药性和结合模式", "ligands_text": "A:CCO"},
     {"task_type": "screening"}, True),
    ("对话展开·越界（闲聊）", {"mode": "chat", "advanced": True, "message": "今天天气怎么样"},
     {"needs_llm": True}, True),
    ("对话展开·无关请求", {"mode": "chat", "advanced": True, "message": "帮我订一份午饭"},
     {"needs_llm": True}, True),
    ("对话展开·只给阳性对照", {"mode": "chat", "advanced": True, "message": "用苯甲脒做对照分析",
                       "ligands_text": "A:CCO", "positive_control": "NC(=N)c1ccccc1"},
     {"positive_control.provided_by": "user"}, True),
    ("对话折叠·明确要求跳过对照", {"mode": "chat", "advanced": True, "message": "不要做对照分析",
                          "ligands_text": "A:CCO", "skip_positive_control": True},
     {"positive_control.provided_by": "skipped"}, True),
    ("参数模式·上传受体文件", {"mode": "manual", "ligands_text": "A:CCO",
                       "receptor_file": "assets/uploads/x.pdb"},
     {"receptor.file": "assets/uploads/x.pdb", "receptor.source": "user"}, False),
    ("空输入", {"mode": "chat", "advanced": True, "message": ""},
     {"needs_llm": False, "ligands.source": "library"}, False),
]


def _dig(data: Dict[str, Any], path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _build(kwargs: Dict[str, Any]) -> AgentRequest:
    base = {"mode": "manual", "message": "", "receptor": "thrombin", "ligands_text": ""}
    base.update(kwargs)
    return AgentRequest(**base)


def main() -> int:
    ap = argparse.ArgumentParser(description="受理层评估")
    ap.add_argument("--live", action="store_true", help="额外调用真实受理模型（需要 LLM 配置）")
    ap.add_argument("--only-llm-cases", action="store_true", help="只跑需要模型理解的用例")
    args = ap.parse_args()

    print("=" * 96)
    print("任务受理层评估（确定性规则" + (" + 真实模型" if args.live else "") + "）")
    print("=" * 96)
    header = f"{'用例':30s} {'期望命中':>8s} {'决策':>8s} {'任务类型':16s} {'耗时(ms)':>9s}"
    if args.live:
        header += f" {'模型':>6s} {'LLM耗时(ms)':>11s}"
    print(header)
    print("-" * 96)

    failures: List[str] = []
    llm_calls = 0
    llm_ms_total = 0.0
    deterministic_ms_total = 0.0
    rows = 0

    for name, kwargs, expects, needs_llm in CASES:
        if args.only_llm_cases and not needs_llm:
            continue
        req = _build(kwargs)
        t0 = time.perf_counter()
        spec = intake.build_task_spec(req)
        ms = (time.perf_counter() - t0) * 1000
        deterministic_ms_total += ms
        rows += 1

        llm_used = ""
        llm_ms = 0.0
        if args.live and spec.get("needs_llm"):
            t1 = time.perf_counter()
            spec = intake.refine_task_spec(spec)
            llm_ms = (time.perf_counter() - t1) * 1000
            llm_ms_total += llm_ms
            llm_used = spec.get("llm_status", "")
            if llm_used == "ok":
                llm_calls += 1

        hits, misses = 0, []
        for path, want in expects.items():
            got = _dig(spec, path)
            if got == want:
                hits += 1
            else:
                misses.append(f"{path}={got!r}≠{want!r}")
        if misses:
            failures.append(f"{name}: " + "；".join(misses))

        line = (f"{name:30s} {hits}/{len(expects):>6d} {str(spec.get('decision')):>8s} "
                f"{str(spec.get('task_type')):16s} {ms:9.2f}")
        if args.live:
            line += f" {llm_used:>6s} {llm_ms:11.1f}"
        print(line)
        if not misses:
            continue

    print("-" * 96)
    print(f"用例数：{rows}　确定性耗时合计：{deterministic_ms_total:.1f} ms"
          f"（平均 {deterministic_ms_total / max(rows, 1):.2f} ms/次，零模型调用）")
    if args.live:
        print(f"受理模型：调用 {llm_calls} 次，耗时合计 {llm_ms_total:.0f} ms"
              f"（平均 {llm_ms_total / max(llm_calls, 1):.0f} ms/次）")
    if failures:
        print(f"\n未命中 {len(failures)} 项：")
        for item in failures:
            print("  -", item)
    else:
        print("\n✅ 全部用例符合预期")
    print("=" * 96)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
