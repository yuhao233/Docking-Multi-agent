"""打包与依赖一致性（P0 规范改造）。

真实缺陷（本轮修复）：项目此前**没有安装进自己的 venv**（`find_spec('docking_agent')` 为 None），
`[project.scripts] docking-agent` 在开发环境完全不可用，所有入口都靠 `PYTHONPATH=src` 兜着；
依赖还在 `pyproject.toml` 与 `requirements-local.txt` 两处各写一份、无人看守。

这里把三件事固定成回归：
1. 两份依赖清单必须集合一致（改一处忘另一处会立刻红）；
2. 本包可导入、版本与 pyproject 一致、`paths.project_root()` 指向仓库内的 `projects/`
   （部署层依赖这条：运行产物与 assets 都按它定位）；
3. `[project.scripts]` 声明的入口真实存在且可调用。
"""
from __future__ import annotations

import importlib
import importlib.metadata as md
import tomllib
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((PROJECT_DIR / "pyproject.toml").read_text(encoding="utf-8"))


def test_requirements_local_mirrors_pyproject() -> None:
    declared = set(_pyproject()["project"]["dependencies"])
    mirrored = {
        line.strip() for line in (PROJECT_DIR / "requirements-local.txt")
        .read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert declared == mirrored, (
        f"依赖清单不一致：pyproject 多 {sorted(declared - mirrored)}、"
        f"requirements-local 多 {sorted(mirrored - declared)}")


def test_package_is_installed_and_paths_point_into_repo() -> None:
    from docking_agent.paths import project_root, workspace_dir

    module = importlib.import_module("docking_agent")
    assert Path(module.__file__).resolve().is_relative_to(PROJECT_DIR), module.__file__
    assert project_root() == PROJECT_DIR, f"project_root() 应指向仓库内的 projects/：{project_root()}"
    assert workspace_dir() == PROJECT_DIR or str(workspace_dir()).startswith(str(PROJECT_DIR))
    installed = md.version("docking-agent")
    assert installed == _pyproject()["project"]["version"], (installed, _pyproject()["project"]["version"])


def test_console_script_entrypoint_is_importable() -> None:
    scripts = _pyproject()["project"].get("scripts") or {}
    assert scripts, "必须在 [project.scripts] 声明命令行入口"
    for name, target in scripts.items():
        module_name, _, attr = target.partition(":")
        module = importlib.import_module(module_name)
        entry = getattr(module, attr, None)
        assert callable(entry), f"{name} → {target} 不可调用"
