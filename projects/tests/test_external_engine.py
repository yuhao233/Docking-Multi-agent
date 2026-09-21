"""外部对接引擎（用户自行安装）：登记、探测、拒绝启动语义与调用形状。

对应缺陷/风险：GPU 版对接工具随项目分发不现实，若做成"设置了路径但悄悄不用"，用户会以为
在用 GPU 而实际拿到 CPU 结果。因此这里把三条边界固定成回归：

1. 未提供路径 → 明确"未配置"，继续用内置 CPU Vina；
2. 提供了路径但探测不通过 → `require_engine()` 抛错，调用方**拒绝启动**并展示补齐方法；
3. 提供了路径且探测通过 → 识别引擎类型与版本，并可生成调用参数（P1 执行适配使用）。

探测本身不 mock：用临时目录里真实的可执行脚本模拟三类 CLI 的版本输出。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

from docking_agent.core import external_tools as ET

# --------------------------------------------------------------------------- #
# 脚手架：造一个"像那么回事"的外部引擎可执行文件
# --------------------------------------------------------------------------- #
_FAKE_SCRIPTS = {
    "unidock": "Uni-Dock v1.2.0 (CUDA 12.2)\n",
    "vina-gpu": "AutoDock-Vina-GPU 2.1\nQuickVina2-GPU 2.1\n",
    "autodock-gpu": "AutoDock-GPU version 1.6 (OpenCL)\n",
    "unknown": "some-docking-tool 0.1\n",
}


def _fake_binary(tmp_path: Path, kind: str, *, executable: bool = True) -> Path:
    path = tmp_path / f"fake_{kind}"
    path.write_text(f'#!/bin/sh\necho "{_FAKE_SCRIPTS[kind]}"\n', encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)
    return path


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """本模块自带的 API 客户端（与 tests/test_settings.py 同一套最小环境变量）。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-test-secret-1234")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    from fastapi.testclient import TestClient

    from docking_agent.api.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (ET.ENV_DOCKING_BIN, ET.ENV_GPU_DEVICE, ET.ENV_GPU_BATCH):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# 1) 未配置：明确报"未配置"，不抛错
# --------------------------------------------------------------------------- #
def test_not_configured_uses_builtin(clean_env: None) -> None:
    report = ET.collect()
    assert report["configured"] is False
    assert report["ok"] is True and report["state"] == "not_configured"
    assert "内置" in report["message"]
    assert ET.require_engine() == {}


# --------------------------------------------------------------------------- #
# 2) 探测：路径 / 权限 / 类型识别
# --------------------------------------------------------------------------- #
def test_missing_path_is_invalid(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ET.ENV_DOCKING_BIN, str(tmp_path / "nope"))
    report = ET.collect(check_gpu=False)
    assert report["ok"] is False and report["state"] == "invalid"
    assert "不存在" in report["reason"]
    assert report["hint"]


def test_directory_is_invalid(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ET.ENV_DOCKING_BIN, str(tmp_path))
    report = ET.collect(check_gpu=False)
    assert report["ok"] is False
    assert "目录" in report["reason"]


