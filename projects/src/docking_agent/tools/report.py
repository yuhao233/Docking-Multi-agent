"""整体协调 Agent 的报告工具。

图表与排序文件均由**真实计算结果**生成，落地为本地产物并提供下载 URL。
绘图/CSV 逻辑在 `docking_agent.reporting`，本模块只做 @tool 适配。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from langchain.tools import tool

from docking_agent.core import POSITIVE_CONTROL_NAME
from docking_agent.reporting import (
    build_ranking_csv,
    docking_bar_chart,
    property_scatter_chart,
    rank_molecules,
    save_artifact,
    similarity_chart,
)
from docking_agent.runtime.context import AgentContext, active_blackboard
from langchain.tools import ToolRuntime

logger = logging.getLogger(__name__)


def _from_blackboard(pos_control: Dict[str, Any],
                     runtime: Any = None) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """从共享黑板汇总「分子 + 性质 + 对接 + 结合模式」并融合排序。"""
    from docking_agent.core import merge_and_rank

    board = active_blackboard(runtime)
    if board is None:
        return [], pos_control
    molecules = board.molecules()
    props = board.properties()
    docking = board.docking()
    binding = {"rows": board.binding()}
    if board.positive_control and not (pos_control or {}).get("smiles"):
        pos_control = dict(pos_control or {})
        pos_control.setdefault("smiles", board.positive_control)
    if not molecules and not docking:
        return [], pos_control
    docking_rows = [r for r in docking if r.get("name") != POSITIVE_CONTROL_NAME]
    ranked = rank_molecules(merge_and_rank(props or molecules, docking_rows, binding))
    return ranked, pos_control


@tool
def generate_screening_report(aggregated_json: str = "", runtime: ToolRuntime[AgentContext] = None) -> str:
    """基于汇总结果生成真实可视化与排序产物（本地文件 + 下载 URL）。

    aggregated_json 形如：
    {
      "molecules": [分子（含属性、亲和力、引擎、相似度）],   # 无需预先排序，工具会按亲和力升序整理
      "positive_control": {"name":..., "smiles":..., "affinity_kcal_mol":...}
    }

    返回 JSON：docking_chart_url、similarity_chart_url、property_chart_url、screening_csv_url，
    以及 artifacts 明细（含本机绝对路径 path）。CSV 自带 engine / exhaustiveness 参数列。
    """
    try:
        data: Dict[str, Any] = {}
        if (aggregated_json or "").strip():
            try:
                data = json.loads(aggregated_json)
            except json.JSONDecodeError:
                data = {}
        molecules = rank_molecules(list(data.get("molecules") or []))
        pos_control: Dict[str, Any] = data.get("positive_control", {}) or {}
        if not molecules:
            # 横向协作：参数留空（或只给了阳性对照）时，直接从共享黑板取完整数据。
            # 这样协调 Agent 不必把上万条明细塞进工具参数/上下文。
            molecules, pos_control = _from_blackboard(pos_control, runtime)
        if not molecules:
            return json.dumps(
                {"status": "no_data",
                 "message": "没有可生成报告的数据：共享黑板上还没有分子与对接结果。"},
                ensure_ascii=False)

        dock = save_artifact(docking_bar_chart(molecules, pos_control),
                             "screening_docking_affinity.png", "image/png")
        sim = save_artifact(similarity_chart(molecules), "screening_similarity.png", "image/png")
        prop = save_artifact(property_scatter_chart(molecules), "screening_property_space.png", "image/png")
        # CSV 必须以 UTF-8 原始字节写出（历史上误用 json 编码，会产出无法解析的伪 CSV）
        csv_bytes = build_ranking_csv(molecules, pos_control).encode("utf-8")
        csv = save_artifact(csv_bytes, "screening_ranking.csv", "text/csv; charset=utf-8")

        return json.dumps({
            "status": "ok",
            "docking_chart_url": dock["url"],
            "similarity_chart_url": sim["url"],
            "property_chart_url": prop["url"],
            "screening_csv_url": csv["url"],
            "artifacts": {
                "docking_chart": dock,
                "similarity_chart": sim,
                "property_chart": prop,
                "screening_csv": csv,
            },
        }, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        logger.exception("报告生成失败")
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)
