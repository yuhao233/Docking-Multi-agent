"""项目路径解析。

以本文件位置反推项目根目录（`projects/`），因此无论从哪个 cwd 启动都能定位资源。
可用 `DOCKING_WORKSPACE`（兼容 `COZE_WORKSPACE_PATH`）覆盖工作区根目录。

目录约定：
    assets/libraries/    分子库与阳性对照
    assets/receptors/    预置受体（registry/ 为可对接受体，structures/ 为原始 PDB）
    assets/cache/        在线下载与现场准备的缓存
    assets/uploads/      用户上传文件
    config/              LLM 与受体注册表配置
    web/                 前端静态资源
    var/runs/            每次运行的完整中间数据（含产物清单）
    var/logs/            日志
"""
from __future__ import annotations

import os
from pathlib import Path

# src/docking_agent/paths.py -> parents[0]=docking_agent, [1]=src, [2]=projects
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]


def project_root() -> Path:
    return PROJECT_ROOT


def workspace_dir() -> Path:
    """工作区根目录（默认即项目根目录）。"""
    env = os.getenv("DOCKING_WORKSPACE") or os.getenv("COZE_WORKSPACE_PATH")
    if env:
        return Path(env).expanduser().resolve()
    return PROJECT_ROOT


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def assets_dir() -> Path:
    return workspace_dir() / "assets"


def libraries_dir() -> Path:
    return assets_dir() / "libraries"



def cache_dir() -> Path:
    """在线下载 / 现场准备（受体、配体文件）的缓存目录。"""
    return _ensure(assets_dir() / "cache")


def uploads_dir() -> Path:
    return _ensure(assets_dir() / "uploads")



def web_dir() -> Path:
    return workspace_dir() / "web"


def var_dir() -> Path:
    return _ensure(workspace_dir() / "var")


def runs_dir() -> Path:
    """每次运行的中间数据目录。"""
    return _ensure(var_dir() / "runs")


def outputs_dir() -> Path:
    """扁平产物目录（兼容旧 /files 接口与报告工具）。"""
    return _ensure(var_dir() / "outputs")


def logs_dir() -> Path:
    return _ensure(var_dir() / "logs")
