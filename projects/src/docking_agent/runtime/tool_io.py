"""工具大结果的「落盘 + 有界摘要」基础设施。

## 为什么必须这么做

Agent 的上下文会被分子数**线性撑大**，而这两头都会炸：

| 数据 | 每分子 | 1 万分子 | 折算 tokens |
| --- | --- | --- | --- |
| 分子清单（import） | ~50 B | ~0.5 MB | ~13 万 |
| 对接结果（docking） | ~491 B | **4.7 MB** | **~130 万** |
| 理化性质（properties） | ~600 B | ~6 MB | ~160 万 |

任何上下文窗口（64k/128k）都装不下，而且子 Agent 还要把结果**再输出一遍**
（输出上限更低），必然截断 → JSON 不合法 → `agent_output_invalid`。

## 约定

1. 工具把**完整结果**写到运行目录（`*_tool.json`，登记为可下载产物）并写入共享黑板；
2. 返回给模型的载荷：
   - **小结果（≤ `AGENT_TOOL_TOP_N` 条）**：保持原来的完整结构（向后兼容，历史工具契约不变）；
   - **大结果**：只给 `summary + top N + artifacts`，并置 `detail_omitted: true` 说明明细在产物里；
3. 落盘层（`agents/persistence.py`）与报告工具**从运行目录/共享黑板读完整数据**，
   不再依赖模型把结果搬运回上下文。

**模块位置**：本模块属 `runtime/` 层（工具结果的落盘/摘要基础设施），原先在 `agents/` 下 ——
那会让 `tools/*` 反向 import `agents`（审计 SCC-3 的一部分）。

注意：`AGENT_TOOL_TOP_N` 限制的是**给模型看的明细条数（视图大小）**，
**不是**"最多对接多少个分子"——对接规模由任务本身决定（流式/分片/流水线都不受影响）。
"""
from __future__ import annotations

import json
import logging
import os
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from docking_agent.config import env_int
from docking_agent.runs import current_run

logger = logging.getLogger(__name__)

# 工具原始返回的落盘文件名（运行目录内）
TOOL_FILES: Dict[str, str] = {
    "molecules": "molecules_tool.json",
    "properties": "properties_tool.json",
    "docking": "docking_tool.json",
    "binding": "binding_tool.json",
    "pockets": "pockets_tool.json",
}
TOOL_LABELS: Dict[str, str] = {
    "molecules": "分子库（工具原始返回，完整明细）",
    "properties": "理化性质（工具原始返回，完整明细）",
    "docking": "对接结果（工具原始返回，完整明细）",
    "binding": "结合模式结果（工具原始返回，完整明细）",
    "pockets": "口袋预测（工具原始返回，完整明细）",
}


def summary_limit() -> int:
    """给模型看的明细条数上限（视图大小，不是对接规模上限）。"""
    return max(1, min(500, env_int("AGENT_TOOL_TOP_N", 20)))


def record(name: str, payload: Any, run: Any = None) -> Optional[Path]:
    """把工具的完整结果写入运行目录；没有运行上下文时静默跳过（CLI/单测场景）。

    `run`：显式传入的运行对象（图调用链路上由 `active_run(runtime)` 给），
    为空时回退 ContextVar（CLI / 单测直调）。
    """
    run = run if run is not None else current_run.get()
    if run is None:
        return None
    rel = TOOL_FILES.get(name)
    if not rel:
        return None
    try:
        path = run.write_json(f"{name}_tool", payload, rel_path=rel,
                              label=TOOL_LABELS.get(name, name))
    except Exception as e:  # noqa: BLE001
        logger.warning("工具原始结果落盘失败(%s)：%s", name, e)
        return None
    # 记住「这个工具的完整明细在哪个文件」：它是 Agent 之间**按文件交接**数据的总线 ——
    # 上万条分子/对接明细不必再经消息或黑板搬运，给下一个 Agent 一个绝对路径即可。
    try:
        run.data.setdefault("tool_files", {})[name] = str(path)
    except Exception as e:  # noqa: BLE001 - 记账失败不影响落盘
        logger.debug("记录工具产物路径失败(%s)：%s", name, e)
    return path


def artifact_path(name: str, run: Any = None) -> str:
    """该工具**最近一次**产物文件的绝对路径（不存在则空串）。Agent 间按文件交接用它。"""
    run = run if run is not None else current_run.get()
    if run is None:
        return ""
    path = str((run.data.get("tool_files") or {}).get(name) or "")
    return path if path and os.path.isfile(path) else ""


