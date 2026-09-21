"""回归：上传成功的小分子库在对话里必须能被读到（真实缺陷 20260917-112206-5017）。

事件经过：
  - 用户上传 PGR.sdf（上传响应 count=149），落盘为
    `assets/uploads/20260917-112109-3e5739-PGR.sdf`；
  - 协调 Agent 只拿到**显示名** `PGR.sdf`，把它当路径传给 import_molecule_library，
    RDKit 报 `Bad input file PGR.sdf`，随后又猜了 3 个错误路径，最终退回示例分子库；
  - 对接没跑，用户被要求重新提供文件。

两处根因（本文件分别回归）：
  1. 受理层在对话模式（advanced=false）下用系统默认参数，把请求里**真实存在的**
     `molecule_file` 丢掉了，给编排层的指令变成「使用示例分子库」；
  2. 工具层没有「裸文件名 → 上传/缓存目录里的真实文件」的解析兜底，也不回传
     「实际尝试过的路径与失败原因」。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

from docking_agent.agents.blackboard import Blackboard, current_blackboard  # noqa: E402
from docking_agent.runs import Run, current_run  # noqa: E402


def _write_sdf(directory: Path, filename: str = "20260917-112109-3e5739-PGR.sdf",
               names=("aspirin", "caffeine", "ibuprofen")) -> Path:
    """用 RDKit 生成**互不相同**的多记录 SDF（名称写入 `_Name`）。

    注意不能用同一套原子手写 3 个记录：统一归一化层按 canonical SMILES 去重，
    相同的分子会被合并成 1 条，测不出「读到全部 N 个」。
    """
    from rdkit import Chem
    from rdkit.Chem import AllChem

    smiles_map = {
        "aspirin": "CC(=O)Oc1ccccc1C(=O)O",
        "caffeine": "Cn1cnc2c1c(=O)n(C)c(=O)n2C",
        "ibuprofen": "CC(C)Cc1ccc(C(C)C(=O)O)cc1",
    }
    blocks = []
    for name in names:
        mol = Chem.MolFromSmiles(smiles_map.get(name, "CCO"))
        assert mol is not None
        AllChem.Compute2DCoords(mol)
        mol.SetProp("_Name", name)
        blocks.append(Chem.MolToMolBlock(mol) + "$$$$\n")
    path = directory / filename
    path.write_text("".join(blocks), encoding="utf-8")
    return path


def test_looks_like_molecule_path_distinguishes_smiles() -> None:
    from docking_agent.tools.dispatch import looks_like_molecule_path

    assert looks_like_molecule_path("PGR.sdf") is True
    assert looks_like_molecule_path("assets/uploads/PGR.sdf") is True
    assert looks_like_molecule_path("https://x.test/lib.sdf") is True
    # 常见 SMILES 不能被误判成路径
    assert looks_like_molecule_path("CCO") is False
    assert looks_like_molecule_path("CC(=O)Oc1ccccc1C(=O)O") is False
    assert looks_like_molecule_path("") is False


def test_resolve_bare_filename_matches_timestamped_upload(tmp_path, monkeypatch) -> None:
    """上传端点的落盘名带时间戳前缀；裸文件名必须能解析到它。"""
    from docking_agent.tools import dispatch

    uploaded = _write_sdf(tmp_path, "20260917-112109-3e5739-PGR.sdf")
    monkeypatch.setattr(dispatch, "molecule_search_dirs", lambda: [tmp_path])

    resolved, attempts, candidates = dispatch.resolve_molecule_file("PGR.sdf")
    assert Path(resolved) == uploaded.resolve()
    assert candidates == []
    # 精确文件名未命中会被如实记录（供失败时上报），但后缀匹配成功返回
    assert any("PGR.sdf" in a for a in attempts)

    # 无扩展名的词干也要能命中
    resolved2, _, candidates2 = dispatch.resolve_molecule_file("PGR")
    assert Path(resolved2) == uploaded.resolve() and candidates2 == []


def test_resolve_ambiguous_suffix_returns_candidates_without_guessing(tmp_path, monkeypatch) -> None:
    """多个候选命中同一后缀时必须返回候选清单、不得随便取一个。"""
    from docking_agent.tools import dispatch

    _write_sdf(tmp_path, "20260917-112109-3e5739-PGR.sdf", names=("aspirin",))
    _write_sdf(tmp_path, "20260918-090000-aaaaaa-PGR.sdf", names=("caffeine",))
    monkeypatch.setattr(dispatch, "molecule_search_dirs", lambda: [tmp_path])

    resolved, attempts, candidates = dispatch.resolve_molecule_file("PGR.sdf")
    assert resolved == "PGR.sdf"          # 不猜
    assert len(candidates) == 2
    assert attempts and "无法确定" in attempts[-1]


def test_resolve_reports_every_attempted_path_with_reason(tmp_path, monkeypatch) -> None:
    from docking_agent.tools import dispatch

    monkeypatch.setattr(dispatch, "molecule_search_dirs", lambda: [tmp_path])
    resolved, attempts, candidates = dispatch.resolve_molecule_file("not_here.sdf")
    assert resolved == "not_here.sdf" and candidates == []
    assert attempts, "必须逐条记录尝试过的路径"
    assert any("not_here.sdf" in a and "不存在" in a for a in attempts)


def test_import_molecule_library_accepts_bare_filename(tmp_path, monkeypatch) -> None:
    """真实缺陷主场景：只给显示名 PGR.sdf，也要读到上传的 3 个分子。"""
    from docking_agent.tools import dispatch

    _write_sdf(tmp_path, names=("aspirin", "caffeine", "ibuprofen"))
    monkeypatch.setattr(dispatch, "molecule_search_dirs", lambda: [tmp_path])

    out = json.loads(dispatch.import_molecule_library.invoke({"molecule_file": "PGR.sdf"}))
    assert out["status"] == "ok"
    assert out["source"] == "file"
    assert [m["name"] for m in out["molecules"]] == ["aspirin", "caffeine", "ibuprofen"]


def test_import_molecule_library_treats_path_in_query_or_text_as_file(tmp_path, monkeypatch) -> None:
    """模型把路径塞进 query_or_text 时，不能被当成 SMILES 文本解析。"""
    from docking_agent.tools import dispatch

    _write_sdf(tmp_path, names=("aspirin", "caffeine"))
    monkeypatch.setattr(dispatch, "molecule_search_dirs", lambda: [tmp_path])

    out = json.loads(dispatch.import_molecule_library.invoke({"query_or_text": "PGR.sdf"}))
    assert out["status"] == "ok" and out["source"] == "file"
    assert [m["name"] for m in out["molecules"]] == ["aspirin", "caffeine"]


def test_import_molecule_library_falls_back_to_request_molecule_file(tmp_path, monkeypatch) -> None:
    """「上传成功 = 对话里一定能用」：模型一个参数都不传，也要用上传的库。"""
    from docking_agent.tools import dispatch

    uploaded = _write_sdf(tmp_path, names=("aspirin", "caffeine", "ibuprofen"))
    monkeypatch.setattr(dispatch, "molecule_search_dirs", lambda: [tmp_path])

    run = Run(tmp_path, "R-UP", "agent", {"mode": "chat", "molecule_file": str(uploaded)})
    run_token = current_run.set(run)
    board_token = current_blackboard.set(Blackboard("R-UP"))
    try:
        out = json.loads(dispatch.import_molecule_library.invoke({}))
    finally:
        current_blackboard.reset(board_token)
        current_run.reset(run_token)
    assert out["status"] == "ok" and out["source"] == "file"
    assert len(out["molecules"]) == 3


def test_import_molecule_library_failure_lists_attempts(tmp_path, monkeypatch) -> None:
    from docking_agent.tools import dispatch

    monkeypatch.setattr(dispatch, "molecule_search_dirs", lambda: [tmp_path])
    out = json.loads(dispatch.import_molecule_library.invoke({"molecule_file": "/no/such/lib.sdf"}))
    assert out["status"] == "no_molecules"
    assert out["molecules"] == []
    assert out["attempted"], "失败时必须逐条列出实际尝试过的路径与失败原因"
    assert any("lib.sdf" in a for a in out["attempted"])


def test_intake_chat_mode_carries_uploaded_molecule_file(tmp_path) -> None:
    """受理层根因：对话模式（advanced=false）不得丢掉请求里的 molecule_file。"""
    from docking_agent import intake
    from docking_agent.api.schemas import AgentRequest

    uploaded = _write_sdf(tmp_path, names=("aspirin",))
    req = AgentRequest(
        message="使用上传的受体结构与小分子库进行对接，输出最好带上小分子的ID",
        mode="chat", advanced=False,
        receptor_file=str(tmp_path / "r.pdbqt"),
        molecule_file=str(uploaded),
    )
    message, spec = intake.build_message(req, allow_llm=False)

    assert spec["ligands"]["source"] == "file"
    assert spec["ligands"]["file"] == str(uploaded)
    # 指令里必须出现**绝对路径**，且明确要求原样传给 import_molecule_library
    assert str(uploaded) in message
    assert "import_molecule_library" in message
    assert "使用示例分子库" not in message


def test_intake_chat_mode_without_ligands_does_not_auto_use_example_library() -> None:
    """没有分子库时**不再**默认提示示例库回退，而是要求向用户索取（2026-09-17 产品要求）。"""
    from docking_agent import intake
    from docking_agent.api.schemas import AgentRequest

    req = AgentRequest(message="帮我筛一批分子", mode="chat", advanced=False)
    message, spec = intake.build_message(req, allow_llm=False)
    assert spec["ligands"]["source"] == "library"
    assert "不要擅自使用内置示例库" in message, message
    assert "请向用户索取候选分子库" in message, message


def _write_big_sdf(directory: Path, count: int = 120) -> Path:
    """生成 count 个互不相同、带 `_Name` 的分子（模拟用户上传的小分子库）。"""
    from rdkit import Chem
    from rdkit.Chem import AllChem

    blocks = []
    for i in range(count):
        mol = Chem.MolFromSmiles("C" * (i + 1) + "O")
        assert mol is not None
        AllChem.Compute2DCoords(mol)
        mol.SetProp("_Name", f"PGR{i + 1:03d}")
        blocks.append(Chem.MolToMolBlock(mol) + "$$$$\n")
    path = directory / f"PGR_{count}.sdf"
    path.write_text("".join(blocks), encoding="utf-8")
    return path


def test_upload_count_equals_import_count_and_ids_survive(tmp_path) -> None:
    """端到端不变量：`/api/uploads` 解析出的分子数 == `import_molecule_library` 读到的分子数，
    且 SDF 的 `_Name` 作为 ID 贯穿（用户明确要求「输出最好带上小分子的ID」）。"""
    import json as _json

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from docking_agent.api.app import app
    from docking_agent.tools import dispatch

    big = _write_big_sdf(tmp_path, 120)
    with TestClient(app) as client, open(big, "rb") as fh:
        r = client.post("/api/uploads",
                        files={"file": (big.name, fh, "application/octet-stream")},
                        data={"kind": "ligand"})
    assert r.status_code == 200, r.text[:300]
    uploaded = r.json()
    assert uploaded["pending"] is True and "count" not in uploaded, "上传不解析（v0.22）"

    with TestClient(app) as client:
        up = client.post("/api/uploads/inspect",
                         json={"path": uploaded["path"], "kind": "ligand"}).json()
    assert up["count"] == 120
    assert up["input_normalization"]["records_ok"] == 120

    out = _json.loads(dispatch.import_molecule_library.invoke({"molecule_file": uploaded["path"]}))
    assert out["status"] == "ok"
    assert out["molecules_total"] == up["count"] == 120
    assert out["summary"]["first_names"][0] == "PGR001"
    assert out["input_normalization"]["records_ok"] == 120


def test_import_ambiguous_suffix_asks_user_with_candidates(tmp_path, monkeypatch) -> None:
    """歧义时工具不得猜：返回候选清单并置 needs_user_input。"""
    from docking_agent.tools import dispatch

    _write_sdf(tmp_path, "20260917-112109-3e5739-PGR.sdf", names=("aspirin",))
    _write_sdf(tmp_path, "20260918-090000-bbbbbb-PGR.sdf", names=("caffeine",))
    monkeypatch.setattr(dispatch, "molecule_search_dirs", lambda: [tmp_path])

    out = json.loads(dispatch.import_molecule_library.invoke({"molecule_file": "PGR.sdf"}))
    assert out["status"] == "no_molecules"
    assert out["needs_user_input"] is True
    assert len(out["candidates"]) == 2


def test_normalize_molecule_library_accepts_text_file_and_blackboard(tmp_path) -> None:
    """统一入口：属性评估的规范化工具也要能吃「脏」输入，而不是 JSONDecodeError
    （真实缺陷：子 Agent 传非 JSON 文本 → `分子库规范化失败`）。"""
    from docking_agent.tools.properties import normalize_molecule_library

    # ① 自由文本（名称:SMILES / 名称 SMILES）
    text_out = json.loads(normalize_molecule_library.invoke(
        {"molecules_json": "阿司匹林:CC(=O)Oc1ccccc1C(=O)O\n乙醇 CCO"}))
    assert text_out["status"] == "ok" and text_out["count"] == 2
    assert {m["name"] for m in text_out["molecules"]} == {"阿司匹林", "乙醇"}

    # ② 文件路径
    sdf = _write_sdf(tmp_path, names=("aspirin", "caffeine"))
    file_out = json.loads(normalize_molecule_library.invoke({"molecules_json": str(sdf)}))
    assert file_out["status"] == "ok" and file_out["count"] == 2
    assert {m["id"] for m in file_out["molecules"]} == {"aspirin", "caffeine"}

    # ③ 留空 → 共享黑板（不是报错）
    board = Blackboard("R-NORM")
    board.add_molecules([{"name": "乙醇", "smiles": "CCO"}])
    token = current_blackboard.set(board)
    try:
        empty_out = json.loads(normalize_molecule_library.invoke({"molecules_json": ""}))
    finally:
        current_blackboard.reset(token)
    assert empty_out["status"] == "ok" and empty_out["count"] == 1
    assert empty_out["molecules"][0]["name"] == "乙醇"


def test_upload_sniffs_structure_content_when_extension_unknown() -> None:
    """扩展名不认识（.dat）但内容是 PDB → 必须按受体处理（内容嗅探优先于扩展名）。"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from docking_agent.api.app import app

    pdb = PROJECT_ROOT / "assets" / "receptors" / "structures" / "thrombin.pdb"
    assert pdb.is_file()
    with TestClient(app) as client, open(pdb, "rb") as fh:
        r = client.post("/api/uploads",
                        files={"file": ("mystery.dat", fh, "application/octet-stream")},
                        data={"kind": "auto"})
        assert r.status_code == 200, r.text[:300]
        uploaded = r.json()
        # 上传阶段只做**内容嗅探**（判断 kind），不做准备
        assert uploaded["kind"] == "receptor" and uploaded["pending"] is True
        assert "receptor_file" not in uploaded
        body = client.post("/api/uploads/inspect",
                           json={"path": uploaded["path"], "kind": "receptor"}).json()
    assert body["kind"] == "receptor"
    assert body["receptor_format"] == "pdb"
    assert body["receptor_file"].endswith(".pdbqt")
    assert body["input_normalization"]["format"] == "pdb"
