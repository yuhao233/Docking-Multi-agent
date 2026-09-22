"""历史运行检索：关掉页面后仍能找回之前的运行结果。

用户需求原话：「增加一下历史任务查询机制，让用户在关闭页面后也可以再查询之前的运行结果」。
运行记录本来就持久化在 `var/runs/<run_id>/`，这里补的是**检索能力**：关键词（run_id / 受体 /
任务描述 / 排序表里的分子名与 ID）、状态、类型、受体、时间范围、分页。

真实数据上实测（本机 3.4k 条运行）：首次建索引约 0.4 s，之后走进程内缓存（毫秒级）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from docking_agent.runs import get_run_store

# --------------------------------------------------------------------------- #
# 脚手架：在临时工作区里造 4 条真实结构的运行目录
# --------------------------------------------------------------------------- #
def _write_run(root: Path, run_id: str, *, status: str, receptor: str, created: str,
               molecules: str = "", goal: str = "") -> None:
    run_dir = root / "var" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {"run_id": run_id, "kind": "agent", "status": status, "created_at": created,
            "receptor_label": receptor, "molecule_count": 2,
            "request": {"message": goal or f"对接受体 {receptor}"},
            "notes": [f"本次使用受体 {receptor}"], "artifacts": [], "log": []}
    (run_dir / "run.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    if molecules:
        rows = ["rank,id,name,smiles,affinity_kcal_mol"]
        for i, name in enumerate(molecules.split(","), start=1):
            rows.append(f"{i},PGR{i:03d},{name},CCO,-5.0")
        (run_dir / "ranking.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")


@pytest.fixture()
def populated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("DOCKING_WORKSPACE", str(tmp_path))
    import docking_agent.runs as R

    R._store = None                       # 每个用例独立工作区：清掉 store 单例
    root = tmp_path
    _write_run(root, "20260921-100000-aaaa", status="ok", receptor="thrombin(1DWC)",
               created="2026-09-21T10:00:00", molecules="阿司匹林,布洛芬", goal="对接受体 thrombin")
    _write_run(root, "20260920-090000-bbbb", status="ok", receptor="trypsin(1PTU)",
               created="2026-09-20T09:00:00", molecules="姜黄素", goal="试试 trypsin")
    _write_run(root, "20260919-080000-cccc", status="no_op", receptor="",
               created="2026-09-19T08:00:00", goal="你好")
    _write_run(root, "20260918-070000-dddd", status="error", receptor="thrombin(1DWC)",
               created="2026-09-18T07:00:00", goal="受体文件缺失")
    return root


# --------------------------------------------------------------------------- #
# 1) 关键词：run_id / 受体 / 分子名 / 分子 ID / 多词 AND
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("query,expected", [
    ("20260921-100000-aaaa", ["20260921-100000-aaaa"]),
    ("阿司匹林", ["20260921-100000-aaaa"]),
    # 排序表里的分子 ID 也参与匹配（索引只读排序表前若干行，故各运行的首行 ID 都会命中）
    ("PGR001", ["20260921-100000-aaaa", "20260920-090000-bbbb"]),
    ("trypsin", ["20260920-090000-bbbb"]),
    ("thrombin 布洛芬", ["20260921-100000-aaaa"]),
    ("thrombin 姜黄素", []),
])
def test_keyword_search(populated: Path, query: str, expected: list) -> None:
    out = get_run_store().search(q=query, limit=10)
    assert [r["run_id"] for r in out["runs"]] == expected, out


# --------------------------------------------------------------------------- #
# 2) 状态 / 类型 / 受体 / 时间范围
# --------------------------------------------------------------------------- #
def test_filters(populated: Path) -> None:
    store = get_run_store()
    assert store.search(status="ok")["total"] == 2
    assert store.search(status="no_op")["total"] == 1
    assert store.search(receptor="trypsin")["total"] == 1
    assert store.search(since="2026-09-20")["total"] == 2
    assert store.search(until="2026-09-19")["total"] == 2
    assert store.search(since="2026-09-19", until="2026-09-20")["total"] == 2
    assert store.search(kind="agent")["total"] == 4


def test_pagination_and_shape(populated: Path) -> None:
    store = get_run_store()
    page1 = store.search(limit=2, offset=0)
    page2 = store.search(limit=2, offset=2)
    assert page1["total"] == page2["total"] == 4
    assert len(page1["runs"]) == 2 and len(page2["runs"]) == 2
    assert {r["run_id"] for r in page1["runs"]} & {r["run_id"] for r in page2["runs"]} == set()
    assert page1["offset"] == 0 and page1["limit"] == 2
    assert set(page1["runs"][0]) == {"run_id", "created_at", "status", "kind", "receptor",
                                     "molecule_count"}, "索引行不应把内部检索文本暴露给前端"
    assert page1["runs"][0]["run_id"] == "20260921-100000-aaaa", "默认按时间倒序"


def test_search_results_are_loadable_by_detail_api(populated: Path) -> None:
    """检索命中的 run_id 必须能直接用于 `/api/runs/{id}` 载入（用户点「载入」的路径）。"""
    store = get_run_store()
    hit = store.search(q="阿司匹林")["runs"][0]["run_id"]
    detail = store.detail(hit)
    assert detail is not None and detail["run"]["status"] == "ok"


# --------------------------------------------------------------------------- #
# 3) HTTP 层：带检索参数返回分页结构，不带参数保持旧结构
# --------------------------------------------------------------------------- #
def test_api_runs_search_and_backward_compat(populated: Path) -> None:
    from fastapi.testclient import TestClient

    from docking_agent.api.app import app

    with TestClient(app) as client:
        legacy = client.get("/api/runs", params={"limit": 2}).json()
        assert "runs" in legacy and "total" not in legacy, "不带检索参数时保持旧结构"
        assert len(legacy["runs"]) == 2

        found = client.get("/api/runs", params={"q": "阿司匹林", "limit": 5}).json()
        assert found["total"] == 1
        assert found["runs"][0]["run_id"] == "20260921-100000-aaaa"
        assert found["query"]["q"] == "阿司匹林"

        empty = client.get("/api/runs", params={"q": "不存在的分子"}).json()
        assert empty["total"] == 0 and empty["runs"] == []


# --------------------------------------------------------------------------- #
# 4) 被中断的运行必须收尾：进程重启后不可能还有运行在执行
# --------------------------------------------------------------------------- #
def test_reconcile_marks_stale_running_runs_as_interrupted(populated: Path) -> None:
    """残留的 running 只能来自进程被杀/崩溃 —— 收尾为 interrupted 并留下说明。

    真实缺陷：用户看到历史里某次运行一直「运行中」、`finished_at` 为空，实际早已中断。
    """
    from docking_agent.runs import get_run_store

    store = get_run_store()
    stale = populated / "var" / "runs" / "20260921-100000-aaaa" / "run.json"
    meta = json.loads(stale.read_text(encoding="utf-8"))
    meta["status"] = "running"
    meta["finished_at"] = None
    meta["duration_sec"] = None
    meta["choices"] = [{"id": "x", "kind": "molecule", "label": "候选", "prompt": "继续"}]
    stale.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    fixed = store.reconcile_interrupted()
    assert fixed == ["20260921-100000-aaaa"], fixed
    after = json.loads(stale.read_text(encoding="utf-8"))
    assert after["status"] == "interrupted"
    assert after["finished_at"] and after["duration_sec"] is not None
    assert "进程重启" in str(after["error"])
    assert any("已中断" in line for line in after["log"]), after["log"][-2:]
    # 候选必须保留：用户仍需从中点选（点选会以同一会话发起新运行）
    assert len(after["choices"]) == 1

    # 已完成/失败的运行不受影响；重复调用是幂等的
    assert store.reconcile_interrupted() == []
    assert json.loads((populated / "var" / "runs" / "20260920-090000-bbbb" / "run.json")
                      .read_text(encoding="utf-8"))["status"] == "ok"


def test_app_startup_reconciles_interrupted_runs(populated: Path) -> None:
    """启动钩子必须真的调用收尾（否则重启后仍会显示「运行中」）。"""
    from fastapi.testclient import TestClient

    from docking_agent.api.app import app

    stale = populated / "var" / "runs" / "20260919-080000-cccc" / "run.json"
    meta = json.loads(stale.read_text(encoding="utf-8"))
    meta["status"] = "running"
    stale.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    with TestClient(app):
        pass
    assert json.loads(stale.read_text(encoding="utf-8"))["status"] == "interrupted"


# --------------------------------------------------------------------------- #
# 5) 受体自带共晶配体且未指定阳性对照 → 询问是否用作对照（不阻塞筛选）
# --------------------------------------------------------------------------- #
def _pdb_with_ligand(tmp_path: Path) -> Path:
    """最小受体结构：一条蛋白残基 + 一个**由 RDKit 生成**的合法共晶配体块。

    手写 HETATM 太简陋会被 RDKit 判为无效价态（实测），因此这里用 RDKit 把乙醇写成 PDB 块，
    再补一条蛋白 ATOM 记录 —— 与真实受体文件的结构形式一致。
    """
    from rdkit import Chem

    mol = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    ligand_block = Chem.MolToPDBBlock(mol)
    protein_line = ("ATOM      1  N   ALA A   1      11.000  11.000  11.000  "
                    "1.00  0.00           N\n")
    pdb = tmp_path / "rec_with_lig.pdb"
    pdb.write_text(protein_line + ligand_block, encoding="utf-8")
    return pdb


def test_cocrystal_ligand_smiles_from_structure(tmp_path: Path) -> None:
    from docking_agent.core.pockets import cocrystal_ligand, cocrystal_ligand_smiles

    pdb = _pdb_with_ligand(tmp_path)
    ligand = cocrystal_ligand(str(pdb))
    assert ligand and ligand.get("resname"), ligand
    smiles = cocrystal_ligand_smiles(str(pdb), ligand)
    assert smiles, "应当能从结构解出共晶配体的 SMILES"
    from rdkit import Chem

    assert Chem.MolFromSmiles(smiles) is not None, smiles


def test_offer_when_no_positive_control_specified(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """未指定对照 + 受体带共晶配体 → 发布 kind=positive_control 的两个选项。"""
    from docking_agent.core.pockets import cocrystal_ligand
    from docking_agent.tools import choices as CH

    pdb = _pdb_with_ligand(tmp_path)
    blocks = [{"receptor": "thrombin", "receptor_pdb": str(pdb),
               "cocrystal_ligand": cocrystal_ligand(str(pdb))}]
    published: Dict[str, Any] = {}
    monkeypatch.setattr(CH, "publish_choices",
                        lambda kind, items, note="", runtime=None: published.update(
                            {"kind": kind, "items": items, "note": note}))
    offered = CH.offer_cocrystal_positive_control(blocks, specified_control="")
    assert published.get("kind") == "positive_control"
    assert len(offered) == 2
    assert offered[0]["kind"] == "positive_control" and offered[0]["value"]
    assert "共晶配体" in offered[0]["label"]
    assert offered[1]["value"] == "" and "不使用" in offered[1]["label"]
    assert "阳性对照" in published["note"]


def test_no_offer_when_control_already_specified(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """用户/上游已给出阳性对照时不得再问（避免无意义的打扰）。"""
    from docking_agent.core.pockets import cocrystal_ligand
    from docking_agent.tools import choices as CH

    pdb = _pdb_with_ligand(tmp_path)
    blocks = [{"receptor": "thrombin", "receptor_pdb": str(pdb),
               "cocrystal_ligand": cocrystal_ligand(str(pdb))}]
    called: list = []
    monkeypatch.setattr(CH, "publish_choices",
                        lambda *a, **k: called.append((a, k)))
    assert CH.offer_cocrystal_positive_control(blocks, specified_control="NC(=N)c1ccccc1") == []
    assert not called


def test_no_offer_without_ligand_or_smiles(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """没有共晶配体、或解不出 SMILES 时不询问（绝不拿不确定结构当对照）。"""
    from docking_agent.core.pockets import cocrystal_ligand
    from docking_agent.tools import choices as CH

    monkeypatch.setattr(CH, "publish_choices", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("不应发布询问")))
    assert CH.offer_cocrystal_positive_control([], specified_control="") == []
    assert CH.offer_cocrystal_positive_control(
        [{"cocrystal_ligand": {}, "receptor_pdb": ""}], specified_control="") == []

    # 有配体但结构文件缺失 → 解不出 SMILES → 不询问
    pdb = _pdb_with_ligand(tmp_path)
    ligand = cocrystal_ligand(str(pdb))
    assert CH.offer_cocrystal_positive_control(
        [{"cocrystal_ligand": ligand, "receptor_pdb": str(tmp_path / "missing.pdb")}],
        specified_control="") == []


def test_ligand_smiles_recovered_from_request_file(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """对接用的是「已去配体」的准备结构时，要能从请求里的原始受体文件恢复配体 SMILES。

    真实案例（run 20260921-191324-9245，7YHP 的共晶配体 5CM）：只读准备结构会解不出，
    于是系统放弃了询问；现在按「准备结构 → 请求原始文件 → 在线解析缓存」依次尝试。
    """
    from docking_agent.core.pockets import cocrystal_ligand
    from docking_agent.tools import choices as CH

    with_ligand = _pdb_with_ligand(tmp_path)
    ligand = cocrystal_ligand(str(with_ligand))
    assert ligand
    prepared = tmp_path / "prepared.pdb"                 # 只有蛋白、没有配体
    prepared.write_text("ATOM      1  N   ALA A   1      11.000  11.000  11.000  "
                        "1.00  0.00           N\nEND\n", encoding="utf-8")

    class _Run:
        data = {"request": {"receptor_file": str(with_ligand)}}

    monkeypatch.setattr(CH, "active_run", lambda runtime=None: _Run())
    block = {"receptor": "ROS1 (7YHP)", "receptor_pdb": str(prepared),
             "cocrystal_ligand": ligand}
    paths = CH._ligand_candidate_paths(block)
    assert str(prepared) in paths and str(with_ligand) in paths
    smiles, tried = CH._ligand_smiles_from_candidates(ligand, paths)
    assert smiles, f"应能从请求里的原始文件恢复（试过 {tried}）"
    assert str(with_ligand) in tried


def test_missing_ligand_message_lists_tried_paths(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """解不出时，运行日志要说清"试过哪些结构"，而不是只报一句失败。"""
    from docking_agent.tools import choices as CH

    logs: list = []

    class _Run:
        data = {"request": {}}

        def log(self, message: str) -> None:
            logs.append(message)

    monkeypatch.setattr(CH, "active_run", lambda runtime=None: _Run())
    monkeypatch.setattr(CH, "publish_choices", lambda *a, **k: None)
    ligand = {"key": "C:26:5CM", "resname": "5CM", "n_atoms": 20}
    assert CH.offer_cocrystal_positive_control(
        [{"receptor_pdb": str(tmp_path / "missing.pdb"), "cocrystal_ligand": ligand}]) == []
    assert logs and "5CM（C:26:5CM，20 原子）" in logs[0], logs
    assert "missing.pdb" in logs[0] and "解不出 SMILES" in logs[0], logs


def test_cocrystal_offer_is_asked_at_most_once_per_run(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """一次运行内最多问一次（真实缺陷：run_docking 被重试时用户被重复打扰）。

    现场：前两次调用时受体结构还没准备好 → 解不出 SMILES（日志写「因此未询问」），
    第三次却能解出并发布选项，用户先看到「不问」再被问一次。
    """
    from docking_agent.core.pockets import cocrystal_ligand
    from docking_agent.tools import choices as CH

    pdb = _pdb_with_ligand(tmp_path)
    blocks = [{"receptor": "thrombin", "receptor_pdb": str(pdb),
               "cocrystal_ligand": cocrystal_ligand(str(pdb))}]

    published: list = []
    monkeypatch.setattr(CH, "publish_choices", lambda *a, **k: published.append((a, k)))

    class _Run:
        def __init__(self) -> None:
            self.data: Dict[str, Any] = {}
        def log(self, message: str) -> None:
            self.data.setdefault("log", []).append(message)

    run = _Run()
    monkeypatch.setattr(CH, "active_run", lambda runtime=None: run)

    first = CH.offer_cocrystal_positive_control(blocks, specified_control="")
    assert len(first) == 2 and len(published) == 1, "第一次调用应当询问"
    assert run.data.get("cocrystal_control_offer"), "询问必须留下可追溯记录"

    second = CH.offer_cocrystal_positive_control(blocks, specified_control="")
    assert second == [] and len(published) == 1, "同一次运行内不得再问一遍"


def test_no_late_cocrystal_ask_once_docking_started(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """对接前判定一次：第一次解不出就**不再**在后期突然发问。

    真实现场（run 20260922-140903-6542）：前两次调用写「因此未询问」，第三次（对接已跑完、
    报告将生成时）却发布选项，用户被迟到的提问打扰，且与前面的「不问」自相矛盾。
    """
    from docking_agent.tools import choices as CH

    blocks = [{"receptor": "7YHP", "receptor_pdb": "",
               "cocrystal_ligand": {"resname": "5CM", "key": "C:26:5CM", "n_atoms": 20}}]

    class _Run:
        def __init__(self) -> None:
            self.data: Dict[str, Any] = {}
        def log(self, message: str) -> None:
            self.data.setdefault("log", []).append(message)

    run = _Run()
    monkeypatch.setattr(CH, "active_run", lambda runtime=None: run)
    answers = iter([("", ["a.pdb"]), ("CC1CN", ["b.pdb"])])   # 第一次解不出，第二次能解出
    monkeypatch.setattr(CH, "_ligand_smiles_from_candidates", lambda *a, **k: next(answers))
    published: list = []
    monkeypatch.setattr(CH, "publish_choices", lambda *a, **k: published.append((a, k)))

    assert CH.offer_cocrystal_positive_control(blocks, specified_control="") == []
    assert CH.offer_cocrystal_positive_control(blocks, specified_control="") == [], "不得迟到发问"
    assert published == [], "第一次解不出 → 本次运行不再询问"
    assert len([line for line in run.data.get("log", []) if "解不出 SMILES" in line]) == 1


def test_cocrystal_smiles_failure_is_logged_once_per_run(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """解不出 SMILES 时「未询问」只写一次日志（重试不刷屏）。"""
    from docking_agent.tools import choices as CH

    blocks = [{"receptor": "7YHP", "receptor_pdb": "",
               "cocrystal_ligand": {"resname": "5CM", "key": "C:26:5CM", "n_atoms": 20}}]

    class _Run:
        def __init__(self) -> None:
            self.data: Dict[str, Any] = {}
        def log(self, message: str) -> None:
            self.data.setdefault("log", []).append(message)

    run = _Run()
    monkeypatch.setattr(CH, "active_run", lambda runtime=None: run)
    monkeypatch.setattr(CH, "_ligand_smiles_from_candidates", lambda *a, **k: ("", ["fake.pdb"]))
    monkeypatch.setattr(CH, "publish_choices", lambda *a, **k: None)

    for _ in range(3):
        assert CH.offer_cocrystal_positive_control(blocks, specified_control="") == []
    logs = [line for line in run.data.get("log", []) if "解不出 SMILES" in line]
    assert len(logs) == 1, f"「解不出」说明只应写一次：{logs}"
