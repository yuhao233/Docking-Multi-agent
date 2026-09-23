"""外部引擎的**执行适配**（`core/external_run.py`）：真实调用形状、输出解析、格点图。

与 `tests/test_external_engine.py` 的分工：那边测"登记与探测"，这边测"真的跑起来之后"。
外部二进制一律用临时目录里的假脚本替代 —— 断言的是我们下发的参数与解析结果，不是 GPU 本身；
真正的 GPU 端到端由本文件末尾的 `test_real_autodock_gpu_end_to_end`（本机登记且就绪时才跑）
与 `scripts/verify_docking.py` 覆盖。

历史上的真实故障（都已固定成回归）：

* `--devnum 0` 被 AutoDock-GPU 拒绝（它要求 1 基），登记了 GPU 却 0.15 s 失败、结果行报错；
* 配体带极性氢（meeko 的 `HD` 类型）而格点图只有 A/C/N/NA/OA/S/SA → 引擎"任务未成功"；
* `--nrun N` 时 DLG 里有多组能量，取第一组会拿到非最优的结合能。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from docking_agent.core import external_run as ER
from docking_agent.core.external_tools import ExternalEngineError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MULTI_RUN_DLG = FIXTURES / "autodock_gpu_multirun.dlg"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 假二进制：AutoDock-GPU / autogrid4 / vina-cpu
# --------------------------------------------------------------------------- #
def _fake_autodock_gpu(tmp_path: Path) -> Path:
    """接收 `--resnam X` 并写出 `X.dlg`（含两组结果，第二组更优），同时把 argv 记到文件里。"""
    path = tmp_path / "fake_adgpu"
    dlg = """    FINAL DOCKED STATE:

Run:   1 / 2
DOCKED: USER    Estimated Free Energy of Binding    =  -5.59 kcal/mol  [=(1)+(2)+(3)-(4)]
DOCKED: USER    (1) Final Intermolecular Energy     =  -5.88 kcal/mol
DOCKED: USER    (2) Final Total Internal Energy     =  -0.03 kcal/mol
DOCKED: USER    (3) Torsional Free Energy           =  +0.30 kcal/mol

Run:   2 / 2
DOCKED: USER    Estimated Free Energy of Binding    =  -7.12 kcal/mol  [=(1)+(2)+(3)-(4)]
DOCKED: USER    (1) Final Intermolecular Energy     =  -7.40 kcal/mol
DOCKED: USER    (2) Final Total Internal Energy     =  -0.02 kcal/mol
DOCKED: USER    (3) Torsional Free Energy           =  +0.30 kcal/mol

    CLUSTERING HISTOGRAM
    ____________________
   1 |     -7.12 |   2 |     -7.12 |   2 |##
