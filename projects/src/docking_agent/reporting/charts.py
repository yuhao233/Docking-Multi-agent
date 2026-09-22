"""报告图表：由真实计算结果绘制的对比图（返回 PNG 字节）。"""
from __future__ import annotations

import io
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from docking_agent.core.ranking import sort_by_affinity
from docking_agent.config import env_int

import matplotlib

matplotlib.use("Agg")
from matplotlib import font_manager, rcParams  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

logger = logging.getLogger(__name__)

# 中文字体优先级：分子名与图例可能是中文，缺字体会渲染成方块
_CJK_FONTS = [
    "Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Sans CJK TC",
    "Source Han Sans SC", "WenQuanYi Zen Hei", "WenQuanYi Micro Hei",
    "Noto Serif CJK SC", "SimHei", "Microsoft YaHei", "PingFang SC",
]


def _recommend_top_n() -> int:
    """推荐排行的展示条数（设置项 RECOMMEND_TOP_N，默认 10）。"""
    try:
        from docking_agent.config import env_int

        return max(1, env_int("RECOMMEND_TOP_N", 10))
    except Exception:  # noqa: BLE001
        return 10


def _configure_fonts() -> List[str]:
    try:
        available = {f.name for f in font_manager.fontManager.ttflist}
    except Exception:  # noqa: BLE001
        available = set()
    chosen = [name for name in _CJK_FONTS if name in available]
    rcParams["font.sans-serif"] = chosen + ["DejaVu Sans"]
    rcParams["font.family"] = "sans-serif"
    rcParams["axes.unicode_minus"] = False  # 避免负号在中文字体下变成方块
    if not chosen:
        logger.warning("未找到中文字体，图表中的中文可能显示为方块；"
                       "可安装 fonts-noto-cjk 后重试")
    return chosen


CJK_FONT = _configure_fonts()

# 大库图表策略：分子数超过 BAR_MAX 时，横向条形图改为「Top-N」，并另出分布直方图，
# 否则上万个柱子既不可读也无法渲染。
BAR_MAX = env_int("CHART_BAR_MAX", 50)
TOP_N = env_int("CHART_TOP_N", 20)


def _to_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def docking_bar_chart(molecules: List[Dict[str, Any]], pos_control: Dict[str, Any],
                      top_n: int = 0) -> bytes:
    """对接亲和力横向对比图（含阳性对照参考线）。

    分子数超过 BAR_MAX 时自动只画亲和力最优的 Top-N，避免上万柱子不可读。
    """
    total = len(molecules)
    if total > BAR_MAX:
        n = top_n or TOP_N
        ordered = sort_by_affinity(molecules, drop_missing=True)[:n]
        return _ranked_bar_chart(ordered, pos_control,
                                 title=f"对接亲和力 Top-{len(ordered)}（共 {total} 个分子）",
                                 color="#4C72B0")

    labels = [m.get("name") or m.get("smiles") for m in molecules]
    values = [m["affinity_kcal_mol"] for m in molecules if m.get("affinity_kcal_mol") is not None]
    pc_aff = (pos_control or {}).get("affinity_kcal_mol")
    if pc_aff is not None:
        labels = labels + [pos_control.get("name") or "PositiveControl"]
        values = values + [pc_aff]
    if not values:
        values, labels = [0.0], ["(无数据)"]

    colors = ["#4C72B0"] * len(labels)
    if pc_aff is not None and colors:
        colors[-1] = "#C44E52"

    fig, ax = plt.subplots(figsize=(8, max(3, 0.5 * len(labels) + 1)))
    ax.barh(labels, values, color=colors, edgecolor="white")
    ax.set_xlabel("对接亲和力 (kcal/mol)")
    ax.set_title("分子对接亲和力对比（含阳性对照）")
    ax.invert_xaxis()
    for i, v in enumerate(values):
        ax.text(v, i, f" {v:.2f}", va="center")
    if pc_aff is not None:
        ax.axvline(pc_aff, color="#C44E52", linestyle="--", alpha=0.6)
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    return _to_png(fig)


