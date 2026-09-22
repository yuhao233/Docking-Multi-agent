"""分子库与阳性对照的解析（产品面与 Agent 路径共用）。

从原 `pipeline.py` 迁出：删除"不经过 Agent 的确定性对接"后，这几个函数仍被 Agent 路径使用
（`agents/dispatch.py` 的阳性对照、`api/app.py` 的库解析入口），因此单独成模块，
避免它们随执行器一起消失。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from docking_agent.core import (
    POSITIVE_CONTROL_NAME,  # noqa: F401  (对外的常量出口，保持一致)
    load_library_file,
    parse_smiles_text,
    read_molecule_file,
)
from docking_agent.paths import libraries_dir

logger = logging.getLogger(__name__)

#: 内置阳性对照（苯甲脒：凝血酶 S1 口袋经典探针）
DEFAULT_POSITIVE_CONTROL = "NC(=N)c1ccccc1"
#: 结果与报告里阳性对照的显示标签
POSITIVE_CONTROL_LABEL = "阳性对照"


def default_library_path() -> Path:
    """示例分子库路径（`assets/libraries/mol_library.csv`）。"""
    return libraries_dir() / "mol_library.csv"


def default_positive_control_path() -> Path:
    """示例阳性对照路径（`assets/libraries/positive_control.csv`）。"""
    return libraries_dir() / "positive_control.csv"


def load_positive_control() -> str:
    """阳性对照 SMILES：优先 `assets/libraries/positive_control.csv`，其次内置苯甲脒。"""
    path = default_positive_control_path()
    try:
        if path.exists():
            mols = load_library_file(str(path))
            if mols:
                return mols[0]["smiles"]
    except Exception as e:  # noqa: BLE001
        logger.warning("阳性对照读取失败: %s", e)
    return DEFAULT_POSITIVE_CONTROL


def positive_control_info() -> Dict[str, Any]:
    """阳性对照的名称、SMILES 与来源路径。"""
    smiles = load_positive_control()
    name = POSITIVE_CONTROL_NAME
    try:
        mols = load_library_file(str(default_positive_control_path()))
        if mols:
            name = mols[0].get("name") or name
    except Exception as e:  # noqa: BLE001
        logger.debug("读取阳性对照库失败（沿用默认名称）：%s", e)
    return {"name": name, "smiles": smiles, "source": str(default_positive_control_path())}


def resolve_molecules(ligands_text: str = "", molecule_file: str = "",
                      allow_example_fallback: bool = False) -> Tuple[List[Dict[str, str]], str]:
    """返回 (分子列表, 来源标识)。优先级：文件 > 文本 > 示例库（仅在明确要求时）。"""
    if molecule_file:
        fmt, mols = read_molecule_file(molecule_file)
        if mols:
            return mols, f"file:{fmt}"
    if ligands_text and ligands_text.strip():
        mols = parse_smiles_text(ligands_text)
        if mols:
            return mols, "input"
    if allow_example_fallback:
        path = default_library_path()
        if path.exists():
            mols = load_library_file(str(path))
            if mols:
                return mols, "example-library"
    return [], "none"