"""
    path.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$FAKE_ARGV_LOG"\n'
        'resnam=""\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--resnam" ]; then resnam="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        'if [ -z "$resnam" ]; then echo "no --resnam" >&2; exit 2; fi\n'
        'cat > "${resnam}.dlg" <<\'DLG\'\n' + dlg + "DLG\n",
        encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_autogrid4(tmp_path: Path) -> Path:
    """接收 `-p X.gpf`，把 GPF 复制一份留档，并写出 `X` 里声明的 `.maps.fld`。"""
    path = tmp_path / "fake_autogrid4"
    path.write_text(
        "#!/bin/sh\n"
        'gpf=""\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "-p" ]; then gpf="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        'cp "$gpf" "$FAKE_GPF_COPY" || exit 3\n'
        'fld=$(awk \'/^gridfld/ {print $2}\' "$gpf")\n'
        'for m in $(awk \'/^map / {print $2}\' "$gpf"); do : > "$m"; done\n'
        ': > rec.e.map; : > rec.d.map\n'
        ': > "$fld"\n'
        'echo "autogrid4: successful completion."\n',
        encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_vina(tmp_path: Path) -> Path:
    """写位姿 PDBQT（带 `REMARK VINA RESULT`）并在 `--score_only` 时打印能量分解。"""
    path = tmp_path / "fake_vina"
    path.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "--score_only" ]; then\n'
        '    echo "Estimated Free Energy of Binding    : -6.44 (kcal/mol)"\n'
        '    echo "  (1) Final Intermolecular Energy     : -6.72 (kcal/mol)"\n'
        '    echo "  (2) Final Total Internal Energy     : -0.05 (kcal/mol)"\n'
        '    echo "  (3) Torsional Free Energy           : 0.33 (kcal/mol)"\n'
        "    exit 0\n"
        "  fi\n"
        "done\n"
        'out=""\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "--out" ]; then out="$a"; fi\n'
        '  prev="$a"\n'
        "done\n"
        'printf "REMARK VINA RESULT:    -6.35      0.000      0.000\\nMODEL 1\\nENDMDL\\n" > "$out"\n',
        encoding="utf-8")
    path.chmod(0o755)
    return path


# --------------------------------------------------------------------------- #
# 1) 输出解析
# --------------------------------------------------------------------------- #
def test_parse_dlg_takes_the_best_run_not_the_first() -> None:
    """真实 AutoDock-GPU 产物（thrombin × benzamidine，`--nrun 3`，集群直方图 1 个簇）。"""
    energies = ER.parse_dlg_energies(MULTI_RUN_DLG)
    assert energies["affinity_kcal_mol"] == pytest.approx(-5.59)
    assert energies["intermolecular_kcal_mol"] == pytest.approx(-5.88)
    assert energies["intramolecular_kcal_mol"] == pytest.approx(-0.03)
    assert energies["torsion_kcal_mol"] == pytest.approx(0.30)


def test_parse_dlg_prefers_lowest_energy_block(tmp_path: Path) -> None:
    """多组结果里必须取最低结合能那一组（含它的能量分解项），而不是文件里第一组。"""
    dlg = _fake_autodock_gpu(tmp_path)
    os.environ["FAKE_ARGV_LOG"] = str(tmp_path / "argv.txt")
    import subprocess

    subprocess.run([str(dlg), "--resnam", str(tmp_path / "out")], check=True)
    energies = ER.parse_dlg_energies(tmp_path / "out.dlg")
    assert energies["affinity_kcal_mol"] == pytest.approx(-7.12)
    assert energies["intermolecular_kcal_mol"] == pytest.approx(-7.40), "分解项必须与最优组同源"


def test_parse_vina_pose_energy(tmp_path: Path) -> None:
    pose = tmp_path / "pose.pdbqt"
    pose.write_text("REMARK VINA RESULT:    -8.10      0.000      0.000\nMODEL 1\n", encoding="utf-8")
    assert ER.parse_vina_pose_energy(pose) == pytest.approx(-8.10)


# --------------------------------------------------------------------------- #
# 2) 格点图：GPF 内容与配体类型覆盖
# --------------------------------------------------------------------------- #
def test_standard_ligand_types_cover_polar_hydrogens() -> None:
    """meeko 准备的配体一律带 `HD`：缺这张图时 AutoDock-GPU 直接判"任务未成功"（实测 0.15 s）。"""
    assert "HD" in ER.STANDARD_LIGAND_TYPES
    for element_type in ("A", "C", "N", "NA", "OA", "S", "SA", "F", "Cl", "Br", "I", "P"):
        assert element_type in ER.STANDARD_LIGAND_TYPES


def test_build_grid_maps_writes_gpf_covering_every_ligand_type(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    gpf_copy = tmp_path / "seen.gpf"
    monkeypatch.setenv("FAKE_GPF_COPY", str(gpf_copy))
    receptor = tmp_path / "rec.pdbqt"
    receptor.write_text(
        "ATOM      1  N   ALA A   1      11.000  12.000  13.000  1.00  0.00    -0.300 N \n"
        "ATOM      2  H   ALA A   1      11.500  12.500  13.500  1.00  0.00     0.150 HD\n",
        encoding="utf-8")
    workdir = tmp_path / "grids"
    workdir.mkdir()
    fld = ER.build_grid_maps(str(receptor), [10.0, 20.0, 30.0], [20.0, 20.0, 20.0],
                             str(workdir), autogrid4=str(_fake_autogrid4(tmp_path)),
                             ligand_types=("A", "HD"))
    assert Path(fld).exists()
    gpf = gpf_copy.read_text(encoding="utf-8")
    assert "ligand_types A HD" in gpf
    assert "map rec.A.map" in gpf and "map rec.HD.map" in gpf
    assert "spacing 0.375" in gpf and "gridcenter 10.000 20.000 30.000" in gpf
    # 网格点数必须为奇数（AutoDock 要求中心落在一个格点上）
    npts = [int(v) for v in gpf.split("npts ")[1].splitlines()[0].split()[:3]]
    assert all(n % 2 == 1 for n in npts), npts


def test_build_grid_maps_reports_engine_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken_autogrid4"
    broken.write_text("#!/bin/sh\necho 'ERROR:  unknown ligand atom type Cu' >&2\nexit 1\n",
                      encoding="utf-8")
    broken.chmod(0o755)
    receptor = tmp_path / "rec.pdbqt"
    receptor.write_text("ATOM      1  N   ALA A   1      11.000  12.000  13.000  1.00  0.00    -0.300 N \n",
                        encoding="utf-8")
    with pytest.raises(ExternalEngineError) as excinfo:
        ER.build_grid_maps(str(receptor), [10.0, 20.0, 30.0], [20.0, 20.0, 20.0],
                           str(tmp_path), autogrid4=str(broken))
    assert "unknown ligand atom type" in str(excinfo.value), "失败原因要原样带给用户"


# --------------------------------------------------------------------------- #
# 3) 调用形状与结果行
# --------------------------------------------------------------------------- #
def test_dock_ligand_external_autodock_gpu(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    argv_log = tmp_path / "argv.txt"
    monkeypatch.setenv("FAKE_ARGV_LOG", str(argv_log))
    binary = _fake_autodock_gpu(tmp_path)
    ligand = tmp_path / "lig.pdbqt"
    ligand.write_text("ATOM      1  C   UNL     1      11.000  12.000  13.000  1.00  0.00     0.000 A \n",
                      encoding="utf-8")
    row = ER.dock_ligand_external(
        flavor="autodock-gpu", binary=str(binary), ligand_pdbqt=str(ligand),
        workdir=str(tmp_path), center=[1.0, 2.0, 3.0], size=[20.0, 20.0, 20.0],
        exhaustiveness=8, n_poses=2, seed=42, fld=str(tmp_path / "rec.maps.fld"),
        pose_base=str(tmp_path / "poses" / "benzamidine"), device=0,
        engine_version="AutoDock-GPU version: v1.6-release")
    assert row["engine"] == "autodock-gpu"
    assert row["engine_version"].endswith("v1.6-release")
    assert row["affinity_kcal_mol"] == pytest.approx(-7.12)
    assert Path(row["pose_file"]).exists(), "留档位姿必须真的落盘"
    argv = argv_log.read_text(encoding="utf-8").split()
    assert argv[argv.index("--ffile") + 1].endswith("rec.maps.fld")
    assert argv[argv.index("--nrun") + 1] == "2", "n_poses 映射到 GA 运行数"
    assert argv[argv.index("--devnum") + 1] == "1", "0 基设备号下发时转 1 基"


def test_dock_ligand_external_vina_cpu(tmp_path: Path) -> None:
    binary = _fake_vina(tmp_path)
    ligand = tmp_path / "lig.pdbqt"
    ligand.write_text("ATOM      1  C   UNL     1      11.000  12.000  13.000  1.00  0.00     0.000 C \n",
                      encoding="utf-8")
    receptor = tmp_path / "rec.pdbqt"
    receptor.write_text("ATOM      1  N   ALA A   1      11.000  12.000  13.000  1.00  0.00    -0.300 N \n",
                        encoding="utf-8")
    row = ER.dock_ligand_external(
        flavor="vina-cpu", binary=str(binary), ligand_pdbqt=str(ligand),
        workdir=str(tmp_path), receptor_pdbqt=str(receptor),
        center=[1.0, 2.0, 3.0], size=[20.0, 20.0, 20.0],
        exhaustiveness=8, n_poses=1, seed=42, threads=4)
    assert row["affinity_kcal_mol"] == pytest.approx(-6.35)
    assert row["intermolecular_kcal_mol"] == pytest.approx(-6.72), "能量分解来自 --score_only"
    assert row["torsion_kcal_mol"] == pytest.approx(0.33)


def test_unadapted_flavors_refuse_to_guess_argv(tmp_path: Path) -> None:
    """Uni-Dock / Vina-GPU 只登记不执行：本机没有可验证的输出格式，不能拿猜的参数去算。"""
    for flavor in ("unidock", "vina-gpu"):
        with pytest.raises(ExternalEngineError) as excinfo:
            ER.dock_ligand_external(flavor=flavor, binary="/opt/x", ligand_pdbqt="l.pdbqt",
                                    workdir=str(tmp_path), center=[0, 0, 0], size=[20, 20, 20],
                                    exhaustiveness=8, n_poses=1, seed=42)
        assert "尚未接入执行适配" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 4) 会话：按需扩图 + 拒绝静默回退
# --------------------------------------------------------------------------- #
def _bare_session(tmp_path: Path) -> object:
    """只测格点图缓存逻辑：不走 `__init__`，避免依赖真实 Vina / 已登记引擎。"""
    from docking_agent.core.docking import DockingSession

    session = DockingSession.__new__(DockingSession)
    session.spec = {"pdbqt": str(tmp_path / "rec.pdbqt"),
                    "center": [1.0, 2.0, 3.0], "size": [20.0, 20.0, 20.0]}
    session._external_fld = ""
    session._external_map_dir = ""
    session._external_map_types = set()
    return session


def test_session_builds_maps_once_per_session(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """格点图按会话共享：同一会话内不同配体不重复生成。"""
    calls: list = []

    def _fake_build(receptor, center, size, workdir, *, autogrid4=None, tag="rec",
                    ligand_types=()):
        calls.append(tuple(ligand_types))
        fld = Path(workdir) / "rec.maps.fld"
        fld.parent.mkdir(parents=True, exist_ok=True)
        fld.write_text("fld\n", encoding="utf-8")
        return str(fld)

    monkeypatch.setattr(ER, "build_grid_maps", _fake_build)
    session = _bare_session(tmp_path)
    first = session._external_maps({"A", "HD"})  # type: ignore[attr-defined]
    again = session._external_maps({"A", "C", "N", "OA"})  # type: ignore[attr-defined]
    assert first == again and len(calls) == 1, "同一会话只建一次图"
    assert set(calls[0]) == set(ER.STANDARD_LIGAND_TYPES), "一次就按实测可用全集建图"


def test_session_reports_ligand_types_autogrid_cannot_map(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """配体含 autogrid4 参数库没有的类型（Cu/Hg/Se/Na/K）时明确报错，不假装算过。"""
    def _must_not_build(*args, **kwargs):  # pragma: no cover - 调用即失败
        raise AssertionError("不支持的类型不该走到建图步骤")

    monkeypatch.setattr(ER, "build_grid_maps", _must_not_build)
    session = _bare_session(tmp_path)
    with pytest.raises(ExternalEngineError) as excinfo:
        session._external_maps({"A", "Cu"})  # type: ignore[attr-defined]
    assert "Cu" in str(excinfo.value) and "Vina" in str(excinfo.value)


def test_external_engine_without_registration_refuses_to_fall_back(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`engine=external` 未登记时直接报错 —— 不静默改用内置 CPU（否则用户以为在用 GPU）。"""
    monkeypatch.delenv("EXTERNAL_DOCKING_BIN", raising=False)
    from docking_agent.core.docking import DockingSession

    with pytest.raises(RuntimeError) as excinfo:
        DockingSession({"pdbqt": str(tmp_path / "rec.pdbqt"), "center": [0, 0, 0],
                        "size": [20, 20, 20]}, engine="external")
    assert "不静默改用内置引擎" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 5) 真实 GPU 端到端（本机登记且探测就绪时才跑）