def load(name: str, run: Any = None) -> Optional[Any]:
    """读取工具的完整结果（优先当前运行目录；供落盘层与报告工具使用）。"""
    run = run if run is not None else current_run.get()
    if run is None:
        return None
    rel = TOOL_FILES.get(name)
    if not rel:
        return None
    path = run.dir / rel
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("工具原始结果读取失败(%s)：%s", rel, e)
        return None


def top_rows(rows: Sequence[Dict[str, Any]], *, limit: int,
             fields: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """取前 N 行（可按字段裁剪，压缩给模型的体积）。"""
    out: List[Dict[str, Any]] = []
    for row in list(rows)[:limit]:
        if not isinstance(row, dict):
            continue
        if fields:
            out.append({k: row.get(k) for k in fields if row.get(k) is not None})
        else:
            out.append(dict(row))
    return out


def affinity_stats(values: Sequence[Any]) -> Dict[str, Any]:
    """亲和力统计（真实数值；用于摘要里给出整体画像）。"""
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    if not nums:
        return {"count": 0}
    return {"count": len(nums), "best": round(min(nums), 3), "worst": round(max(nums), 3),
            "median": round(statistics.median(nums), 3),
            "mean": round(sum(nums) / len(nums), 3)}


def artifact_refs(run_id: str = "", run: Any = None) -> Dict[str, str]:
    """告诉模型「明细在哪里」，而不是把明细塞给它。

    同时给出**绝对路径**（`molecules_file` / `properties_file` / `docking_file` / …）：
    子 Agent 可以直接把路径作为参数交给下一个工具，按文件交接数据（省 token、零失真）；
    共享黑板只用来交接**小状态**（受体、位点、阳性对照、计数）。
    """
    refs: Dict[str, str] = {"run_id": run_id}
    run = run if run is not None else current_run.get()
    if run is not None:
        refs["run_id"] = run.id
        refs["artifacts_api"] = f"/api/runs/{run.id}"
        refs["zip"] = f"/api/runs/{run.id}/download.zip"
    for name, key in (("molecules", "molecules_file"), ("properties", "properties_file"),
                      ("docking", "docking_file"), ("pockets", "pockets_file"),
                      ("binding", "binding_file")):
        path = artifact_path(name)
        if path:
            refs[key] = path
    return refs


def big_payload_notice(name: str, total: int, limit: int) -> str:
    return (f"{name}共 {total} 条，超过单次回传上限 {limit} 条：**完整明细已写入运行产物"
            f"（{TOOL_FILES.get(name, name)}）**，请只依据下面的 summary 与 top 列表汇报，"
            "不要把明细逐条复述，也不要凭记忆补全未列出的分子。")


# --------------------------------------------------------------------------- #
# 两阶段漏斗（大库的正确打法）：先全库粗筛，再对头部精算
# --------------------------------------------------------------------------- #
def funnel_settings() -> Dict[str, Any]:
    """漏斗与分片护栏参数（设置页面可改）。"""
    return {
        "funnel_min": max(0, env_int("AGENT_FUNNEL_MIN", 500)),
        "refine_top_n": max(1, min(5000, env_int("AGENT_REFINE_TOP_N", 200))),
        "coarse_exhaustiveness": max(1, min(32, env_int("AGENT_COARSE_EXHAUSTIVENESS", 1))),
        "fine_exhaustiveness": max(1, min(64, env_int("AGENT_FINE_EXHAUSTIVENESS", 16))),
        "shard_max": max(1, min(64, env_int("AGENT_SHARD_MAX", 8))),
    }


def funnel_advice(total: int) -> str:
    """给定分子数，给出该不该走漏斗、以及具体怎么打（写进给模型的提示）。"""
    s = funnel_settings()
    if not s["funnel_min"] or total < s["funnel_min"]:
        return ""
    return (f"候选数 {total} ≥ {s['funnel_min']}：请走**两阶段漏斗** —— "
            f"① 先全库粗筛 run_docking(exhaustiveness={s['coarse_exhaustiveness']})；"
            f"② 再只对头部 run_docking(top_from_previous={s['refine_top_n']}, "
            f"exhaustiveness={s['fine_exhaustiveness']}) 精算。"
            f"**不要**按固定分子数逐片调度（片数越多，LLM 开销越大）；"
            f"确需分片时片数不超过 {s['shard_max']}。")
