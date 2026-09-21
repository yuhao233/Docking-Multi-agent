#!/usr/bin/env python
"""渲染架构图（README / 技术报告用），输出到 `docs/images/`。

架构图必须随代码演进，因此由脚本生成而不是手绘：

    .venv/bin/python scripts/render_architecture.py
    .venv/bin/python scripts/render_architecture.py --out DIR

排版约束：每个框只有「标题 + 一行短说明」，细节写入 README 正文；框内文字按字体实际高度
排布，带区之间留足间距，避免拥挤与文字压叠。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")
from matplotlib import font_manager, patches, rcParams  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "images"

COLORS = {
    "user": "#2f6f9f",
    "intake": "#7a5ea8",
    "agent": "#1f7a5a",
    "worker": "#2c7a7b",
    "share": "#b06d1f",
    "engine": "#9c3f4c",
    "store": "#4a5568",
    "line": "#5a6472",
}

FONT_CANDIDATES = ["Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei", "SimHei"]
TITLE_SIZE = 13.5
LINE_SIZE = 10.5
CAPTION_SIZE = 9.5


def _configure_font() -> None:
    available = {f.name for f in font_manager.fontManager.ttflist}
    chosen = [name for name in FONT_CANDIDATES if name in available]
    rcParams["font.sans-serif"] = chosen + ["DejaVu Sans"]
    rcParams["font.family"] = "sans-serif"
    rcParams["axes.unicode_minus"] = False


def _text_units(ax: plt.Axes, size_pt: float, *, leading: float = 1.6) -> float:
    """字号换算成纵轴坐标单位（1.0 = 整幅图高）。"""
    return size_pt * leading / (ax.figure.get_figheight() * 72.0)


def _box(ax: plt.Axes, x: float, y: float, w: float, h: float, title: str,
         lines: Sequence[str] = (), color: str = "#4a5568",
         title_size: float = TITLE_SIZE, line_size: float = LINE_SIZE) -> None:
    ax.add_patch(patches.FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.010,rounding_size=0.018",
        linewidth=1.4, edgecolor=color, facecolor=color + "12"))
    title_h = _text_units(ax, title_size, leading=1.8)
    ax.text(x + w / 2, y + h - title_h * 0.70, title, ha="center", va="center",
            fontsize=title_size, color=color, fontweight="bold")
    if lines:
        row_h = _text_units(ax, line_size, leading=1.7)
        content_top = y + h - title_h * 1.45
        for i, line in enumerate(lines):
            ax.text(x + w / 2, content_top - row_h * (i + 0.45), line,
                    ha="center", va="center", fontsize=line_size, color="#3d4653")


def _arrow(ax: plt.Axes, start: Tuple[float, float], end: Tuple[float, float],
           *, color: str = "#5a6472", style: str = "-|>", width: float = 1.6,
           label: str = "") -> None:
    ax.add_patch(patches.FancyArrowPatch(
        start, end, arrowstyle=style, mutation_scale=17, linewidth=width,
        color=color, shrinkA=2.0, shrinkB=2.0, zorder=3))
    if label:
        ax.text((start[0] + end[0]) / 2, (start[1] + end[1]) / 2, label,
                ha="center", va="center", fontsize=CAPTION_SIZE, color=color,
                bbox={"facecolor": "white", "edgecolor": "none", "pad": 2.0}, zorder=4)


def render_architecture(out: Path) -> Path:
    """协作架构：每层只写「是什么 + 一句话」，细节留给 README 正文与图注。"""
    fig = plt.figure(figsize=(14.0, 12.8), dpi=160)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    ax.text(0.5, 0.982, "分子对接多 Agent 协作系统 · 协作架构",
            ha="center", va="top", fontsize=21, fontweight="bold", color="#1f2733")

    bands = [("users", 0.085), ("intake", 0.085), ("coord", 0.092), ("workers", 0.090),
             ("share", 0.085), ("core", 0.085), ("store", 0.090)]
    top, gap = 0.868, 0.034
    y: Dict[str, float] = {}
    cursor = top
    for name, height in bands:
        y[name] = cursor - height
        cursor = y[name] - gap

    _box(ax, 0.045, y["users"], 0.28, 0.085, "对话模式", ["网页 · 自然语言指令"], COLORS["user"])
    _box(ax, 0.360, y["users"], 0.28, 0.085, "参数模式", ["网页 · 表单参数权威"], COLORS["user"])
    _box(ax, 0.675, y["users"], 0.28, 0.085, "Studio / SDK", ["LangGraph 平台面"], COLORS["user"])

    _box(ax, 0.045, y["intake"], 0.91, 0.085, "任务受理层 intake",
         ["结构化任务规约 · 决策 run / ask / reject"], COLORS["intake"])
    _arrow(ax, (0.5, y["users"]), (0.5, y["intake"] + 0.085), color=COLORS["intake"])

    _box(ax, 0.045, y["coord"], 0.91, 0.092, "整体协调 Agent",
         ["决定执行环节与顺序 · 汇总结果 · 定制报告"], COLORS["agent"], title_size=15)
    _arrow(ax, (0.5, y["intake"]), (0.5, y["coord"] + 0.092), color=COLORS["agent"])

    workers = [("口袋分析 Agent", "P2Rank · 几何法"),
               ("属性评估 Agent", "RDKit 物化性质"),
               ("Docking 执行 Agent", "Vina · 两阶段漏斗"),
               ("结合模式检测 Agent", "指纹 · 阳性对照")]
    xs = [0.045, 0.272, 0.499, 0.726]
    widths = [0.219, 0.219, 0.219, 0.229]
    for (title, line), wx, ww in zip(workers, xs, widths):
        _box(ax, wx, y["workers"], ww, 0.090, title, [line], COLORS["worker"],
             title_size=12.5, line_size=10.0)
        _arrow(ax, (wx + ww / 2, y["coord"]), (wx + ww / 2, y["workers"] + 0.090),
               color=COLORS["agent"])
    label_y = (y["coord"] + y["workers"] + 0.090) / 2
    ax.text(0.5, label_y, "无状态执行器 · 每次独立线程", ha="center", va="center",
            fontsize=CAPTION_SIZE, color=COLORS["agent"],
            bbox={"facecolor": "white", "edgecolor": "none", "pad": 2.0}, zorder=5)

    _box(ax, 0.045, y["share"], 0.445, 0.085, "共享黑板",
         ["受体 · 位点盒 · 分子库 · 阳性对照"], COLORS["share"])
    _box(ax, 0.510, y["share"], 0.445, 0.085, "运行产物文件",
         ["分子库 · 性质 · 对接明细 JSON"], COLORS["share"])
    _arrow(ax, (0.30, y["workers"]), (0.30, y["share"] + 0.085), color=COLORS["share"], style="<|-|>")
    _arrow(ax, (0.70, y["workers"]), (0.70, y["share"] + 0.085), color=COLORS["share"], style="<|-|>")

    _box(ax, 0.045, y["core"], 0.91, 0.085, "真实计算核心",
         ["AutoDock Vina · RDKit · Meeko · P2Rank · pdb2pqr"], COLORS["engine"])
    _arrow(ax, (0.30, y["share"]), (0.30, y["core"] + 0.085), color=COLORS["engine"])
    _arrow(ax, (0.70, y["share"]), (0.70, y["core"] + 0.085), color=COLORS["engine"])

    _box(ax, 0.045, y["store"], 0.91, 0.090, "运行记录与交付：var/runs/<run_id>/",
         ["报告 · 排序 CSV · 图表 · 位姿 · 受体结构 · 整包 ZIP"], COLORS["store"], title_size=13)
    _arrow(ax, (0.5, y["core"]), (0.5, y["store"] + 0.090), color=COLORS["store"])

    caption_y = y["store"] - 0.020
    ax.text(0.045, caption_y,
            "协调 Agent 工具：import_molecule_library · run_pocket_analysis · run_property_assessment · "
            "run_docking · run_binding_mode_analysis ·",
            ha="left", va="top", fontsize=CAPTION_SIZE, color="#6b7280")
    ax.text(0.045, caption_y - 0.019,
            "recommend_compounds · submit_recommendations · customize_report · "
            "generate_screening_report · fetch_protein_structure · fetch_molecule_record",
            ha="left", va="top", fontsize=CAPTION_SIZE, color="#6b7280")

    fig.savefig(out, facecolor="white")
    plt.close(fig)
    return out


def render_lifecycle(out: Path) -> Path:
    """一次运行的链路：泳道 + 九步，标签保持短句。"""
    fig = plt.figure(figsize=(14.0, 7.2), dpi=160)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.text(0.5, 0.975, "一次运行的完整链路", ha="center", va="top",
            fontsize=19, fontweight="bold", color="#1f2733")

    lanes = [("网页", 0.09, COLORS["user"]), ("受理 intake", 0.28, COLORS["intake"]),
             ("协调 Agent", 0.47, COLORS["agent"]), ("子 Agent", 0.66, COLORS["worker"]),
             ("计算核心", 0.84, COLORS["engine"]), ("运行目录", 0.96, COLORS["store"])]
    for name, x, color in lanes:
        ax.plot([x, x], [0.10, 0.84], color=color, linewidth=1.2, alpha=0.42, zorder=1)
        ax.text(x, 0.875, name, ha="center", va="bottom", fontsize=12.5,
                color=color, fontweight="bold")

    steps = [
        (0.09, 0.28, 0.755, "① 提交指令"),
        (0.28, 0.47, 0.675, "② 任务规约"),
        (0.47, 0.66, 0.595, "③ 分发工具"),
        (0.66, 0.84, 0.515, "④ 真实计算"),
        (0.84, 0.96, 0.435, "⑤ 明细落盘"),
        (0.66, 0.47, 0.355, "⑥ 摘要回传"),
        (0.47, 0.09, 0.275, "⑦ 流式帧"),
        (0.47, 0.96, 0.195, "⑧ 报告与产物"),
        (0.96, 0.09, 0.115, "⑨ 下载与回看"),
    ]
    for x1, x2, y_pos, label in steps:
        _arrow(ax, (x1, y_pos), (x2, y_pos), color=COLORS["line"], label=label)

    fig.savefig(out, facecolor="white")
    plt.close(fig)
    return out


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="渲染架构图（README / 技术报告）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录（默认 docs/images/）")
    args = parser.parse_args(argv)
    _configure_font()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in (render_architecture(out_dir / "architecture.png"),
                 render_lifecycle(out_dir / "lifecycle.png")):
        print(f"[render] 已生成 {path}（{path.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