def _ranked_bar_chart(molecules: List[Dict[str, Any]], pos_control: Dict[str, Any],
                      title: str, color: str = "#4C72B0") -> bytes:
    """通用横向条形图（分子已按需排序）。"""
    labels = [m.get("name") or m.get("smiles") for m in molecules]
    values = [m["affinity_kcal_mol"] for m in molecules]
    pc_aff = (pos_control or {}).get("affinity_kcal_mol")
    if pc_aff is not None:
        labels = labels + [pos_control.get("name") or "阳性对照"]
        values = values + [pc_aff]
    fig, ax = plt.subplots(figsize=(8, max(3, 0.4 * len(labels) + 1.2)))
    colors = [color] * len(molecules) + (["#C44E52"] if pc_aff is not None else [])
    ax.barh(labels, values, color=colors, edgecolor="white")
    ax.set_xlabel("对接亲和力 (kcal/mol)")
    ax.set_title(title)
    ax.invert_xaxis()
    for i, v in enumerate(values):
        ax.text(v, i, f" {v:.2f}", va="center", fontsize=8)
    if pc_aff is not None:
        ax.axvline(pc_aff, color="#C44E52", linestyle="--", alpha=0.6)
    ax.tick_params(axis="y", labelsize=8)
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    return _to_png(fig)


def affinity_histogram(molecules: List[Dict[str, Any]], pos_control: Dict[str, Any]) -> bytes:
    """亲和力分布直方图（大库场景的主图）：含阳性对照参考线与均值/中位数。"""
    import statistics

    values = [float(m["affinity_kcal_mol"]) for m in molecules
              if isinstance(m.get("affinity_kcal_mol"), (int, float))]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    if values:
        bins = max(10, min(60, int(len(values) ** 0.5) * 2))
        ax.hist(values, bins=bins, color="#4C72B0", edgecolor="white", alpha=0.9)
        mean = statistics.fmean(values)
        median = statistics.median(values)
        ax.axvline(mean, color="#55A868", linestyle="-", alpha=0.9,
                   label=f"均值 {mean:.2f}")
        ax.axvline(median, color="#8172B2", linestyle="-.", alpha=0.9,
                   label=f"中位数 {median:.2f}")
        pc_aff = (pos_control or {}).get("affinity_kcal_mol")
        if isinstance(pc_aff, (int, float)):
            ax.axvline(pc_aff, color="#C44E52", linestyle="--", alpha=0.9,
                       label=f"阳性对照 {pc_aff:.2f}")
        ax.legend(fontsize=8)
        hits = len([v for v in values if isinstance(pc_aff, (int, float)) and v < pc_aff])
        ax.set_title(f"亲和力分布（{len(values)} 个分子，优于对照 {hits} 个）")
    else:
        ax.set_title("亲和力分布（无数据）")
    ax.set_xlabel("对接亲和力 (kcal/mol)")
    ax.set_ylabel("分子数")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    return _to_png(fig)


def similarity_chart(molecules: List[Dict[str, Any]]) -> bytes:
    """与阳性对照的指纹相似度图（大库时只画 Top-N）。"""
    if len(molecules) > BAR_MAX:
        ordered = sorted(molecules,
                         key=lambda m: m.get("similarity_to_positive_control") or 0.0,
                         reverse=True)[:TOP_N]
        return _similarity_bars(ordered,
                                title=f"与阳性对照的相似度 Top-{len(ordered)}（共 {len(molecules)} 个分子）")
    return _similarity_bars(molecules, title="与阳性对照的结构相似度")


def _similarity_bars(molecules: List[Dict[str, Any]], title: str) -> bytes:
    data = [((m.get("name") or m.get("smiles")), m.get("similarity_to_positive_control") or 0.0)
            for m in molecules]
    data.sort(key=lambda x: x[1], reverse=True)
    names = [x[0] for x in data] or ["(无数据)"]
    sims = [x[1] for x in data] or [0.0]

    fig, ax = plt.subplots(figsize=(8, max(3, 0.5 * len(names) + 1)))
    ax.barh(names, sims, color="#55A868", edgecolor="white")
    ax.set_xlim(0, 1.0)
    ax.set_xlabel("Morgan 指纹 Tanimoto 相似度 (0–1)")
    ax.set_title(title)
    for i, v in enumerate(sims):
        ax.text(v, i, f" {v:.2f}", va="center")
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    return _to_png(fig)


