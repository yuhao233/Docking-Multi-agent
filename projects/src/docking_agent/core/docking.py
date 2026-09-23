"""分子对接引擎：AutoDock Vina（主）与 AutoDock4 CPU（备用），含位姿导出。

面向大库（上千～上万分子）的设计：
  * **网格图复用**：Vina 的 `compute_vina_maps` 开销与对接本身同量级，
    逐配体重算会让大库筛选慢一倍以上；这里每个 worker 进程只对「受体+盒子」算一次，
    之后在整批配体间复用同一个 Vina 实例。
  * **多进程并行**：`DOCKING_WORKERS` 留空时由 `plan_concurrency` 自动规划 ——
    可用核 = CPU 数 − 1，分子数 ≥ 8 时取 `min(可用核, 24)` 个进程（再被分子数与
    `DOCKING_WORKER_MEM_MB` 的内存护栏夹紧）；分子数 < 8 时改走「1 进程 × 多线程」，
    避免多进程各自建网格图的固定开销。Vina 自身线程数由 `VINA_CPU`（默认 1）控制，
    避免与进程级并行超订。
  * **小库走串行**：分子数很少时不付进程启动成本，行为与串行完全一致。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Sequence, Tuple

from docking_agent.cancellation import CancelledRun
from docking_agent.config import env, env_int
from docking_agent.core.files import slug
from docking_agent.core.ligands import smiles_to_pdbqt
from docking_agent.core.params import DEFAULT_EXHAUSTIVENESS
from docking_agent.core.pockets import (
    BOX_LARGE_MAX_SIZE,
    apply_box_floor,
    box_group_margin,
    box_large_padding,
    engine_settings,
    library_span_bound,
    ligand_span as _ligand_span,
    select_site,
)
from docking_agent.core.receptors import (
    DEFAULT_BOX_SIZE,
    _pdbqt_centroid,
    box_atom_stats,
    resolve_receptor_specs,
)

logger = logging.getLogger(__name__)



# 阳性对照在对接清单里的内部标记名（对外展示时会被替换为「阳性对照」）
POSITIVE_CONTROL_NAME = "__positive_control__"

# `DEFAULT_EXHAUSTIVENESS` 从 `core.params` 导入（全仓唯一来源），这里不再另立数字 ——
# 历史上这里写的是 6，而设置页/工具签名/自动规划基准都是 16。


DEFAULT_N_POSES = 1


def receptor_label(spec: Dict[str, Any]) -> str:
    """受体的展示名：`名称(PDB号)`，没有 PDB 号时只显示名称。"""
    pdb = str(spec.get("pdb") or "").strip()
    name = str(spec.get("name") or spec.get("key") or "receptor")
    return f"{name}({pdb})" if pdb and pdb != name else name


def vina_cpu() -> int:
    """单个对接进程使用的线程数上限（默认 1：进程级并行时不超订）。"""
    return max(1, env_int("VINA_CPU", 1))


#: 每个 worker 的线程数上限：Vina 单分子的线程扩展在 ~8 线程饱和（这是**引擎的性质**，
#: 与机器无关；实测 16/32 线程无额外收益）。可用 DOCKING_THREADS_PER_WORKER 覆盖。
_DEFAULT_THREADS_PER_WORKER = 8


def _cpu_quota() -> Optional[float]:
    """容器/服务的 CPU 配额（cgroup v2 的 `cpu.max`，或 v1 的 quota/period）。

    为什么必须看它：`os.cpu_count()` 报的是**宿主机**核数，容器里被 `--cpus=4` 限流时
    仍然会返回 32 —— 按它规划并发会严重超订，把容器拖垮。
    """
    try:
        parts = Path("/sys/fs/cgroup/cpu.max").read_text(encoding="utf-8").split()
        if parts and parts[0] != "max":
            quota, period = float(parts[0]), float(parts[1])
            if quota > 0 and period > 0:
                return max(1.0, quota / period)
    except (OSError, ValueError, IndexError):  # 允许静默：拿不到配额就按核数走
        pass
    try:
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text(encoding="utf-8").strip())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text(encoding="utf-8").strip())
        if quota > 0 and period > 0:
            return max(1.0, quota / period)
    except (OSError, ValueError):  # 允许静默：同上
        pass
    return None


def _physical_cores(logical: int) -> int:
    """物理核数（用于判断是否开了 SMT）。取不到就保守返回逻辑核数。

    读 `/proc/cpuinfo` 的 (physical id, core id) 去重 —— 容器里可能只看到部分 CPU，
    因此最后与「可用逻辑核」取小，绝不高估。
    """
    pairs = set()
    phys_id = core_id = None
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    if phys_id is not None and core_id is not None:
                        pairs.add((phys_id, core_id))
                    phys_id = core_id = None
                elif line.startswith("physical id"):
                    phys_id = line.split(":", 1)[1].strip()
                elif line.startswith("core id"):
                    core_id = line.split(":", 1)[1].strip()
        if phys_id is not None and core_id is not None:
            pairs.add((phys_id, core_id))
    except OSError:  # 允许静默：非 Linux / 无 cpuinfo 时退回逻辑核
        return max(1, logical)
    if pairs:
        return max(1, min(len(pairs), logical))
    return max(1, logical)


def machine_profile() -> Dict[str, Any]:
    """探测「这台部署机器实际能给多少算力」——并发规划的唯一输入。

    优先级：**cgroup 配额 > CPU 亲和性 > cpu_count**（容器里前面两个才是真的），
    再留 1 个逻辑核给服务主进程/事件循环。返回里带 `source` 说明结论从哪来，便于排查。
    """
    cpu_count = max(1, os.cpu_count() or 1)
    affinity: Optional[int] = None
    try:
        affinity = len(os.sched_getaffinity(0))     # Linux：尊重 taskset/cpuset
    except (AttributeError, OSError):               # 允许静默：非 Linux 或无该系统调用
        affinity = None
    host_view = affinity or cpu_count
    quota = _cpu_quota()
    if quota is not None and quota <= 0:            # 异常/损坏的配额文件：按「没有配额」处理
        quota = None
    logical = max(1, int(min(host_view, quota)) if quota else host_view)
    if quota is not None and quota < host_view:
        source = "cgroup 配额"
    elif affinity is not None and affinity < cpu_count:
        source = "CPU 亲和性"
    else:
        source = "cpu_count"
    return {"logical": logical, "physical": _physical_cores(logical),
            "budget": max(1, logical - 1),          # 留一核给服务，避免打满
            "cpu_count": cpu_count, "affinity": affinity,
            "quota": round(quota, 2) if quota else None, "source": source}


def _usable_cpus() -> int:
    """可用 CPU（逻辑核，已扣掉留给服务的那一核）。"""
    return machine_profile()["budget"]


def _memory_worker_cap() -> int:
    """按可用内存给进程数设上限：每个 worker 一份 Vina 网格图 + RDKit，约 400 MB。"""
    per_mb = max(64, env_int("DOCKING_WORKER_MEM_MB", 400))
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail_mb = int(line.split()[1]) // 1024
                    return max(1, int(avail_mb * 0.7) // per_mb)  # 留 30% 余量
    except (OSError, ValueError, IndexError):
        return 64
    return 64


def plan_concurrency(total: int) -> Dict[str, int]:
    """规划「进程数 × 每进程线程数」——**由部署机器自动推导**，不依赖任何一台机器的硬编码。

    ## 规则（两条，都是引擎/硬件的性质，不是某台机器的实测值）

    1. **线程优先**：Vina 单分子的搜索能靠线程并行，而多进程要各自重复建网格图、导入模块，
       还会争抢内存带宽。所以先把线程开到饱和点，再用进程数把剩下的核填满：
       `threads = min(8, budget // 2)`、`workers = ceil(budget / threads)`。
       只留一个进程会让大库退化成串行，所以 `budget//2` 保证小机器上至少有 2 个进程。
    2. **~8 线程是 Vina 的饱和点**（引擎性质，与机器无关；实测 16/32 线程无额外收益）。

    `budget` 来自 `machine_profile()`：**cgroup 配额 > CPU 亲和性 > cpu_count**，再留一核给服务。
    因此容器（`--cpus=4`）、cpuset、大核机器、笔记本都会各自得到合适的配置，无需人工调参。

    ## 本机（16 物理核 / 32 逻辑核）实测验证规则（不是规则的输入）

    | 分子数 | 旧（进程优先） | 本规则给出 | 结果 |
    | --- | --- | --- | --- |
    | 6 | 1 × 8 = 7.9s | 4 × 8 | **4.4s** |
    | 8 | 8 × 3 = 6.5s | 4 × 8 | **4.8s** |
    | 16 | 16 × 1 = 39.4s | 4 × 8 | **13.5s** |
    | 64 | 24 × 1 = 70.4s | 4 × 8 | **38.5s**（CPU 1003 → 716 CPU·s） |

    `DOCKING_THREADS_PER_WORKER`（默认 8，旧名 `DOCKING_SERIAL_THREADS` 兼容）可覆盖线程数；
    `DOCKING_WORKERS` 显式给定时按**精确值**使用（专家模式：宁可多进程也不要多线程，
    例如库里有极个别超大柔性分子、需要缩小长尾阻塞面时）。
    """
    profile = machine_profile()
    budget = profile["budget"]
    configured = env_int("DOCKING_THREADS_PER_WORKER",
                         env_int("DOCKING_SERIAL_THREADS", _DEFAULT_THREADS_PER_WORKER))
    t_max = max(1, min(_DEFAULT_THREADS_PER_WORKER, configured, budget))

    explicit = env("DOCKING_WORKERS")
    if explicit is not None and explicit.strip():
        workers = max(1, min(env_int("DOCKING_WORKERS", 1), total or 1, _memory_worker_cap()))
        threads = max(1, min(t_max, budget // workers or 1))
        return {"workers": workers, "threads": threads, **profile}

    if total <= 1:
        return {"workers": 1, "threads": t_max, **profile}
    threads = min(t_max, max(1, budget // 2))        # 保证至少 2 个进程
    workers = max(1, -(-budget // threads))          # ceil(budget / threads)
    workers = min(workers, total, _memory_worker_cap())
    return {"workers": workers, "threads": threads, **profile}


def _ligand_cost(smiles: str) -> int:
    """分子对接成本的粗估（可旋转键 + 重原子）：用于"大分子先跑"的调度，减少拖尾。"""
    try:
        from rdkit import Chem
        from rdkit.Chem import Descriptors, Lipinski

        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return 0
        return int(Lipinski.NumRotatableBonds(mol) * 4 + Descriptors.HeavyAtomCount(mol))
    except Exception:  # noqa: BLE001
        return 0


def pose_save_max() -> int:
    """位姿保存上限：超过则只保存亲和力最优的前 N 个（避免单目录写入过多文件）。"""
    return max(0, env_int("POSE_SAVE_MAX", 5000))


# 配体 3D 跨度的唯一实现在 `core/pockets.py::ligand_span`（此处保留旧名别名，
# 避免对接内部与外部调用点重复实现同一段 PDBQT 坐标解析）。


class DockingSession:
    """一个「受体 + 盒子」的对接会话：网格图只计算一次，同一批配体复用。

    **可复现性（实测，非假设）**：Vina 在每次 `dock()` 调用时按构造时给定的种子重新开始采样，
    因此同一分子的分数**与批次组成、提交顺序、进程数/分片方式无关** —— 这一点有永久回归测试
    （`tests/test_agent_context.py::test_scores_are_independent_of_batch_and_workers`）：
    8 分子并行批（8 进程）里某分子的分数 == 它单独对接（1 进程）的分数，整批重跑也逐位一致。
    结果行里据此固定报告 `seed_policy="session"` 与 `seed`，便于他人复算。
    """

    def __init__(self, spec: Dict[str, Any], engine: str = "vina", seed: int = 42,
                 ga_num_evals: int = 25000, threads: Optional[int] = None,
                 seed_policy: Optional[str] = None, protonation: Optional[str] = None,
                 protonation_ph: Any = None):
        self.spec = spec
        # 质子化策略/目标 pH：显式参数 > spec（由 dock_library 统一下发，保证同一运行同口径）
        self.protonation = protonation or spec.get("protonation") or None
        self.protonation_ph = protonation_ph if protonation_ph is not None \
            else spec.get("protonation_ph")
        self.engine = (engine or "auto").lower()
        self.seed = seed
        self.ga_num_evals = ga_num_evals
        self.threads = threads
        # 保留参数只为兼容调用方；目前只有 "session"（Vina 每次 dock 都会按该种子重新开始，
        # 所以不需要也没有 per_molecule 的收益 —— 实测见类文档）。
        self.seed_policy = "session"
        self.mode = "autodock"
        self._vina = None
        self._external: Dict[str, Any] = {}
        self._external_fld = ""
        self._external_map_dir = ""
        if self.engine in ("external", "gpu"):
            self._init_external()
        elif self.engine in ("auto", "vina"):
            try:

                v = self._new_vina(seed)
                v.set_receptor(spec["pdbqt"])
                v.compute_vina_maps(center=list(spec["center"]), box_size=list(spec["size"]))
                self._vina = v
                self.mode = "vina"
            except Exception as e:  # noqa: BLE001
                if self.engine == "vina":
                    raise
                logger.warning("Vina 不可用，回退 AutoDock4：%s", e)

    def _init_external(self) -> None:
        """外部引擎（GPU/CLI）：登记必须**就绪**，否则直接报错 —— 不静默改用内置 CPU。

        格点图（AutoDock-GPU 需要）在首次 `_dock_external()` 时按本次盒子生成一次并缓存；
        执行适配在 `core/external_run.py`，结果行与内置引擎同口径（能量项 + 位姿 + engine 标注）。
        """
        from docking_agent.core.external_tools import require_engine

        report = require_engine()
        if not report:
            raise RuntimeError(
                "engine=external 需要先在设置页「外部工具」登记可对外执行的引擎二进制"
                "（EXTERNAL_DOCKING_BIN，例如 AutoDock-GPU）；未登记时不静默改用内置引擎。")
        self._external = dict(report)
        self.mode = f"external:{report.get('flavor') or 'unknown'}"
        logger.info("外部引擎就绪：%s（%s）", report.get("flavor_label"), report.get("version_line"))

    def _external_maps(self, ligand_types: Sequence[str] = ()) -> str:
        """按当前受体 + 盒子生成（或复用）格点图，返回 `.fld` 路径。

        配体类型取 `external_run.STANDARD_LIGAND_TYPES`（本机 autogrid4 实测可用的全集），
        因为格点图在一个会话内共享、要在下一个配体到来之前就建好；配体若带全集之外的原子类型，
        这里**直接报错并说明原因** —— autogrid4 的参数库确实没有这些类型，不能假装能算。
        """
        from docking_agent.core.external_run import STANDARD_LIGAND_TYPES, build_grid_maps
        from docking_agent.core.external_tools import ExternalEngineError

        unsupported = sorted({str(t) for t in ligand_types if t} - set(STANDARD_LIGAND_TYPES))
        if unsupported:
            raise ExternalEngineError(
                f"配体含 AutoDock 参数库不支持的原子类型：{'、'.join(unsupported)}；"
                "AutoDock-GPU 无法为这些原子生成格点图。请改用内置 Vina 引擎"
                "（engine=vina/auto）或先处理该配体，不要指望它被静默跳过。")
        if self._external_fld and os.path.exists(self._external_fld):
            return self._external_fld
        import tempfile

        self._external_map_dir = tempfile.mkdtemp(prefix="external_maps_")
        self._external_fld = build_grid_maps(self.spec["pdbqt"], self.spec["center"],
                                             self.spec["size"], self._external_map_dir,
                                             ligand_types=STANDARD_LIGAND_TYPES)
        return self._external_fld

    def _dock_external(self, prep: Dict[str, Any], exhaustiveness: int, n_poses: int,
                       pose_base: Optional[str]) -> Dict[str, Any]:
        """调用外部引擎对接一个配体（`prep` 为 `describe_ligand` 的准备结果）。"""
        import tempfile

        from docking_agent.core.external_run import dock_ligand_external

        flavor = str(self._external.get("flavor") or "")
        binary = str(self._external.get("path") or self._external.get("bin") or "")
        pdbqt = smiles_to_pdbqt(str(prep.get("smiles") or ""), seed=self.seed)
        workdir = tempfile.mkdtemp(prefix="external_dock_")
        try:
            lig = os.path.join(workdir, "lig.pdbqt")
            with open(lig, "w", encoding="utf-8") as handle:
                handle.write(pdbqt)
            row = dock_ligand_external(
                flavor=flavor, binary=binary, ligand_pdbqt=lig, workdir=workdir,
                receptor_pdbqt=self.spec["pdbqt"],
                fld=(self._external_maps(_pdbqt_atom_types(lig))
                     if flavor == "autodock-gpu" else ""),
                center=self.spec["center"], size=self.spec["size"],
                exhaustiveness=exhaustiveness, n_poses=n_poses, seed=self.seed,
                pose_base=pose_base, threads=self.threads,
                device=self._external.get("device"),
                engine_version=str(self._external.get("version_line") or ""))
        finally:
            import shutil as _sh

            _sh.rmtree(workdir, ignore_errors=True)
        span = _ligand_span(lig)
        row.update({
            "receptor": receptor_label(self.spec),
            "protein": self.spec.get("protein", ""),
            "box_center": list(self.spec["center"]),
            "box_size": list(self.spec["size"]),
            "box_group": "main",
            "ligand_span": [round(float(x), 2) for x in span] if span else [],
            "engine_flavor": flavor,
        })
        return row

    def _new_vina(self, seed: int):
        from vina import Vina  # type: ignore

        return Vina(sf_name="vina", verbosity=0, cpu=max(1, int(self.threads or vina_cpu())),
                    seed=int(seed))

    def dock(self, smiles: str, exhaustiveness: int = DEFAULT_EXHAUSTIVENESS,
             n_poses: int = DEFAULT_N_POSES, pose_base: Optional[str] = None) -> Dict[str, Any]:
        # ---- 配体化学体检（盐/反离子、金属、电荷、手性…）----
        # 工具只如实报告事实与所采取的保守处理，是否接受由 Agent 决定。
        from docking_agent.core.ligands import describe_ligand  # noqa: PLC0415

        prep = describe_ligand(smiles, self.protonation, self.protonation_ph)
        facts = dict(prep.get("facts") or {})
        warns = list(prep.get("warnings") or [])
        if not prep.get("ok"):
            return {"smiles": smiles, "error": "配体化学解析失败：" + ("；".join(warns) or "未知原因"),
                    "ligand_warnings": warns, "ligand_facts": facts}

        dock_smiles = prep.get("smiles") or smiles
        if self.mode.startswith("external:"):
            r = self._dock_external(prep, exhaustiveness, n_poses, pose_base)
        elif self.mode == "vina":
            r = self._dock_vina(dock_smiles, exhaustiveness, n_poses, pose_base)
        else:
            r = self._dock_autodock(dock_smiles, pose_base)
        # 保持 `smiles` 与输入一致：上层的合并/排序/去重都以它为主键
        r["smiles"] = smiles
        if dock_smiles != smiles:
            r["dock_smiles"] = dock_smiles
            r["removed_fragments"] = prep.get("removed_fragments") or []
        if facts:
            r["ligand_facts"] = facts
        if warns:
            r["ligand_warnings"] = warns
        return r

    def _dock_vina(self, smiles: str, exhaustiveness: int, n_poses: int,
                   pose_base: Optional[str]) -> Dict[str, Any]:
        used_seed = self.seed
        v = self._vina
        pdbqt = smiles_to_pdbqt(smiles, seed=used_seed)
        v.set_ligand_from_string(pdbqt)
        v.dock(exhaustiveness=int(exhaustiveness), n_poses=int(n_poses))
        best = v.energies(n_poses=int(n_poses))[0]

        pose_file, pose_count = "", 0
        if pose_base:
            pose_file = f"{pose_base}.pdbqt"
            try:
                os.makedirs(os.path.dirname(pose_file) or ".", exist_ok=True)
                # n_poses>1 时必须把**全部**位姿写出来（原来只用 write_pose 写了最佳一个，
                # 与「输出位姿数 n_poses」的参数语义不一致）
                if int(n_poses) > 1:
                    v.write_poses(pose_file, n_poses=int(n_poses), energy_range=3.0, overwrite=True)
                    pose_count = int(n_poses)
                else:
                    v.write_pose(pose_file, overwrite=True)
                    pose_count = 1
            except Exception as e:  # noqa: BLE001
                logger.warning("位姿写出失败 %s: %s", pose_file, e)
                pose_file, pose_count = "", 0

        # 盒子适配检查（标准做法：搜索盒每维应 ≥ 配体跨度 + 2×5 Å）：
        # 这里**如实检测并上报**；主组内超限的分子会由 dock_library 划进 large 组用更大的盒子重跑
        # （判据余量与 BOX_GROUP_MARGIN 保持一致，避免「告警了但不分组」的口径漂移）。
        span = _ligand_span(pdbqt)
        box = [float(x) for x in (self.spec.get("size") or [])]
        margin = box_group_margin()
        fit_note = ""
        if span and len(box) == 3:
            overflow = [i for i in range(3) if span[i] + margin > box[i]]
            if overflow:
                fit_note = (f"配体跨度 {[round(x, 1) for x in span]} Å 接近/超出盒子 "
                            f"{[round(x, 1) for x in box]} Å（余量 <{margin:.0f} Å）")

        if float(best[0]) == 0.0 and float(best[1]) == 0.0:
            # 兜底（正常路径已被 dock_library 的盒子护栏挡住）：全 0 能量不是分数，
            # 绝不能让 0.0 作为「亲和力」流进排序/CSV/报告。
            logger.error("Vina 返回全 0 能量（盒子内可能没有受体原子）：%s", smiles)
            return {"smiles": smiles, "engine": "vina",
                    "error": "Vina 返回全 0 能量（盒子内没有受体原子或网格图构建失败）："
                             "该行不是有效分数，已按失败处理",
                    "box_center": list(self.spec.get("center") or []),
                    "box_size": list(self.spec.get("size") or []),
                    "receptor": receptor_label(self.spec),
                    "exhaustiveness": exhaustiveness}
        result = {
            "smiles": smiles,
            "affinity_kcal_mol": round(float(best[0]), 2),   # 结合亲和力(Vina score)
            "intermolecular_kcal_mol": round(float(best[1]), 2),
            "intramolecular_kcal_mol": round(float(best[2]), 2),
            "torsion_kcal_mol": round(float(best[3]), 2),
            "receptor": receptor_label(self.spec),
            "protein": self.spec.get("protein", ""),
            "box_center": list(self.spec["center"]),
            "box_size": list(self.spec["size"]),
            "box_group": "main",
            "ligand_span": [round(float(x), 2) for x in span] if span else [],
            "exhaustiveness": exhaustiveness,
            "engine": "vina",
            "pose_file": pose_file,
            "pose_count": pose_count,
            "seed": used_seed,
            "seed_policy": self.seed_policy,
        }
        if fit_note:
            result["box_fit_warning"] = fit_note
        return result

    def _dock_autodock(self, smiles: str, pose_base: Optional[str]) -> Dict[str, Any]:
        r = run_docking_autodock_internal(self.spec, smiles, ga_num_evals=self.ga_num_evals,
                                          seed=self.seed, pose_base=pose_base)
        r["engine"] = "autodock"
        return r


def run_docking_internal(spec: Dict[str, Any], smiles: str,
                         exhaustiveness: int = DEFAULT_EXHAUSTIVENESS,
                         n_poses: int = DEFAULT_N_POSES,
                         seed: int = 42,
                         pose_base: Optional[str] = None) -> Dict[str, Any]:
    """单分子对接（便捷入口：内部建立一次性会话；批量场景请复用 `DockingSession`）。

    energies 列: [total, inter(疏水+氢键+静电), intra, torsions, intra_best]
    pose_base: 若提供，则把最佳位姿写出为 `{pose_base}.pdbqt`（中间数据可追溯）。
    """
    session = DockingSession(spec, engine="vina", seed=seed)
    result = session.dock(smiles, exhaustiveness, n_poses, pose_base)
    result["engine"] = session.mode
    return result


# --------------------------------------------------------------------------- #
# 备用对接引擎：AutoDock 4 (CPU 模式，autogrid4 + autodock4)
# 当 Vina 不可用或显式指定时，使用经典 AutoDock 遗传算法做真实 CPU 对接。
# --------------------------------------------------------------------------- #


def _pdbqt_atom_types(pdbqt_path: str) -> List[str]:
    """提取 PDBQT 中出现的原子类型（第 78 列）。"""
    types: set = set()
    for line in open(pdbqt_path, encoding="utf-8", errors="ignore"):
        if line.startswith(("ATOM", "HETATM")):
            t = line[77:79].strip()
            if t:
                types.add(t)
    s = sorted(types)
    # AutoDock4 受体常用类型兜底（如同型氢标记为 HD）
    return s


def _pdbqt_tors(pdbqt_path: str) -> int:
    for line in open(pdbqt_path, encoding="utf-8", errors="ignore"):
        if line.startswith("TORSDOF"):
            try:
                return int(line.split()[-1])
            except ValueError:
                return 0
    return 0


def _autodock_bin(name: str) -> Optional[str]:
    """定位 AD4 家族二进制：`<NAME>_BIN`（如 `AUTODOCK4_BIN`）→ PATH。找不到返回 None。"""
    import shutil

    override = str(env(f"{name.upper()}_BIN", "") or "").strip()
    if override:
        target = Path(override).expanduser()
        if target.is_file() and os.access(target, os.X_OK):
            return str(target)
        logger.warning("%s_BIN 指向的文件不可执行，回退 PATH 查找：%s", name.upper(), override)
    return shutil.which(name) or None


def run_docking_autodock_internal(spec: Dict[str, Any], smiles: str,
                                  ga_num_evals: int = 25000,
                                  ga_run: int = 1, seed: int = 42,
                                  pose_base: Optional[str] = None) -> Dict[str, Any]:
    """经典 AutoDock 4（autogrid4+autodock4）CPU 模式单分子对接。

    返回真实能量项：
      affinity_kcal_mol = Estimated Free Energy of Binding (kcal/mol)
      intermolecular_kcal_mol / intramolecular_kcal_mol / torsion_kcal_mol
    """
    import subprocess, tempfile, shutil
    autodock4 = _autodock_bin("autodock4")
    autogrid4 = _autodock_bin("autogrid4")
    if not autodock4 or not autogrid4:
        raise RuntimeError("AutoDock CPU 引擎不可用：未找到 autodock4/autogrid4。"
                           "请安装经典 AutoDock4：apt-get install autodock autogrid。")
    if not os.path.exists(spec["pdbqt"]):
        raise FileNotFoundError(f"AutoDock 受体文件不存在: {spec['pdbqt']}")
    lig_pdbqt = smiles_to_pdbqt(smiles, seed=seed)

    workdir = tempfile.mkdtemp(prefix="autodock_engine_")
    try:
        rec = os.path.join(workdir, "rec.pdbqt")
        lig = os.path.join(workdir, "lig.pdbqt")
        shutil.copy(spec["pdbqt"], rec)
        with open(lig, "w", encoding="utf-8") as f:
            f.write(lig_pdbqt)
        lt = _pdbqt_atom_types(lig)
        rt = _pdbqt_atom_types(rec)
        tors = _pdbqt_tors(lig)
        center = [float(c) for c in spec["center"]]
        box = [float(sz) for sz in spec["size"]]
        spacing = 0.375
        npts = [int(sz // spacing) for sz in box]
        npts = [n if n % 2 else n + 1 for n in npts]

        # ---- autogrid4: 生成格点能量图 ----
        gpf = ["autogrid_parameter_version 4.2.6",
               f"npts {npts[0]} {npts[1]} {npts[2]}",
               "gridfld rec.maps.fld", f"spacing {spacing}",
               "receptor_types " + " ".join(rt),
               "ligand_types " + " ".join(lt),
               "receptor rec.pdbqt",
               f"gridcenter {center[0]} {center[1]} {center[2]}", "smooth 0.5"]
        for t in lt:
            gpf.append(f"map rec.{t}.map")
        gpf += ["elecmap rec.e.map", "dsolvmap rec.d.map", "dielectric -0.1465"]
        with open(os.path.join(workdir, "rec.gpf"), "w", encoding="utf-8") as f:
            f.write("\n".join(gpf) + "\n")
        subprocess.run([autogrid4, "-p", "rec.gpf", "-l", "rec.glg"],
                       check=True, capture_output=True, timeout=1200, cwd=workdir)
        if not os.path.exists(os.path.join(workdir, "rec.maps.fld")):
            raise RuntimeError("AutoDock autogrid4 未能生成格点图(rec.maps.fld)。")

        # ---- autodock4: 遗传算法对接 ----
        about = _pdbqt_centroid(lig)
        dpf = ["autodock_parameter_version 4.2.6", "outlev 1", "intelec",
               f"seed {int(seed)}",
               "ligand_types " + " ".join(lt), "fld rec.maps.fld"]
        for t in lt:
            dpf.append(f"map rec.{t}.map")
        dpf += ["elecmap rec.e.map", "desolvmap rec.d.map", "move lig.pdbqt",
                f"about {about[0]:.3f} {about[1]:.3f} {about[2]:.3f}",
                f"tran0 {center[0]:.3f} {center[1]:.3f} {center[2]:.3f}",
                "quaternion0 0 0 0 1", "dihe0 " + " ".join(["0"] * max(tors, 1)),
                f"torsdof {max(tors, 1)}", "rmstol 2.0",
                "ga_pop_size 50", f"ga_num_evals {int(ga_num_evals)}",
                "ga_num_generations 2700", "ga_elitism 1",
                "ga_mutation_rate 0.02", "ga_crossover_rate 0.8", "set_ga",
                "sw_max_its 30", "sw_max_succ 2", "sw_max_fail 2",
                "sw_rho 1.0", "sw_lb_rho 0.01", "ls_search_freq 0.06",
                "set_psw1", f"ga_run {int(ga_run)}", "analysis"]
        with open(os.path.join(workdir, "dock.dpf"), "w", encoding="utf-8") as f:
            f.write("\n".join(dpf) + "\n")
        subprocess.run([autodock4, "-p", "dock.dpf", "-l", "out.dlg"],
                       check=True, capture_output=True, timeout=1800, cwd=workdir)
        if not os.path.exists(os.path.join(workdir, "out.dlg")):
            raise RuntimeError("AutoDock autodock4 未生成对接日志(out.dlg)。")

        # ---- 解析 DLG 能量 ----
        est = inter = internal = torsional = None
        for line in open(os.path.join(workdir, "out.dlg"), encoding="utf-8", errors="ignore"):
            if "Estimated Free Energy of Binding" in line and est is None:
                est = float(line.split("=")[1].split()[0])
            elif "Final Intermolecular Energy" in line and inter is None:
                inter = float(line.split("=")[1].split()[0])
            elif "Final Total Internal Energy" in line and internal is None:
                internal = float(line.split("=")[1].split()[0])
            elif "Torsional Free Energy" in line and torsional is None:
                torsional = float(line.split("=")[1].split()[0])
        if est is None:
            est = float('nan')
        # AutoDock4 的位姿/能量明细在 DLG 日志中，直接留档为中间数据
        pose_file = ""
        if pose_base:
            dlgsrc = os.path.join(workdir, "out.dlg")
            pose_file = f"{pose_base}.dlg"
            os.makedirs(os.path.dirname(pose_file) or ".", exist_ok=True)
            shutil.copyfile(dlgsrc, pose_file)
        span = _ligand_span(lig_pdbqt)
        if float(est) == 0.0 and not inter:
            # 与 Vina 路径同一口径：全 0 能量不是分数（盒子空/构图失败），按失败如实上报
            logger.error("AutoDock 返回全 0 能量（盒子内可能没有受体原子）：%s", smiles)
            return {"smiles": smiles, "engine": "autodock",
                    "error": "AutoDock 返回全 0 能量（盒子内没有受体原子或网格图构建失败）："
                             "该行不是有效分数，已按失败处理",
                    "box_center": list(spec.get("center") or []),
                    "box_size": list(spec.get("size") or []),
                    "receptor": receptor_label(spec)}
        return {
            "smiles": smiles,
            "affinity_kcal_mol": round(float(est), 2),          # Estimated Free Energy of Binding
            "intermolecular_kcal_mol": round(float(inter) if inter is not None else float('nan'), 2),
            "intramolecular_kcal_mol": round(float(internal) if internal is not None else float('nan'), 2),
            "torsion_kcal_mol": round(float(torsional) if torsional is not None else float('nan'), 2),
            "receptor": receptor_label(spec),
            "protein": spec.get("protein", ""),
            "box_center": list(spec["center"]),
            "box_size": list(spec["size"]),
            "box_group": "main",
            "ligand_span": [round(float(x), 2) for x in span] if span else [],
            "ga_num_evals": int(ga_num_evals),
            "pose_file": pose_file,
        }
    finally:
        import shutil as _sh
        _sh.rmtree(workdir, ignore_errors=True)


def _run_docking_with_engine(spec: Dict[str, Any], smiles: str, engine: str,
                             exhaustiveness: int, n_poses: int, seed: int,
                             ga_num_evals: int, pose_base: Optional[str] = None) -> Dict[str, Any]:
    """按 engine 选择对接实现：vina / autodock / auto(优先 Vina，不可用时回退 AutoDock4)。"""
    session = DockingSession(spec, engine=engine, seed=seed, ga_num_evals=ga_num_evals)
    try:
        r = session.dock(smiles, exhaustiveness, n_poses, pose_base)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"所有对接引擎均失败(vina/autodock): {e}") from e
    r["engine"] = session.mode
    return r


# --------------------------------------------------------------------------- #
# 批量对接：多进程并行 + 网格图复用
# --------------------------------------------------------------------------- #
_WORKER_SESSION: Optional[DockingSession] = None
_WORKER_CFG: Dict[str, Any] = {}



#: 分子输入记录里需要**带进结果行**的身份字段。
#: 用户常在 SDF/CSV 里给分子编号（ID）、并希望报告/CSV 带上它；只解析进 molecules.json
#: 是不够的 —— 对接、性质、排序、报告、CSV 每一环都要能拿到（真实缺陷：整条链路把 ID 丢了）。
IDENTITY_FIELDS: tuple = ("id", "source_file", "source_index")


def carry_identity(row: Any, source: Any) -> Any:
    """把输入分子的身份字段补进结果行（不覆盖已有值，缺什么补什么）。"""
    if not isinstance(row, dict) or not isinstance(source, dict):
        return row
    for key in IDENTITY_FIELDS:
        value = source.get(key)
        if value not in (None, "") and row.get(key) in (None, ""):
            row[key] = value
    return row

def _worker_init(spec: Dict[str, Any], engine: str, seed: int, ga_num_evals: int,
                 exhaustiveness: int, n_poses: int, pose_dir: Optional[str],
                 save_poses: bool, threads: int = 1) -> None:
    """每个 worker 进程只执行一次：建立会话（含计算一次网格图）。"""
    global _WORKER_SESSION, _WORKER_CFG
    _WORKER_SESSION = DockingSession(spec, engine=engine, seed=seed, ga_num_evals=ga_num_evals,
                                     threads=threads)
    _WORKER_CFG = {"exhaustiveness": int(exhaustiveness), "n_poses": int(n_poses),
                   "pose_dir": pose_dir, "save_poses": bool(save_poses)}


def _worker_dock(item: Tuple[str, str]) -> Dict[str, Any]:
    """子进程任务：对接单个配体（复用本进程已算好的受体网格图）。"""
    name, smiles = item
    cfg = _WORKER_CFG
    session = _WORKER_SESSION
    if session is None:  # 理论上不会发生（initializer 保证）
        return {"name": name, "smiles": smiles, "error": "docking 失败: worker 未初始化"}
    pose_base = None
    if cfg.get("save_poses") and cfg.get("pose_dir"):
        pose_base = os.path.join(cfg["pose_dir"], f"pose_{slug(name or smiles)}")
    try:
        r = session.dock(smiles, cfg["exhaustiveness"], cfg["n_poses"], pose_base)
        r["name"] = name
        return r
    except Exception as e:  # noqa: BLE001
        logger.error("对接失败 %s: %s", name, _err_text(e))
        return {"name": name, "smiles": smiles, "error": f"docking 失败: {_err_text(e)}"}


def _start_cancel_watcher(executor: Any, cancel_event: Optional[Any]) -> Optional[Any]:
    """起一个守护线程盯取消标志，一旦置位**立刻**终止进程池并返回。

    为什么不能在 `as_completed` 循环里只查标志：那个循环只在**有 future 完成**时才转一圈。
    如果一整批都是超大柔性分子（例如 70 个可旋转键的长链），第一批要跑十几分钟，
    用户点「停止」后进程池仍然满负荷运转 —— 真实缺陷：chat 模式取消 70 s 后 load 仍是 27。

    所有对接都在 worker 进程里跑（见 `dock_batch`），因此这个 watcher 对**任意分子数**都有效，
    包括单分子。
    """
    if cancel_event is None:
        return None
    done = threading.Event()

    def _watch() -> None:
        while not done.wait(0.5):
            if cancel_event.is_set():
                logger.info("收到取消请求：立即终止对接进程池")
                _terminate_pool(executor)
                return

    thread = threading.Thread(target=_watch, name="dock-cancel-watch", daemon=True)
    thread.start()
    return done


def _err_text(exc: BaseException) -> str:
    """异常的可读文本：消息为空时退回异常类名，绝不留出 `对接失败 XXX:` 这种空原因。"""
    text = str(exc).strip()
    return text or type(exc).__name__


def _terminate_pool(executor: Any) -> None:
    """强制终止进程池中仍在运行的 worker（取消时必须让 Vina 立刻停下）。

    **顺序很关键**：先 `shutdown(cancel_futures=True)` 把队列里还没跑的分子取消掉，
    再 `terminate()` 正在跑的 worker。反过来做的话，执行器的管理线程会发现 worker 死了、
    而队列里还有一百多个待跑任务，于是**重新拉起 worker 继续啃**（真实缺陷：取消后又
    多算了 43 个分子、load 反涨到 40）。
    """
    # 必须在 shutdown 之前抓住进程句柄：shutdown() 会把 `_processes` 清空，
    # 之后再取就拿不到正在跑 Vina 的 worker 了。
    processes = list((getattr(executor, "_processes", None) or {}).values())
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001
        logger.debug("关闭进程池（取消未开始的任务）失败", exc_info=True)
    for proc in processes:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            logger.debug("终止 worker 进程失败", exc_info=True)
    # Vina 在 C++ 里跑，SIGTERM 未必立刻返回（大柔性分子的一次局部优化可能还要几十秒），
    # 因此给一个很短的收尾窗口后**强杀**：用户点了停止就不该再看到分子数往上走。
    deadline = time.monotonic() + 1.0
    for proc in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.join(timeout=remaining)
        except Exception:  # noqa: BLE001
            logger.debug("等待 worker 退出失败", exc_info=True)
    for proc in processes:
        try:
            if proc.is_alive():
                logger.info("worker %s 未响应 SIGTERM，强制终止", proc.pid)
                proc.kill()
        except Exception:  # noqa: BLE001
            logger.debug("强杀 worker 进程失败", exc_info=True)


def _chemistry_notes(rows: List[Dict[str, Any]], limit: int = 5) -> List[str]:
    """把「配体化学」相关的告警与盒子适配告警汇总成给 Agent 看的提示（去重、限量）。"""
    notes: List[str] = []
    seen: set = set()
    for r in rows:
        for w in (r.get("ligand_warnings") or []):
            key = str(w)
            if key not in seen:
                seen.add(key)
                notes.append(f"配体 {r.get('name') or r.get('smiles')}：{key}")
        if r.get("box_fit_warning"):
            key = f"box:{r['box_fit_warning']}"
            if key not in seen:
                seen.add(key)
                notes.append(f"盒子适配（{r.get('name') or r.get('smiles')}）：{r['box_fit_warning']}")
    if len(notes) > limit:
        notes = notes[:limit] + [f"……另有 {len(notes) - limit} 条同类提示，详见各分子明细字段"]
    return notes


def dock_batch(spec: Dict[str, Any], molecules: List[Dict[str, str]], *,
               engine: str = "auto", exhaustiveness: int = DEFAULT_EXHAUSTIVENESS,
               n_poses: int = DEFAULT_N_POSES, seed: int = 42, ga_num_evals: int = 25000,
               pose_dir: Optional[str] = None, save_poses: bool = True,
               on_result: Optional[Any] = None,
               cancel_event: Optional[Any] = None) -> List[Dict[str, Any]]:
    """对同一受体批量对接（顺序与输入一致）。

    on_result(result) 每完成一个配体回调一次，用于进度与实时推送。
    cancel_event（threading.Event）置位时立即停止：终止进程池并抛出 CancelledRun。
    """
    total = len(molecules)
    if total == 0:
        return []
    plan = plan_concurrency(total)
    n_workers, threads = plan["workers"], plan["threads"]
    logger.info("对接并发规划：%s 个分子 → %s 个进程 × %s 线程（受体 %s；"
                "本机 logical=%s physical=%s quota=%s 来源=%s）",
                total, n_workers, threads, spec.get("name"), plan.get("logical"),
                plan.get("physical"), plan.get("quota"), plan.get("source"))
    out: List[Optional[Dict[str, Any]]] = [None] * total

    def _emit(res: Dict[str, Any]) -> None:
        if on_result:
            try:
                on_result(res)
            except Exception:  # noqa: BLE001
                logger.debug("on_result 回调异常", exc_info=True)

    # 已经请求取消就一个分子都别跑（进程池路径尤其要在建池之前拦住，避免白起 24 个进程）
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledRun("对接已取消（开始前）")

    logger.info("并行对接：%s 个分子 / %s 个进程（受体 %s）", total, n_workers, spec.get("name"))
    executor = ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_worker_init,
        initargs=(spec, engine, seed, ga_num_evals, exhaustiveness, n_poses,
                  pose_dir if save_poses else None, save_poses, threads),
    )
    completed = 0
    watcher = _start_cancel_watcher(executor, cancel_event)
    try:
        # 调度：成本高的（可旋转键多/重原子多）先提交 —— 进程池是动态领任务的，
        # 先跑大分子可以避免"最后只剩一个大分子在跑、其余 worker 空闲"的拖尾。
        order = sorted(range(total), key=lambda i: -_ligand_cost(molecules[i].get("smiles", "")))
        future_index = {}
        for i in order:
            m = molecules[i]
            future_index[executor.submit(
                _worker_dock, (m.get("name") or m.get("smiles", ""), m["smiles"]))] = i
        for future in as_completed(future_index):
            if cancel_event is not None and cancel_event.is_set():
                logger.info("对接已被取消（已完成 %s/%s），终止进程池", completed, total)
                _terminate_pool(executor)
                raise CancelledRun(f"对接已取消（已完成 {completed}/{total}）")
            i = future_index[future]
            try:
                r = future.result()
            except Exception as e:  # noqa: BLE001
                logger.error("对接任务异常: %s", e)
                r = {"name": molecules[i].get("name"), "smiles": molecules[i].get("smiles", ""),
                     "error": f"docking 失败: {e}"}
            carry_identity(r, molecules[i])
            out[i] = r
            completed += 1
            _emit(r)
    finally:
        if watcher is not None:
            watcher.set()
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            logger.debug("关闭进程池异常", exc_info=True)
    results = [r for r in out if r is not None]
    spec.setdefault("_chemistry_notes", []).extend(_chemistry_notes(results))
    return results


def run_docking(smiles: str, receptor: Any = None,
                center: Optional[Sequence[float]] = None,
                box_size: Optional[Sequence[float]] = None,
                exhaustiveness: int = DEFAULT_EXHAUSTIVENESS,
                n_poses: int = DEFAULT_N_POSES,
                seed: int = 42,
                keep_hetatm: Optional[Sequence[str]] = None,
                protonation: str = "", protonation_ph: Any = None) -> Dict[str, Any]:
    """单分子对指定受体（默认凝血酶）的 Vina 对接，返回真实能量项。"""
    from docking_agent.core.protonation import protonation_ph as _ph_value
    from docking_agent.core.protonation import protonation_policy as _policy

    prot_policy = _policy(protonation)
    prot_ph = _ph_value(protonation_ph)
    specs, _notes = resolve_receptor_specs(receptor, keep_hetatm=tuple(keep_hetatm or ()),
                                           protonation=prot_policy, ph=prot_ph)
    spec = specs[0]
    spec["protonation"] = prot_policy
    spec["protonation_ph"] = prot_ph
    if center is not None:
        spec["center"] = list(center)
    if box_size is not None:
        spec["size"] = list(box_size)
    return run_docking_internal(spec, smiles, exhaustiveness, n_poses, seed)




def _box_overflows(span: Optional[Sequence[float]], box: Sequence[float],
                   margin: float) -> bool:
    """该分子是否「跨度 + 余量 > 主盒对应边」（任一维超限即算超限）。"""
    if not span or len(span) < 3 or len(box) < 3:
        return False
    return any(float(span[i]) + float(margin) > float(box[i]) for i in range(3))


def _large_group_box(main_size: Sequence[float], spans: Sequence[Sequence[float]], *,
                     padding: float, max_size: float) -> List[float]:
    """大配体组的盒子：`clamp(组内最大跨度 + padding, 主盒, 大配体组上限)`。

    中心与主组完全相同（中心永远不随分子变）；下界取主盒是为了保证「大配体组的盒子不会比主组小」；
    上界允许超过主盒的 `max_size`（否则分组没有意义），但受 `BOX_LARGE_MAX_SIZE` 硬上限约束。
    """
    largest = max((max(float(x) for x in s) for s in spans if s), default=0.0)
    target = largest + max(0.0, float(padding))
    out: List[float] = []
    for i in range(3):
        lo = float(main_size[i]) if i < len(main_size) else float(max_size)
        hi = max(lo, min(BOX_LARGE_MAX_SIZE, max(target, lo)))
        out.append(round(min(max(target, lo), hi), 2))
    return out


def external_engine_note(external: Dict[str, Any], engine: str) -> str:
    """外部引擎的**如实播报**：本次到底由谁执行，一看就知道。

    登记了 GPU 引擎却用内置引擎跑，是用户最容易误解的一种状态（以为在 GPU 上）；
    反过来，真的用外部引擎执行时也要说清楚设备与批次。
    """
    from docking_agent.core.params import resolve_engine

    effective = resolve_engine(engine)
    where = (f"{external.get('flavor_label', '')}"
             f"（设备 {external.get('device')}，单批 {external.get('batch_size')} 个配体）")
    if effective == "external":
        return f"外部对接引擎：{where} —— 本次对接由它执行。"
    return (f"已登记外部对接引擎 {where}；本次 engine={effective}，仍由内置引擎计算 —— "
            "要用它请把引擎设为 external。")


def _note(note_cb: Optional[Any], message: str) -> None:
    """播报准备阶段说明（对接尚未开始）。回调异常绝不影响对接本身。"""
    if note_cb is None:
        return
    try:
        note_cb(str(message))
    except Exception:  # noqa: BLE001
        logger.debug("note_cb 异常", exc_info=True)


def _format_point(center: Sequence[float]) -> str:
    """盒中心的紧凑展示：`86.6,76.2,92.0`。"""
    return ",".join(f"{float(v):.1f}" for v in (center or []))


def _format_box(size: Sequence[float]) -> str:
    """盒子的紧凑展示：`22.0x22.0x22.0`。"""
    return "x".join(f"{float(v):.1f}" for v in size)


def dock_library(molecules: List[Dict[str, str]], receptor: Any = None,
                 exhaustiveness: int = DEFAULT_EXHAUSTIVENESS,
                 n_poses: int = DEFAULT_N_POSES,
                 seed: int = 42,
                 engine: str = "auto",
                 ga_num_evals: int = 25000,
                 site: Optional[Dict[str, Any]] = None,
                 pocket_engine: Optional[str] = None,
                 keep_hetatm: Optional[Sequence[str]] = None,
                 pose_dir: Optional[str] = None,
                 max_ligands: Optional[int] = None,
                 progress_cb: Optional[Any] = None,
                 note_cb: Optional[Any] = None,
                 cancel_event: Optional[Any] = None,
                 protonation: str = "", protonation_ph: Any = None) -> Dict[str, Any]:
    """小分子库 × 蛋白质库 对接：对每个受体 × 每个配体执行真实对接，按受体分组返回。

    engine: 'auto'(默认，优先 Vina，失败自动回退 AutoDock CPU) / 'vina' / 'autodock'(AutoDock4 CPU) /
            'external'(设置页登记的外部引擎，如 AutoDock-GPU；未登记或未就绪**直接报错**，不静默回退)。
    site:   覆盖受体注册位点，形如 {"center": [x,y,z], "size": [a,b,c]}（用于自定义/微调已知位点）。
    pose_dir: 若提供，每个分子的最佳位姿写入该目录（中间数据留档）。
    max_ligands: 覆盖环境变量 DOCKING_MAX_LIGANDS 的上限（None 表示读环境变量，0 表示不限制）。
    progress_cb: 可选回调 fn(index, total, result)。
    note_cb: 可选回调 fn(message)：播报「对接尚未开始」的准备阶段（定盒 / 库级下限抽样）。
             没有它时，大库首次抽样期间界面长时间收不到新消息，看起来像卡死。

    **盒子一致性（C 方案，第一原则）**：同一运行里主组所有分子共用同一个盒子（每个受体只定一次）；
    库级下限会把服务端算出的盒子抬高到「库内 P95 跨度 + 10 Å」；只有跨度明显超出主盒的分子
    才会被划进 `box_group="large"`，用**同一中心**的更大盒子单独重跑（结果行如实标注）。
    """
    if not molecules:
        return {"status": "no_molecules",
                "message": "未提供待对接的小分子配体，请先提供候选小分子库（SMILES/名称）。"}

    # 外部引擎（用户在设置页提供的 GPU 对接工具）：已配置但不可用时**拒绝启动**，
    # 不静默回退到内置实现 —— 否则用户以为在用 GPU，实际是 CPU 结果（真实缺陷类问题）。
    from docking_agent.core.external_tools import configured_bin, require_engine

    external = require_engine() if configured_bin() else {}
    if external:
        _note(note_cb, external_engine_note(external, engine))

    # 安全阀：限制单次对接分子数，避免误传大库导致长时间占用
    if max_ligands is None:
        try:
            cap = env_int("DOCKING_MAX_LIGANDS", 0)
        except ValueError:
            cap = 0
    else:
        cap = int(max_ligands)
    if cap and len(molecules) > cap:
        logger.warning("本次仅对接前 %s 个分子（共 %s 个）", cap, len(molecules))
        molecules = molecules[:cap]

    # 质子化态策略：**运行级**解析一次，同时用于配体（逐分子）与受体（准备时重分配），
    # 保证「受体与配体同一套化学条件」；逐分子/逐受体结果里都有 provenance。
    from docking_agent.core.protonation import protonation_ph as _ph_value
    from docking_agent.core.protonation import protonation_policy as _policy

    prot_policy = _policy(protonation)
    prot_ph = _ph_value(protonation_ph)

    try:
        specs, notes = resolve_receptor_specs(receptor, keep_hetatm=tuple(keep_hetatm or ()),
                                              protonation=prot_policy, ph=prot_ph)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": f"受体解析失败: {str(e)}"}
    for spec in specs:
        spec["protonation"] = prot_policy
        spec["protonation_ph"] = prot_ph
    if prot_policy == "keep":
        notes.append("质子化态策略=keep（保持输入形式，仅告警）")
    elif prot_policy == "ph":
        notes.append(f"质子化态策略=ph（目标 pH {prot_ph:g}：先中和到中性形式，再按内置 pKa 规则表"
                     "重新分配质子化态；逐分子记录命中规则与净电荷前/后。这是规则近似，不是 pKa 预测）")
    else:
        notes.append("质子化态策略=neutralize"
                     "（仅对带净电荷的分子做中和，逐分子记录来源与去向；中性分子不变）")

    # 受体准备丢弃的杂原子要如实上报（金属酶/辅因子体系的信息不能被默默抹掉）
    for spec in specs:
        dropped = spec.get("dropped_hetatm") or {}
        waters = spec.get("dropped_waters") or 0
        kept = spec.get("kept_hetatm") or {}
        if dropped:
            top = "、".join(f"{k}×{v}" for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])[:6])
            notes.append(f"受体 {spec.get('key')} 丢弃了非水杂原子：{top}"
                         f"（水 {waters} 个）；若这些金属/辅因子对结合重要，"
                         "可用 keep_hetatm 指定残基名保留后重跑")
        elif kept:
            notes.append(f"受体 {spec.get('key')} 按要求保留了杂原子："
                         + "、".join(f"{k}×{v}" for k, v in kept.items()))
        unsupported = spec.get("unsupported_hetatm") or []
        if unsupported:
            notes.append(f"受体 {spec.get('key')}：要求保留的 {'、'.join(unsupported)} "
                         "缺少对接所需的化学模板（meeko 无该残基模板，可能会触发其内部错误），"
                         "**未能进入受体**，其余部分照常对接；若这些组分对结合重要，"
                         "请提供模板 SDF（meeko --add_templates）或直接提供已准备好的 "
                         "PDBQT 受体后重跑。金属离子（ZN/MG/CA/FE/MN 等）通常可直接保留。")

    # 口袋/盒子参数（口袋引擎与 C 方案的四个新参数都从这里读）
    settings = engine_settings()
    if pocket_engine and str(pocket_engine).strip():
        settings["engine"] = str(pocket_engine).strip().lower()
    margin = float(settings.get("box_group_margin", box_group_margin()))
    large_padding = float(settings.get("box_large_padding", box_large_padding()))
    max_size = float(settings.get("max_size", 30.0))

    # 位点覆盖（用户自定义/微调已知结合位点，或口袋分析 Agent 提交的盒子）
    explicit_source = str((site or {}).get("source") or "用户指定")
    # 库级配体感知下限：**用户显式指定的盒子不受影响**（显式优先是不变量）。
    # 但「口袋分析 Agent 选定的盒子」是**工具产物**，不是用户意图：它的**中心**必须尊重，
    # 尺寸则与自动定盒一样按库级下限抬高 —— 否则大配体库会被塞进 22 Å 的小盒子，
    # 大量分子只能落到互不可比的大盒分组（真实缺陷：120 个农药大分子库主盒 22³）。
    tool_site = str((site or {}).get("chosen_by") or "") == "pocket_agent"
    span_bound: Optional[Dict[str, Any]] = None
    if site:
        center = site.get("center")
        size = site.get("size")
        floor_for_box: Optional[Dict[str, Any]] = None
        if tool_site:
            _note(note_cb, "正在确定对接盒：按库级下限抽样配体 3D 跨度"
                             "（大库首次需几十秒，命中同库缓存则即时）")
            span_bound = library_span_bound(molecules, min_size=settings["min_size"],
                                            sample=settings["box_span_sample"])
            notes.append(span_bound.get("message") or "")
            # 开关关闭时也把 span_bound 传下去：apply_box_floor 会原样返回尺寸，
            # 同时留下「本可抬到多少」的溯源记录（与自动定盒路径口径一致）。
            floor_for_box = span_bound
        for spec in specs:
            if center:
                spec["center"] = [float(x) for x in center]
            if size:
                spec["size"] = [float(x) for x in size]
            spec_size, floor_info, floor_notes = apply_box_floor(
                spec.get("size") or list(DEFAULT_BOX_SIZE), floor_for_box, max_size=max_size)
            if floor_info is not None:
                spec["size"] = spec_size
                if floor_notes:
                    notes.extend(floor_notes)
            site_warnings = list((site or {}).get("warnings") or []) + floor_notes
            site_source = explicit_source
            if floor_info and floor_info.get("raised"):
                # 与 select_site 同口径：盒子被库级下限抬高时，来源里留下标记
                site_source += f" · 库级下限 {float(floor_info['bound']):.1f} Å"
            spec["site"] = {**spec.get("site", {}), "center": list(spec["center"]),
                            "size": list(spec["size"]), "source": site_source,
                            "chosen_by": str((site or {}).get("chosen_by") or "request"),
                            "warnings": site_warnings,
                            "library_floor": floor_info}
            spec["_site_explicit"] = True
    else:
        # 没有显式指定位点时，用**真实工具**确定盒子（P2Rank / 内置几何法），
        # 而不是把盒子放在蛋白质心。实验位点（共晶配体）优先，工具做独立验证。
        # 库级下限：关闭时既不算 3D、也不参与夹取，行为与旧版逐位一致（只在结果里留一条说明）。
        _note(note_cb, "正在确定对接盒：按库级下限抽样配体 3D 跨度"
                         "（大库首次需几十秒，命中同库缓存则即时）")
        span_bound = library_span_bound(molecules, min_size=settings["min_size"],
                                        sample=settings["box_span_sample"])
        notes.append(span_bound.get("message") or "")
        floor_for_box = span_bound if span_bound.get("enabled") else None
        for spec in specs:
            path = str(spec.get("pdbqt") or spec.get("file") or "")
            if not path:
                continue
            try:
                picked = select_site(spec, engine=settings["engine"], top_n=settings["top_n"],
                                     padding=settings["padding"], min_size=settings["min_size"],
                                     max_size=settings["max_size"], box_floor=floor_for_box)
            except Exception as e:  # noqa: BLE001
                logger.warning("口袋预测失败，沿用受体原有点位：%s", e)
                continue
            spec["center"] = list(picked["center"])
            spec["size"] = list(picked["size"])
            floor_info = picked.get("library_floor")
            if floor_info is None and span_bound and span_bound.get("enabled") is False:
                floor_info = {**span_bound, "raised": False,
                              "size_before": [round(float(x), 2) for x in picked["size"]]}
            spec["site"] = {
                "center": list(picked["center"]), "size": list(picked["size"]),
                "source": picked["source"], "engine": picked.get("engine"),
                "chosen_by": picked.get("chosen_by"), "validation": picked.get("validation") or {},
                "pocket": picked.get("pocket") or {}, "pockets": picked.get("pockets") or [],
                "warnings": picked.get("warnings") or [],
                "library_floor": floor_info,
            }
            spec["pocket_analysis"] = True

    total = len(molecules)
    receptors = []
    counter = 0
    grand_total = total * len(specs)
    # 大库时位姿只保留最优的前 N 个（由上层在排序后补写），避免写入过多小文件
    save_all_poses = bool(pose_dir) and total <= pose_save_max()
    grouped_total = 0

    for spec in specs:
        # ---- 盒子有效性硬护栏（在调用任何引擎之前）----
        # Vina 在盒子内**没有受体原子**时不报错，而是返回全 0 能量（affinity=0.0）；
        # 0.0 不是分数而是「什么都没算」。真实事故：盒子来自另一个蛋白的位点坐标
        # （盒中心距该受体最近原子 75 Å），147 个分子跑 7.5 分钟得到一堆 0.0。
        # 这里直接拒绝，并把「差多远」如实写进 notes，让 Agent 一眼看出是盒子/受体错配。
        stats = box_atom_stats(str(spec.get("pdbqt") or ""), spec.get("center") or [],
                               spec.get("size") or [])
        if stats.get("atoms") == 0:
            bad_msg = (f"对接盒内**没有任何受体原子**（盒子中心 "
                       f"{_format_point(spec.get('center'))}、尺寸 "
                       f"{_format_box(spec.get('size') or [])}；距最近受体原子 "
                       f"{stats.get('nearest_angstrom')} Å）→ 已拒绝对接该受体。"
                       "请核对该盒子是否属于这个受体（例如是否误用了另一个蛋白的位点坐标）"
                       "或重新定盒；引擎在这种情况下会静默返回全 0 分数，因此系统不再让它跑。")
            logger.error("受体 %s：%s", spec.get("name"), bad_msg)
            notes.append(f"受体 {spec.get('key')}：{bad_msg}")
            counter += len(molecules)          # 进度不落空（这些分子被明确拒绝，不是被吞掉）
            receptors.append({
                "receptor_key": spec["key"], "receptor": receptor_label(spec),
                "protein": spec.get("protein", ""), "pdbqt": spec.get("pdbqt", ""),
                "box_center": list(spec.get("center") or []),
                "box_size": list(spec.get("size") or []),
                "site": spec.get("site", {}),
                "box_source": (spec.get("site") or {}).get("source", ""),
                "box_chosen_by": (spec.get("site") or {}).get("chosen_by", ""),
                "box_validation": (spec.get("site") or {}).get("validation", {}),
                "box_warnings": list((spec.get("site") or {}).get("warnings") or []) + [bad_msg],
                "box_library_floor": (spec.get("site") or {}).get("library_floor"),
                "box_group_sizes": {"main": list(spec.get("size") or []), "large": None},
                "box_group_counts": {"main": 0, "large": 0},
                "box_atom_stats": stats,
                "receptor_protonation": spec.get("receptor_protonation") or {},
                "dropped_bad_residues": spec.get("dropped_bad_residues") or [],
                "status": "error", "error": bad_msg, "results": [],
            })
            continue
        spec["box_atom_stats"] = stats
        sub_pose_dir = None
        if pose_dir:
            sub_pose_dir = os.path.join(pose_dir, spec["key"]) if len(specs) > 1 else pose_dir
            os.makedirs(sub_pose_dir, exist_ok=True)
        main_box = [float(x) for x in (spec.get("size") or DEFAULT_BOX_SIZE)]
        held: List[Dict[str, Any]] = []

        def _emit_result(r: Dict[str, Any], _spec: Dict[str, Any] = spec) -> None:
            nonlocal counter
            counter += 1
            r.setdefault("receptor", receptor_label(_spec))
            if progress_cb:
                try:
                    progress_cb(counter, grand_total, r)
                except Exception:  # noqa: BLE001
                    logger.debug("progress_cb 异常", exc_info=True)

        def _on_result(r: Dict[str, Any], _spec: Dict[str, Any] = spec) -> None:
            # 超限分子先**扣住不报**，等 large 组用大盒子跑完再报最终结果：
            # 这样进度总数不重复计，界面看到的也是最终分数。
            if not r.get("error") and _box_overflows(r.get("ligand_span"), main_box, margin):
                # 主盒下的适配告警对最终结果不成立（会被大盒子重跑），先摘掉，
                # 否则汇总后的 notes 会留下一条已失效的告警。
                r.pop("box_fit_warning", None)
                held.append(r)
                return
            r.setdefault("box_group", "main")
            _emit_result(r)

        per = dock_batch(
            spec, molecules,
            engine=engine, exhaustiveness=exhaustiveness, n_poses=n_poses, seed=seed,
            ga_num_evals=ga_num_evals, pose_dir=sub_pose_dir, save_poses=save_all_poses,
            on_result=_on_result, cancel_event=cancel_event,
        )

        large_size: Optional[List[float]] = None
        if held:
            large_size = _large_group_box(main_box, [r.get("ligand_span") or [] for r in held],
                                          padding=large_padding, max_size=max_size)
            logger.info("大配体分组：%s 个分子超出主盒 %s，用盒子 %s 单独重跑（中心不变 %s）",
                        len(held), _format_box(main_box), _format_box(large_size), spec["center"])
            spec_large = {**spec, "size": list(large_size)}
            held_mols = [{"name": r.get("name") or r.get("smiles", ""), "smiles": r.get("smiles", "")}
                         for r in held]
            large_rows = dock_batch(
                spec_large, held_mols,
                engine=engine, exhaustiveness=exhaustiveness, n_poses=n_poses, seed=seed,
                ga_num_evals=ga_num_evals, pose_dir=sub_pose_dir, save_poses=save_all_poses,
                on_result=None, cancel_event=cancel_event,
            )
            by_key = {(r.get("name"), r.get("smiles")): r for r in large_rows}
            for old in held:
                new = by_key.get((old.get("name"), old.get("smiles")))
                if new is None:
                    # 理论上不会发生；保底仍按主组结果上报（不丢数据）
                    old.setdefault("box_group", "main")
                    _emit_result(old)
                    continue
                note = f"已单独分组重跑（盒子 {_format_box(large_size)} Å）"
                if new.get("box_fit_warning"):
                    note += "；" + str(new["box_fit_warning"])
                new["box_fit_warning"] = note
                new["box_group"] = "large"
                new["main_box_size"] = list(main_box)
                _emit_result(new)
                old.clear()
                old.update(new)      # per 中持有的引用随之更新，结果里两组都不丢
            grouped_total += len(held)

        main_rows = [r for r in per if str(r.get("box_group") or "main") != "large"]
        large_rows_final = [r for r in per if str(r.get("box_group")) == "large"]
        receptors.append({"receptor_key": spec["key"],
                          "receptor": receptor_label(spec),
                          "protein": spec.get("protein", ""),
                          "pdbqt": spec.get("pdbqt", ""),
                          "box_center": list(spec["center"]),
                          "box_size": list(spec["size"]),
                          "site": spec.get("site", {}),
                          "box_source": (spec.get("site") or {}).get("source", ""),
                          "box_chosen_by": (spec.get("site") or {}).get("chosen_by", ""),
                          "pockets": (spec.get("site") or {}).get("pockets", [])[:10],
                          "box_validation": (spec.get("site") or {}).get("validation", {}),
                          "box_warnings": (spec.get("site") or {}).get("warnings", []),
                          "box_library_floor": (spec.get("site") or {}).get("library_floor"),
                          "box_atom_stats": spec.get("box_atom_stats") or {},
                          "box_group_sizes": {"main": list(main_box), "large": large_size},
                          "box_group_counts": {"main": len(main_rows), "large": len(large_rows_final)},
                          "dropped_hetatm": spec.get("dropped_hetatm") or {},
                          "kept_hetatm": spec.get("kept_hetatm") or {},
                          "dropped_waters": spec.get("dropped_waters") or 0,
                          "unsupported_hetatm": spec.get("unsupported_hetatm") or [],
                          "cocrystal_ligand": spec.get("cocrystal_ligand") or {},
                          # 受体结构路径：供上层解析共晶配体 SMILES（是否用作阳性对照的询问）。
                          # `source_pdb` 是**准备这份 PDBQT 的原始结构**（去配体后只有它还有配体原子）。
                          "source_pdb": str(spec.get("source_pdb") or ""),
                          "receptor_pdb": str(spec.get("pdb") or ""),
                          "receptor_protonation": spec.get("receptor_protonation") or {},
                          "dropped_bad_residues": spec.get("dropped_bad_residues") or [],
                          "results": per})
    for _spec in specs:
        for _n in _spec.get("_chemistry_notes") or []:
            notes.append(_n)
    if grouped_total:
        boxes = "、".join(
            f"{b.get('receptor_key')}={_format_box((b.get('box_group_sizes') or {}).get('large') or [])}"
            for b in receptors if (b.get("box_group_counts") or {}).get("large"))
        notes.append(f"超限分子分组：{grouped_total} 个分子的 3D 跨度 + {margin:.0f} Å 超过主盒，"
                     f"已在**同一中心**用更大盒子单独重跑（{boxes} Å）；"
                     "大配体组与主组盒子不同，**不要跨组直接比较分数**")
    # 大环配体的准备口径要如实说明（否则报告里"环构象没采样"是隐藏前提）
    rigid_rows = [r for b in receptors for r in (b.get("results") or [])
                  if (r.get("ligand_facts") or {}).get("rigid_macrocycle_rings")]
    if rigid_rows:
        biggest = max(max(r["ligand_facts"]["rigid_macrocycle_rings"]) for r in rigid_rows)
        notes.append(f"{len(rigid_rows)} 个配体含 7 元及以上环：按**刚性环**准备（不切环、不采样环构象，"
                     f"本次最大环 {biggest} 元）。这是为了与 AutoDock4 / AutoDock-GPU 的格点打分兼容 —— "
                     "meeko 默认切环会插入格点参数库不认识的伪原子（CG0/G0），经典引擎必然失败；"
                     "统一刚性后各引擎口径一致，代价是环构象未采样。")
    plan = plan_concurrency(len(molecules))
    notes.append(f"对接并发：{plan['workers']} 进程 × {plan['threads']} 线程"
                 f"（{len(molecules)} 个分子；大分子优先调度以减少拖尾）")
    # 全部受体都被护栏拒绝时，状态必须是 error（不能让「一条都没算」看起来像成功）
    status = "error" if receptors and all(b.get("status") == "error" for b in receptors) else "ok"
    return {"status": status, "receptors": receptors, "notes": notes,
            "concurrency": plan, "poses_saved": save_all_poses}


# --------------------------------------------------------------------------- #
# 结合模式检测（真实相似度 + 对接结果对比）
# --------------------------------------------------------------------------- #
