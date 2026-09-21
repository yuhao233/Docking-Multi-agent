"""本轮新增能力的回归测试：在线工具容错、任务取消、阳性对照可选。

全部为离线可跑（不依赖外网）；在线工具只测纯函数逻辑与错误映射。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

from support.fake_llm import coordinator_script_for_form, run_agent  # noqa: E402


@pytest.fixture(scope="module")
def client() -> Iterator[Any]:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from docking_agent.api.app import app

    with TestClient(app) as c:
        yield c


def _run_agent(client, body: dict, monkeypatch) -> str:
    """走标准 Agent Protocol 路径（假 LLM 驱动真实多 Agent 编排），返回业务 run_id。"""
    return run_agent(client, coordinator_script_for_form(body), body, monkeypatch)


# --------------------------------------------------------------------------- #
# 在线数据库工具：检索式容错（回归：此前把蛋白名拼进 accession: 导致 UniProt 400）
# --------------------------------------------------------------------------- #
def test_uniprot_query_chain_never_puts_names_into_accession_filter():
    from docking_agent.tools.online import _uniprot_queries

    # 基因名：不得出现 accession: 过滤器
    for q in _uniprot_queries("EGFR"):
        assert "accession:EGFR" not in q, f"基因名被拼进 accession 过滤器：{q}"
    assert any("gene_exact" in q for q in _uniprot_queries("EGFR")), "基因符号应优先用 gene_exact"

    # 蛋白名（含空格）：不得出现 accession: 与非法的 gene_exact 空格匹配
    multi = _uniprot_queries("epidermal growth factor receptor")
    assert all("accession:epidermal" not in q for q in multi)
    assert all("gene_exact" not in q for q in multi), "含空格的名称不应使用 gene_exact"

    # 真正的 accession：必须用 accession 过滤器
    assert any(q.startswith("accession:P08922") for q in _uniprot_queries("P08922"))


def test_pdb_candidates_prefer_xray_and_best_resolution():
    from docking_agent.tools.online import _pdbs_from_entry

    entry = {"uniProtKBCrossReferences": [
        {"database": "PDB", "id": "aaaa", "properties": [
            {"key": "Method", "value": "EM"}, {"key": "Resolution", "value": "3.50 A"}]},
        {"database": "PDB", "id": "bbbb", "properties": [
            {"key": "Method", "value": "X-ray"}, {"key": "Resolution", "value": "2.80 A"}]},
        {"database": "PDB", "id": "cccc", "properties": [
            {"key": "Method", "value": "X-ray"}, {"key": "Resolution", "value": "1.10 A"}]},
        {"database": "PDB", "id": "dddd", "properties": [
            {"key": "Method", "value": "NMR"}]},
        {"database": "CHEBI", "id": "ignored"},
    ]}
    refs = _pdbs_from_entry(entry)
    assert [r["pdb"] for r in refs[:3]] == ["CCCC", "BBBB", "AAAA"]  # X-ray 优先且由优到劣
    assert all(r["pdb"] != "IGNORED" for r in refs)


def test_http_error_messages_are_actionable():
    import urllib.error

    from docking_agent.tools.online import _explain_http_error

    e400 = urllib.error.HTTPError("u", 400, "bad", {}, None)
    msg = _explain_http_error("https://rest.uniprot.org/uniprotkb/x", e400)
    assert "400" in msg and "基因名" in msg and "3ZBF" in msg

    e404 = urllib.error.HTTPError("u", 404, "nf", {}, None)
    assert "未找到" in _explain_http_error("https://x/y", e404)


def test_fetch_protein_structure_rejects_empty_input():
    from docking_agent.tools.online import fetch_protein_structure

    out = json.loads(fetch_protein_structure.invoke({"source": "  "}))
    assert out["status"] == "error" and "PDB" in out["message"]


def test_fetch_molecule_record_validates_id_type():
    from docking_agent.tools.online import fetch_molecule_record

    out = json.loads(fetch_molecule_record.invoke({"query": "aspirin", "id_type": "bogus"}))
    assert out["status"] == "error" and "id_type" in out["message"]


# --------------------------------------------------------------------------- #
# 任务取消
# --------------------------------------------------------------------------- #
def test_cancel_flag_lifecycle():
    from docking_agent.cancellation import clear_cancel, is_cancelled, request_cancel

    rid = "test-run-cancel"
    clear_cancel(rid)
    assert is_cancelled(rid) is False
    assert request_cancel(rid) is True       # 首次请求
    assert request_cancel(rid) is False      # 重复请求
    assert is_cancelled(rid) is True
    clear_cancel(rid)
    assert is_cancelled(rid) is False


def test_dock_batch_raises_immediately_when_cancelled():
    """取消标志已置位时，批量对接必须在开始前就抛出 CancelledRun（不做任何真实对接）。"""
    import threading

    from docking_agent.core import CancelledRun, dock_batch, resolve_receptor_specs

    spec = resolve_receptor_specs("thrombin")[0][0]
    ev = threading.Event()
    ev.set()
    with pytest.raises(CancelledRun):
        dock_batch(spec, [{"name": "a", "smiles": "CCO"}], engine="vina",
                   exhaustiveness=1, cancel_event=ev)


# --------------------------------------------------------------------------- #
# 阳性对照可选
# --------------------------------------------------------------------------- #
def test_agent_skips_positive_control_when_not_provided(client, monkeypatch) -> None:
    run_id = _run_agent(client, {"ligands_text": "乙醇:CCO", "receptor": "thrombin",
                                 "positive_control": "", "engine": "vina",
                                 "exhaustiveness": 1, "save_poses": False}, monkeypatch)
    detail = client.get(f"/api/runs/{run_id}").json()
    result = detail["result"]
    assert detail["run"]["status"] == "ok"
    assert not result.get("positive_control"), "未提供阳性对照时不应产生对照结果"
    # 等价于原流水线 note：受理层把「未提供阳性对照」记为假设，报告也如实写明
    spec = result.get("task_spec") or {}
    assert any("未提供阳性对照" in a for a in (spec.get("assumptions") or [])), spec.get("assumptions")
    assert "本次未提供阳性对照" in detail["report_markdown"]
    assert result["ranking"], "候选分子仍应正常排序"


def test_agent_runs_positive_control_when_provided(client, monkeypatch) -> None:
    run_id = _run_agent(client, {"ligands_text": "乙醇:CCO", "receptor": "thrombin",
                                 "positive_control": "NC(=N)c1ccccc1", "engine": "vina",
                                 "exhaustiveness": 1, "save_poses": False}, monkeypatch)
    result = client.get(f"/api/runs/{run_id}").json()["result"]
    pc = result.get("positive_control") or {}
    assert pc.get("affinity_kcal_mol") is not None, "提供了阳性对照就应完成对照对接"
    assert result["ranking"][0].get("similarity_to_positive_control") is not None


def test_smi_accepts_both_column_orders(tmp_path):
    """.smi 既要支持标准的「SMILES 名称」，也要兼容中文用户常写的「名称 SMILES」。"""
    from docking_agent.core import read_molecule_file

    f1 = tmp_path / "standard.smi"
    f1.write_text("CCO ethanol\n", encoding="utf-8")
    fmt, mols = read_molecule_file(str(f1))
    assert fmt == "smi" and mols[0]["smiles"] == "CCO" and mols[0]["name"] == "ethanol"

    f2 = tmp_path / "chinese.smi"
    f2.write_text("乙醇 CCO\n", encoding="utf-8")
    fmt, mols = read_molecule_file(str(f2))
    assert fmt == "smi" and mols[0]["smiles"] == "CCO" and mols[0]["name"] == "乙醇"


def test_registry_pdbqt_keeps_known_site():
    """直接使用注册表 PDBQT 时必须沿用已知位点，而不是退化成蛋白质心。"""
    from docking_agent.core.receptors import RECEPTOR_REGISTRY, _pdbqt_spec

    spec = RECEPTOR_REGISTRY["thrombin"]
    parsed = _pdbqt_spec(spec["pdbqt"])
    assert abs(parsed["center"][0] - 31.5) < 1.0, parsed["center"]
    assert "注册表" in parsed["site"]["source"]


def test_copied_registry_pdbqt_inherits_site_by_content(tmp_path):
    """把预置受体复制一份（路径不同、内容相同）后，仍应沿用同一活性位点。"""
    import shutil

    from docking_agent.core.receptors import RECEPTOR_REGISTRY, _pdbqt_spec

    src = RECEPTOR_REGISTRY["thrombin"]["pdbqt"]
    copy = tmp_path / "uploaded_copy.pdbqt"
    shutil.copy(src, copy)

    spec = _pdbqt_spec(str(copy))
    assert abs(spec["center"][0] - 31.5) < 1.0, f"内容一致的副本应沿用已知位点，实际 {spec['center']}"
    assert "内容一致" in spec["site"]["source"]


def _queue_probe_worker(item):  # noqa: ANN001, ANN201 - 模块级函数才可 pickle 进子进程
    """每个分子「算完」时往日志里追加一行（用于判断取消后是否还有分子被算完）。"""
    import os
    import time as _time

    _time.sleep(0.3)
    path = os.environ.get("DOCK_CANCEL_TEST_LOG") or ""
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{item[0]}\n")
    _time.sleep(5)                       # 模拟一个大分子还在跑
    return {"name": item[0], "smiles": item[1], "affinity_kcal_mol": -1.0}


def _slow_worker_30s(item):  # noqa: ANN001, ANN201 - 必须是模块级函数才能 pickle 进子进程
    """模拟一个要跑 30 s 的超大柔性分子（用于验证取消能在分子中途生效）。"""
    import time as _time

    _time.sleep(30)
    return {"name": item[0], "smiles": item[1], "affinity_kcal_mol": -1.0}


# --------------------------------------------------------------------------- #
# 取消必须在**单个分子还在跑**时就生效（真实缺陷：chat 模式点停止后 load 不降）
# --------------------------------------------------------------------------- #
def test_cancel_during_long_molecule_stops_promptly(monkeypatch: pytest.MonkeyPatch) -> None:
    """第一个分子就要跑很久时，「停止」必须在秒级内终止进程池，而不是等它跑完。"""
    import threading
    import time

    from docking_agent.core import CancelledRun, docking as D

    monkeypatch.setattr(D, "_worker_dock", _slow_worker_30s)
    spec = D.resolve_receptor_specs("thrombin")[0][0]
    ev = threading.Event()
    error: list = []

    def _call() -> None:
        try:
            # 8 个分子才会走「多进程」路径（≤7 个是单进程多线程，见 plan_concurrency）
            D.dock_batch(spec, [{"name": f"m{i}", "smiles": "CCO"} for i in range(8)],
                         engine="vina", exhaustiveness=1, cancel_event=ev)
        except BaseException as e:  # noqa: BLE001 - 线程里要把异常带回主线程
            error.append(e)

    thread = threading.Thread(target=_call, daemon=True)
    started = time.time()
    thread.start()
    time.sleep(1.5)          # 让进程池真的跑起来
    ev.set()                 # 用户点了「停止」
    thread.join(timeout=15)
    elapsed = time.time() - started

    assert not thread.is_alive(), f"取消后 15 s 仍未停下（load 会一直高）：elapsed={elapsed:.1f}s"
    assert error and isinstance(error[0], CancelledRun), error
    assert elapsed < 12, f"取消响应太慢：{elapsed:.1f}s（应远小于单个分子的 30 s）"


def test_agent_mode_docking_receives_run_cancel_flag(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path) -> None:
    """chat/多 Agent 模式的对接必须接上本次运行的取消标志，否则停止按钮是摆设。"""
    from docking_agent.cancellation import cancel_flag, clear_cancel, request_cancel
    from docking_agent.runs import current_run
    from docking_agent.tools import docking as TD

    captured: dict = {}
    monkeypatch.setattr(TD, "dock_library",
                        lambda *_a, **kw: captured.update(kw) or {"status": "ok", "receptors": []})

    class _Run:
        id = "test-agent-cancel"
        data: dict = {}

        def __init__(self) -> None:
            self.dir = tmp_path

        def log(self, *_a, **_k) -> None:  # pragma: no cover - 仅满足接口
            return None

    run = _Run()
    clear_cancel(run.id)
    expected = cancel_flag(run.id)          # 必须在 clear_cancel 之前取，否则拿到的是新对象
    token = current_run.set(run)
    try:
        TD.molecular_docking.func(molecules_json='[{"name":"A","smiles":"CCO"}]',
                                  receptor_sources="thrombin")
    finally:
        current_run.reset(token)

    assert "cancel_event" in captured, "对接调用必须带 cancel_event"
    assert captured["cancel_event"] is expected, "必须是本次运行的同一个取消标志"
    request_cancel(run.id)
    assert captured["cancel_event"].is_set(), "用户请求取消后该标志必须置位"
    clear_cancel(run.id)


def test_cancelled_run_refuses_to_start_new_docking(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path) -> None:
    """已取消的运行里，在途的工具调用不得再开始整库对接（真实缺陷：取消后 load 反涨）。"""
    from docking_agent.runs import current_run
    from docking_agent.tools import docking as TD

    called: list = []
    monkeypatch.setattr(TD, "dock_library", lambda *_a, **kw: called.append(kw) or {})

    class _Run:
        id = "test-cancelled-run"
        data = {"status": "cancelled"}

        def __init__(self) -> None:
            self.dir = tmp_path

        def log(self, *_a, **_k) -> None:  # pragma: no cover - 仅满足接口
            return None

    token = current_run.set(_Run())
    try:
        out = json.loads(TD.molecular_docking.func(
            molecules_json='[{"name":"A","smiles":"CCO"}]', receptor_sources="thrombin"))
    finally:
        current_run.reset(token)

    assert out["status"] == "cancelled", out
    assert "不再开始新的对接" in out["message"]
    assert not called, "已取消的运行绝不能再调用对接层"


def test_cancelled_flag_blocks_before_starting_pool() -> None:
    """进程池路径也必须在建池之前就短路（否则会白起一整池 24 个进程）。"""
    import threading

    from docking_agent.core import CancelledRun, docking as D

    spec = D.resolve_receptor_specs("thrombin")[0][0]
    ev = threading.Event()
    ev.set()
    with pytest.raises(CancelledRun):
        D.dock_batch(spec, [{"name": f"m{i}", "smiles": "CCO"} for i in range(8)],
                     engine="vina", exhaustiveness=1, cancel_event=ev)


def test_cancel_does_not_let_pool_chew_the_queued_molecules(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """取消后**不能再有**新分子被算完：队列里待跑的任务要一起取消，worker 要真被杀掉。

    真实缺陷：147 个分子在 24 个 worker 上一次性提交，`terminate()` 之后执行器的管理线程
    发现队列还有一百多个任务，于是重新拉起 worker 继续啃 —— 用户点了停止，load 反而涨到 40。
    """
    import threading
    import time

    from docking_agent.core import CancelledRun, docking as D

    log = tmp_path / "done.txt"
    monkeypatch.setenv("DOCK_CANCEL_TEST_LOG", str(log))
    monkeypatch.setattr(D, "_worker_dock", _queue_probe_worker)

    spec = D.resolve_receptor_specs("thrombin")[0][0]
    ev = threading.Event()
    error: list = []

    def _call() -> None:
        try:
            D.dock_batch(spec, [{"name": f"m{i}", "smiles": "CCO"} for i in range(40)],
                         engine="vina", exhaustiveness=1, cancel_event=ev)
        except BaseException as e:  # noqa: BLE001
            error.append(e)

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    time.sleep(2.5)                       # 8 个 worker 已在跑，队列里还有 32 个
    done_before = len(log.read_text().splitlines()) if log.is_file() else 0
    ev.set()
    thread.join(timeout=20)
    time.sleep(3)                         # 若还有 worker 活着，这里会继续产出

    done_after = len(log.read_text().splitlines()) if log.is_file() else 0
    assert error and isinstance(error[0], CancelledRun), error
    assert done_before > 0, "取消前应当已有分子完成（否则用例没测到点子上）"
    assert done_after - done_before <= D.plan_concurrency(40)["workers"], \
        f"取消后仍有 {done_after - done_before} 个分子被算完（队列没被一起取消）"
    assert done_after < 40, "不允许把整库跑完"


def test_single_molecule_docking_is_cancellable(monkeypatch: pytest.MonkeyPatch) -> None:
    """单分子对接也必须能被「停止」打断。

    旧实现里 ≤7 个分子走「单进程多线程」，Vina 在进程内跑、没法从外部杀掉，
    只能等本分子跑完（用户点停止后 load 不降）。现在所有对接都在 worker 进程里，
    因此单分子同样秒级可取消。
    """
    import threading
    import time

    from docking_agent.core import CancelledRun, docking as D

    monkeypatch.setattr(D, "_worker_dock", _slow_worker_30s)
    spec = D.resolve_receptor_specs("thrombin")[0][0]
    ev = threading.Event()
    error: list = []

    def _call() -> None:
        try:
            D.dock_batch(spec, [{"name": "only", "smiles": "CCO"}],
                         engine="vina", exhaustiveness=1, cancel_event=ev)
        except BaseException as e:  # noqa: BLE001
            error.append(e)

    thread = threading.Thread(target=_call, daemon=True)
    started = time.time()
    thread.start()
    time.sleep(1.5)
    ev.set()
    thread.join(timeout=15)
    assert error and isinstance(error[0], CancelledRun), error
    assert time.time() - started < 12, "单分子取消响应太慢"