def _top_property_points(points: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """理化性质空间图要画的点：优先按综合分（推荐排行），没有综合分时按亲和力取前 limit 个。

    单独抽成函数是为了**可测**：用户明确要求"只展示 top 的分子（与排序相同），不然太乱了"，
    这个集合必须与报告第 3.1 节的排行口径一致（同一批 top N）。
    """
    if limit <= 0 or len(points) <= limit:
        return list(points)
    if any(m.get("composite") is not None for m in points):
        # 综合分**降序**（最高分在前）；没有综合分时按亲和力升序（越负越强）
        ranked = sorted(points, key=lambda m: (
            float(m.get("composite") or 0.0),
            m.get("affinity_kcal_mol") if isinstance(m.get("affinity_kcal_mol"), (int, float)) else 0.0,
        ), reverse=True)
    else:
        ranked = sorted(points, key=lambda m: m.get("affinity_kcal_mol") if isinstance(
            m.get("affinity_kcal_mol"), (int, float)) else 0.0)
    return ranked[:limit]


def property_scatter_chart(molecules: List[Dict[str, Any]], top_only: bool = False) -> bytes:
    """理化性质空间图：分子量 vs logP，点大小表示亲和力强度。

    `top_only=True` 时只画**推荐排行前 N 个**分子（与第 3 节排序同一集合）：
    整库散点在大库（几百上千个）时点与标注互相覆盖，图上什么也读不出来 ——
    真实反馈：「理化性质空间只展示 top 的分子，不然太乱了」。
    """
    pts = [m for m in molecules
           if isinstance(m.get("molecular_weight"), (int, float)) and isinstance(m.get("logP"), (int, float))]
    if top_only:
        pts = _top_property_points(pts, _recommend_top_n())
    fig, ax = plt.subplots(figsize=(7, 5))
    if pts:
        mw = [m["molecular_weight"] for m in pts]
        logp = [m["logP"] for m in pts]
        aff = [abs(m.get("affinity_kcal_mol") or 0.0) for m in pts]
        sizes = [max(30.0, a * 22.0) for a in aff]
        colors = [m.get("affinity_kcal_mol") if m.get("affinity_kcal_mol") is not None else 0.0 for m in pts]
        sc = ax.scatter(mw, logp, s=sizes, c=colors, cmap="viridis_r", edgecolor="white", zorder=3)
        for m in pts:
            ax.annotate(m.get("name") or "", (m["molecular_weight"], m["logP"]),
                        fontsize=7, xytext=(3, 3), textcoords="offset points")
        fig.colorbar(sc, ax=ax, label="对接亲和力 (kcal/mol)")
        ax.axvline(500, color="#C44E52", linestyle="--", alpha=0.5)
        ax.axhline(5, color="#C44E52", linestyle="--", alpha=0.5)
        ax.text(505, 5.1, "Lipinski 边界", fontsize=7, color="#C44E52")
    ax.set_xlabel("分子量 (Da)")
    ax.set_ylabel("logP")
    ax.set_title("理化性质空间（点大小 = 对接强度）"
                 + (f"｜仅推荐排行前 {len(pts)} 个" if top_only else ""))
    ax.grid(alpha=0.25, zorder=0)
    fig.tight_layout()
    return _to_png(fig)


# --------------------------------------------------------------------------- #
# 结合模式相关图（会直接嵌入报告）
# --------------------------------------------------------------------------- #
def _draw_molecule_png(smiles: str, width: int = 260, height: int = 200) -> bytes:
    """用 RDKit 画单个分子（不加 legend —— RDKit 字体会丢弃中文）。"""
    from rdkit import Chem
    from rdkit.Chem.Draw import rdMolDraw2D

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"无法解析 SMILES: {smiles}")
    drawer = rdMolDraw2D.MolDraw2DCairo(width, height)
    opts = drawer.drawOptions()
    opts.bondLineWidth = 2
    opts.minFontSize = 11
    opts.maxFontSize = 15
    drawer.DrawMolecule(mol)
    drawer.FinishDrawing()
    return drawer.GetDrawingText()


