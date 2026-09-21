"""真实计算核心：分子库、理化性质、受体（已知位点）、对接引擎、排序。

本包只做「真实计算」，不含任何 LLM 或平台依赖；上层（Agent / API / 流水线）统一从此导入。
"""
from docking_agent.cancellation import CancelledRun  # noqa: F401
from docking_agent.core.chemistry import (  # noqa: F401
    SMARTS_PATTERNS,
    _fingerprint,
    binding_mode_profile,
    compute_binding_report,
    compute_maccs_similarity,
    compute_properties,
    compute_tanimoto,
    maccs_fingerprint,
    morgan_fingerprint,
    pharmacophore_flags,
    tanimoto,
)
from docking_agent.core.docking import (  # noqa: F401
    DEFAULT_EXHAUSTIVENESS,
    DEFAULT_N_POSES,
    POSITIVE_CONTROL_NAME,
    DockingSession,
    _run_docking_with_engine,
    dock_batch,
    dock_library,
    plan_concurrency,
    pose_save_max,
    run_docking,
    save_poses_for,
    vina_cpu,
)
from docking_agent.core.files import _fetch_file  # noqa: F401
from docking_agent.core.ligands import (  # noqa: F401
    load_library_file,
    parse_smiles_text,
    read_molecule_file,
    read_molecule_file_normalized,
    smiles_to_pdbqt,
)
from docking_agent.core.params import (  # noqa: F401
    library_stats,
    plan_docking_params,
)
from docking_agent.core.ranking import merge_and_rank  # noqa: F401
from docking_agent.core.receptors import (  # noqa: F401
    DEFAULT_BOX_SIZE,
    DEFAULT_RECEPTOR,
    RECEPTOR_ALIASES,
    RECEPTOR_EXTS,
    RECEPTOR_NON_STRUCTURE_EXTS,
    RECEPTOR_PDBQT_EXTS,
    RECEPTOR_REGISTRY,
    RECEPTOR_STRUCTURE_EXTS,
    is_structure_source,
    list_receptors,
    load_registry,
    prepare_user_receptor,
    read_receptor_file,
    receptor_ext,
    resolve_receptor_specs,
)

__all__ = [
    "compute_properties", "compute_tanimoto", "compute_binding_report",
    # 指纹与结合模式
    "morgan_fingerprint", "_fingerprint", "maccs_fingerprint", "tanimoto",
    "compute_maccs_similarity", "pharmacophore_flags", "binding_mode_profile", "SMARTS_PATTERNS",
    "parse_smiles_text", "load_library_file", "read_molecule_file",
    "read_molecule_file_normalized", "smiles_to_pdbqt",
    "resolve_receptor_specs", "prepare_user_receptor", "read_receptor_file",
    "RECEPTOR_REGISTRY", "RECEPTOR_ALIASES", "DEFAULT_RECEPTOR", "DEFAULT_BOX_SIZE",
    "RECEPTOR_STRUCTURE_EXTS", "RECEPTOR_PDBQT_EXTS", "RECEPTOR_EXTS",
    "RECEPTOR_NON_STRUCTURE_EXTS", "receptor_ext", "is_structure_source",
    "list_receptors", "load_registry",
    "run_docking", "dock_library",
    "DEFAULT_EXHAUSTIVENESS", "DEFAULT_N_POSES", "POSITIVE_CONTROL_NAME",
    "library_stats", "plan_docking_params",
    "merge_and_rank",
    "_fetch_file",
    "_run_docking_with_engine",
    "CancelledRun",
    "DockingSession", "dock_batch", "save_poses_for", "vina_cpu", "pose_save_max",
    "plan_concurrency",
]