# --------------------------------------------------------------------------- #
def _real_engine_report() -> dict:
    from docking_agent.core.external_tools import collect

    report = collect()
    return report if report.get("state") == "ready" else {}


REAL_ENGINE = _real_engine_report()


@pytest.mark.skipif(not REAL_ENGINE or REAL_ENGINE.get("flavor") != "autodock-gpu",
                    reason="本机未登记就绪的 AutoDock-GPU")
def test_real_autodock_gpu_end_to_end() -> None:
    """真机跑一次（预置受体 thrombin × 苯甲脒）：结果行必须带引擎与版本，而不是内置引擎的分数。"""
    from docking_agent.core.docking import dock_library

    result = dock_library([{"name": "benzamidine", "smiles": "NC(=N)c1ccccc1"}],
                          receptor="thrombin", engine="external",
                          exhaustiveness=1, n_poses=1)
    rows = [row for rec in result.get("receptors", []) for row in rec.get("results", [])]
    assert rows, result
    row = rows[0]
    assert not row.get("error"), row
    assert row["engine"] == "autodock-gpu"
    assert "AutoDock-GPU" in str(row.get("engine_version") or "")
    assert isinstance(row["affinity_kcal_mol"], float)
    assert row["affinity_kcal_mol"] < 0


def test_external_engine_note_tells_the_truth_about_who_runs() -> None:
    """登记了外部引擎但本次不用它时必须说出来（否则用户以为跑在 GPU 上）。"""
    from docking_agent.core.docking import external_engine_note

    report = {"flavor": "autodock-gpu", "flavor_label": "AutoDock-GPU",
              "device": 0, "batch_size": 100}
    used = external_engine_note(report, "external")
    assert "由它执行" in used and "AutoDock-GPU" in used and "100" in used
    idle = external_engine_note(report, "vina")
    assert "仍由内置引擎计算" in idle and "engine=vina" in idle
    assert "external" in idle, "要告诉用户怎么切换到外部引擎"