def structure_grid(molecules: List[Dict[str, Any]], pos_control: Optional[Dict[str, Any]] = None,
                   top_n: int = 12, cols: int = 4) -> Optional[bytes]:
    """候选分子结构网格图（RDKit 画结构 + matplotlib 加中文标注，含阳性对照对照格）。"""
    import io
    import math

    import matplotlib.image as mpl_image

    items = [m for m in molecules if m.get("smiles")][:max(1, top_n)]
    if not items:
        return None
    control = pos_control if (pos_control or {}).get("smiles") else None
    cells: List[Dict[str, Any]] = ([{"name": (control or {}).get("name") or "阳性对照",
                                     "smiles": (control or {}).get("smiles"),
                                     "affinity_kcal_mol": (control or {}).get("affinity_kcal_mol"),
                                     "is_control": True}] if control else []) + items

    rows = math.ceil(len(cells) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 2.5, rows * 2.35))
    axes_list = axes.flatten() if hasattr(axes, "flatten") else [axes]

    for ax, cell in zip(axes_list, cells):
        try:
            png = _draw_molecule_png(str(cell["smiles"]))
            ax.imshow(mpl_image.imread(io.BytesIO(png), format="png"))
        except Exception as e:  # noqa: BLE001
            logger.warning("绘制分子结构失败 %s: %s", cell.get("name"), e)
            ax.text(0.5, 0.5, "结构绘制失败", ha="center", va="center", fontsize=8)
        aff = cell.get("affinity_kcal_mol")
        title = str(cell.get("name") or "")[:22]
        if isinstance(aff, (int, float)):
            title += f"\n{aff:.2f} kcal/mol"
        ax.set_title(title, fontsize=8.5, color="#C44E52" if cell.get("is_control") else "#22303f")
        ax.set_xticks([]); ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(bool(cell.get("is_control")))
            spine.set_color("#C44E52")
    for ax in axes_list[len(cells):]:
        ax.axis("off")

    shown = len(items)
    total = len([m for m in molecules if m.get("smiles")])
    fig.suptitle(f"候选分子结构（按对接亲和力排序，前 {shown}/{total} 个）", fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    return _to_png(fig)


def recommendation_structure_grid(rows: List[Dict[str, Any]], top_n: int = 0,
                                  cols: int = 5) -> Optional[bytes]:
    """推荐化合物排行对应的 **2D 结构图**（每格：名称 / 综合分 / 亲和力 / 等级）。

    这是 PDF 报告里"看得见分子长什么样"的那张图：2D 结构由 RDKit 真实绘制，
    数值来自同一次运行的综合分计算，格内文字用 matplotlib（RDKit 字体会丢中文）。
    """
    import io
    import math

    import matplotlib.image as mpl_image

    items = [r for r in rows if r.get("smiles")][:max(1, int(top_n or _recommend_top_n()))]
    if not items:
        return None
    n = len(items)
    cols = max(1, min(cols, n))
    grid_rows = math.ceil(n / cols)
    fig, axes = plt.subplots(grid_rows, cols, figsize=(cols * 2.6, grid_rows * 2.7))
    axes_list = axes.flatten() if hasattr(axes, "flatten") else [axes]
    for ax, item in zip(axes_list, items):
        try:
            png = _draw_molecule_png(str(item["smiles"]))
            ax.imshow(mpl_image.imread(io.BytesIO(png), format="png"))
        except Exception as e:  # noqa: BLE001
            logger.warning("绘制推荐分子结构失败 %s: %s", item.get("name"), e)
            ax.text(0.5, 0.5, "结构绘制失败", ha="center", va="center", fontsize=8)
        aff = item.get("affinity_kcal_mol")
        head = f"#{item.get('rank') or ''} {str(item.get('name') or '')[:18]}".strip()
        comp = item.get("composite")
        line2 = f"综合分 {comp:.3f}（{item.get('grade') or '—'} 级）" if isinstance(comp, (int, float)) else ""
        line3 = f"{aff:.2f} kcal/mol" if isinstance(aff, (int, float)) else ""
        ax.set_title("\n".join(x for x in (head, line2, line3) if x), fontsize=8)
        ax.set_xticks([]); ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_color("#4c78a8")
    for ax in axes_list[n:]:
        ax.axis("off")
    fig.suptitle(f"推荐化合物 2D 结构（综合分降序，共 {n} 个）", fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    return _to_png(fig)


#: 相互作用类型 → 颜色（2D/3D 图与图例共用一份，保证两种图解释一致）
INTERACTION_COLORS = {
    "氢键": "#1f77b4",
    "盐桥": "#d62728",
    "疏水接触": "#7f7f7f",
    "π–π 堆叠": "#9467bd",
    "π–阳离子": "#8c564b",
    "金属配位": "#ff7f0e",
}


def _interaction_color(kinds) -> str:
    for key in ("盐桥", "氢键", "金属配位", "π–π 堆叠", "π–阳离子", "疏水接触"):
        if any(str(k).startswith(key) for k in kinds or []):
            return INTERACTION_COLORS[key]
    return "#7f7f7f"


def _anchor_atoms(analysis: Dict[str, Any], pose_path: str,
                  smiles: str) -> List[Dict[str, Any]]:
    """相互作用残基 → 2D 图上的锚点（0-based SMILES 原子下标）。

    靠位姿文件里的 `REMARK SMILES IDX`（PDBQT 原子序号 → SMILES 原子序号）对齐：
    名字不同、顺序不同都能对上，**不靠猜**。拿不到映射时返回空 dict（图上退化为只列残基）。
    """
    from docking_agent.core import interactions as I

    if not smiles or not pose_path:
        return []
    serial_to_smiles = I.pose_smiles_index_map(pose_path)
    if not serial_to_smiles:
        return []
    name_to_serial: Dict[str, int] = {}
    for atom in I.read_pdbqt(pose_path):
        if atom.get("serial") is not None and atom["name"] not in name_to_serial:
            name_to_serial[atom["name"]] = int(atom["serial"])
    out: List[Dict[str, Any]] = []
    for residue in analysis.get("residues") or []:
        for item in residue.get("detail") or []:
            # 位姿里的原子名会重复（所有碳都叫 C）→ 优先用唯一序号
            serial = item.get("ligand_serial")
            if serial is None:
                serial = name_to_serial.get(str(item.get("ligand_atom") or ""))
            idx1 = serial_to_smiles.get(int(serial)) if serial is not None else None
            if not idx1:
                continue
            out.append({"atom_idx": int(idx1) - 1, "residue": residue.get("residue"),
                        "types": residue.get("types") or [],
                        "distance": residue.get("min_distance")})
            break
    return out


def binding_interaction_map_2d(analysis: Dict[str, Any], *, pose_path: str = "",
                               smiles: str = "", title: str = "",
                               width: int = 900, height: int = 700) -> Optional[bytes]:
    """**2D** 结合分析图：配体结构式 + 相互作用残基（虚线指向对应原子，按类型着色）。

    结构式由 RDKit 画（用的是位姿文件里记录的 SMILES，即对接实际使用的化学形式），
    残基标签与虚线由 matplotlib 叠加 —— 这样中文字不会丢，原子位置也精确。
    """
    import io
    import math as _math

    import matplotlib.image as mpl_image
    from rdkit import Chem
    from rdkit.Chem import AllChem  # noqa: F401  (保证 rdDepictor 可用)
    from rdkit.Chem.Draw import rdMolDraw2D

    from docking_agent.core import interactions as I

    smiles = smiles or (I.pose_smiles(pose_path) if pose_path else "")
    if not smiles:
        return None
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    Chem.rdDepictor.Compute2DCoords(mol)
    anchors = _anchor_atoms(analysis, pose_path, smiles)

    drawer = rdMolDraw2D.MolDraw2DCairo(width, height)
    opts = drawer.drawOptions()
    opts.bondLineWidth = 2
    opts.minFontSize = 12
    opts.maxFontSize = 18
    highlight_atoms = sorted({a["atom_idx"] for a in anchors if 0 <= a["atom_idx"] < mol.GetNumAtoms()})
    # RDKit 的高亮色要 (r, g, b) 浮点元组，不是十六进制串
    def _rgb(hex_color: str) -> Tuple[float, float, float]:
        text = hex_color.lstrip("#")
        return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]

    highlight_colors = {a["atom_idx"]: _rgb(_interaction_color(a["types"])) for a in anchors
                        if 0 <= a["atom_idx"] < mol.GetNumAtoms()}
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol, highlightAtoms=highlight_atoms,
                                       highlightAtomColors=highlight_colors)
    drawer.FinishDrawing()
    png = drawer.GetDrawingText()
    coords = {a["atom_idx"]: drawer.GetDrawCoords(a["atom_idx"]) for a in anchors
              if 0 <= a["atom_idx"] < mol.GetNumAtoms()}

    fig, ax = plt.subplots(figsize=(9.2, 7.2))
    ax.imshow(mpl_image.imread(io.BytesIO(png), format="png"))
    ax.set_axis_off()
    cx = sum(p[0] for p in coords.values()) / len(coords) if coords else width / 2
    cy = sum(p[1] for p in coords.values()) / len(coords) if coords else height / 2
    # 按方向角排序后沿左右两侧均匀铺开标签：**每个残基一条**（多个残基可以指向同一个原子 ——
    # LigPlot 也是这么画的；用 setdefault 合并会丢掉"同一原子被多个残基接触"的信息）。
    anchored = [a for a in anchors if a["atom_idx"] in coords]
    anchored.sort(key=lambda a: _math.atan2(coords[a["atom_idx"]][1] - cy,
                                            coords[a["atom_idx"]][0] - cx))
    left = [a for a in anchored if coords[a["atom_idx"]][0] <= cx]
    right = [a for a in anchored if coords[a["atom_idx"]][0] > cx]
    for side, bucket in (("left", left), ("right", right)):
        for order, info in enumerate(bucket):
            px, py = coords[info["atom_idx"]]
            label_x = -0.06 * width if side == "left" else 1.06 * width
            label_y = height * (order + 1) / (len(bucket) + 1)
            color = _interaction_color(info["types"])
            ax.annotate(f"{info['residue']}\n{'/'.join(info['types'])}",
                        xy=(px, py), xytext=(label_x, label_y), textcoords="data",
                        fontsize=9.5, color=color, va="center",
                        ha="right" if side == "left" else "left",
                        arrowprops=dict(arrowstyle="-", color=color, linestyle="--",
                                        linewidth=1.2, shrinkA=2, shrinkB=2))
    handles = [plt.Line2D([], [], color=c, linestyle="--", label=k)
               for k, c in INTERACTION_COLORS.items()
               if any(k in (a["types"] or []) for a in anchors)]
    if handles:
        ax.legend(handles=handles, loc="lower center", ncol=min(3, len(handles)), fontsize=9,
                  frameon=False, bbox_to_anchor=(0.5, -0.02))
    summary = analysis.get("summary") or {}
    counts = summary.get("type_counts") or {}
    detail = "、".join(f"{k}×{v}" for k, v in counts.items()) or "无达标接触"
    ax.set_title(f"{title}\n2D 结合分析（{detail}；共 {summary.get('contact_residues', 0)} 个接触残基）",
                 fontsize=11.5)
    ax.set_xlim(-0.22 * width, 1.22 * width)
    ax.set_ylim(height * 1.04, -0.04 * height)
    fig.tight_layout()
    return _to_png(fig)


