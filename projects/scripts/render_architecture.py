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
         title_size: float = TITLE_SIZE, line_size: float = LINE_SIZE,
         pad: float = 0.010) -> None:
    ax.add_patch(patches.FancyBboxPatch(
        (x, y), w, h, boxstyle=f"round,pad={pad},rounding_size=0.018",
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
    fig = plt.figure(figsize=(14.0, 13.2), dpi=160)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    ax.text(0.5, 0.984, "分子对接多 Agent 协作系统 · 协作架构",
            ha="center", va="top", fontsize=21, fontweight="bold", color="#1f2733")

    # 纵向预算：标题 0.93 以下开始，底部 0.145 留给两行图注；每个带的高度按内容给，间距等分
    bands = [("users", 0.078), ("intake", 0.078), ("coord", 0.084), ("govern", 0.088),
             ("workers", 0.080), ("share", 0.078), ("core", 0.078), ("store", 0.080)]
    top, bottom = 0.905, 0.145
    gap = (top - bottom - sum(h for _n, h in bands)) / (len(bands) - 1)
    y: Dict[str, float] = {}
    cursor = top
    for name, height in bands:
        y[name] = cursor - height
        cursor = y[name] - gap

    _box(ax, 0.045, y["users"], 0.28, 0.085, "简易模式（默认）",
         ["任务描述 + 筛选结果"], COLORS["user"], pad=0.003)
    _box(ax, 0.360, y["users"], 0.28, 0.085, "高级模式",
         ["参数 · 对话 · 运行详情"], COLORS["user"], pad=0.003)
    _box(ax, 0.675, y["users"], 0.28, 0.085, "Studio / SDK",
         ["标准 Agent Protocol"], COLORS["user"], pad=0.003)

    _box(ax, 0.045, y["intake"], 0.91, 0.085, "任务受理层 intake",
         ["任务规约与执行决策（run / ask / reject）· 会话继承 · 在线解析"], COLORS["intake"], pad=0.003)
    _arrow(ax, (0.5, y["users"]), (0.5, y["intake"] + 0.085), color=COLORS["intake"])

    _box(ax, 0.045, y["coord"], 0.91, 0.092, "协调 Agent",
         ["环节与顺序 · 工具分发 · 结果汇总 · 报告定制"], COLORS["agent"], title_size=15, pad=0.003)
    _arrow(ax, (0.5, y["intake"]), (0.5, y["coord"] + 0.092), color=COLORS["agent"])

    _box(ax, 0.045, y["govern"], 0.91, 0.085, "执行治理（每次模型调用）",
         ["中间件：重试 · 模型/工具限额 · 长对话摘要 · 工具调用序列自愈",
          "步数预算：120 → 240 → 480 自动放宽并从检查点续跑；到顶用现有结果收尾"],
         COLORS["share"], title_size=13, line_size=10, pad=0.003)

    workers = [("口袋分析 Agent", "P2Rank · 几何法"),
               ("性质评估 Agent", "RDKit · 质子化"),
               ("对接执行 Agent", "Vina · 两阶段"),
               ("结合模式检测 Agent", "指纹 · 阳性对照")]
    xs = [0.045, 0.272, 0.499, 0.726]
    widths = [0.219, 0.219, 0.219, 0.229]
    for (title, line), wx, ww in zip(workers, xs, widths):
        _box(ax, wx, y["workers"], ww, 0.090, title, [line], COLORS["worker"],
             title_size=12.5, line_size=10.0, pad=0.003)
        _arrow(ax, (wx + ww / 2, y["govern"]), (wx + ww / 2, y["workers"] + 0.090),
               color=COLORS["agent"])
    # 「子 Agent 是无状态执行器、每次独立线程」写在 README 正文里，图上不再加一条横在两个带
    # 之间的标注 —— 它会与向下箭头互相穿插（真实排版问题）。

    _box(ax, 0.045, y["share"], 0.445, 0.085, "共享黑板（小状态）",
         ["受体 · 位点盒 · 分子库 · 阳性对照 · 溯源"], COLORS["share"], pad=0.003)
    _box(ax, 0.510, y["share"], 0.445, 0.085, "运行产物（按路径交接）",
         ["分子库 · 性质 · 对接明细"], COLORS["share"], pad=0.003)
    _arrow(ax, (0.30, y["workers"]), (0.30, y["share"] + 0.085), color=COLORS["share"], style="<|-|>")
    _arrow(ax, (0.70, y["workers"]), (0.70, y["share"] + 0.085), color=COLORS["share"], style="<|-|>")

    _box(ax, 0.045, y["core"], 0.91, 0.085, "计算内核",
         ["Vina · RDKit · Meeko · P2Rank · pdb2pqr · dimorphite-dl"], COLORS["engine"], pad=0.003)
    _arrow(ax, (0.30, y["share"]), (0.30, y["core"] + 0.085), color=COLORS["engine"])
    _arrow(ax, (0.70, y["share"]), (0.70, y["core"] + 0.085), color=COLORS["engine"])

    _box(ax, 0.045, y["store"], 0.91, 0.090, "运行记录 var/runs/<run_id>/",
         ["报告 · 排序 CSV · 图表 · 位姿 · 受体结构 · 整包 ZIP"], COLORS["store"], title_size=13, pad=0.003)
    _arrow(ax, (0.5, y["core"]), (0.5, y["store"] + 0.090), color=COLORS["store"])

    caption_y = 0.118
    ax.text(0.045, caption_y,
            "协调 Agent 工具：import_molecule_library · run_pocket_analysis · run_property_assessment · "
            "run_docking · run_binding_mode_analysis ·",
            ha="left", va="top", fontsize=CAPTION_SIZE, color="#6b7280")
    ax.text(0.045, caption_y - 0.030,
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
        (0.47, 0.09, 0.275, "⑦ 事件流（文本 · 思考 · 进展 · 候选）"),
        (0.47, 0.96, 0.195, "⑧ 报告与产物"),
        (0.96, 0.09, 0.115, "⑨ 下载与回看"),
    ]
    for x1, x2, y_pos, label in steps:
        _arrow(ax, (x1, y_pos), (x2, y_pos), color=COLORS["line"], label=label)

    ax.text(0.5, 0.045,
            "步数预算：达到递归上限自动放宽并从检查点续跑（120 → 240 → 480）；到顶由协调 Agent 用现有结果收尾。",
            ha="center", va="top", fontsize=10, color="#6b7280")

    fig.savefig(out, facecolor="white")
    plt.close(fig)
    return out




def render_layers(out: Path) -> Path:
    """分层与依赖方向：接入 → 受理 → 编排 → 运行支撑 → 计算 → 记录（依赖单向向下）。

    排版：每层一行「左侧层名 + 右侧两行说明」。行高与间距按可用高度**算出来**，标题、副标题、
    脚注各占独立区域 —— 旧版写死坐标，层数从四层增到六层后就压到脚注上了（文字重叠）。
    """
    fig = plt.figure(figsize=(13.2, 9.2), dpi=160)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.text(0.5, 0.980, "系统分层与依赖方向", ha="center", va="top",
            fontsize=21, fontweight="bold", color="#1f2733")
    ax.text(0.5, 0.940, "依赖单向向下：上层可以调用下层，下层不感知上层",
            ha="center", va="top", fontsize=11.5, color="#5a6472")

    bands = [
        ("接入层", "网页界面（简易模式 / 高级模式）· 标准 Agent Protocol 接口 · 命令行 · Studio",
         "api/（含 routers/）· web/ · cli.py", COLORS["user"]),
        ("受理层", "自然语言与表单 → 任务规约与执行决策；会话继承；受体与配体在线解析",
         "intake.py", COLORS["intake"]),
        ("编排层", "协调 Agent 与四个子 Agent；中间件治理、条件提示词、工具调用序列自愈",
         "agents/", COLORS["agent"]),
        ("运行支撑层", "共享黑板、产物落盘、运行事实、步数预算、事件流、检查点",
         "runtime/", COLORS["share"]),
        ("计算层", "对接、性质、口袋、质子化、结合模式（不依赖语言模型）",
         "core/ · tools/", COLORS["engine"]),
        ("记录层", "运行目录、产物登记、报告与图表、打包下载",
         "runs.py · reporting/", COLORS["store"]),
    ]
    x0, x1 = 0.055, 0.945
    top, bottom, gap = 0.902, 0.198, 0.024
    band_h = (top - bottom - gap * (len(bands) - 1)) / len(bands)
    label_w = 0.150
    for i, (label, detail, modules, color) in enumerate(bands):
        y = top - band_h - i * (band_h + gap)
        ax.add_patch(patches.FancyBboxPatch(
            (x0, y), x1 - x0, band_h, boxstyle="round,pad=0.006,rounding_size=0.014",
            linewidth=1.4, edgecolor=color, facecolor=color + "10"))
        # 左侧色条标出「这是哪一层」
        ax.add_patch(patches.Rectangle((x0 + 0.009, y + 0.012), 0.005, band_h - 0.024,
                                       facecolor=color, edgecolor="none"))
        ax.text(x0 + 0.032, y + band_h / 2, label, ha="left", va="center",
                fontsize=13.5, color=color, fontweight="bold")
        ax.text(x0 + label_w, y + band_h * 0.70, detail, ha="left", va="center",
                fontsize=10.5, color="#2f3743")
        ax.text(x0 + label_w, y + band_h * 0.28, modules, ha="left", va="center",
                fontsize=9.5, color="#6b7280")
        if i < len(bands) - 1:                      # 层间依赖方向
            ax.annotate("", xy=(x0 + 0.020, y - gap + 0.002), xytext=(x0 + 0.020, y - 0.001),
                        arrowprops={"arrowstyle": "-|>", "color": "#9aa3af", "lw": 1.2})

    ax.text(x0, 0.162,
            "边界：数值计算不经过模型（亲和力、性质、相似度均取自计算内核）；外部工具（P2Rank / pdb2pqr / AutoDock4 / GPU 引擎）由用户自备，缺失时降级并在报告中注明。",
            ha="left", va="top", fontsize=9.5, color="#6b7280")
    ax.text(x0, 0.122,
            "依赖校验：反向依赖由 tests/test_dependency_layering.py 与 scripts/lint_local.py 拦截；配置读取集中在 envs.py / settings.py。",
            ha="left", va="top", fontsize=9.5, color="#6b7280")
    fig.savefig(out, facecolor="white")
    plt.close(fig)
    return out


def render_dataflow(out: Path) -> Path:
    """一次筛选的数据流：从分子库到报告，标注中间产物文件与小状态交接。"""
    fig = plt.figure(figsize=(13.6, 6.6), dpi=160)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.text(0.5, 0.965, "一次筛选的数据流与产物", ha="center", va="top",
            fontsize=20, fontweight="bold", color="#1f2733")

    steps = [
        ("分子库导入", "归一化 · 按化学身份去重", "molecules.json", COLORS["user"]),
        ("性质评估", "RDKit 性质 · 目标 pH 质子化", "properties.json", COLORS["worker"]),
        ("口袋与定盒", "P2Rank / 几何法 · 盒子溯源", "pockets.json", COLORS["worker"]),
        ("分子对接", "Vina / AutoDock4 · 两阶段", "docking.json · poses/", COLORS["engine"]),
        ("结合模式", "指纹 · 阳性对照比较", "binding.json", COLORS["worker"]),
        ("排序与报告", "综合评分 · 图表 · 报告", "ranking.csv · report.md/pdf", COLORS["store"]),
    ]
    x = 0.028
    width = 0.140
    for i, (title, detail, artifact, color) in enumerate(steps):
        _box(ax, x, 0.52, width, 0.30, title, [detail, artifact], color,
             title_size=12.5, line_size=9.6)
        if i < len(steps) - 1:
            _arrow(ax, (x + width, 0.67), (x + width + 0.016, 0.67), color="#5a6472")
        x += width + 0.020

    _box(ax, 0.035, 0.20, 0.44, 0.22, "共享黑板（小状态）",
         ["受体 · 位点盒 · 分子库标识 · 阳性对照", "跨子 Agent 交接，不放大表"],
         COLORS["share"], title_size=12.5, line_size=10)
    _box(ax, 0.525, 0.20, 0.44, 0.22, "运行目录（大表）",
         ["中间产物按文件交接，不进入模型上下文", "整包 ZIP 自包含，可离线复现"],
         COLORS["store"], title_size=12.5, line_size=10)
    _arrow(ax, (0.30, 0.52), (0.25, 0.42), color=COLORS["share"])
    _arrow(ax, (0.70, 0.52), (0.75, 0.42), color=COLORS["store"])
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
                 render_lifecycle(out_dir / "lifecycle.png"),
                 render_layers(out_dir / "layers.png"),
                 render_dataflow(out_dir / "dataflow.png")):
        print(f"[render] 已生成 {path}（{path.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
