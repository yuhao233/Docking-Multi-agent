"""外部对接引擎：用户自行安装，系统只负责登记、探测与调用。

设计取舍
--------
GPU 版对接工具（Vina-GPU / Uni-Dock / AutoDock-GPU 等）编译与驱动依赖复杂，随项目分发不现实，
因此约定：**二进制由用户在设置页提供路径**，本项目不新增引擎身份 —— 外部工具是 `vina` 引擎的
另一种执行器（同一打分函数族、不同运行后端）。

本模块是探测与适配的**唯一实现**：设置页的「检测」按钮、`scripts/doctor.sh`、引擎调度与
报告标注都从这里取结果，避免出现"设置页说可用、实际不可用"。

三种边界行为（对应"不静默"原则）：

* 未提供路径 → 使用内置 CPU Vina，行为与现在完全一致；
* 提供路径但探测不通过 → **拒绝启动**并给出补齐方法，不静默回退；
* 提供路径且探测通过 → 记录引擎类型、版本与设备状态，供执行适配与报告标注使用。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from docking_agent.config import env, env_int

logger = logging.getLogger(__name__)

#: 用户提供的对接可执行文件（Vina 兼容）—— 填了它 = **改用它执行对接**
ENV_DOCKING_BIN = "EXTERNAL_DOCKING_BIN"
#: 仅**登记/探测**用的 CPU 版 Vina CLI 路径（不改执行路径；留空则自动看 PATH）
ENV_VINA_BIN = "VINA_BIN"
#: GPU 设备序号（多卡机器用）
ENV_GPU_DEVICE = "GPU_DEVICE"
#: 单批提交给外部引擎的配体数
ENV_GPU_BATCH = "GPU_BATCH_SIZE"

#: 探测超时（秒）：外部二进制卡住时不能拖死请求
PROBE_TIMEOUT = 10


class ExternalEngineError(RuntimeError):
    """外部引擎已配置但不可用：调用方应据此拒绝启动并展示 `hint`。"""

    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.details = details or {}


# --------------------------------------------------------------------------- #
# 引擎类型识别：不新增引擎身份，只识别"这是哪一类 Vina 兼容 CLI"
# --------------------------------------------------------------------------- #
#: (flavor, 显示名, 版本输出中出现的特征串)
FLAVORS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    # CPU 版官方 CLI（`vina --version` → "AutoDock Vina 1.2.7" / "AutoDock Vina <git-hash>"）：
    # 与内置绑定同一打分函数，不要求 GPU —— 用于「只登记路径、不改默认执行」的场景。
    ("vina-cpu", "AutoDock Vina（CPU CLI）", ("autodock vina",)),
    ("unidock", "Uni-Dock", ("unidock", "uni-dock")),
    ("vina-gpu", "Vina-GPU", ("vina-gpu", "vinagpu", "quickvina2-gpu", "quickvina-w-gpu")),
    ("autodock-gpu", "AutoDock-GPU", ("autodock-gpu", "autodock_gpu", "autodockgpu")),
)

#: 需要 GPU 的类型：只有这些才在探测时校验设备可见性（CPU CLI 不该因为“没有 GPU”被判不可用）
GPU_FLAVORS = frozenset({"unidock", "vina-gpu", "autodock-gpu"})

#: 各类型的调用形状（P1 执行适配用；P0 只生成并校验 argv）
FLAVOR_ARGV_STYLE: Dict[str, str] = {
    "vina-cpu": "vina",
    "unidock": "unidock",
    "vina-gpu": "vina-gpu",
    "autodock-gpu": "autodock-gpu",
}


def _flavor_label(flavor: str) -> str:
    for key, label, _ in FLAVORS:
        if key == flavor:
            return label
    return "未识别"


def detect_flavor(text: str) -> str:
    """从 `--version` / `--help` 输出里识别引擎类型；识别不出返回空串。"""
    low = (text or "").lower()
    for flavor, _label, signatures in FLAVORS:
        if any(sig in low for sig in signatures):
            return flavor
    return ""


def _first_version_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:160]
    return ""


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #
def configured_bin() -> str:
    """用户在设置页填写的对接可执行文件路径；未填写返回空串。"""
    return str(env(ENV_DOCKING_BIN, "") or "").strip()


def gpu_device() -> int:
    return max(0, int(env_int(ENV_GPU_DEVICE, 0)))


def gpu_batch_size() -> int:
    return max(1, int(env_int(ENV_GPU_BATCH, 100)))


def vina_cli_bin() -> str:
    """定位 CPU 版 AutoDock Vina CLI：`VINA_BIN` → PATH。找不到返回空串。

    与 `external.docking_bin` 的区别（重要）：
      * 本函数只用于**登记与探测**（设置页「检测」/ `doctor.sh` / 报告标注）；
      * 真正「改用它执行对接」要填 `EXTERNAL_DOCKING_BIN`（见 `configured_bin()`）。
    这样「本机装了哪个 Vina」与「对接由谁执行」是两件事，不会因为登记一个路径就换掉执行后端。
    """
    explicit = str(env(ENV_VINA_BIN, "") or "").strip()
    if explicit:
        target = Path(explicit).expanduser()
        if target.is_file() and os.access(target, os.X_OK):
            return str(target)
    return shutil.which("vina") or ""


def detect_available_vina() -> Dict[str, Any]:
    """登记 PATH（或 `VINA_BIN`）上的 AutoDock Vina CLI：识别类型与版本，不改变执行路径。"""
    found = vina_cli_bin()
    if not found:
        return {}
    version = run_version(found)
    if not version.get("ok"):
        return {}
    flavor = str(version.get("flavor") or "")
    return {"path": found, "flavor": flavor, "flavor_label": _flavor_label(flavor),
            "version_line": version.get("version_line", ""),
            "recognized": flavor in FLAVOR_ARGV_STYLE}


def probe_path(path: str) -> Dict[str, Any]:
    """检查路径本身：存在、是文件、可执行。"""
    raw = str(path or "").strip()
    if not raw:
        return {"ok": False, "reason": "未提供路径", "hint": "在设置页「外部工具」填入可执行文件路径"}
    target = Path(raw).expanduser()
    if not target.exists():
        return {"ok": False, "reason": "路径不存在", "hint": f"确认文件存在：{target}"}
    if target.is_dir():
        return {"ok": False, "reason": "这是目录，不是可执行文件",
                "hint": f"指向具体的可执行文件，例如 {target}/unidock"}
    if not os.access(target, os.X_OK):
        return {"ok": False, "reason": "没有执行权限",
                "hint": f"chmod +x {target}"}
    return {"ok": True, "path": str(target), "size": target.stat().st_size}


def run_version(path: str, *, timeout: int = PROBE_TIMEOUT) -> Dict[str, Any]:
    """执行 `--version`（失败再试 `--help`），拿到识别所需的输出。

    固定 argv、`shell=False`、带超时：用户提供的是可执行文件，不是 shell 片段。
    """
    for flag in ("--version", "--help"):
        try:
            proc = subprocess.run(  # noqa: S603 - 用户在本机提供的工具，argv 固定
                [str(path), flag], capture_output=True, text=True, timeout=timeout,
                cwd=str(Path(path).parent), check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": f"{flag} 超时（>{timeout}s）",
                    "hint": "该工具可能要求交互或依赖缺失，先在终端手动运行确认"}
        except OSError as exc:
            return {"ok": False, "reason": f"无法执行：{exc}", "hint": "检查动态库依赖（ldd）"}
        text = f"{proc.stdout}\n{proc.stderr}"
        if proc.returncode == 0 or text.strip():
            return {"ok": True, "flag": flag, "returncode": proc.returncode,
                    "output": text.strip()[:2000], "version_line": _first_version_line(text),
                    "flavor": detect_flavor(text)}
    return {"ok": False, "reason": "--version 与 --help 都没有输出",
            "hint": "确认这是命令行对接工具，而不是图形界面程序"}


def gpu_visibility() -> Dict[str, Any]:
    """检查 GPU 是否可见：`nvidia-smi` 优先，其次 `clinfo`。

    容器/受限命名空间里看不到 `/dev/nvidia*` 时这里会如实报失败 —— 这是事实，不是异常。
    """
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            proc = subprocess.run([smi, "-L"], capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                                  check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "tool": "nvidia-smi", "reason": str(exc)[:200],
                    "hint": "驱动/设备节点不可用；GPU 引擎需要在能访问 GPU 的会话中运行"}
        out = (proc.stdout or proc.stderr or "").strip()
        lines = [ln for ln in out.splitlines() if ln.strip().startswith("GPU")]
        if proc.returncode == 0 and lines:
            return {"ok": True, "tool": "nvidia-smi", "devices": lines[:8]}
        return {"ok": False, "tool": "nvidia-smi", "reason": _first_version_line(out) or "无 GPU 输出",
                "hint": "确认驱动已加载且当前进程能看到设备（容器需 --gpus / 设备映射）"}
    clinfo = shutil.which("clinfo")
    if clinfo:
        try:
            proc = subprocess.run([clinfo, "--list"], capture_output=True, text=True,
                                  timeout=PROBE_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "tool": "clinfo", "reason": str(exc)[:200]}
        if proc.returncode == 0 and proc.stdout.strip():
            return {"ok": True, "tool": "clinfo", "devices": proc.stdout.strip().splitlines()[:8]}
        return {"ok": False, "tool": "clinfo", "reason": "未列出 OpenCL 设备"}
    return {"ok": False, "reason": "未找到 nvidia-smi 或 clinfo",
            "hint": "安装 NVIDIA 驱动（或 OpenCL 运行时）后重试"}


def collect(*, check_gpu: bool = True) -> Dict[str, Any]:
    """汇总外部工具状态：设置页「检测」与 `doctor.sh` 共用这一份结果。"""
    path = configured_bin()
    report: Dict[str, Any] = {"configured": bool(path), "bin": path}
    if not path:
        # 「只登记、不改默认执行」：把 PATH 上发现的 Vina CLI 如实报出来供查看/复制路径，
        # 但 configured=False、执行仍走内置绑定（同版本、支持每分子盒子/分批/取消）。
        detected = detect_available_vina()
        report.update({
            "ok": True, "state": "not_configured",
            "message": "未提供外部引擎，使用内置 CPU Vina",
        })
        if detected:
            report["detected"] = detected
            report["message"] += (f"；发现系统 PATH 上的 {detected.get('flavor_label', '')}"
                                  f"（{detected.get('version_line', '')}），如需改用它请在设置页填入路径")
        return report

    path_check = probe_path(path)
    if not path_check.get("ok"):
        report.update({"ok": False, "state": "invalid", **path_check})
        return report

    version = run_version(path_check["path"])
    report.update({"path_check": path_check, "version": version})
    if not version.get("ok"):
        report.update({"ok": False, "state": "probe_failed",
                       "reason": version.get("reason", ""), "hint": version.get("hint", "")})
        return report

    flavor = str(version.get("flavor") or "")
    report["flavor"] = flavor
    report["flavor_label"] = _flavor_label(flavor)
    if not flavor:
        report.update({
            "ok": False, "state": "unrecognized",
            "reason": "无法从 --version/--help 输出识别引擎类型",
            "hint": "目前识别 Uni-Dock / Vina-GPU / AutoDock-GPU；其它工具请提 issue 附版本输出",
        })
        return report

    needs_gpu = flavor in GPU_FLAVORS
    gpu = (gpu_visibility() if (needs_gpu and check_gpu)
           else {"ok": True, "skipped": True, "reason": "CPU 引擎，无需 GPU"})
    report["gpu"] = gpu
    report.update({
        "ok": bool(gpu.get("ok")),
        "state": "ready" if gpu.get("ok") else "no_gpu",
        "device": gpu_device(),
        "batch_size": gpu_batch_size(),
        "argv_style": FLAVOR_ARGV_STYLE.get(flavor, ""),
        "version_line": version.get("version_line", ""),
    })
    if not gpu.get("ok"):
        report["reason"] = gpu.get("reason", "GPU 不可见")
        report["hint"] = gpu.get("hint", "")
    return report


def require_engine() -> Dict[str, Any]:
    """执行前校验：未配置返回 `{}`；已配置但不可用则抛 `ExternalEngineError`。"""
    report = collect()
    if not report.get("configured"):
        return {}
    if not report.get("ok"):
        reason = report.get("reason") or report.get("state") or "未知原因"
        hint = report.get("hint") or "在设置页「外部工具」检查路径与探测结果"
        raise ExternalEngineError(
            f"外部对接引擎不可用：{reason}。{hint}", details=report)
    return report


# --------------------------------------------------------------------------- #
# 调用形状（P1 执行适配使用；这里先固定并测试，避免执行期再猜参数）
# --------------------------------------------------------------------------- #
def build_argv(flavor: str, *, binary: str, receptor: str, ligands: Sequence[str],
               out_dir: str, center: Sequence[float], size: Sequence[float],
               exhaustiveness: int, n_poses: int, seed: int,
               fld: str = "", resnam: str = "", threads: Optional[int] = None,
               out_pose: str = "", device: Optional[int] = None) -> List[str]:
    """按引擎类型生成调用参数。

    `autodock-gpu` 与 `vina-cpu` 的参数形状按各自官方 CLI 实测固定（本机验证过）；
    `unidock` / `vina-gpu` 仍按官方文档固定，但**执行适配未接入**（`external_run.py` 会直接报错，
    不拿未验证的参数去跑真实计算）。

    `device`：项目内部一律用 **0 基** GPU 序号（`GPU_DEVICE=0` 表示第一块卡）；AutoDock-GPU 的
    `--devnum` 是 **1 基**（实测传 0 会被拒："must be an integer between 1 and 65536"），
    因此这里 `+1` 后再下发。
    """
    if flavor not in FLAVOR_ARGV_STYLE:
        raise ExternalEngineError(f"未识别的外部引擎类型：{flavor or '(空)'}")
    cx, cy, cz = (float(v) for v in center)
    sx, sy, sz = (float(v) for v in size)
    base = [binary]
    if flavor == "vina-cpu":
        # 官方 CLI：一次一个配体文件 + 显式盒子（无格点图）；`--out` 写位姿
        argv = [
            "--receptor", receptor,
            "--ligand", ligands[0] if ligands else "",
            "--center_x", f"{cx:.3f}", "--center_y", f"{cy:.3f}", "--center_z", f"{cz:.3f}",
            "--size_x", f"{sx:.3f}", "--size_y", f"{sy:.3f}", "--size_z", f"{sz:.3f}",
            "--exhaustiveness", str(int(exhaustiveness)),
            "--num_modes", str(int(n_poses)),
            "--seed", str(int(seed)),
            "--out", str(out_pose or (Path(out_dir) / "out.pdbqt")),
        ]
        if threads:
            argv += ["--cpu", str(int(threads))]
        return base + argv
    if flavor == "unidock":
        return base + [
            "--receptor", receptor,
            "--gpu_batch", *[str(p) for p in ligands],
            "--center_x", f"{cx:.3f}", "--center_y", f"{cy:.3f}", "--center_z", f"{cz:.3f}",
            "--size_x", f"{sx:.3f}", "--size_y", f"{sy:.3f}", "--size_z", f"{sz:.3f}",
            "--exhaustiveness", str(int(exhaustiveness)),
            "--num_modes", str(int(n_poses)),
            "--seed", str(int(seed)),
            "--dir", out_dir,
        ]
    if flavor == "vina-gpu":
        # Vina-GPU 2.x 走 config 文件；把等价参数写成一份，交由执行层落盘
        return base + [
            "--receptor", receptor,
            "--ligand_directory", out_dir,
            "--output_directory", out_dir,
            "--center_x", f"{cx:.3f}", "--center_y", f"{cy:.3f}", "--center_z", f"{cz:.3f}",
            "--size_x", f"{sx:.3f}", "--size_y", f"{sy:.3f}", "--size_z", f"{sz:.3f}",
            "--thread", str(int(exhaustiveness)),
            "--seed", str(int(seed)),
        ]
    if flavor == "autodock-gpu":
        # 官方 CLI：吃 autogrid4 生成的格点图（`--ffile`），盒子信息已在图里
        if not fld:
            raise ExternalEngineError("autodock-gpu 需要格点图描述文件（`.maps.fld`）")
        return base + [
            "--ffile", fld,
            "--lfile", ligands[0] if ligands else "",
            "--nrun", str(int(n_poses)),
            "--seed", str(int(seed)),
            "--resnam", resnam or (Path(out_dir) / "adgpu_out").as_posix(),
            "--xmloutput", "0",
            # 0 基 → 1 基（见 docstring 实测说明）
            "--devnum", str(int(device if device is not None else 0) + 1),
        ]
    # 其余（AutoDock-GPU 之外的 GPU 工具）：按官方文档固定，执行适配未接入
    return base + [
        "--receptor", receptor,
        "--ligand_directory", out_dir,
        "--center_x", f"{cx:.3f}", "--center_y", f"{cy:.3f}", "--center_z", f"{cz:.3f}",
        "--size_x", f"{sx:.3f}", "--size_y", f"{sy:.3f}", "--size_z", f"{sz:.3f}",
        "--nrun", str(int(n_poses)),
        "--seed", str(int(seed)),
    ]
