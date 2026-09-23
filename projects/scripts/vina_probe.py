"""独立实现的对接探针（只依赖 rdkit / meeko / vina，**不引用项目代码**）。

从 `scripts/verify_docking.py` 拆出：那份脚本已顶到 700 行上限（规范见
`docs/architecture.md` §10），而这三个函数与它们用到的常量自成一组 ——
「用另一条实现路径重算、再与项目工具逐位比对」是这份证据链的地基。

常量刻意硬编码（不从项目代码读取），避免把被污染的配置继承进独立复算。
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

RECEPTOR_PDBQT = PROJECT_ROOT / "assets" / "receptors" / "registry" / "thrombin_1DWC.pdbqt"
TRYPSIN_PDBQT = PROJECT_ROOT / "assets" / "receptors" / "registry" / "trypsin_1PTU.pdbqt"
BOX_CENTER = [31.5, 13.74, 24.36]
BOX_SIZE = [22.0, 22.0, 22.0]
SEED = 42
EXHAUSTIVENESS = 6
N_POSES = 1


def make_ligand_pdbqt(smiles: str, seed: int = SEED) -> str:
    """与项目等价的配体准备流程（RDKit ETKDGv3 + MMFF + meeko）。"""
    from rdkit import Chem
    from rdkit.Chem import AllChem

    from meeko import MoleculePreparation, PDBQTWriterLegacy

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"无法解析 SMILES: {smiles}")
    mol = Chem.AddHs(mol)
    params = AllChem.ETKDGv3()
    params.randomSeed = seed
    if AllChem.EmbedMolecule(mol, params) != 0:
        raise RuntimeError(f"3D 构象生成失败: {smiles}")
    AllChem.MMFFOptimizeMolecule(mol, maxIters=500)
    setups = MoleculePreparation().prepare(mol)
    pdbqt, ok, err = PDBQTWriterLegacy.write_string(setups[0])
    if not ok:
        raise RuntimeError(f"PDBQT 写入失败: {err}")
    return pdbqt


def vina_dock(receptor_pdbqt: Path, ligand_pdbqt: str,
              center: List[float], size: List[float],
              exhaustiveness: int = EXHAUSTIVENESS, n_poses: int = N_POSES,
              seed: int = SEED, pose_out: Optional[Path] = None) -> List[float]:
    """直接调用 AutoDock Vina Python API 完成对接，返回首个位姿的能量行。"""
    from vina import Vina

    v = Vina(sf_name="vina", verbosity=0, cpu=2, seed=seed)
    v.set_receptor(str(receptor_pdbqt))
    v.set_ligand_from_string(ligand_pdbqt)
    v.compute_vina_maps(center=list(center), box_size=list(size))
    v.dock(exhaustiveness=exhaustiveness, n_poses=n_poses)
    energies = v.energies(n_poses=n_poses)
    if pose_out is not None:
        v.write_pose(str(pose_out), overwrite=True)
    return [float(x) for x in energies[0]]


def vina_rescore(receptor_pdbqt: Path, pose_file: Path,
                 center: List[float], size: List[float], seed: int = SEED) -> List[float]:
    """用全新 Vina 实例对已写出的位姿重新打分（score_only）。"""
    from vina import Vina

    v = Vina(sf_name="vina", verbosity=0, cpu=2, seed=seed)
    v.set_receptor(str(receptor_pdbqt))
    v.set_ligand_from_file(str(pose_file))
    v.compute_vina_maps(center=list(center), box_size=list(size))
    return [float(x) for x in v.score()]


__all__ = ["PROJECT_ROOT", "RECEPTOR_PDBQT", "TRYPSIN_PDBQT", "BOX_CENTER", "BOX_SIZE",
           "SEED", "EXHAUSTIVENESS", "N_POSES", "make_ligand_pdbqt", "vina_dock", "vina_rescore"]
