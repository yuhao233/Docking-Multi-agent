"""安全边界的负向回归：路径穿越 / 任意文件读取 / 跨站请求 / URL 方案白名单。

这四条都是 2026-09-21 审计**实测确认**过的漏洞，属于「一旦信任模型被打破就全盘失守」
的一类，因此每条都留一个负向用例钉住：

1. `POST /run` 的 `x-run-id` 头被当成目录名 → `../x` 逃出运行目录（任意路径写）；
2. 标准面 `POST /threads` 的 `thread_id` 来自请求体 → 可覆盖工作区任意 `*.json`；
3. `POST /api/uploads/inspect` 接受任意绝对路径 → 任意文件读取，且解析失败会回显文件行；
4. 无鉴权 + 无同源校验 → 任意网页可跨站触发 `PUT /api/settings` 等写操作。

服务定位是「本机工具」，所以这里守的是**默认部署**下的边界；`--host 0.0.0.0`
只是把攻击面从"本机进程"扩大到"局域网"，校验逻辑本身与监听地址无关。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    from docking_agent.api.app import app

    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# 1) 标识符白名单（纯函数）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    "../x", "..", "/tmp/x", "a/b", "a\\b", "a b", "", "   ", ".hidden" if False else "a\nb",
])
def test_safe_run_component_rejects_traversal_and_control_chars(bad: str) -> None:
    from docking_agent.runs import safe_run_component

    with pytest.raises(ValueError):
        safe_run_component(bad)


@pytest.mark.parametrize("good", ["R-BIG", "t-board-2", "20260921-224059-4998", "abc.DEF_1"])
def test_safe_run_component_accepts_real_world_ids(good: str) -> None:
    from docking_agent.runs import safe_run_component

    assert safe_run_component(good) == good


def test_ensure_inside_rejects_escape(tmp_path: Path) -> None:
    from docking_agent.runs import ensure_inside

    root = tmp_path / "runs"
    root.mkdir()
    assert ensure_inside(root, root / "ok.json", field="x") == (root / "ok.json").resolve()
    with pytest.raises(ValueError):
        ensure_inside(root, root / ".." / "escaped.json", field="x")


def test_run_store_rejects_unsafe_run_id(tmp_path: Path) -> None:
    from docking_agent.runs import RunStore

    store = RunStore(tmp_path / "runs")
    with pytest.raises(ValueError):
        store.new("agent", {"mode": "run"}, run_id="../escaped")
    assert not (tmp_path / "escaped").exists()


# --------------------------------------------------------------------------- #
# 2) `x-run-id` 路径穿越（兼容入口）
# --------------------------------------------------------------------------- #
def test_legacy_run_rejects_traversal_in_x_run_id(client: TestClient, tmp_path: Path) -> None:
    """`x-run-id: ../escaped-run` 必须 400，且不得在 var/ 之外创建目录。"""
    from docking_agent.paths import runs_dir

    resp = client.post("/run", headers={"x-run-id": "../escaped-run"},
                       json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 400, resp.text
    assert "x-run-id" in resp.text
    assert not (runs_dir().parent / "escaped-run").exists()
    assert not (runs_dir() / ".." / "escaped-run").exists()


def test_legacy_run_accepts_wellformed_x_run_id(client: TestClient,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """正常 id 不受影响（兼容入口必须继续可用）；用完即清理，避免污染运行目录。

    本用例只验证 id 白名单与运行目录落盘，因此把图替换成**最小假图**：
    真图会把请求打到模型端点，既慢又依赖本机是否配置了 API Key（干净检出/CI 上曾因此假失败）。
    """
    import shutil
    import uuid

    from langchain_core.messages import AIMessage, HumanMessage

    from docking_agent.api import support
    from docking_agent.runs import get_run_store

    class _StubGraph:
        """只回一条 AI 消息；记录调用以便断言请求确实走到图这一步。"""

        def __init__(self) -> None:
            self.calls: list = []

        async def ainvoke(self, payload: Any, config: Any = None, context: Any = None,
                          **kwargs: Any) -> Any:
            self.calls.append(payload)
            return {"messages": [AIMessage(content="收到（测试桩）", id="a1")]}

        async def aget_state(self, config: Any = None) -> Any:
            values = {"messages": [HumanMessage("hi", id="h1"),
                                   AIMessage("收到（测试桩）", id="a1")]}
            return type("_Snapshot", (), {"values": values})()

    stub = _StubGraph()
    monkeypatch.setattr(support.state, "get_graph", lambda: stub)

    rid = f"audit-xrun-{uuid.uuid4().hex[:8]}"
    store = get_run_store()
    try:
        resp = client.post("/run", headers={"x-run-id": rid},
                           json={"messages": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 200, resp.text
        assert json.loads(resp.text)["run_id"] == rid
        assert (store.root / rid / "request.json").is_file()
        assert stub.calls, "请求没有走到图执行"
    finally:
        shutil.rmtree(store.root / rid, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 3) `thread_id` 路径穿越（标准面）
# --------------------------------------------------------------------------- #
def test_create_thread_rejects_traversal(client: TestClient) -> None:
    """`thread_id: ../escaped-thread` 必须 400，且不得在 var/ 下留下文件。"""
    from docking_agent.paths import var_dir

    resp = client.post("/threads", json={"thread_id": "../escaped-thread",
                                         "metadata": {"pwned": True}})
    assert resp.status_code == 400, resp.text
    assert not (var_dir() / "escaped-thread.json").exists()
    assert not (var_dir().parent / "escaped-thread.json").exists()


def test_create_thread_accepts_normal_id(client: TestClient) -> None:
    resp = client.post("/threads", json={"thread_id": "audit-thread-ok"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["thread_id"] == "audit-thread-ok"
    client.delete("/threads/audit-thread-ok")


def test_conversation_id_traversal_is_rejected(client: TestClient) -> None:
    """会话 id 会当 thread_id 用，同样必须过白名单。"""
    resp = client.post("/api/agent/stream", json={"mode": "chat", "message": "hi",
                                                  "conversation_id": "../evil"})
    assert resp.status_code == 400, resp.text


# --------------------------------------------------------------------------- #
# 4) 任意文件读取 + 明文回显（上行校验端点）
# --------------------------------------------------------------------------- #
def test_upload_inspect_rejects_paths_outside_uploads(client: TestClient) -> None:
    """只允许校验本次上传的文件；工作区内的其它文件（如 .env）必须 403。"""
    env_file = PROJECT_ROOT / ".env"
    target = str(env_file if env_file.is_file() else PROJECT_ROOT / "pyproject.toml")
    resp = client.post("/api/uploads/inspect", json={"path": target, "kind": "ligand"})
    assert resp.status_code == 403, resp.text
    body = resp.text
    # 不得回显被拒文件的内容（错误信息里只允许出现被拒路径的脱敏摘要）
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            value = line.split("=", 1)[1].strip() if "=" in line else ""
            if len(value) >= 8:
                assert value not in body, f"错误体回显了 .env 的值：{line.split('=', 1)[0]}"


def test_upload_inspect_rejects_absolute_system_path(client: TestClient) -> None:
    resp = client.post("/api/uploads/inspect", json={"path": "/etc/hostname", "kind": "ligand"})
    assert resp.status_code == 403, resp.text


def test_upload_inspect_still_accepts_uploaded_file(client: TestClient, tmp_path: Path) -> None:
    """正常路径不受影响：上传 → 校验 → 200。"""
    smi = tmp_path / "lib.smi"
    smi.write_text("CCO\nCCN\nCCC\n", encoding="utf-8")
    with open(smi, "rb") as fh:
        up = client.post("/api/uploads", files={"file": (smi.name, fh, "text/plain")},
                         data={"kind": "ligand"})
    assert up.status_code == 200, up.text
    inspect = client.post("/api/uploads/inspect",
                          json={"path": up.json()["path"], "kind": "ligand"})
    assert inspect.status_code == 200, inspect.text
    assert inspect.json()["count"] == 3


def test_parse_failure_hint_never_echoes_credential_lines(tmp_path: Path) -> None:
    """`.env` 这类内容交给解析器时，失败提示**不得**回显疑似凭据行。"""
    from docking_agent.core.normalize import normalize_ligand_text

    text = "LLM_API_KEY=sk-1eaf945e6e584c4b837282e5f5f77e97\nLLM_BASE_URL=https://example.test\n"
    _mols, normalization = normalize_ligand_text(text)
    reasons = " ".join(str(s.get("reason") or "") for s in (normalization.get("skipped") or []))
    assert "sk-1eaf945e6e584c4b837282e5f5f77e97" not in reasons, reasons
    assert "已隐去" in reasons, reasons


def test_parse_failure_hint_still_shows_normal_bad_lines() -> None:
    """普通坏行仍要给出可定位的提示（不能因为脱敏把诊断能力一并砍掉）。"""
    from docking_agent.core.normalize import normalize_ligand_text

    _mols, normalization = normalize_ligand_text("CCO\n这不是分子也不是名称行太长太长太长太长太长太长\n")
    reasons = " ".join(str(s.get("reason") or "") for s in (normalization.get("skipped") or []))
    assert "第 2 行" in reasons, reasons


# --------------------------------------------------------------------------- #
# 5) 跨站请求（同源校验）
# --------------------------------------------------------------------------- #
def test_cross_site_write_is_rejected(client: TestClient) -> None:
    """带不同源 Origin 的写请求必须 403（无鉴权服务的唯一浏览器侧防线）。"""
    resp = client.put("/api/settings", headers={"origin": "http://evil.example"},
                      json={"values": {}})
    assert resp.status_code == 403, resp.text
    assert "跨站" in resp.text


def test_cross_site_write_is_rejected_via_sec_fetch_site(client: TestClient) -> None:
    resp = client.post("/api/settings/test", headers={"sec-fetch-site": "cross-site"},
                       json={"role": "coordinator"})
    assert resp.status_code == 403, resp.text


def test_same_origin_write_is_allowed(client: TestClient) -> None:
    resp = client.post("/api/settings/test", headers={"origin": "http://testserver",
                                                     "host": "testserver"},
                       json={"role": "__nonexistent__"})
    assert resp.status_code == 200, resp.text
    assert resp.json().get("status") == "error"      # 角色不存在，但请求本身被放行


def test_request_without_origin_is_allowed(client: TestClient) -> None:
    """CLI / 测试 / 同源导航不带 Origin，必须放行。"""
    resp = client.get("/api/health")
    assert resp.status_code == 200, resp.text


def test_security_headers_are_present(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert "script-src 'self'" in resp.headers.get("content-security-policy", "")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"


def test_models_endpoint_does_not_echo_upstream_base_url(client: TestClient) -> None:
    """端点指纹不必要地暴露给无鉴权接口。"""
    resp = client.get("/api/models")
    assert resp.status_code == 200, resp.text
    assert "base_url" not in resp.json()
