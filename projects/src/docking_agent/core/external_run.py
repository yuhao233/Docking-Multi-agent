"""外部对接引擎的**执行适配**：把已登记的外部 CLI 真的跑起来，并把结果解析成项目的结果行。

与 `external_tools.py` 的分工（那是"登记与探测"，这是"执行"）：

| 模块 | 职责 |
| --- | --- |
| `external_tools.py` | 识别引擎类型、探测版本与 GPU 可见性、`build_argv()` 生成命令行、配置校验 |
| `external_run.py`（本模块） | 生成/复用格点图、调用外部二进制、解析其输出（DLG / 位姿 PDBQT）、产出与内置引擎同形的结果行 |

已接入执行适配的类型：

* `autodock-gpu`：需要 `autogrid4` 预先生成的 `.maps.fld` + `.map`（本模块负责生成），
  输出 AD4 格式 `.dlg`，能量项与内置 AutoDock4 路径同口径；
* `vina-cpu`：官方 CPU CLI，`--out` 写位姿 PDBQT，再用 `--score_only` 取能量分解。

`unidock` / `vina-gpu` 目前**只登记不执行**：本机没有这两类二进制可验证输出格式，
按项目「不猜、不静默」的口径直接报错，而不是硬猜参数。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from docking_agent.core.external_tools import ExternalEngineError, build_argv

logger = logging.getLogger(__name__)

#: 格点间距（Å）：AutoDock 官方推荐值，写进 GPF 与 npts 计算
GRID_SPACING = 0.375

#: 配体侧格点类型（AutoDock 标准集）：格点图按**会话内共享**生成，必须覆盖本次可能出现的配体
#: 原子类型，而不是只按某一个配体的类型生成（否则换一个含 S/卤素的配体就对不上图）。
#: 受体一侧用其 PDBQT 里真实出现的类型（autogrid4 需要据此算能量)。
#:
#: 下面是本机 autogrid4（conda-forge autogrid 4.2.9）**实测可用**的类型全集：
#: 多极性氢 `HD`（meeko 准备的配体一律带 HD，缺图会让 AutoDock-GPU 直接判任务失败）、
#: 卤素与磷、以及常见金属。Cu/Hg/Se/Na/K 被 autogrid4 参数库判为 unknown 类型，故不列入；
#: 配体里真出现这类原子时由 `DockingSession` 按需扩展格点图并给出明确报错（不静默）。
STANDARD_LIGAND_TYPES = ("A", "C", "HD", "H", "N", "NA", "OA", "S", "SA",
                         "F", "Cl", "Br", "I", "P", "Fe", "Mg", "Mn", "Zn", "Ca", "B", "Si")


def parse_dlg_energies(dlg_path: str | Path) -> Dict[str, Optional[float]]:
    """解析 AD4 格式 DLG 的能量行（内置 AutoDock4 与 AutoDock-GPU 的输出格式相同）。

    `--nrun N`（本项目把 `n_poses` 映射到它）会让 DLG 里出现 N 组结果，**第一组不一定是最好的一组**；
    因此这里逐组解析、取「Estimated Free Energy of Binding」最低的那一组，并用文件末尾
    `CLUSTERING HISTOGRAM` 的最低结合能交叉校验（存在时以直方图为准 —— 那是 AD4 自己聚类后的最优解）。
    """
    est = inter = internal = torsional = None
    best: Dict[str, Optional[float]] = {}
    current: Optional[Dict[str, Optional[float]]] = None
    histogram: Optional[float] = None
    in_histogram = False

    def _close(block: Optional[Dict[str, Optional[float]]]) -> None:
        nonlocal best
        if not block or block.get("affinity_kcal_mol") is None:
            return
        if not best or float(block["affinity_kcal_mol"]) < float(best["affinity_kcal_mol"] or 1e9):
            best = block

    with open(dlg_path, encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if "CLUSTERING HISTOGRAM" in line:
                in_histogram = True
                _close(current)
                current = None
                continue
            if "RMSD TABLE" in line:
                in_histogram = False
            if in_histogram:
                # 直方图数据行形如 `   1 |     -5.59 |   3 |     -5.59 |    3 |###`
                cells = [c.strip() for c in line.split("|")]
                if cells and cells[0].isdigit() and len(cells) > 1:
                    try:
                        value = float(cells[1])
                    except ValueError:
                        continue
                    histogram = value if histogram is None else min(histogram, value)
                continue
            if "Estimated Free Energy of Binding" in line:
                _close(current)
                current = {"affinity_kcal_mol": _first_float_after_equals(line)}
            elif current is not None and "Final Intermolecular Energy" in line:
                current["intermolecular_kcal_mol"] = _first_float_after_equals(line)
            elif current is not None and "Final Total Internal Energy" in line:
                current["intramolecular_kcal_mol"] = _first_float_after_equals(line)
            elif current is not None and "Torsional Free Energy" in line:
                current["torsion_kcal_mol"] = _first_float_after_equals(line)
    _close(current)

    if best:
        est = best.get("affinity_kcal_mol")
        inter = best.get("intermolecular_kcal_mol")
        internal = best.get("intramolecular_kcal_mol")
        torsional = best.get("torsion_kcal_mol")
    if histogram is not None:
        est = histogram
    return {"affinity_kcal_mol": est, "intermolecular_kcal_mol": inter,
            "intramolecular_kcal_mol": internal, "torsion_kcal_mol": torsional}


def extract_best_pose_pdbqt(dlg_path: str | Path, out_path: str | Path,
                            *, energy: Optional[float] = None) -> str:
    """从 AD4 / AutoDock-GPU 的 DLG 里抽出**最优那一组**的位姿，写成标准 PDBQT。

    为什么需要：`--nrun N`（由 `n_poses` 映射）时 DLG 内含 N 组结果，而下游的位姿分析、
    报告与下载都按 **PDBQT** 读（`core/interactions.read_pdbqt`）。此前外部适配只把 `.dlg`
    原样留档 → `analyze_pose_pocket` 读不出 → 报告整段"未产生可读取的位姿文件"，
    2845 个真实位姿等于白算（真实故障 2026-09-23）。这里把最优组的 `DOCKED:` 载荷
    （ROOT/BRANCH/ATOM/TORSDOF）剥掉前缀后原样写出，等于 AD4 原生位姿转成 PDBQT。

    返回写出的路径；DLG 里找不到可解析的位姿时抛 `ExternalEngineError`（不静默产出空文件）。
    """
    best_lines: list = []
    best_energy: Optional[float] = None
    current: list = []
    current_energy: Optional[float] = None
    in_run = False

    def _flush() -> None:
        nonlocal best_lines, best_energy, current, current_energy
        if current and current_energy is not None:
            if best_energy is None or current_energy < best_energy:
                best_energy, best_lines = current_energy, list(current)
        current, current_energy = [], None

    with open(dlg_path, encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if line.startswith("Run:"):
                _flush()
                in_run = True
                continue
            if "Estimated Free Energy of Binding" in line:
                current_energy = _first_float_after_equals(line)
                continue
            if line.startswith("DOCKED: ") and in_run:
                payload = line[len("DOCKED: "):].rstrip("\n")
                if payload.startswith(("MODEL", "ENDMDL")):
                    continue            # 组边界由 Run: 划分，MODEL/ENDMDL 不写进单模型文件
                current.append(payload)
    _flush()

    if not best_lines:
        raise ExternalEngineError(
            f"DLG 里没有可解析的位姿（{os.path.basename(str(dlg_path))}）："
            "AutoDock-GPU 可能未写出 DOCKED 载荷。")
    if energy is None:
        energy = best_energy
    header = [f"REMARK SOURCE {os.path.basename(str(dlg_path))}"]
    if energy is not None:
        header.append(f"REMARK BEST BINDING ENERGY {float(energy):.2f} kcal/mol")
    header.append("MODEL        1")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(header + best_lines + ["ENDMDL", ""]), encoding="utf-8")
    return str(out)


def parse_vina_pose_energy(pose_pdbqt: str | Path) -> Optional[float]:
    """从 Vina 写出的位姿 PDBQT 里取最佳亲和力（`REMARK VINA RESULT:` 第一行）。"""
    with open(pose_pdbqt, encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if "REMARK VINA RESULT" in line:
                parts = line.split()
                for token in parts[2:]:
                    try:
                        return float(token)
                    except ValueError:
                        continue
    return None


def _first_float_after_equals(line: str) -> Optional[float]:
    try:
        return float(line.split("=")[1].split()[0])
    except (IndexError, ValueError):
        return None


def _grid_points(box: Sequence[float]) -> list:
    """格点数：按间距换算成**奇数**（AutoDock 要求网格中心落在格点上）。"""
    points = []
    for size in box:
        n = int(float(size) // GRID_SPACING)
        points.append(n if n % 2 else n + 1)
    return points


def build_grid_maps(receptor_pdbqt: str, center: Sequence[float], size: Sequence[float],
                    workdir: str, *, autogrid4: Optional[str] = None,
                    tag: str = "rec",
                    ligand_types: Sequence[str] = STANDARD_LIGAND_TYPES) -> str:
    """用 `autogrid4` 生成格点能量图，返回描述文件（`.maps.fld`）路径。

    AutoDock-GPU 与内置 AutoDock4 都吃这份图；同一受体 + 同一盒子只生成一次（调用方缓存）。
    受体/配体的原子类型从 PDBQT 正文读取，不猜。
    """
    from docking_agent.core.docking import _pdbqt_atom_types  # 延迟导入：避免循环依赖

    binary = autogrid4 or _autodock_bin("autogrid4")
    if not binary:
        raise ExternalEngineError(
            "未找到 autogrid4：AutoDock-GPU 需要它生成格点图。"
            "安装经典 AutoDock/AutoGrid（conda-forge 的 autogrid）或设置 AUTOGRID4_BIN。")
    rec = os.path.join(workdir, f"{tag}.pdbqt")
    if os.path.abspath(receptor_pdbqt) != os.path.abspath(rec):
        shutil.copy(receptor_pdbqt, rec)
    receptor_types = _pdbqt_atom_types(rec)
    if not receptor_types:
        raise ExternalEngineError("受体 PDBQT 未解析出原子类型，无法生成格点图。")
    ligand_types = [t for t in ligand_types] or list(STANDARD_LIGAND_TYPES)

    npts = _grid_points(size)
    gpf = [
        "autogrid_parameter_version 4.2.6",
        f"npts {npts[0]} {npts[1]} {npts[2]}",
        f"gridfld {tag}.maps.fld",
        f"spacing {GRID_SPACING}",
        "receptor_types " + " ".join(receptor_types),
        "ligand_types " + " ".join(ligand_types),
        f"receptor {tag}.pdbqt",
        f"gridcenter {float(center[0]):.3f} {float(center[1]):.3f} {float(center[2]):.3f}",
        "smooth 0.5",
    ]
    gpf += [f"map {tag}.{t}.map" for t in ligand_types]
    gpf += [f"elecmap {tag}.e.map", f"dsolvmap {tag}.d.map", "dielectric -0.1465"]
    with open(os.path.join(workdir, f"{tag}.gpf"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(gpf) + "\n")

    proc = subprocess.run([binary, "-p", f"{tag}.gpf", "-l", f"{tag}.glg"],
                          capture_output=True, text=True, timeout=1800, cwd=workdir, check=False)
    fld = os.path.join(workdir, f"{tag}.maps.fld")
    if proc.returncode != 0 or not os.path.exists(fld):
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
        raise ExternalEngineError("autogrid4 生成格点图失败：" + " / ".join(tail),
                                  details={"returncode": proc.returncode, "fld": fld})
    logger.info("格点图已生成：%s（npts=%s）", fld, npts)
    return fld


def dock_ligand_external(*, flavor: str, binary: str, ligand_pdbqt: str, workdir: str,
                         center: Sequence[float], size: Sequence[float],
                         exhaustiveness: int, n_poses: int, seed: int,
                         receptor_pdbqt: str = "", fld: str = "",
                         pose_base: Optional[str] = None,
                         threads: Optional[int] = None,
                         device: Optional[int] = None,
                         engine_version: str = "") -> Dict[str, Any]:
    """调用外部二进制对接单个配体，返回与内置引擎同形的结果行。

    `flavor` 决定调用形状与输出解析；未接入执行适配的类型直接报错（不猜参数）。
    """
    if flavor not in ("autodock-gpu", "vina-cpu"):
        raise ExternalEngineError(
            f"{flavor or '（未知类型）'} 尚未接入执行适配：本机没有该类二进制可验证输出格式。"
            "目前可用于执行的是 autodock-gpu 与 vina-cpu。")
    if flavor == "autodock-gpu":
        if not fld:
            raise ExternalEngineError("autodock-gpu 需要格点图描述文件（.maps.fld）")
        row = _dock_autodock_gpu(binary=binary, ligand_pdbqt=ligand_pdbqt, workdir=workdir,
                                 fld=fld, n_poses=n_poses, seed=seed, pose_base=pose_base,
                                 exhaustiveness=exhaustiveness, device=device)
    else:
        row = _dock_vina_cpu(binary=binary, ligand_pdbqt=ligand_pdbqt, workdir=workdir,
                             receptor_pdbqt=receptor_pdbqt, center=center, size=size,
                             exhaustiveness=exhaustiveness, n_poses=n_poses, seed=seed,
                             threads=threads, pose_base=pose_base)
    row["engine"] = flavor
    if engine_version:
        row["engine_version"] = engine_version
    return row


def _dock_autodock_gpu(*, binary: str, ligand_pdbqt: str, workdir: str, fld: str,
                       n_poses: int, seed: int, exhaustiveness: int,
                       pose_base: Optional[str], device: Optional[int] = None) -> Dict[str, Any]:
    resnam = os.path.join(workdir, "adgpu_out")
    argv = build_argv("autodock-gpu", binary=binary, receptor="", ligands=[ligand_pdbqt],
                      out_dir=workdir, center=[0.0, 0.0, 0.0], size=[0.0, 0.0, 0.0],
                      exhaustiveness=exhaustiveness, n_poses=n_poses, seed=seed,
                      fld=fld, resnam=resnam, device=device)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=3600,
                          cwd=workdir, check=False)
    dlg = f"{resnam}.dlg"
    if not os.path.exists(dlg):
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-4:]
        raise ExternalEngineError("AutoDock-GPU 未生成结果文件：" + " / ".join(tail),
                                  details={"returncode": proc.returncode, "argv": argv[:6]})
    energies = parse_dlg_energies(dlg)
    if energies["affinity_kcal_mol"] is None:
        raise ExternalEngineError(
            "AutoDock-GPU 结果里没有能量行（可能配体或格点图不匹配、或任务未成功）。"
            f"日志尾部：{(proc.stdout or '').strip().splitlines()[-2:]}")
    pose_file = ""
    pose_raw = ""
    if pose_base:
        pose_file = f"{pose_base}.pdbqt"
        os.makedirs(os.path.dirname(pose_file) or ".", exist_ok=True)
        # 位姿统一落成 PDBQT（下游分析/报告/下载都按 PDBQT 读），原始 DLG 同时保留留档
        extract_best_pose_pdbqt(dlg, pose_file, energy=energies["affinity_kcal_mol"])
        pose_raw = f"{pose_base}.dlg"
        shutil.copyfile(dlg, pose_raw)
    return {
        "affinity_kcal_mol": round(float(energies["affinity_kcal_mol"]), 2),
        "intermolecular_kcal_mol": _round_or_nan(energies["intermolecular_kcal_mol"]),
        "intramolecular_kcal_mol": _round_or_nan(energies["intramolecular_kcal_mol"]),
        "torsion_kcal_mol": _round_or_nan(energies["torsion_kcal_mol"]),
        "pose_file": pose_file,
        "pose_raw": pose_raw,
    }


def _dock_vina_cpu(*, binary: str, ligand_pdbqt: str, workdir: str, receptor_pdbqt: str,
                   center: Sequence[float], size: Sequence[float], exhaustiveness: int,
                   n_poses: int, seed: int, threads: Optional[int],
                   pose_base: Optional[str]) -> Dict[str, Any]:
    if not receptor_pdbqt or not os.path.exists(receptor_pdbqt):
        raise ExternalEngineError("vina-cpu 需要可读的受体 PDBQT")
    out_pose = os.path.join(workdir, "vina_out.pdbqt")
    argv = build_argv("vina-cpu", binary=binary, receptor=receptor_pdbqt,
                      ligands=[ligand_pdbqt], out_dir=workdir, center=center, size=size,
                      exhaustiveness=exhaustiveness, n_poses=n_poses, seed=seed,
                      threads=threads, out_pose=out_pose)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=3600,
                          cwd=workdir, check=False)
    best = parse_vina_pose_energy(out_pose) if os.path.exists(out_pose) else None
    if best is None:
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-4:]
        raise ExternalEngineError("vina-cpu 未产生可解析的位姿：" + " / ".join(tail),
                                  details={"returncode": proc.returncode})
    terms = _vina_score_only(binary, receptor_pdbqt, out_pose, center, size, threads)
    pose_file = ""
    if pose_base:
        pose_file = f"{pose_base}.pdbqt"
        os.makedirs(os.path.dirname(pose_file) or ".", exist_ok=True)
        shutil.copyfile(out_pose, pose_file)
    return {
        "affinity_kcal_mol": round(float(best), 2),
        "intermolecular_kcal_mol": _round_or_nan(terms.get("inter")),
        "intramolecular_kcal_mol": _round_or_nan(terms.get("intra")),
        "torsion_kcal_mol": _round_or_nan(terms.get("torsion")),
        "pose_file": pose_file,
    }


def _vina_score_only(binary: str, receptor: str, pose: str, center: Sequence[float],
                     size: Sequence[float], threads: Optional[int]) -> Dict[str, Optional[float]]:
    """用 `--score_only` 取能量分解（官方 CLI 的输出格式随版本略有差异，解析失败按不可用处理）。"""
    argv = [binary, "--receptor", receptor, "--ligand", pose, "--score_only",
            "--center_x", f"{float(center[0]):.3f}", "--center_y", f"{float(center[1]):.3f}",
            "--center_z", f"{float(center[2]):.3f}", "--size_x", f"{float(size[0]):.3f}",
            "--size_y", f"{float(size[1]):.3f}", "--size_z", f"{float(size[2]):.3f}"]
    if threads:
        argv += ["--cpu", str(int(threads))]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired):  # 允许静默：能量分解是附加信息，主分数已拿到
        return {}
    out: Dict[str, Optional[float]] = {}
    for line in (proc.stdout or "").splitlines():
        low = line.lower()
        value = _first_float_after_colon(line)
        if value is None:
            continue
        if "intermolecular" in low:
            out["inter"] = value
        elif "total internal" in low or "intramolecular" in low:
            out["intra"] = value
        elif "torsional" in low:
            out["torsion"] = value
    return out


def _first_float_after_colon(line: str) -> Optional[float]:
    if ":" not in line:
        return None
    for token in line.split(":", 1)[1].replace("=", " ").split():
        try:
            return float(token)
        except ValueError:
            continue
    return None


def _round_or_nan(value: Optional[float]) -> float:
    return round(float(value), 2) if value is not None else float("nan")


def _autodock_bin(name: str) -> Optional[str]:
    """定位 AD4 家族二进制（`AUTODOCK4_BIN` / `AUTOGRID4_BIN` → PATH），与内置路径同一口径。"""
    from docking_agent.core.docking import _autodock_bin as _impl  # 延迟导入：避免循环依赖

    return _impl(name)
