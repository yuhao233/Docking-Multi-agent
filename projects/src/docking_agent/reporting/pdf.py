"""PDF 版固定报告：把 `report.md` 的固定章节渲染成分页 PDF。

为什么用 matplotlib 而不是新增 PDF 库：matplotlib 已在依赖里，
`reporting/charts.py::_configure_fonts()` 已经挑选好系统 CJK 字体，
`PdfPages` 又能把已有的图表 PNG 原样内嵌 —— 因此零新增运行时依赖。

排版约定（与 Markdown 报告同一套骨架）：

* A4 纵向、统一页边距；每页页脚为「run id · 页码 · 生成时间」；
* 章节标题与其后首块内容保持同页（避免标题孤立在页底）；
* 表格用等宽文本 + 显示宽度（CJK 记 2 列）对齐；列过多时自动改为**纵向记录**
  （每行分子一段「字段: 取值」列表），避免把 11 列硬挤进 6.8 英寸；
* 跨页表格自动重复表头；
* Markdown 里的图片以相对路径内嵌，PNG 由 `chart_paths` 解析为本地文件后真正嵌入页面；
* 图题/表题来自 Markdown 的编号题注（`**图 N …**` / `**表 N …**`），PDF 不再重复生成。
"""
from __future__ import annotations

import io
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")
from matplotlib import font_manager, rcParams  # noqa: E402
from matplotlib import image as mpl_image  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from docking_agent.reporting.charts import _configure_fonts  # noqa: E402
from docking_agent.reporting.report import CHART_FILES  # noqa: E402
from docking_agent.reporting.report import build_markdown_report  # noqa: E402
from docking_agent.reporting.report import tool_versions as _tool_versions  # noqa: E402

logger = logging.getLogger(__name__)

# 复用图表模块挑好的 CJK 字体（中文能否显示，取决于这里是否真正加载了中文字体）
CJK_FONTS = _configure_fonts()

# 表格必须等宽对齐，因此优先用「等宽 CJK」字体：拉丁字符 1 列、汉字 1 个 = 2 列，
# 这样按显示宽度补空格就能可靠对齐；找不到时退回系统的等宽字体（中文仍走 CJK 回退）。
_MONO_PREFERRED = [
    "Noto Sans Mono CJK SC", "Noto Sans Mono CJK JP", "Noto Sans Mono CJK TC",
    "Noto Sans Mono CJK HK", "WenQuanYi Zen Hei Mono", "WenQuanYi Micro Hei Mono",
    "AR PL UMing CN", "DejaVu Sans Mono", "Liberation Mono", "Noto Mono",
]
_MONO_FONTS = [name for name in _MONO_PREFERRED
               if name in {f.name for f in font_manager.fontManager.ttflist}]
rcParams["font.monospace"] = _MONO_FONTS or ["DejaVu Sans Mono"]

# A4 纵向（英寸）。页边距与字号是经验值：正文 9pt 时一页能容纳约 50 行。
PAGE_W, PAGE_H = 8.27, 11.69
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 0.72, 0.72, 0.66
TITLE_SIZE, H1_SIZE, H2_SIZE = 16.0, 12.5, 11.0
BODY_SIZE, TABLE_SIZE, SMALL_SIZE, FOOTER_SIZE = 9.0, 7.2, 8.0, 7.5
LINE_FACTOR = 1.55
MONO_ADVANCE = 0.6  # 等宽字体 ASCII 字符宽度（em）
TEXT_COLOR, MUTED_COLOR = "#1f2a37", "#6b7a8d"

# 单个表格最多渲染的行数（不含表头）：PDF 供人阅读，完整数据仍以 CSV/JSON 产物为准
PDF_TABLE_MAX_ROWS = 24
# 列数超过该值的表格改为纵向记录渲染（每行分子一段「字段: 取值」）
PDF_WIDE_TABLE_COLS = 8
PDF_WIDE_TABLE_MAX_ROWS = 12

_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)]+)\)")
_CAPTION_RE = re.compile(r"^\*\*((?:图|表)\s*\d+[^*]*)\*\*$")
_BOLD_RE = re.compile(r"\*\*|__|`|~~")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TABLE_RULE_RE = re.compile(r"^\|[\s\-:|]+\|$")


