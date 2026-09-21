"""langgraph-deploy 真实服务集成测试（live）。

跑法：

    LG_LIVE=1 .venv/bin/python -m pytest tests -q -m live
    bash scripts/test.sh --live                       # 先离线、再集成
    LG_BASE_URL=http://127.0.0.1:2024 LG_LIVE=1 ...   # 复用已在跑的服务
    LG_LOG=/path/dev.log LG_BASE_URL=...              # 复用服务时启用日志回归断言

覆盖：
  1) 服务存活 / 图清单 / 图的 schema 契约（Studio 的 Input 表单来自它）
  2) pipeline 图真实对接（Vina）→ 产物落盘、运行目录隔离
  3) 并发 3 个 run → run_id 与运行目录互不串台
  4) 无分子输入的错误路径 → 如实返回 no_molecules，run 有收尾
  5) 未知 assistant → 客户端错误（实测 422）
  6) 多轮会话（真实 LLM，轻量问答）：同一 thread 的历史**累积且不重复**
  7) 服务日志里不得出现 BlockingError（事件循环阻塞回归）

隔离：默认自己拉一个 `langgraph dev`（端口 2033 + 临时工作区，assets/config/web 软链到
projects、var 独立），所以真实对接产物不会写进 projects/var/。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1]
PROJECT = DEPLOY.parent / "projects"
PORT = int(os.getenv("LG_TEST_PORT", "2033"))
EXTERNAL = os.getenv("LG_BASE_URL", "").rstrip("/")
EXPECTED_GRAPHS = {"coordinator", "pipeline", "intake", "property", "pocket", "docking", "binding"}

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("LG_LIVE") != "1",
                       reason="真实服务集成测试：设置 LG_LIVE=1（或 bash scripts/test.sh --live）"),
]

# 服务进程必须拿到**真实** LLM 配置（离线用例会往本进程塞假 key，不能顺延给服务）
_STRIP_FOR_SERVER = {"LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "LLM_TEMPERATURE", "INTAKE_LLM",
                     "INCHIKEY_ONLINE", "LOCAL_SETTINGS_PATH", "DOCKING_WORKSPACE", "MPLCONFIGDIR"}


# --------------------------------------------------------------------------- #
# HTTP 小工具
# --------------------------------------------------------------------------- #
def http(base: str, path: str, payload: dict | None = None, *, method: str = "POST",
         timeout: float = 120.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw) if raw else {}


def http_status(base: str, path: str, payload: dict | None = None, *, method: str = "POST") -> int:
    try:
        req = urllib.request.Request(
            f"{base}{path}", data=json.dumps(payload).encode() if payload is not None else None,
            method=method, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def wait_ok(base: str, timeout: float = 240.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if http(base, "/ok", None, method="GET", timeout=5).get("ok") is True:
                return True
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
    return False


class Server:
    def __init__(self, base: str, log: Path | None, proc: subprocess.Popen | None,
                 workspace: Path | None):
        self.base, self.log, self.proc, self.workspace = base, log, proc, workspace

    def log_text(self) -> str:
        if not self.log or not self.log.exists():
            return ""
        return self.log.read_text(encoding="utf-8", errors="replace")


def _busy(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.4)
    out = s.connect_ex(("127.0.0.1", port)) == 0
    s.close()
    return out


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="session")
def server() -> Server:
    if EXTERNAL:
        assert wait_ok(EXTERNAL, 30), f"外部服务不可用：{EXTERNAL}"
        log = os.getenv("LG_LOG", "")
        yield Server(EXTERNAL, Path(log) if log else None, None, None)
        return

    lg = DEPLOY / ".venv/bin/langgraph"
    assert lg.exists(), "缺少 deploy/.venv，请先 bash scripts/install.sh"

    tmp = Path(tempfile.mkdtemp(prefix="lg-live-"))
    ws = tmp / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    for name in ("assets", "config", "web"):
        (ws / name).symlink_to(PROJECT / name)
    (ws / "var").mkdir(parents=True, exist_ok=True)

    port = PORT if not _busy(PORT) else _free_port()
    log = tmp / "dev.log"
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_FOR_SERVER}
    env.update({
        "DOCKING_WORKSPACE": str(ws),
        "MPLCONFIGDIR": str(PROJECT / "var/cache/matplotlib"),
        "UV_CACHE_DIR": os.getenv("UV_CACHE_DIR", str(DEPLOY.parent / ".uv-cache")),
    })
    proc = subprocess.Popen(
        [str(lg), "dev", "--host", "127.0.0.1", "--port", str(port),
         "--no-browser", "--no-reload"],
        cwd=str(DEPLOY), env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
        start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    assert wait_ok(base), f"langgraph dev 启动失败：\n{log.read_text(errors='replace')[-3000:]}"
    yield Server(base, log, proc, ws)

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 1) 服务与 schema 契约
# --------------------------------------------------------------------------- #
def test_ok_and_graph_list(server: Server) -> None:
    assert http(server.base, "/ok", None, method="GET")["ok"] is True
    got = {r["graph_id"] for r in http(server.base, "/assistants/search", {"limit": 50})}
    assert got == EXPECTED_GRAPHS, f"图清单不符：{sorted(got)}"


def _assistant_id(server: Server, graph_id: str) -> str:
    rows = http(server.base, "/assistants/search", {"limit": 50})
    return next(r["assistant_id"] for r in rows if r["graph_id"] == graph_id)


def test_pipeline_schema_exposed(server: Server) -> None:
    """Studio 的 Input 表单来自图的输入 schema，字段必须齐（含输出字段回显）。"""
    pid = _assistant_id(server, "pipeline")
    blob = json.dumps(http(server.base, f"/assistants/{pid}/schemas", None, method="GET"),
                      ensure_ascii=False)
    for key in ("ligands_text", "molecule_file", "receptor", "receptor_file", "positive_control",
                "exhaustiveness", "n_poses", "engine", "pocket_engine", "site_center", "site_size",
                "save_poses", "max_ligands", "protonation", "protonation_ph",
                "allow_example_fallback", "run_id", "artifacts"):
        assert key in blob, f"pipeline schema 缺字段 {key}"
    cid = _assistant_id(server, "coordinator")
    assert "messages" in json.dumps(
        http(server.base, f"/assistants/{cid}/schemas", None, method="GET"), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 2/3/4/5) 真实对接 / 并发 / 错误路径
# --------------------------------------------------------------------------- #
def run_pipeline(server: Server, *, smiles: str, name: str = "mol",
                 timeout: float = 900.0) -> dict:
    return http(server.base, "/runs/wait", {
        "assistant_id": "pipeline",
        "input": {
            "ligands_text": f"{name},{smiles}",
            "receptor": "thrombin",
            "pocket_engine": "known_site",
            "exhaustiveness": 1, "n_poses": 1, "save_poses": True,
            "site_center": [31.50, 13.74, 24.36], "site_size": [22.0, 22.0, 22.0],
        },
    }, timeout=timeout)


def test_pipeline_real_docking_artifacts(server: Server) -> None:
    out = run_pipeline(server, smiles="CCO", name="乙醇")
    assert out.get("status") == "ok", out
    run_dir = Path(out["run_dir"])
    for name in ("run.json", "docking.json", "ranking.csv", "report.md", "studio_result.json"):
        assert (run_dir / name).is_file(), f"{name} 未生成"
    arts = {a["name"] for a in out["artifacts"]}
    assert {"report_md", "report_pdf", "ranking_csv", "studio_result"} <= arts, sorted(arts)
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["kind"] == "studio" and meta["status"] == "ok"
    assert out["molecule_count"] == 1
    if server.workspace:      # 运行目录必须落在测试自己的临时工作区
        assert str(server.workspace) in str(run_dir), run_dir


def test_concurrent_runs_are_isolated(server: Server) -> None:
    import concurrent.futures as cf

    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        outs = list(pool.map(lambda s: run_pipeline(server, smiles=s, name=f"M{s}"),
                             ["CCO", "Oc1ccccc1", "CCN"]))
    assert all(o.get("status") == "ok" for o in outs), outs
    assert len({o["run_id"] for o in outs}) == 3
    dirs = [Path(o["run_dir"]) for o in outs]
    assert len({str(d) for d in dirs}) == 3
    for d in dirs:
        assert (d / "docking.json").is_file()
        assert json.loads((d / "run.json").read_text(encoding="utf-8"))["kind"] == "studio"


def test_no_molecules_is_honest_not_crash(server: Server) -> None:
    out = http(server.base, "/runs/wait", {"assistant_id": "pipeline", "input": {
        "ligands_text": "", "allow_example_fallback": False, "receptor": "thrombin"}}, timeout=300)
    assert out.get("status") == "no_molecules", out
    run_dir = Path(out["run_dir"])
    assert run_dir.is_dir()
    # 裸返回路径也必须收尾（部署层缺陷 B 的回归）
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert meta["status"] not in ("", "running"), meta["status"]


def test_unknown_assistant_is_client_error(server: Server) -> None:
    # 实测 LangGraph API 对未知 assistant 返回 422（不是 404）；只要求是 4xx 且不是 200
    code = http_status(server.base, "/runs/wait", {"assistant_id": "does-not-exist", "input": {}})
    assert 400 <= code < 500, f"未知 assistant 返回 {code}"


# --------------------------------------------------------------------------- #
# 6) 多轮会话（真实 LLM，轻量：只问答，不建库不对接）
# --------------------------------------------------------------------------- #
def test_thread_history_accumulates_without_duplicates(server: Server) -> None:
    """多轮记忆必须用**线程级**端点 `/threads/{id}/runs/wait`。

    实测陷阱：把 `thread_id` 放进 `/runs/wait` 的 body 会被当成**无状态**运行
    （每次新建线程），于是第二轮看不到第一轮的历史。规范用法是线程级端点。
    """
    tid = http(server.base, "/threads", {})["thread_id"]
    q1 = "只用 list_known_receptors 回答：系统里有哪些预置可对接受体？不要做任何对接或建库。"
    q2 = "再确认一次：上一条你列出了几个受体？只回一句，不要调用工具。"
    counts = []
    for q in (q1, q2):
        out = http(server.base, f"/threads/{tid}/runs/wait", {
            "assistant_id": "coordinator",
            "input": {"messages": [{"role": "user", "content": q}]},
            "config": {"recursion_limit": 30}}, timeout=900)
        counts.append(len(out.get("messages") or []))

    state = http(server.base, f"/threads/{tid}/state", None, method="GET")
    messages = (state.get("values") or {}).get("messages") or []
    assert messages, state
    assert counts[1] > counts[0], f"第二轮没有带上历史：{counts}"
    humans = [m.get("content") for m in messages if m.get("type") == "human"]
    assert humans.count(q1) == 1 and humans.count(q2) == 1, humans
    assert len(messages) >= 4, f"历史没有累积：{len(messages)} 条"


# --------------------------------------------------------------------------- #
# 7) 阻塞调用回归
# --------------------------------------------------------------------------- #
def test_no_blocking_error_in_server_log(server: Server) -> None:
    if not server.log:
        pytest.skip("复用外部服务且未给 LG_LOG，跳过日志回归")
    log = server.log_text()
    assert log, "服务日志为空"
    assert "BlockingError" not in log, "事件循环里出现同步阻塞调用（BlockingError）"
    assert "blocking call to" not in log.lower()