def test_non_executable_is_invalid(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = _fake_binary(tmp_path, "unidock", executable=False)
    monkeypatch.setenv(ET.ENV_DOCKING_BIN, str(path))
    report = ET.collect(check_gpu=False)
    assert report["ok"] is False
    assert "执行权限" in report["reason"] and "chmod" in report["hint"]


@pytest.mark.parametrize("kind", ["unidock", "vina-gpu", "autodock-gpu"])
def test_recognizes_known_flavors(clean_env: None, monkeypatch: pytest.MonkeyPatch,
                                 tmp_path: Path, kind: str) -> None:
    path = _fake_binary(tmp_path, kind)
    monkeypatch.setenv(ET.ENV_DOCKING_BIN, str(path))
    version = ET.run_version(str(path))
    assert version["ok"] is True
    assert version["flavor"] == kind
    report = ET.collect(check_gpu=False)          # GPU 状态单列，便于沙箱/容器里断言识别结果
    assert report["flavor"] == kind
    assert report["flavor_label"] == ET._flavor_label(kind)
    assert report["argv_style"] == ET.FLAVOR_ARGV_STYLE[kind]


def test_unknown_flavor_is_rejected(clean_env: None, monkeypatch: pytest.MonkeyPatch,
                                    tmp_path: Path) -> None:
    path = _fake_binary(tmp_path, "unknown")
    monkeypatch.setenv(ET.ENV_DOCKING_BIN, str(path))
    report = ET.collect(check_gpu=False)
    assert report["ok"] is False and report["state"] == "unrecognized"
    assert report["hint"]


# --------------------------------------------------------------------------- #
# 3) 拒绝启动：已配置但不可用 → 抛错并带补齐方法
# --------------------------------------------------------------------------- #
def test_require_engine_refuses_when_configured_but_broken(
        clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ET.ENV_DOCKING_BIN, str(tmp_path / "gone"))
    with pytest.raises(ET.ExternalEngineError) as excinfo:
        ET.require_engine()
    assert "外部对接引擎不可用" in str(excinfo.value)
    assert excinfo.value.details.get("state") == "invalid"


def test_run_refuses_before_docking(clean_env: None, monkeypatch: pytest.MonkeyPatch,
                                    tmp_path: Path) -> None:
    """对接入口必须先校验外部引擎：配置错误时不得进入任何计算。"""
    monkeypatch.setenv(ET.ENV_DOCKING_BIN, str(tmp_path / "gone"))
    from docking_agent.core.docking import dock_library

    with pytest.raises(ET.ExternalEngineError):
        dock_library([{"name": "乙醇", "smiles": "CCO"}])


# --------------------------------------------------------------------------- #
# 4) 调用形状：三类引擎的 argv（执行适配按此生成，避免运行期再猜参数）
# --------------------------------------------------------------------------- #
def test_build_argv_per_flavor() -> None:
    common: Dict[str, Any] = dict(
        binary="/opt/unidock", receptor="/tmp/rec.pdbqt", ligands=["/tmp/a.pdbqt", "/tmp/b.pdbqt"],
        out_dir="/tmp/out", center=[1.0, 2.0, 3.0], size=[20.0, 20.0, 20.0],
        exhaustiveness=8, n_poses=3, seed=42)
    uni = ET.build_argv("unidock", **common)
    assert uni[0] == "/opt/unidock" and "--gpu_batch" in uni and "--receptor" in uni
    assert "/tmp/a.pdbqt" in uni and "--center_x" in uni and "--size_z" in uni
    vg = ET.build_argv("vina-gpu", **common)
    assert "--ligand_directory" in vg and "--thread" in vg
    adg = ET.build_argv("autodock-gpu", **common)
    assert "--nrun" in adg and "--ligand_directory" in adg
    with pytest.raises(ET.ExternalEngineError):
        ET.build_argv("", **common)


def test_gpu_device_and_batch_from_env(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ET.ENV_GPU_DEVICE, "3")
    monkeypatch.setenv(ET.ENV_GPU_BATCH, "250")
    assert ET.gpu_device() == 3
    assert ET.gpu_batch_size() == 250


# --------------------------------------------------------------------------- #
# 5) 设置页与 doctor 的一致性
# --------------------------------------------------------------------------- #
def test_settings_exposes_external_group() -> None:
    from docking_agent.settings import SPEC_BY_PATH, SPECS

    for path in ("external.docking_bin", "external.gpu_device", "external.gpu_batch_size",
                 "external.p2rank_home", "external.pdb2pqr_bin"):
        assert path in SPEC_BY_PATH, f"设置页缺少 {path}"
        assert SPEC_BY_PATH[path].env, f"{path} 必须映射到环境变量"
    assert [s.path for s in SPECS if s.group == "external"], "external 分组应当非空"


def test_probe_api_and_doctor_agree(client: Any) -> None:
    """`/api/tools/probe` 必须给出三块结果，字段与 doctor 的能力项同名同义。"""
    r = client.post("/api/tools/probe")
    assert r.status_code == 200
    data = r.json()
    for key in ("engine", "p2rank", "pdb2pqr"):
        assert key in data, f"probe 缺少 {key}"
    engine = data["engine"]
    assert "configured" in engine and "state" in engine


def test_doctor_reports_external_engine_row() -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    try:
        import doctor_probe  # type: ignore[import-not-found]

        report = doctor_probe.collect(online=False)
        keys = {item["key"] for item in report["items"]}
        assert "external_engine" in keys
        row = next(i for i in report["items"] if i["key"] == "external_engine")
        assert row["required"] is False, "外部引擎是可选能力（未提供时用内置 CPU Vina）"
        assert row["ok"] is False or row["detail"], row
    finally:
        sys.path.pop(0)
