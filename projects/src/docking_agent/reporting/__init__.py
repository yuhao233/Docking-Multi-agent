"""报告与产物层：图表、排序 CSV、Markdown 报告、产物存储。

注意：图表模块依赖 matplotlib，这里**惰性导入**——只用到 store/tables 的调用方
（例如对接 worker、运行记录）不应被迫加载 matplotlib（会显著拖慢进程启动）。
"""
from typing import Any

from docking_agent.reporting.artifacts import (  # noqa: F401
    copy_receptor_files,
    write_report_charts,
    write_report_pdf,
)
from docking_agent.reporting.pdf import build_report_pdf  # noqa: F401
from docking_agent.reporting.recommend import build_recommendations, parse_weights  # noqa: F401
from docking_agent.reporting.report import build_markdown_report  # noqa: F401
from docking_agent.reporting.store import (  # noqa: F401
    artifact_base_url,
    content_type_for,
    resolve_output,
    safe_name,
    save_artifact,
)
from docking_agent.reporting.tables import (  # noqa: F401
    CSV_FIELDS,
    build_ranking_csv,
    collect_failures,
    rank_molecules,
)

_CHART_EXPORTS = {"docking_bar_chart", "similarity_chart", "property_scatter_chart",
                  "affinity_histogram", "structure_grid", "binding_scatter",
                  "recommendation_structure_grid"}

__all__ = [
    "docking_bar_chart",
    "similarity_chart",
    "property_scatter_chart",
    "affinity_histogram",
    "recommendation_structure_grid",
    "build_recommendations",
    "parse_weights",
    "build_ranking_csv",
    "collect_failures",
    "rank_molecules",
    "CSV_FIELDS",
    "build_markdown_report",
    "build_report_pdf",
    "write_report_charts",
    "write_report_pdf",
    "save_artifact",
    "resolve_output",
    "artifact_base_url",
    "content_type_for",
    "safe_name",
]


def __getattr__(name: str) -> Any:
    """惰性暴露图表函数（首次访问时才导入 matplotlib）。"""
    if name in _CHART_EXPORTS:
        from docking_agent.reporting import charts

        return getattr(charts, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
