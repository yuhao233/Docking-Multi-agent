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

import pytest
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


def test_runtime_layout_guard_fails_loudly_on_wheel_install(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """wheel 安装（缺 `config/` / `web/` / `assets/`）必须**当场失败或响亮告警**。

    本系统按**源码检出**运行：前端、受体注册表、示例资源都在仓库里，不在 wheel 内。
    没有这道自检时，用户会看到「前端 404 / 找不到受体注册表」这类晦涩症状。
    """
    from docking_agent import paths

    monkeypatch.setenv("DOCKING_WORKSPACE", str(tmp_path))
    assert sorted(paths.missing_layout_dirs()) == ["assets", "config", "web"]
    with pytest.raises(RuntimeError, match="布局不完整"):
        paths.assert_runtime_layout(strict=True)
    paths.assert_runtime_layout(strict=False)          # 非严格：只告警，不打断
    for name in paths.LAYOUT_DIRS:
        (tmp_path / name).mkdir()
    assert paths.missing_layout_dirs() == []
    paths.assert_runtime_layout(strict=True)           # 补齐后通过


def test_runtime_layout_is_intentionally_outside_the_wheel() -> None:
    """打包口径**明确为源码检出 / editable 安装**（审计 3.8 的方案 B）。

    理由：`assets/cache`、`assets/uploads`、`config/local_settings.json`、`var/` 都是
    **运行期写入**的目录，塞进 site-packages 属于错误设计；因此**不提供 force-include**，
    而是在真正的服务入口（`cli -m http`）用 `strict=True` 当场失败，而不是等前端 404。
    这条用例把「声明」和「执行」绑在一起：谁只加一半（例如加了 force-include 却仍按
    文件系统路径读资源）都会在这里变红。
    """
    wheel = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "force-include" not in wheel, (
        "不要用 force-include 把运行期可写资源塞进 wheel；如需支持 wheel 安装，"
        "必须同时把 paths.py 改成 importlib.resources 解析并处理可写目录")
    cli = (PROJECT_DIR / "src" / "docking_agent" / "cli.py").read_text(encoding="utf-8")
    assert "assert_runtime_layout(strict=True)" in cli, "HTTP 服务入口必须严格自检运行布局"
    readme = (PROJECT_DIR / "README.md").read_text(encoding="utf-8")
    assert "editable" in readme and "-e ." in readme, "README 必须写明只支持 editable 安装"
