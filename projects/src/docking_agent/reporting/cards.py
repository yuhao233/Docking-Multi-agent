"""推荐分子的「结构 + 数据」卡片图（报告 3.1 节逐个分子用）。

拆成独立模块的原因：报告图表模块已经接近 700 行的本地门禁上限，而卡片渲染
（左 RDKit 2D 结构 + 右 matplotlib 指标表）与其它图表没有共享状态，独立更清晰。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from docking_agent.reporting.charts import _draw_molecule_png, _to_png  # noqa: E402

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 推荐分子「结构 + 数据」卡片：报告里让 2D 结构直接挨着它的指标表
# --------------------------------------------------------------------------- #
def _card_metrics(row: Dict[str, Any]) -> List[Tuple[str, str]]:
    """卡片右侧的指标（中文标签, 值）——与第 3.1 节排行表同口径。"""
    def num(key: str, digits: int = 2) -> str:
        value = row.get(key)
        return f"{value:.{digits}f}" if isinstance(value, (int, float)) else "—"

    le = row.get("ligand_efficiency")
    violations = row.get("lipinski_violations")
    ident = [("ID", str(row["id"]))] if row.get("id") else []
    return ident + [
        ("综合分", num("composite", 3)),
        ("等级", str(row.get("grade") or "—")),
        ("亲和力", f'{num("affinity_kcal_mol")} kcal/mol'),
        ("配体效率 LE", f"{le:.3f}" if isinstance(le, (int, float)) else "—"),
        ("分子量", f'{num("molecular_weight", 1)} Da'),
        ("logP", num("logP")),
        ("TPSA", f'{num("tpsa", 0)} Å²'),
        ("Lipinski 违例", f"{violations:.0f}" if isinstance(violations, (int, float)) else "—"),
        ("与对照相似度", num("similarity_to_positive_control", 3)),
        ("对接盒子", {"large": "大配体专用盒", "main": "主组"}.get(
            str(row.get("box_group") or "main"), "主组")),
    ]


def recommendation_card(row: Dict[str, Any], index: int = 1) -> Optional[bytes]:
    """单个推荐分子的卡片图：**左边 2D 结构、右边指标表**。

    为什么做成一张图而不是「markdown 表格 + 图片」两段：markdown/PDF 都没有
    「把图放进表格单元格」的可靠画法，一行一卡片的图片在网页、PDF 与离线包里
    长得完全一样，而且用户要的正是「结构挨着数据」。
    """
    import io

    import matplotlib.image as mpl_image

    smiles = str(row.get("smiles") or "")
    if not smiles:
        return None
    try:
        png = _draw_molecule_png(smiles, width=520, height=430)
    except Exception as e:  # noqa: BLE001 - 画不出结构也要出数据卡，不能整张图消失
        logger.warning("绘制推荐分子结构失败 %s: %s", row.get("name"), e)
        png = b""

    fig = plt.figure(figsize=(7.4, 2.45))
    ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
    ax.axis("off")

    # 左：2D 结构
    struct_ax = fig.add_axes([0.005, 0.02, 0.35, 0.90])
    struct_ax.axis("off")
    if png:
        struct_ax.imshow(mpl_image.imread(io.BytesIO(png), format="png"))
    else:
        struct_ax.text(0.5, 0.5, "结构绘制失败", ha="center", va="center", fontsize=9)

    # 右：指标表（4 列 = 标签/值 × 2 组，5 行）
    metrics = _card_metrics(row)
    half = (len(metrics) + 1) // 2
    left_col, right_col = metrics[:half], metrics[half:]
    table_rows = []
    for i in range(half):
        left = left_col[i] if i < len(left_col) else ("", "")
        right = right_col[i] if i < len(right_col) else ("", "")
        table_rows.append([left[0], left[1], right[0], right[1]])

    table_ax = fig.add_axes([0.37, 0.04, 0.62, 0.80])
    table_ax.axis("off")
    table = table_ax.table(cellText=table_rows, loc="center", cellLoc="left", edges="horizontal")
    table.auto_set_font_size(False)
    table.set_fontsize(8.6)
    table.scale(1.0, 1.28)
    for (r, c), cell in table.get_celld().items():
        cell.set_linewidth(0.4)
        cell.set_edgecolor("#c9d2de")
        cell.PAD = 0.04
        if c % 2 == 0:                      # 标签列浅底，值列白底：扫读更快
            cell.set_facecolor("#f3f6fa")
            cell.get_text().set_color("#4a5568")
        else:
            cell.get_text().set_color("#1a202c")
            cell.get_text().set_weight("bold")

    ident = f"[{row['id']}] " if row.get("id") else ""
    title = f"#{row.get('rank') or index} {ident}{str(row.get('name') or row.get('smiles') or '')}"
    grade = row.get("grade")
    composite = row.get("composite")
    subtitle = (f"综合分 {composite:.3f} · {grade} 级"
                if isinstance(composite, (int, float)) and grade else "")
    ax.set_title(title + ("　" + subtitle if subtitle else ""), fontsize=10.5, loc="left", pad=6)
    return _to_png(fig)