def binding_pose_3d(receptor_pdbqt: str, pose_path: str, analysis: Dict[str, Any], *,
                    title: str = "", box_center: Optional[Sequence[float]] = None,
                    box_size: Optional[Sequence[float]] = None,
                    radius: float = 9.0) -> Optional[bytes]:
    """**3D** 结合分析图：口袋内的真实位姿（受体近邻原子 + 配体骨架 + 相互作用虚线 + 对接盒）。

    坐标全部来自 PDBQT，等比例显示；相互作用线与 2D 图同色，便于对照着看。
    """
    import itertools

    from mpl_toolkits.mplot3d.art3d import Line3DCollection

    from docking_agent.core import interactions as I

    rec = [a for a in I.read_pdbqt(receptor_pdbqt) if a["element"] != "H"]
    lig = [a for a in I.read_pdbqt(pose_path) if a["element"] != "H"]
    if not rec or not lig:
        return None
    lig_center = [sum(a["xyz"][i] for a in lig) / len(lig) for i in range(3)]
    near = [a for a in rec if I.distance(a["xyz"], lig_center) <= radius]
    if not near:
        return None
    # 标签错开尺度：用配体包围盒（真实结构里两个残基的接触原子常常挨得很近，不错开会叠字）
    label_span = max(1.0, max(max(a["xyz"][i] for a in lig) - min(a["xyz"][i] for a in lig)
                              for i in range(3)))

    fig = plt.figure(figsize=(8.4, 7.2))
    ax = fig.add_subplot(111, projection="3d")
    palette = {"C": "#b0b0b0", "N": "#3b6fd4", "O": "#d64545", "S": "#d9b23a", "H": "#dddddd"}
    by_element: Dict[str, List[List[float]]] = {}
    for atom in near:
        element = atom["element"] if atom["element"] in palette else "C"
        if atom["ad4"] in I.METALS:
            element = atom["ad4"]
        by_element.setdefault(element, []).append(atom["xyz"])
    for element, points in by_element.items():
        color = "#ff7f0e" if element in I.METALS else palette.get(element, "#b0b0b0")
        ax.scatter([p[0] for p in points], [p[1] for p in points], [p[2] for p in points],
                   s=14, c=color, alpha=0.55, depthshade=False,
                   label=f"受体 {element}（{len(points)}）")

    # 配体骨架：PDBQT 没有键表，用键长判据（≤1.8 Å）连出来
    segments = [[a["xyz"], b["xyz"]] for a, b in itertools.combinations(lig, 2)
                if I.distance(a["xyz"], b["xyz"]) <= 1.8]
    if segments:
        ax.add_collection3d(Line3DCollection(segments, colors="#2ca02c", linewidths=2.6))
    ax.scatter([a["xyz"][0] for a in lig], [a["xyz"][1] for a in lig], [a["xyz"][2] for a in lig],
               s=42, c="#2ca02c", depthshade=False, label=f"配体（{len(lig)} 个重原子）")

    # 相互作用虚线：按原子名 + 残基回到真实坐标
    lig_by_name: Dict[str, List[float]] = {}
    for atom in lig:
        lig_by_name.setdefault(atom["name"], atom["xyz"])
    rec_by_key: Dict[Tuple[str, str, str], List[float]] = {}
    for atom in rec:
        rec_by_key.setdefault((str(atom.get("chain") or ""), str(atom.get("resnum") or ""),
                               str(atom.get("name"))), atom["xyz"])
    drawn: List[str] = []
    for residue in analysis.get("residues") or []:
        for item in residue.get("detail") or []:
            kinds = str(item.get("type") or "")
            if not any(k in kinds for k in ("氢键", "盐桥", "金属配位", "π")):
                continue
            lig_xyz = lig_by_name.get(str(item.get("ligand_atom") or ""))
            rec_xyz = rec_by_key.get((str(residue.get("chain") or ""),
                                      str(residue.get("resnum") or ""),
                                      str(item.get("receptor_atom") or "")))
            if not lig_xyz or not rec_xyz:
                continue
            color = _interaction_color(kinds.split("、"))
            ax.plot([lig_xyz[0], rec_xyz[0]], [lig_xyz[1], rec_xyz[1]], [lig_xyz[2], rec_xyz[2]],
                    color=color, linestyle="--", linewidth=1.6)
            if residue.get("residue") not in drawn:
                drawn.append(str(residue.get("residue")))
                # 标签轻微错开（真实结构里两个残基的接触原子常常挨得很近，不错开就会叠字）
                shift = 0.025 * label_span * len(drawn)
                ax.text(rec_xyz[0], rec_xyz[1], rec_xyz[2] + shift,
                        " " + str(residue.get("residue")), fontsize=8.5, color=color)

    if box_center and box_size:
        cx, cy, cz = [float(v) for v in box_center]
        sx, sy, sz = [float(v) / 2 for v in box_size]
        for sxs, sys_, szs in itertools.product((-1, 1), repeat=3):
            ax.scatter([cx + sxs * sx], [cy + sys_ * sy], [cz + szs * sz], s=6, c="#444444",
                       marker="s", alpha=0.5)
        corners = {k: (cx + k[0] * sx, cy + k[1] * sy, cz + k[2] * sz)
                   for k in itertools.product((-1, 1), repeat=3)}
        edges = [(a, b) for a in corners for b in corners
                 if sum(1 for i in range(3) if a[i] != b[i]) == 1]
        ax.add_collection3d(Line3DCollection([[corners[a], corners[b]] for a, b in edges],
                                            colors="#444444", linewidths=0.7, alpha=0.5))

    counts = (analysis.get("summary") or {}).get("type_counts") or {}
    detail = "、".join(f"{k}×{v}" for k, v in counts.items() if k != "疏水接触")
    ax.set_title(f"{title}\n3D 结合姿态（口袋内 {len(near)} 个受体原子；{detail or '无非疏水接触'}）",
                 fontsize=11)
    ax.set_xlabel("X (Å)"); ax.set_ylabel("Y (Å)"); ax.set_zlabel("Z (Å)")
    span = max(max(a["xyz"][i] for a in near) - min(a["xyz"][i] for a in near) for i in range(3))
    for setter, axis in ((ax.set_xlim, 0), (ax.set_ylim, 1), (ax.set_zlim, 2)):
        mid = lig_center[axis]
        setter(mid - span / 2, mid + span / 2)
    try:
        ax.set_box_aspect((1, 1, 1))
    except Exception as e:  # noqa: BLE001 - 老版本 matplotlib 不支持该参数
        logger.debug("set_box_aspect 不可用：%s", e)
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    fig.tight_layout()
    return _to_png(fig)


