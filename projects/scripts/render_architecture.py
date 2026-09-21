#!/usr/bin/env python
"""渲染架构图（README / 技术报告用），输出到 `docs/images/`。

为什么用脚本画而不是手画：架构图必须与代码同步演进 —— 手绘的图三个月后必然过期。
这里把「使用者 → 受理 → 协调 Agent → 4 个子 Agent → 计算核心 → 产物」这条真实链路写成代码，
`bash scripts/render_architecture.py` 一条命令重画。

用法：
    .venv/bin/python scripts/render_architecture.py            # 全部图 → <repo>/docs/images/
    .venv/bin/python scripts/render_architecture.py --out DIR  # 指定输出目录
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")
from matplotlib import font_manager, patches, rcParams  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_OUT = REPO_ROOT / "docs" / "images"

#: 与前端一致的深色线框风格（白底便于 GitHub Light / PDF 打印）
COLORS = {
    "user": "#2f6f9f",
    "intake": "#7a5ea8",
    "agent": "#1f7a5a",
    "worker": "#2c7a7b",
    "share": "#b06d1f",
    "engine": "#9c3f4c",
    "store": "#4a5568",
    "bg": "#ffffff",
    "line": "#5a6472",
}


def _configure_font() -> None:
    """中文标签必须用 CJK 字体，否则图里全是方块。"""
    wanted = ["Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei", "SimHei"]
    available = {f.name for f in font_manager.fontManager.ttflist}
    chosen = [name for name in wanted if name in available]
    rcParams["font.sans-serif"] = chosen + ["DejaVu Sans"]
    rcParams["font.family"] = "sans-serif"
    rcParams["axes.unicode_minus"] = False


def _text_units(ax: plt.Axes, size_pt: float, *, leading: float = 1.5) -> float:
    """字号 → 纵轴坐标单位（1.0 = 整幅图高）。排布文字前先换算，避免"看着够、实际压"。"""
    return size_pt * leading / (ax.figure.get_figheight() * 72.0)


def _box(ax: plt.Axes, x: float, y: float, w: float, h: float, title: str,
         lines: Sequence[str] = (), color: str = "#4a5568", fontsize: float = 11,
         line_fontsize: float = 0.0, line_gap: float = 0.031) -> None:
    """带标题 + 若干正文行的圆角框；正文在框内**按字体实际高度均分**，必要时自动缩字号。"""
    ax.add_patch(patches.FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.012,rounding_size=0.02",
        linewidth=1.4, edgecolor=color, facecolor=color + "14"))
    title_h = _text_units(ax, fontsize, leading=1.7)
    ax.text(x + w / 2, y + h - title_h * 0.72, title, ha="center", va="center",
            fontsize=fontsize, color=color, fontweight="bold")

    size = line_fontsize or max(7.0, fontsize - 2.2)
    if lines:
        row_h = _text_units(ax, size, leading=1.55)
        content_top = y + h - title_h * 1.55        # 标题之下
        content_bottom = y + 0.010                  # 贴住下沿
        span = max(0.0, content_top - content_bottom)
        if row_h * len(lines) > span and span > 0:  # 空间不够 → 缩字号（不溢出）
            size = max(6.0, size * span / (row_h * len(lines)))
            row_h = _text_units(ax, size, leading=1.55)
        step = span / len(lines) if lines else 0.0
        for i, line in enumerate(lines):
            ax.text(x + w / 2, content_top - step * (i + 0.5), line,
                    ha="center", va="center", fontsize=size, color="#3d4653")


def _arrow(ax: plt.Axes, start: Tuple[float, float], end: Tuple[float, float],
           *, color: str = "#5a6472", style: str = "-|>", width: float = 1.5,
           label: str = "", dashed: bool = False, label_dx: float = 0.0) -> None:
    ax.add_patch(patches.FancyArrowPatch(
        start, end, arrowstyle=style, mutation_scale=16, linewidth=width,
        color=color, linestyle="--" if dashed else "-",
        shrinkA=1.5, shrinkB=1.5, zorder=3))
    if label:
        ax.text((start[0] + end[0]) / 2 + label_dx, (start[1] + end[1]) / 2,
                label, ha="center", va="center", fontsize=8.4, color=color,
                bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.2}, zorder=4)


def render_architecture(out: Path) -> Path:
    """协作架构图：一次运行里「谁在什么时候做什么、数据怎么流动」。"""
    # 画幅高度给足：7 个带区 + 每区 2–4 行说明，矮画幅会把字号压到 6pt 还互相压叠
    fig = plt.figure(figsize=(13.6, 11.4), dpi=160)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.text(0.5, 0.985, "分子对接多 Agent 协作系统 · 协作架构",
            ha="center", va="top", fontsize=19, fontweight="bold", color="#1f2733")
    ax.text(0.5, 0.952, "使用者 → 受理 → 协调 Agent（LangGraph）→ 4 个子 Agent → 真实计算核心 → 可追溯产物",
            ha="center", va="top", fontsize=11, color="#5a6472")

    # ---------------- 1) 使用者 ----------------
    # 自上而下堆叠各带区：位置由高度算出，绝不重叠
    bands = [("users", 0.082), ("intake", 0.080), ("coord", 0.120), ("workers", 0.104),
             ("share", 0.080), ("core", 0.082), ("store", 0.104)]
    top, gap = 0.836, 0.026
    y: dict = {}
    cursor = top
    for name, height in bands:
        y[name] = cursor - height
        cursor = y[name] - gap

    # 1) 使用者
    _box(ax, 0.04, y["users"], 0.27, 0.082, "对话模式（网页）",
         ["自然语言下达指令 · 运行参数条常驻", "附件上传 / @ 引用已上传文件"], COLORS["user"], fontsize=11.5)
    _box(ax, 0.365, y["users"], 0.27, 0.082, "参数模式（网页）",
         ["表单即权威参数", "确定性流水线（不需要 LLM）"], COLORS["user"], fontsize=11.5)
    _box(ax, 0.69, y["users"], 0.27, 0.082, "LangGraph Studio / SDK",
         ["平台面：langgraph-deploy（7 张图）", "调试与外部集成（ADR-0001）"], COLORS["user"], fontsize=11.5)

    # 2) 受理层
    _box(ax, 0.04, y["intake"], 0.92, 0.080, "任务受理层 intake（规则 + LLM）",
         ["把自然语言 / 表单解析成结构化任务规约：task_type · 受体来源 · 位点来源 · 参数 · 关键假设",
          "决策 run / ask / reject —— reject 时不调用任何工具，也不产生空报告（no_op 运行）"],
         COLORS["intake"], fontsize=12)
    _arrow(ax, (0.5, y["users"]), (0.5, y["intake"] + 0.078), color=COLORS["intake"])

    # 3) 协调 Agent
    _box(ax, 0.04, y["coord"], 0.92, 0.120, "整体协调 Agent（LangChain create_agent → LangGraph 图）",
         ["决定跑哪些环节、何时停、结果怎么组织；数值一律来自工具，不编造",
          "导入 import_molecule_library · 定盒 run_pocket_analysis · 性质 run_property_assessment",
          "对接 run_docking · 结合模式 run_binding_mode_analysis · 排行 recommend_compounds",
          "定制报告 customize_report · 交付 generate_screening_report · 理由 submit_recommendations"],
         COLORS["agent"], fontsize=12.5, line_fontsize=9.4, line_gap=0.028)
    _arrow(ax, (0.5, y["intake"]), (0.5, y["coord"] + 0.120), color=COLORS["agent"])

    # 4) 子 Agent（工具内调用）
    workers = [("口袋分析 Agent", ["P2Rank / 内置几何法", "compare_pocket_with_experiment", "set_docking_site → 黑板"]),
               ("分子属性评估 Agent", ["RDKit 物化性质 / 类药性", "Lipinski 违例", "规范化去重"]),
               ("Docking 执行 Agent", ["确定受体 / 位点 / 参数", "两阶段漏斗（粗筛→精算）", "进程池吃满多核"]),
               ("结合模式检测 Agent", ["Morgan + MACCS 指纹", "药效团锚定 / 一致性", "与阳性对照比较"])]
    xs = [0.04, 0.272, 0.504, 0.736]
    widths = [0.215, 0.215, 0.215, 0.224]
    for (title, lines), wx, ww in zip(workers, xs, widths):
        _box(ax, wx, y["workers"], ww, 0.104, title, lines, COLORS["worker"],
             fontsize=11.5, line_fontsize=9.0, line_gap=0.029)
        _arrow(ax, (wx + ww / 2, y["coord"]), (wx + ww / 2, y["workers"] + 0.104), color=COLORS["agent"])
    label_y = (y["coord"] + y["workers"] + 0.104) / 2
    ax.text(0.5, label_y, "工具内调用：每次独立线程的无状态执行器；大表走文件、小状态走黑板",
            ha="center", va="center", fontsize=9.2, color=COLORS["agent"],
            bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.8}, zorder=5)

    # 5) 共享交接
    _box(ax, 0.04, y["share"], 0.45, 0.080, "共享黑板（LangGraph store）",
         ["受体 · 位点盒 · 分子库 · 阳性对照 · 计数（小状态）",
          "父图与 4 个子 Agent 共享同一 store 实例"],
         COLORS["share"], fontsize=12, line_fontsize=9.6, line_gap=0.030)
    _box(ax, 0.51, y["share"], 0.45, 0.080, "运行产物文件（数据总线）",
         ["molecules / properties / docking / pockets JSON",
          "上万条明细不进上下文，按文件路径交接"],
         COLORS["share"], fontsize=12, line_fontsize=9.6, line_gap=0.030)
    _arrow(ax, (0.30, y["workers"]), (0.30, y["share"] + 0.080), color=COLORS["share"], style="<|-|>")
    _arrow(ax, (0.70, y["workers"]), (0.70, y["share"] + 0.080), color=COLORS["share"], style="<|-|>")

    # 6) 计算核心
    _box(ax, 0.04, y["core"], 0.92, 0.082, "真实计算核心（core/，无 LLM 依赖）",
         ["AutoDock Vina（主）/ AutoDock4（备用，可选）   ·   RDKit（性质 / 指纹 / 2D 结构）",
          "Meeko（配体 / 受体 PDBQT 准备）   ·   P2Rank 或内置几何法（口袋）   ·   pdb2pqr（按 pH 准备受体，可选）"],
         COLORS["engine"], fontsize=13, line_fontsize=9.8, line_gap=0.031)
    _arrow(ax, (0.30, y["share"]), (0.30, y["core"] + 0.082), color=COLORS["engine"], label="调用真实引擎")
    _arrow(ax, (0.70, y["share"]), (0.70, y["core"] + 0.082), color=COLORS["engine"])

    # 7) 落盘与交付
    _box(ax, 0.04, y["store"], 0.92, 0.104, "落盘与交付：var/runs/<run_id>/（自包含，可整目录搬走）",
         ["run.json 产物清单 · request / result JSON · report.md / report.pdf（固定骨架 §1–§9 + Agent 定制节）",
          "ranking.csv（含分子 ID）· charts/（结构卡 / 2D·3D 姿态 / 对比图）· poses/ 位姿 · receptor/ 对接受体结构",
          "网页「中间数据」逐项下载，或 download.zip 整包取走（含受体结构 → 换机器可复现）"],
         COLORS["store"], fontsize=12.5, line_fontsize=9.6, line_gap=0.030)
    _arrow(ax, (0.5, y["core"]), (0.5, y["store"] + 0.104), color=COLORS["store"], label="真实结果落盘")

    fig.savefig(out, facecolor=COLORS["bg"])
    plt.close(fig)
    return out


def render_lifecycle(out: Path) -> Path:
    """一次请求的生命周期（时序图，README 用作「谁在什么时候做什么」）。"""
    fig = plt.figure(figsize=(13.6, 6.6), dpi=160)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.text(0.5, 0.975, "一次运行的完整链路（对话模式为例）",
            ha="center", va="top", fontsize=17, fontweight="bold", color="#1f2733")

    lanes = [
        ("网页", 0.09, COLORS["user"]),
        ("受理 intake", 0.28, COLORS["intake"]),
        ("协调 Agent", 0.47, COLORS["agent"]),
        ("子 Agent ×4", 0.66, COLORS["worker"]),
        ("计算核心", 0.84, COLORS["engine"]),
        ("运行目录", 0.96, COLORS["store"]),
    ]
    for name, x, color in lanes:
        ax.plot([x, x], [0.10, 0.85], color=color, linewidth=1.2, alpha=0.45, zorder=1)
        ax.text(x, 0.875, name, ha="center", va="bottom", fontsize=11.5,
                color=color, fontweight="bold")

    steps = [
        (0.09, 0.28, 0.78, "① 提交指令 / 参数"),
        (0.28, 0.47, 0.70, "② 任务规约（run / ask / reject）"),
        (0.47, 0.66, 0.62, "③ 按需分发工具（子 Agent 无状态）"),
        (0.66, 0.84, 0.54, "④ 真实计算（Vina / RDKit / P2Rank）"),
        (0.84, 0.96, 0.46, "⑤ 明细落盘（产物文件）"),
        (0.66, 0.47, 0.38, "⑥ 摘要 + 产物引用回传"),
        (0.47, 0.09, 0.29, "⑦ 流式帧：token / 进度 / 领域事件"),
        (0.47, 0.96, 0.21, "⑧ 报告 / 排行 / 图 / 位姿 / 受体"),
        (0.96, 0.09, 0.12, "⑨ 下载与回看（含整包 ZIP）"),
    ]
    for x1, x2, y, label in steps:
        color = COLORS["line"]
        _arrow(ax, (x1, y), (x2, y), color=color, label=label, label_dx=0.0)
    fig.savefig(out, facecolor=COLORS["bg"])
    plt.close(fig)
    return out


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="渲染架构图（README / 技术报告）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录（默认 docs/images/）")
    args = parser.parse_args(argv)
    _configure_font()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = [render_architecture(out_dir / "architecture.png"),
               render_lifecycle(out_dir / "lifecycle.png")]
    for path in written:
        print(f"[render] 已生成 {path}（{path.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