# --------------------------------------------------------------------------- #
# 显示宽度与文本工具（CJK 记 2 列）
# --------------------------------------------------------------------------- #
def _display_width(text: str) -> int:
    """文本占用的等宽列数：东亚全角字符按 2 列计。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _truncate(text: str, width: int) -> str:
    """把单元格截断到指定列数；截断标记用 ASCII 省略号避免宽度歧义。"""
    if _display_width(text) <= width:
        return text
    out: List[str] = []
    used = 0
    for ch in text:
        w = _display_width(ch)
        if used + w > max(1, width - 1):
            break
        out.append(ch)
        used += w
    return "".join(out) + "~"


def _split_at(text: str, width: int) -> Tuple[str, str]:
    used = 0
    for i, ch in enumerate(text):
        used += _display_width(ch)
        if used > width:
            return text[:i], text[i:]
    return text, ""


def _wrap(text: str, max_cols: int) -> List[str]:
    """按显示宽度折行：优先在空格处断行，超长单词/无空格中文则硬断。"""
    if max_cols <= 0:
        return [text]
    if not text.strip():
        return [""]
    lines: List[str] = []
    current = ""
    for word in text.split(" "):
        candidate = word if not current else f"{current} {word}"
        if _display_width(candidate) <= max_cols:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        while _display_width(word) > max_cols:
            head, word = _split_at(word, max_cols)
            lines.append(head)
        current = word
    if current or not lines:
        lines.append(current)
    return lines


def _columns_for(width_in: float, size: float) -> int:
    return max(8, int(width_in / (size * MONO_ADVANCE / 72.0)))


def _clean_inline(text: str) -> str:
    """去掉 Markdown 行内标记，保留可读文本（PDF 里不渲染强调语法）。"""
    text = _BR_RE.sub(" / ", text)
    text = _BOLD_RE.sub("", text)
    return text.replace("\u00a0", " ").strip()


def _format_table(rows: Sequence[Sequence[Any]], max_cols: int, *,
                  header: bool = True) -> List[str]:
    """把二维表格格式化成等宽文本行；列宽不足时压缩最宽的列。

    header=True 时首行后加一条分隔线（Markdown 表格的首行就是表头）；
    标题页的元信息表没有表头，用 header=False 避免出现悬空的分隔线。
    """
    clean = [[_clean_inline(str(cell if cell is not None else "")) for cell in row] for row in rows]
    ncol = max((len(r) for r in clean), default=0)
    if ncol == 0:
        return []
    for row in clean:
        row.extend([""] * (ncol - len(row)))
    widths = [max(_display_width(row[i]) for row in clean) for i in range(ncol)]
    # " | " 分隔符占 3 列；逐步压缩最宽列，直到整行能放进页面
    while sum(widths) + 3 * (ncol - 1) > max_cols and max(widths) > 4:
        widths[widths.index(max(widths))] -= 1
    lines: List[str] = []
    for idx, row in enumerate(clean):
        cells = [_pad(_truncate(cell, widths[i]), widths[i]) for i, cell in enumerate(row)]
        lines.append(" | ".join(cells).rstrip())
        if header and idx == 0:
            lines.append("-+-".join("-" * w for w in widths))
    return lines


# --------------------------------------------------------------------------- #
# 分页写入器
# --------------------------------------------------------------------------- #
class _PageWriter:
    """把「文本块 / 表格 / 图片」顺序写进 PdfPages，空间不足自动翻页并加页脚。"""

    def __init__(self, pdf: PdfPages, run_id: str, created_at: str = "") -> None:
        self._pdf = pdf
        self._run_id = run_id or "—"
        self._created_at = created_at or "—"
        self._page = 0
        self._fig = Figure(figsize=(PAGE_W, PAGE_H))
        self._y = MARGIN_TOP
        self._new_page()

    # ---- 页面管理 ----
    def _new_page(self) -> None:
        self._page += 1
        self._y = MARGIN_TOP
        footer = f"run {self._run_id} · 第 {self._page} 页 · {self._created_at}"
        self._fig.text(MARGIN_X / PAGE_W, 0.30 / PAGE_H, footer,
                       fontsize=FOOTER_SIZE, color=MUTED_COLOR,
                       family="monospace", va="bottom", ha="left")

    def _room(self, height: float) -> bool:
        """确保当前页还能放下 height 英寸；放不下就翻页（返回是否翻过页）。"""
        if self._y + height <= PAGE_H - MARGIN_BOTTOM:
            return False
        self._pdf.savefig(self._fig)
        self._fig = Figure(figsize=(PAGE_W, PAGE_H))
        self._new_page()
        return True

    def reserve(self, height: float) -> None:
        """为「标题 + 其后首块内容」预留空间，避免标题孤立在页底（章节不跨页断裂）。"""
        self._room(height)

    def flush(self) -> None:
        self._pdf.savefig(self._fig)
        self._fig = Figure(figsize=(PAGE_W, PAGE_H))
        self._new_page()

    def finish(self) -> None:
        self._pdf.savefig(self._fig)

    # ---- 内容块 ----
    def blank(self, height: float = 0.10) -> None:
        self._room(height)
        self._y += height

    def text(self, content: str, *, size: float = BODY_SIZE, weight: str = "normal",
             color: str = TEXT_COLOR, indent: float = 0.0) -> None:
        max_cols = _columns_for(PAGE_W - 2 * MARGIN_X - indent, size)
        line_h = size * LINE_FACTOR / 72.0
        for line in _wrap(str(content), max_cols):
            self._room(line_h)
            self._fig.text((MARGIN_X + indent) / PAGE_W, 1.0 - self._y / PAGE_H, line,
                           fontsize=size, family="monospace", weight=weight,
                           color=color, va="top", ha="left")
            self._y += line_h

    def table(self, rows: Sequence[Sequence[Any]], *, size: float = TABLE_SIZE,
              header: bool = True) -> None:
        """等宽表格；跨页时自动重复表头。"""
        max_cols = _columns_for(PAGE_W - 2 * MARGIN_X, size)
        line_h = size * LINE_FACTOR / 72.0
        lines = _format_table(rows, max_cols, header=header)
        if not lines:
            return
        head_lines = 2 if header else 0     # 表头 + 分隔线
        for idx, line in enumerate(lines):
            broke = self._room(line_h)
            if broke and header and idx >= head_lines:
                for extra in lines[:head_lines]:
                    self._fig.text(MARGIN_X / PAGE_W, 1.0 - self._y / PAGE_H, extra,
                                   fontsize=size, family="monospace", color=MUTED_COLOR,
                                   va="top", ha="left")
                    self._y += line_h
            self._fig.text(MARGIN_X / PAGE_W, 1.0 - self._y / PAGE_H, line,
                           fontsize=size, family="monospace", color=TEXT_COLOR,
                           va="top", ha="left")
            self._y += line_h

    def wide_table(self, rows: Sequence[Sequence[Any]], *, size: float = TABLE_SIZE) -> None:
        """列过多时的纵向记录：每行分子一段「首字段 值 · 次字段 值」+ 其余字段逐行列出。"""
        clean = [[_clean_inline(str(c if c is not None else "")) for c in row] for row in rows]
        if not clean:
            return
        heads = clean[0]
        for row in clean[1:]:
            pairs = [(heads[i] if i < len(heads) else f"列{i + 1}", value)
                     for i, value in enumerate(row) if value not in ("", "—")]
            if not pairs:
                continue
            self.reserve(0.42)
            title = f"{pairs[0][0]} {pairs[0][1]}"
            if len(pairs) > 1:
                title += f" · {pairs[1][0]} {pairs[1][1]}"
            self.text("• " + title, weight="bold", color=TEXT_COLOR)
            for name, value in pairs[2:]:
                self.text(f"    {name}: {value}", size=SMALL_SIZE, color=MUTED_COLOR)
            self.blank(0.05)

    def image(self, path: Path, caption: str = "") -> bool:
        """内嵌一张 PNG；图片按页面宽度等比缩放，返回是否成功。"""
        try:
            pixels = mpl_image.imread(str(path))
        except Exception as e:  # noqa: BLE001  # 单张图坏了不影响整份报告
            logger.warning("PDF 内嵌图片失败 %s：%s", path, e)
            return False
        height_px, width_px = pixels.shape[0], pixels.shape[1]
        draw_w = PAGE_W - 2 * MARGIN_X
        draw_h = draw_w * height_px / max(1, width_px)
        max_h = PAGE_H - MARGIN_TOP - MARGIN_BOTTOM - 0.45
        if draw_h > max_h:
            ratio = max_h / draw_h
            draw_w, draw_h = draw_w * ratio, max_h
        caption_h = (SMALL_SIZE * LINE_FACTOR / 72.0 + 0.04) if caption else 0.0
        self._room(draw_h + caption_h)
        bottom = 1.0 - (self._y + draw_h) / PAGE_H
        ax = self._fig.add_axes([MARGIN_X / PAGE_W, bottom, draw_w / PAGE_W, draw_h / PAGE_H])
        ax.imshow(pixels)
        ax.axis("off")
        self._y += draw_h
        if caption:
            self.text(caption, size=SMALL_SIZE, color=MUTED_COLOR)
        return True


# --------------------------------------------------------------------------- #
# Markdown → 内容块
# --------------------------------------------------------------------------- #
def _markdown_blocks(markdown: str) -> List[Tuple[str, Any]]:
    """把固定模板 Markdown 拆成可渲染的内容块（标题/题注/段落/表格/图片/空行）。"""
    blocks: List[Tuple[str, Any]] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        image = _IMAGE_RE.search(raw)
        if image:
            blocks.append(("image", (image.group("alt") or "图表", image.group("url"))))
            i += 1
            continue
        caption = _CAPTION_RE.match(stripped)
        if caption:
            blocks.append(("caption", _clean_inline(caption.group(1))))
            i += 1
            continue
        if stripped.startswith("|"):
            table_lines: List[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            rows = _parse_table(table_lines)
            if rows:
                blocks.append(("table", rows))
            continue
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            blocks.append(("heading", (level, _clean_inline(stripped.lstrip("#").strip()))))
            i += 1
            continue
        if stripped.startswith(">"):
            blocks.append(("quote", _clean_inline(stripped.lstrip(">").strip())))
            i += 1
            continue
        if stripped in ("---", "***", "___"):
            i += 1
            continue
        if not stripped:
            blocks.append(("blank", None))
            i += 1
            continue
        blocks.append(("text", _clean_inline(stripped)))
        i += 1
    return blocks


def _parse_table(lines: Sequence[str]) -> List[List[str]]:
    rows: List[List[str]] = []
    for line in lines:
        if _TABLE_RULE_RE.match(line):
            continue
        cells = [_clean_inline(cell) for cell in line.strip("|").split("|")]
        rows.append(cells)
    return rows


def _image_name(url: str) -> str:
    """把 Markdown 图片地址解析成图表产物名。

    新格式是相对路径（`charts/docking_chart.png`）；同时兼容旧运行/旧模板里的
    `/api/runs/<id>/artifacts/<name>?inline=1`，保证历史报告仍能内嵌图片。
    """
    if "/artifacts/" in url:
        tail = url.split("/artifacts/", 1)[1]
        return tail.split("?", 1)[0].split("/", 1)[0]
    tail = url.split("?", 1)[0]
    for name, rel in CHART_FILES.items():
        if tail.endswith(rel) or Path(tail).name == Path(rel).name:
            return name
    # 动态生成的图（按名次命名，如 `charts/interaction_2d_01.png` / `charts/pose_3d_01.png`）
    # 不在 CHART_FILES 里：产物名就是**去掉扩展名的文件名**，这里必须返回 stem。
    # 早期返回 `Path(tail).name`（带 .png）→ 与产物名对不上 → PDF 里这些图被静默跳过
    # （真实踩到：报告正文有图，PDF 只有 4 张基础图）。
    return Path(tail).stem


# --------------------------------------------------------------------------- #
# 标题页与正文
# --------------------------------------------------------------------------- #
def _subtitle(kind: str, receptor_label: str, molecule_count: int, run_id: str) -> str:
    mode = "多 Agent 协作（LLM）" if kind == "agent" else "确定性流水线（无 LLM）"
    count = f"{int(molecule_count)} 个候选分子" if molecule_count else "候选分子数未知"
    return (f"运行 {run_id or '未知'} · 受体 {receptor_label or '未知'} · "
            f"{count} · {mode}")


def _write_title_page(writer: _PageWriter, *, run_id: str, kind: str, receptor_label: str,
                      created_at: str, molecule_count: int) -> None:
    """封面只放大标题/副标题/工具版本/生成时间；参数行留给正文的固定格式表。"""
    writer.blank(0.50)
    writer.text("分子对接筛选报告", size=TITLE_SIZE, weight="bold")
    writer.blank(0.10)
    writer.text(_subtitle(kind, receptor_label, molecule_count, run_id),
                size=BODY_SIZE, color=MUTED_COLOR)
    writer.blank(0.34)
    writer.text("工具版本", size=H2_SIZE, weight="bold")
    writer.blank(0.10)
    writer.table(_tool_versions(), header=False)
    writer.blank(0.18)
    writer.text(f"生成时间：{created_at or '未知'}", size=BODY_SIZE)
    writer.blank(0.24)


def _render_blocks(writer: _PageWriter, blocks: Sequence[Tuple[str, Any]],
                   chart_paths: Dict[str, Path]) -> None:
    for kind, payload in blocks:
        if kind == "heading":
            level, text = payload
            # 标题与首块内容保持同页：预留约 0.9 英寸，避免标题孤立在页底
            writer.reserve(0.90 if level <= 1 else 0.70)
            if level <= 1:
                writer.blank(0.18)
                writer.text(text, size=H1_SIZE, weight="bold")
            else:
                writer.blank(0.14)
                writer.text(text, size=H2_SIZE, weight="bold")
            writer.blank(0.04)
        elif kind == "caption":
            writer.reserve(0.60)
            writer.text(str(payload), size=SMALL_SIZE, weight="bold", color=TEXT_COLOR)
        elif kind == "quote":
            writer.text(f"提示：{payload}", size=SMALL_SIZE, color=MUTED_COLOR, indent=0.16)
        elif kind == "text":
            writer.text(payload, indent=0.08 if str(payload).startswith("-") else 0.0)
        elif kind == "table":
            rows = list(payload)
            ncol = max((len(r) for r in rows), default=0)
            wide = ncol > PDF_WIDE_TABLE_COLS
            limit = PDF_WIDE_TABLE_MAX_ROWS if wide else PDF_TABLE_MAX_ROWS
            truncated = len(rows) - 1 > limit
            if truncated:
                rows = rows[: limit + 1]
            if wide:
                writer.wide_table(rows)
            else:
                writer.table(rows)
            if truncated:
                writer.text(f"（此表仅显示前 {limit} 行；完整数据见第 9 节列出的 CSV / JSON 产物）",
                            size=SMALL_SIZE, color=MUTED_COLOR)
        elif kind == "image":
            alt, url = payload
            path = chart_paths.get(_image_name(url))
            if path is not None and path.is_file():
                # 题注已由 Markdown 的编号图题提供，这里不再重复生成
                caption = "" if _CAPTION_RE.match(f"**{alt}**") else f"图：{alt}"
                writer.image(path, caption=caption)
            else:
                writer.text(f"（图表 {alt} 未找到对应产物文件，可在网页报告的「中间数据」中单独下载）",
                            size=SMALL_SIZE, color=MUTED_COLOR)
        else:  # blank
            writer.blank(0.08)


def build_report_pdf(result: Dict[str, Any], *, kind: str = "pipeline", run_id: str = "",
                     receptor_label: str = "", created_at: str = "", markdown: str = "",
                     chart_paths: Optional[Dict[str, Path]] = None,
                     artifacts: Optional[List[Dict[str, Any]]] = None,
                     molecule_count: int = 0) -> bytes:
    """把报告内容渲染成 PDF 字节。

    markdown 为空时按固定模板现生成，保证 PDF 与页面上的 Markdown 报告章节一致；
    chart_paths 是「产物名 → 本地 PNG 路径」，只有真实存在的图才会被内嵌。
    """
    md = markdown or build_markdown_report(
        result, kind=kind, run_id=run_id, receptor_label=receptor_label,
        artifacts=artifacts, created_at=created_at)
    blocks = _markdown_blocks(md)
    # 封面已有大标题，正文首行的同名一级标题不再重复
    if blocks and blocks[0][0] == "heading" and blocks[0][1][0] <= 1:
        blocks = blocks[1:]
    buffer = io.BytesIO()
    with PdfPages(buffer) as pdf:
        writer = _PageWriter(pdf, run_id, created_at)
        _write_title_page(writer, run_id=run_id, kind=kind,
                          receptor_label=receptor_label, created_at=created_at,
                          molecule_count=molecule_count)
        _render_blocks(writer, blocks, chart_paths or {})
        writer.finish()
    return buffer.getvalue()