def binding_scatter(molecules: List[Dict[str, Any]], pos_control: Optional[Dict[str, Any]] = None,
                    top_n: int = 300) -> Optional[bytes]:
    """结合模式散点图：横轴=与阳性对照的相似度（Morgan），纵轴=对接亲和力。

    点按「是否与对照共享锚定基团（anchor_match）」着色；无相似度数据时返回 None。
    """
    pts = [m for m in molecules
           if isinstance(m.get("affinity_kcal_mol"), (int, float))
           and isinstance(m.get("similarity_to_positive_control"), (int, float))]
    if not pts:
        return None
    pts = pts[:max(1, top_n)]

    fig, ax = plt.subplots(figsize=(8, 5))
    anchored = [m for m in pts if m.get("anchor_match")]
    other = [m for m in pts if not m.get("anchor_match")]
    if other:
        ax.scatter([m["similarity_to_positive_control"] for m in other],
                   [m["affinity_kcal_mol"] for m in other],
                   s=34, c="#4C72B0", edgecolor="white", linewidth=.5, label="未共享锚定基团", zorder=3)
    if anchored:
        ax.scatter([m["similarity_to_positive_control"] for m in anchored],
                   [m["affinity_kcal_mol"] for m in anchored],
                   s=44, c="#C44E52", marker="D", edgecolor="white", linewidth=.5,
                   label="共享脒基/胍基锚定基团", zorder=4)
    # 标注亲和力最优的前 8 个
    for m in sort_by_affinity(pts)[:8]:
        ax.annotate(str(m.get("name") or "")[:14],
                    (m["similarity_to_positive_control"], m["affinity_kcal_mol"]),
                    fontsize=7, xytext=(4, 4), textcoords="offset points")

    pc_aff = (pos_control or {}).get("affinity_kcal_mol")
    if isinstance(pc_aff, (int, float)):
        ax.axhline(pc_aff, color="#C44E52", linestyle="--", alpha=0.7,
                   label=f"阳性对照亲和力 {pc_aff:.2f}")
    ax.set_xlabel("与阳性对照的 Morgan 指纹相似度")
    ax.set_ylabel("对接亲和力 (kcal/mol)")
    ax.set_title("结合模式分析：相似度 vs 亲和力")
    ax.grid(alpha=0.25, zorder=0)
    ax.legend(fontsize=8, loc="best")
    fig.tight_layout()
    return _to_png(fig)
