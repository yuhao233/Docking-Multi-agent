"""受体结构文件扩展名（.ent / .cif / 未知后缀）与「上传 → 对接」链路的回归测试。

真实缺陷（2026-09-16 用户报告）：
  对话模式上传 `pdb2gs3.ent`（GPX4，RCSB 的 .ent 坐标文件）后：
    1. 前端 advanced=false 时把 `receptor_file` 从请求体里丢掉（见 web/app.js 与
       scripts/ui_e2e.js 的附件用例）；
    2. 即使路径到了后端，`resolve_receptor_specs()` 只判断 `key.endswith(".pdb")`，
       `.ent` 落到「未识别受体 → 回退默认 凝血酶(thrombin)」——同样的回退再次发生。

本文件守住第 2 条，以及「上传端点与受体解析链共用同一份扩展名定义」。包含真实的
meeko 现场准备与一次真实对接（1 个分子，exhaustiveness=1），耗时可控。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from support.fake_llm import coordinator_script_for_form, run_agent  # noqa: E402

RECEPTOR_PDB = PROJECT_ROOT / "assets" / "receptors" / "structures" / "thrombin.pdb"
THROMBIN_SITE_X = 31.5      # 凝血酶共晶配体(MIT)质心的 x 分量（见 test_upload_report.py）


@pytest.fixture(scope="module")
def client() -> Any:
    from docking_agent.api.app import app

    with TestClient(app) as c:
        yield c


def _copy_as(tmp_path: Path, name: str) -> Path:
    """把测试用 PDB 复制成指定文件名（内容不变 → 命中同一份准备缓存）。"""
    assert RECEPTOR_PDB.is_file(), f"缺少测试用受体结构：{RECEPTOR_PDB}"
    dest = tmp_path / name
    shutil.copyfile(RECEPTOR_PDB, dest)
    return dest


def _upload(client, path: Path, kind: str = "auto"):
    """上传（v0.22 起只保存文件，不解析/不准备）。"""
    with open(path, "rb") as fh:
        return client.post("/api/uploads",
                           files={"file": (path.name, fh, "application/octet-stream")},
                           data={"kind": kind})


def _inspect(client, payload: dict):
    """用户主动「校验文件」：这一步才做现场准备（等价于旧版上传端点的返回）。"""
    return client.post("/api/uploads/inspect",
                       json={"path": payload["path"], "kind": payload.get("kind") or "auto"})


def _run_agent(client, body: dict, monkeypatch) -> str:
    """走标准 Agent Protocol 路径（假 LLM 驱动真实多 Agent 编排），返回业务 run_id。"""
    return run_agent(client, coordinator_script_for_form(body), body, monkeypatch)


# --------------------------------------------------------------------------- #
# ① .ent（及其它结构后缀）必须被当作「用户提供的结构文件」现场准备
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["thrombin.ent", "thrombin.ENT", "thrombin.pdb1",
                                  "thrombin.structure"])
def test_resolve_receptor_specs_prepares_structure_file(tmp_path, name) -> None:
    """`.ent`（大小写不敏感）与其它结构后缀都要现场准备，**不得**落回默认受体。"""
    from docking_agent.core.receptors import resolve_receptor_specs

    source = _copy_as(tmp_path, name)
    specs, notes = resolve_receptor_specs([str(source)])

    spec = specs[0]
    assert spec.get("user_provided") is True, f"{name} 被当成了注册表/默认受体：{spec}"
    assert spec.get("key") != "thrombin" or spec.get("user_provided"), spec
    pdbqt = Path(str(spec.get("pdbqt") or ""))
    assert pdbqt.is_file() and pdbqt.suffix == ".pdbqt", f"未生成 PDBQT：{spec.get('pdbqt')}"
    # 位点盒来自共晶配体质心 → 证明真的走了 prepare_user_receptor，而不是默认受体
    assert abs(float(spec["center"][0]) - THROMBIN_SITE_X) < 1.0, spec["center"]
    joined = " ".join(notes)
    assert "已现场准备" in joined, joined
    assert "默认" not in joined, f"不应出现任何回退默认的说明：{joined}"


def test_receptor_ext_set_is_case_insensitive() -> None:
    """扩展名判定大小写不敏感（.ENT / .Pdbqt 与 .ent / .pdbqt 等价）。"""
    from docking_agent.core.receptors import (RECEPTOR_EXTS, RECEPTOR_PDBQT_EXTS,
                                              RECEPTOR_STRUCTURE_EXTS, receptor_ext)

    assert receptor_ext("/a/b/PROTEIN.ENT") == ".ent"
    assert receptor_ext("PROTEIN.PDBQT") == ".pdbqt"
    assert ".ent" in RECEPTOR_STRUCTURE_EXTS and ".pdb1" in RECEPTOR_STRUCTURE_EXTS
    assert ".cif" in RECEPTOR_STRUCTURE_EXTS and ".mmcif" in RECEPTOR_STRUCTURE_EXTS
    assert RECEPTOR_EXTS == RECEPTOR_STRUCTURE_EXTS | RECEPTOR_PDBQT_EXTS


def test_ligand_extensions_are_not_guessed_as_receptor(tmp_path) -> None:
    """已知的非结构后缀（小分子库等）不得被猜成受体结构（按未识别处理）。"""
    from docking_agent.core.receptors import is_structure_source, resolve_receptor_specs

    sdf = tmp_path / "ligands.sdf"
    sdf.write_text("CCO\n", encoding="utf-8")
    assert is_structure_source(str(sdf)) is False

    specs, notes = resolve_receptor_specs([str(sdf)])
    assert specs[0].get("user_provided") is None, "小分子文件不得被当作受体准备"
    assert any("未识别受体" in n for n in notes), notes


# --------------------------------------------------------------------------- #
# ② 不存在的 / 不可用的结构文件 → 回退默认，但 note 必须写明原因
# --------------------------------------------------------------------------- #
def test_missing_structure_file_falls_back_with_reason(tmp_path) -> None:
    from docking_agent.core.receptors import resolve_receptor_specs

    missing = tmp_path / "definitely_missing.ent"
    specs, notes = resolve_receptor_specs([str(missing)])

    assert specs[0].get("key") == "thrombin" and not specs[0].get("user_provided")
    joined = " ".join(notes)
    assert "无法作为受体结构使用" in joined, joined
    assert "不存在" in joined, f"note 要写清原因：{joined}"
    assert "这不是你指定的受体" in joined, f"必须明说回退的不是用户指定的受体：{joined}"


def test_unusable_structure_file_falls_back_with_reason(tmp_path) -> None:
    """内容不是结构（只有一行文字）的 .ent：回退默认 + note 写明准备失败原因。"""
    from docking_agent.core.receptors import resolve_receptor_specs

    broken = tmp_path / "broken.ent"
    broken.write_text("this file is not a protein structure\n", encoding="utf-8")
    specs, notes = resolve_receptor_specs([str(broken)])

    assert specs[0].get("key") == "thrombin" and not specs[0].get("user_provided")
    joined = " ".join(notes)
    assert "broken.ent" in joined, joined
    assert "无法作为受体结构使用" in joined, joined
    assert "准备失败" in joined or "无法" in joined, joined
    assert "这不是你指定的受体" in joined, joined


# --------------------------------------------------------------------------- #
# ③ 上传 .ent → /api/uploads 返回 receptor_file，且该路径能被对接工具直接用
# --------------------------------------------------------------------------- #
def test_upload_ent_returns_pdbqt_used_by_docking(client, tmp_path, monkeypatch) -> None:
    ent = _copy_as(tmp_path, "thrombin.ent")

    up = _upload(client, ent, kind="receptor")
    assert up.status_code == 200, up.text[:400]
    assert up.json()["pending"] is True, "上传阶段只保存"
    body = _inspect(client, up.json()).json()
    assert body["kind"] == "receptor", body
    assert body["ext"] == ".ent", body
    uploaded = body["receptor_file"]
    assert uploaded and Path(uploaded).is_file(), f"应返回可用的 PDBQT：{body}"
    assert Path(uploaded).suffix == ".pdbqt", uploaded
    assert abs(float(body["box_center"][0]) - THROMBIN_SITE_X) < 1.0, body["box_center"]

    # 运行时提交的是**原始上传文件路径**（与界面一致；准备发生在运行阶段），
    # 必须用的就是它、绝不回退默认受体。
    run_id = _run_agent(client, {
        "receptor_file": up.json()["path"],
        "ligands_text": "乙醇:CCO",
        "exhaustiveness": 1, "engine": "vina", "save_poses": False,
    }, monkeypatch)
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["run"]["status"] == "ok", detail["run"]
    blocks = (detail["result"].get("docking") or {}).get("receptors") or []
    assert blocks, "应有对接结果"
    assert Path(blocks[0]["pdbqt"]).resolve() == Path(uploaded).resolve(), \
        f"最终对接用的不是上传产物：{blocks[0]['pdbqt']} != {uploaded}"
    assert abs(float(blocks[0]["box_center"][0]) - THROMBIN_SITE_X) < 1.0, blocks[0]["box_center"]
    assert "未指定受体" not in json.dumps(blocks[0].get("notes") or [], ensure_ascii=False)


def test_upload_mmcif_autodetected_as_receptor(client, tmp_path) -> None:
    """.cif 在上传端点（auto 模式）必须被认成受体，并现场准备出可用的 PDBQT。

    这条同时证明上传端点与解析链共用同一份扩展名集合（历史上两处各写一份而漂移）。
    """
    gemmi = pytest.importorskip("gemmi")
    cif = tmp_path / "thrombin.cif"
    structure = gemmi.read_structure(str(RECEPTOR_PDB))
    structure.setup_entities()
    structure.make_mmcif_document().write_file(str(cif))

    up = _upload(client, cif, kind="auto")
    assert up.status_code == 200, up.text[:400]
    assert up.json()["kind"] == "receptor", f".cif 应被自动识别为受体：{up.json()}"
    body = _inspect(client, up.json()).json()
    assert body["kind"] == "receptor", f".cif 应被自动识别为受体：{body}"
    assert Path(body["receptor_file"]).suffix == ".pdbqt"
    assert Path(body["receptor_file"]).is_file()
    assert abs(float(body["box_center"][0]) - THROMBIN_SITE_X) < 1.0, body["box_center"]


def test_upload_rejects_unusable_structure_with_reason(client, tmp_path) -> None:
    """不可用的 .ent 上传必须 400 且把原因说清（不得静默接受后按默认受体跑）。"""
    broken = tmp_path / "broken.ent"
    broken.write_text("not a structure\n", encoding="utf-8")
    up = _upload(client, broken, kind="receptor")
    assert up.status_code == 200 and up.json()["pending"] is True, "上传阶段不解析，故不报错"
    up = _inspect(client, up.json())
    assert up.status_code == 400, up.text[:300]
    detail = str(up.json().get("detail") or "")
    assert "受体文件准备失败" in detail, detail
    assert ".ent" in detail or ".pdb" in detail, f"错误里应列出支持的扩展名：{detail}"
