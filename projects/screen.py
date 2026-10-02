#!/usr/bin/env python3
"""虚拟筛选主入口：受体结构 + 候选分子库 → 候选清单（`results.csv`）。

本脚本是提交材料的**一键运行入口**，直接调用项目计算内核完成真实分子对接，
不依赖大模型即可复现结果（大模型只用于网页端的人机交互与编排，见 README「模型与第三方工具」）。

典型用法：

    # 1) 最小示例（仓库自带 data/example）
    python screen.py --receptor data/example/receptor_demo.pdb \
                     --ligands data/example/ligands_demo.smi \
                     --center 31.5,13.74,24.36 --size 22,22,22 \
                     --exhaustiveness 4 --n-poses 1 --seed 42 \
                     --out results/results.csv

    # 2) 不给位点盒时用内置口袋检测（P2Rank 或几何法）自动定盒
    python screen.py --receptor receptor.pdb --ligands library.sdf --auto-site

输出：`results.csv`（UTF-8，字段见 README「输出说明」），同时把本次运行的参数、
随机种子、引擎版本与耗时写入 `logs/`，便于复核。

依赖：见 requirements.txt（Python 3.12；AutoDock Vina 1.2.7 由 `vina` 包提供）。
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SRC = PROJECT_ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

#: 提交清单字段（与 README「输出说明」一致；列名保持英文，便于机器读取）
RESULT_FIELDS = [
    "candidate_id",        # 候选编号（库内序号，或输入文件里的 ID 字段）
    "track",               # 所属赛道（本作品：AI 模型与代码）
    "name",                # 名称（输入文件里的名称/标题）
    "smiles",              # 候选结构（规范 SMILES）
    "affinity_kcal_mol",   # 关键预测指标：对接结合亲和力（kcal/mol，越负结合越强）
    "ligand_efficiency",   # 配体效率（|ΔG| / 重原子数）
    "engine",              # 实际执行对接的引擎（vina / autodock / external:*）
    "engine_version",      # 引擎版本（可复现性）
    "seed",                # 随机种子
    "exhaustiveness",      # 搜索强度
    "n_poses",             # 输出位姿数
    "box_center",          # 对接盒中心（Å）
    "box_size",            # 对接盒边长（Å）
    "receptor_file",       # 受体结构文件（与随附结构文件对应）
    "pose_file",           # 位姿文件（相对路径；未保存时为空）
    "status",              # ok / error（失败原因见 remark）
    "remark",              # 备注：输入文件里的附加字段、失败原因、告警
]


def _parse_floats(text: str, expect: int) -> list:
    values = [p for p in str(text or "").replace(";", ",").replace("，", ",").split(",") if p.strip()]
    if len(values) != expect:
        raise SystemExit(f"需要 {expect} 个数字，收到 {len(values)} 个：{text}")
    return [float(v) for v in values]


def _engine_version(engine: str) -> str:
    """引擎版本：写入清单，保证结果可追溯。"""
    try:
        import importlib.metadata as meta

        if engine.startswith("external"):
            from docking_agent.core.external_tools import collect

            report = collect(check_gpu=False)
            return str(report.get("version_line") or report.get("flavor") or "")
        return f"AutoDock Vina {meta.version('vina')}"
    except Exception:  # noqa: BLE001 - 版本探测失败不应影响出结果
        return ""


def _auto_site(receptor: str) -> tuple:
    """未给定位点盒时用口袋检测定盒（P2Rank 可用则优先，否则内置几何法）。"""
    from docking_agent.core.pockets import select_site
    from docking_agent.core.receptors import resolve_receptor_specs

    spec = resolve_receptor_specs(receptor)[0][0]
    picked = select_site(spec, engine="auto", use_cache=False)
    return list(picked["center"]), list(picked["size"]), picked.get("source", "")


def _remark(row: dict) -> str:
    fields = row.get("fields") or {}
    parts = [f"{k}={v}" for k, v in fields.items() if str(v).strip()]
    if row.get("error"):
        parts.append(f"失败：{row['error']}")
    for warning in (row.get("ligand_warnings") or [])[:2]:
        parts.append(str(warning))
    return "; ".join(parts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="分子对接虚拟筛选（一键生成候选清单）")
    parser.add_argument("--receptor", required=True, help="受体结构文件（.pdb/.ent/.cif/.pdbqt）")
    parser.add_argument("--ligands", required=True,
                        help="候选分子库（.sdf/.smi/.smiles/.csv/.mol2/.xlsx）")
    parser.add_argument("--center", default="", help="对接盒中心 x,y,z（Å）；与 --auto-site 二选一")
    parser.add_argument("--size", default="22,22,22", help="对接盒边长 a,b,c（Å），默认 22,22,22")
    parser.add_argument("--auto-site", action="store_true", help="未定位点时用口袋检测自动定盒")
    parser.add_argument("--exhaustiveness", type=int, default=8, help="搜索强度（默认 8）")
    parser.add_argument("--n-poses", type=int, default=1, help="输出位姿数（默认 1）")
    parser.add_argument("--engine", default="vina", choices=["vina", "autodock", "auto", "external"],
                        help="对接引擎（默认内置 AutoDock Vina）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（默认 42，固定以保证可复现）")
    parser.add_argument("--max-ligands", type=int, default=0, help="最多对接多少个分子（0=全部）")
    parser.add_argument("--save-poses", action="store_true", help="保存位姿文件到 results/poses/")
    parser.add_argument("--out", default="results/results.csv", help="候选清单输出路径")
    parser.add_argument("--log-dir", default="logs", help="运行日志目录")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = time.time()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    if args.center:
        center, size = _parse_floats(args.center, 3), _parse_floats(args.size, 3)
        site_source = "命令行给出的坐标"
    elif args.auto_site:
        center, size, site_source = _auto_site(args.receptor)
        center, size = [float(x) for x in center], [float(x) for x in size]
    else:
        raise SystemExit("请给出 --center x,y,z，或使用 --auto-site 自动检测结合口袋")

    from docking_agent.core import read_molecule_file_normalized
    from docking_agent.core.docking import dock_library

    _fmt, molecules, _norm = read_molecule_file_normalized(args.ligands)
    if not molecules:
        raise SystemExit(f"分子库没有解析出任何分子：{args.ligands}")

    logging.info("受体：%s", args.receptor)
    logging.info("配体：%s（%d 个分子）", args.ligands, len(molecules))
    logging.info("对接盒：中心 %s，边长 %s（来源：%s）", center, size, site_source)
    logging.info("引擎 %s，搜索强度 %d，种子 %d", args.engine, args.exhaustiveness, args.seed)

    result = dock_library(
        molecules, receptor=args.receptor,
        exhaustiveness=int(args.exhaustiveness), n_poses=int(args.n_poses),
        engine=args.engine, seed=int(args.seed), site={"center": center, "size": size},
        pose_dir=(str(out_path.parent / "poses") if args.save_poses else None),
        max_ligands=(int(args.max_ligands) or None),
    )

    rows = [r for block in result.get("receptors", []) for r in (block.get("results") or [])]
    rows.sort(key=lambda r: (r.get("affinity_kcal_mol") is None, r.get("affinity_kcal_mol") or 0.0))
    version = _engine_version(str(args.engine))

    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for index, row in enumerate(rows, 1):
            affinity = row.get("affinity_kcal_mol")
            heavy = (row.get("ligand_facts") or {}).get("heavy_atoms")
            writer.writerow({
                "candidate_id": row.get("id") or index,
                "track": "AI模型与代码",
                "name": row.get("name") or row.get("smiles"),
                "smiles": row.get("smiles"),
                "affinity_kcal_mol": ("" if affinity is None else f"{float(affinity):.2f}"),
                "ligand_efficiency": ("" if not (affinity and heavy)
                                      else f"{abs(float(affinity)) / float(heavy):.4f}"),
                "engine": row.get("engine") or args.engine,
                "engine_version": version,
                "seed": args.seed,
                "exhaustiveness": row.get("exhaustiveness") or args.exhaustiveness,
                "n_poses": args.n_poses,
                "box_center": ",".join(f"{float(x):.2f}" for x in center),
                "box_size": ",".join(f"{float(x):.2f}" for x in size),
                "receptor_file": str(args.receptor),
                "pose_file": os.path.relpath(row["pose_file"], out_path.parent)
                if row.get("pose_file") else "",
                "status": "ok" if affinity is not None else "error",
                "remark": _remark(row),
            })

    elapsed = time.time() - started
    summary = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "receptor": str(args.receptor),      # 按调用时给出的路径记录（不写宿主机绝对路径）
        "ligands": str(args.ligands),
        "molecules_total": len(molecules),
        "molecules_scored": sum(1 for r in rows if r.get("affinity_kcal_mol") is not None),
        "molecules_failed": sum(1 for r in rows if r.get("error")),
        "engine": args.engine,
        "engine_version": version,
        "exhaustiveness": args.exhaustiveness,
        "n_poses": args.n_poses,
        "seed": args.seed,
        "box_center": center,
        "box_size": size,
        "site_source": site_source,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "elapsed_sec": round(elapsed, 1),
        "notes": result.get("notes") or [],
        "output": str(out_path),
    }
    (log_dir / "run_example.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
    (log_dir / "run_example.log").write_text(
        "\n".join([f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {line}"
                   for line in [
                       f"受体 {summary['receptor']}",
                       f"配体 {summary['ligands']}（{summary['molecules_total']} 个）",
                       f"对接盒 center={center} size={size}（{site_source}）",
                       f"引擎 {args.engine} {version}，exhaustiveness={args.exhaustiveness}，"
                       f"n_poses={args.n_poses}，seed={args.seed}",
                       f"成功 {summary['molecules_scored']} / 失败 {summary['molecules_failed']}，"
                       f"耗时 {summary['elapsed_sec']} s",
                       f"输出 {summary['output']}",
                   ]]) + "\n", encoding="utf-8")

    logging.info("完成：成功 %d / 失败 %d，耗时 %.1f s → %s",
                 summary["molecules_scored"], summary["molecules_failed"], elapsed, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
