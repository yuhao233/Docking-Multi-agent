"""真实计算核心：分子库、理化性质、受体（已知位点）、对接引擎、排序。

本包只做「真实计算」，不含任何 LLM 或平台依赖；上层（Agent / API / 流水线）统一从此导入。

**惰性 facade（PEP 562）**：这里不再急切 import 各子模块，而是按名按需导入。
原因（审计 SCC-2）：`core/` 子模块之间存在函数级懒导入（`pockets⇄docking`、
`normalize⇄ligands/inchikey`、`receptors⇄pockets`），而急切 `__init__` 会把
「子模块 → `docking_agent.core` → 其它子模块」变成一条真实的环；改成按需导出后，
这些边消失，`core/` 内部只剩纯下行导入。副作用是导入更快（不再拉起 rdkit 全量）。
"""
from __future__ import annotations

import importlib
from typing import Any, Dict, List

#: 公开名 → 定义它的模块（全名或 `core.` 下的子模块短名）
_EXPORTS: Dict[str, str] = {
    # chemistry
    "compute_properties": "chemistry", "compute_tanimoto": "chemistry",
    "compute_binding_report": "chemistry", "morgan_fingerprint": "chemistry",
    "_fingerprint": "chemistry", "maccs_fingerprint": "chemistry",
    "tanimoto": "chemistry", "compute_maccs_similarity": "chemistry",
    "pharmacophore_flags": "chemistry", "binding_mode_profile": "chemistry",
    "SMARTS_PATTERNS": "chemistry",
    # docking
    "run_docking": "docking", "dock_library": "docking",
    "DEFAULT_EXHAUSTIVENESS": "docking", "DEFAULT_N_POSES": "docking",
    "POSITIVE_CONTROL_NAME": "docking", "DockingSession": "docking",
    "dock_batch": "docking", "vina_cpu": "docking",
    "pose_save_max": "docking", "plan_concurrency": "docking",
    "_run_docking_with_engine": "docking",
    # files / ligands
    "_fetch_file": "files",
    "parse_smiles_text": "ligands", "load_library_file": "ligands",
    "read_molecule_file": "ligands", "read_molecule_file_normalized": "ligands",
    "smiles_to_pdbqt": "ligands",
    # params / ranking
    "library_stats": "params", "plan_docking_params": "params",
    "merge_and_rank": "ranking",
    # receptors
    "resolve_receptor_specs": "receptors", "prepare_user_receptor": "receptors",
    "read_receptor_file": "receptors", "RECEPTOR_REGISTRY": "receptors",
    "RECEPTOR_ALIASES": "receptors", "DEFAULT_RECEPTOR": "receptors",
    "DEFAULT_BOX_SIZE": "receptors", "RECEPTOR_STRUCTURE_EXTS": "receptors",
    "RECEPTOR_PDBQT_EXTS": "receptors", "RECEPTOR_EXTS": "receptors",
    "RECEPTOR_NON_STRUCTURE_EXTS": "receptors", "receptor_ext": "receptors",
    "is_structure_source": "receptors", "list_receptors": "receptors",
    "load_registry": "receptors",
    # 取消（顶层模块，不属于 core/）
    "CancelledRun": "docking_agent.cancellation",
}

__all__: List[str] = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    """按需导入并缓存（PEP 562）。未知名字保持标准 AttributeError 语义。"""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if not module_name.startswith("docking_agent."):
        module_name = f"{__name__}.{module_name}"
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value       # 缓存：后续访问不再走 __getattr__
    return value


def __dir__() -> List[str]:
    return sorted(set(globals()) | set(_EXPORTS))
