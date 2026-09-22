"""设置页面后端（config/local_settings.json + /api/settings）回归测试。

全部离线可跑：不调用真实模型；模型列表接口用桩替换。
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

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def settings_file(tmp_path, monkeypatch):
    """把设置文件指向临时目录，避免污染仓库与其它测试。"""
    path = tmp_path / "local_settings.json"
    monkeypatch.setenv("LOCAL_SETTINGS_PATH", str(path))
    from docking_agent import settings as S

    yield path
    # 清理可能被写入的环境变量，避免跨用例串扰
    for spec in S.ENV_SPECS:
        if spec.env and spec.env not in S.DEPLOY_PROTECTED:
            monkeypatch.delenv(spec.env, raising=False)


@pytest.fixture()
def client(settings_file, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-secret-1234")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    monkeypatch.delenv("LLM_MODEL_DOCKING", raising=False)
    from docking_agent.api.app import app

    with TestClient(app) as c:
        yield c


def _settings(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    return r.json()


# --------------------------------------------------------------------------- #
# 读写回环
# --------------------------------------------------------------------------- #
def test_settings_snapshot_shape(client, settings_file):
    data = _settings(client)
    assert len(data["specs"]) > 40
    assert [g["id"] for g in data["groups"]] == [
        "llm", "models", "roles", "docking", "external", "runtime", "deploy"]
    for key in ("values", "effective", "sources", "secret", "meta", "roles", "agent_models"):
        assert key in data
    # 每个 spec 都要有当前值/生效值/来源三个视图
    for spec in data["specs"]:
        assert spec["path"] in data["values"]
        assert spec["path"] in data["sources"]
        assert spec["path"] in data["effective"]
    assert data["values"]["llm.model"] is None, "尚未保存时不应有界面设置值"
    assert data["sources"]["llm.model"] == "环境变量"


def test_settings_save_roundtrip(client, settings_file):
    r = client.put("/api/settings", json={"settings": {
        "llm": {"model": "ui-model", "temperature": 0.35},
        "roles": {"docking": {"model": "docking-model", "temperature": 0.1}},
        "runtime": {"sse_batch_size": 7},
        "docking": {"exhaustiveness": 9},
    }})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert "SSE_BATCH_SIZE" in body["applied_env"]
    assert settings_file.is_file()

    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["llm"]["model"] == "ui-model"
    assert on_disk["roles"]["docking"]["model"] == "docking-model"
    assert on_disk["runtime"]["sse_batch_size"] == 7

    data = _settings(client)
    assert data["values"]["llm.model"] == "ui-model"
    assert data["values"]["roles.docking.model"] == "docking-model"
    assert data["sources"]["llm.model"] == "界面设置"
    assert data["sources"]["roles.docking.temperature"] == "界面设置"
    # 未配置的角色继承全局界面设置
    assert data["effective"]["roles.binding.model"] == "ui-model"
    assert data["effective"]["roles.property.temperature"] == 0.0, "内置角色默认仍然生效"


def test_settings_clear_value_returns_to_inherit(client, settings_file):
    client.put("/api/settings", json={"settings": {"roles": {"docking": {"model": "docking-model"}}}})
    # 传空字符串 = 清除该项（回到继承）
    r = client.put("/api/settings", json={"settings": {"roles": {"docking": {"model": ""}}}})
    assert r.status_code == 200
    data = r.json()["settings"]
    assert data["values"]["roles.docking.model"] is None
    assert data["effective"]["roles.docking.model"] == "env-model"


def test_settings_api_key_is_write_only_and_masked(client, settings_file):
    data = _settings(client)
    assert data["values"]["llm.api_key"] is None, "密钥不回显"
    assert data["secret"]["api_key_set"] is True
    assert data["secret"]["api_key_hint"] == "sk-t…1234"
    assert "secret-1234" not in json.dumps(data, ensure_ascii=False)

    client.put("/api/settings", json={"settings": {"llm": {"api_key": "sk-ui-key-9999"}}})
    text = client.get("/api/settings").text
    assert "sk-ui-key-9999" not in text, "保存后也不得回显明文密钥"
    assert "sk-u…9999" in text


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def test_settings_rejects_unknown_and_invalid(client, settings_file):
    r = client.put("/api/settings", json={"settings": {"nope.field": 1}})
    assert r.status_code == 400
    assert "未知配置项" in r.json()["error_message"]

    r = client.put("/api/settings", json={"settings": {"docking": {"engine": "not-an-engine"}}})
    assert r.status_code == 400
    assert "engine" in r.json()["error_message"]

    r = client.put("/api/settings", json={"settings": {"runtime": {"sse_batch_size": -5}}})
    assert r.status_code == 400

    r = client.put("/api/settings", json={"settings": {"llm": {"extra_body": "{not json"}}})
    assert r.status_code == 400
    assert "JSON" in r.json()["error_message"]

    assert not settings_file.exists(), "校验失败时不得写入设置文件"


def test_settings_accepts_json_fields(client, settings_file):
    r = client.put("/api/settings", json={"settings": {
        "llm": {"extra_body": {"top_k": 40}, "extra_headers": {"X-Test": "1"}}}})
    assert r.status_code == 200
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["llm"]["extra_body"] == {"top_k": 40}
    assert on_disk["llm"]["extra_headers"] == {"X-Test": "1"}


# --------------------------------------------------------------------------- #
# 优先级与生效
# --------------------------------------------------------------------------- #
def test_ui_settings_override_env_but_role_env_wins(client, settings_file, monkeypatch):
    client.put("/api/settings", json={"settings": {"llm": {"model": "ui-model"}}})
    from docking_agent.runtime.llm import effective_config, resolve_role_config

    assert effective_config("")["model"] == "ui-model", "界面设置优先于 .env"
    assert effective_config("docking")["model"] == "ui-model"

    # 角色专属环境变量仍然优先（脚本/CI 场景的显式覆盖）
    monkeypatch.setenv("LLM_MODEL_DOCKING", "shell-model")
    cfg, src = resolve_role_config("docking")
    assert cfg["model"] == "shell-model"
    assert src["model"] == "角色环境变量"
    assert effective_config("property")["model"] == "ui-model"


def test_runtime_settings_reach_process_env(client, settings_file):
    from docking_agent.config import env_int

    client.put("/api/settings", json={"settings": {"runtime": {
        "sse_batch_size": 3, "report_top_n": 11, "log_level": "WARNING"}}})
    assert env_int("SSE_BATCH_SIZE", 25) == 3
    assert env_int("REPORT_TOP_N", 50) == 11
    import os

    assert os.environ["LOG_LEVEL"] == "WARNING"
    data = _settings(client)
    assert data["sources"]["runtime.sse_batch_size"] == "界面设置"


def test_deploy_protected_fields_keep_env_priority(client, settings_file, monkeypatch):
    monkeypatch.setenv("DOCKING_MAX_LIGANDS", "5")
    client.put("/api/settings", json={"settings": {"deploy": {"docking_max_ligands": 99}}})
    import os

    from docking_agent import settings as S

    assert os.environ["DOCKING_MAX_LIGANDS"] == "5", "部署保护项由 .env 决定"
    spec = S.SPEC_BY_PATH["deploy.docking_max_ligands"]
    assert S.runtime_source(spec).startswith("环境变量")
    assert str(S.runtime_effective(spec)) == "5"


def test_settings_reset_clears_file_and_env(client, settings_file):
    client.put("/api/settings", json={"settings": {"runtime": {"sse_batch_size": 4}}})
    import os

    assert os.environ.get("SSE_BATCH_SIZE") == "4"
    r = client.post("/api/settings/reset")
    assert r.status_code == 200
    assert r.json()["removed"] is True
    assert not settings_file.exists()
    assert os.environ.get("SSE_BATCH_SIZE") is None
    assert _settings(client)["values"]["llm.model"] is None


def test_settings_reload_rebuilds_agents(client):
    from docking_agent.agents import workers

    r = client.post("/api/settings/reload")
    assert r.status_code == 200 and r.json()["reloaded"] is True
    assert workers.get_property_agent() is None, "重载后子 Agent 需要重新构建"
    from docking_agent.api import app as app_mod

    assert app_mod.state.graph is None


def test_docking_defaults_exposed_for_form_prefill(client, settings_file):
    client.put("/api/settings", json={"settings": {"docking": {
        "engine": "vina", "exhaustiveness": 12, "positive_control": "NC(=N)c1ccccc1"}}})
    data = _settings(client)
    assert data["effective"]["docking.exhaustiveness"] == 12
    assert data["effective"]["docking.positive_control"] == "NC(=N)c1ccccc1"
    assert data["values"]["docking.engine"] == "vina"


# --------------------------------------------------------------------------- #
# 模型列表 / 连通性测试（全部用桩，不联网）
# --------------------------------------------------------------------------- #
def test_models_endpoint_without_credentials(monkeypatch, settings_file):
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LOCAL_SETTINGS_PATH", str(settings_file))
    from docking_agent.api.app import _fetch_endpoint_models

    out = _fetch_endpoint_models()
    assert out["status"] == "error" and out["models"] == []


def test_models_endpoint_parses_openai_shape(monkeypatch, settings_file):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    from docking_agent.api import app as app_mod

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"object": "list", "data": [{"id": "m-b"}, {"id": "m-a"}]}).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    out = app_mod._fetch_endpoint_models()
    assert out["status"] == "ok"
    assert out["models"] == ["m-a", "m-b"]


def test_role_connectivity_test_reports_actual_model(monkeypatch, settings_file):
    from docking_agent.api import app as app_mod
    from docking_agent.runtime import llm as llm_mod

    class _Reply:
        content = "就绪"
        response_metadata = {"model_name": "server-model"}

    class _FakeLLM:
        def invoke(self, *a, **k):
            return _Reply()

    monkeypatch.setattr(llm_mod, "build_chat_llm", lambda ctx=None, role="": _FakeLLM())
    out = app_mod._test_role_llm("docking")
    assert out["status"] == "ok"
    assert out["actual_model"] == "server-model"
    assert out["role"] == "docking"


def test_role_connectivity_test_rejects_unknown_role(settings_file):
    from docking_agent.api.app import _test_role_llm

    assert _test_role_llm("nope")["status"] == "error"


def test_settings_removes_file_when_everything_cleared(client, settings_file):
    """把所有项都清空后应删除设置文件，而不是留下一个空对象。"""
    client.put("/api/settings", json={"settings": {"llm": {"model": "tmp-model"}}})
    assert settings_file.is_file()
    r = client.put("/api/settings", json={"settings": {"llm": {"model": ""}}})
    assert r.status_code == 200
    assert r.json()["removed"] is True
    assert not settings_file.exists()
    assert r.json()["settings"]["values"]["llm.model"] is None


# --------------------------------------------------------------------------- #
# 加固项（对抗性复核发现的问题，逐条留下回归用例）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def clean_injected(settings_file):
    """清空「界面注入环境变量」的基线记录，避免用例间串扰。"""
    from docking_agent import settings as S

    S._INJECTED.clear()
    yield
    S._INJECTED.clear()


def test_broken_settings_file_does_not_break_startup(settings_file, monkeypatch):
    """手改坏的设置文件（结构错误）不得让服务起不来。"""
    monkeypatch.setenv("LLM_API_KEY", "k")
    settings_file.write_text('{"llm": 123, "roles": ["x"], "runtime": "nope"}', encoding="utf-8")
    from docking_agent import settings as S
    from docking_agent.runtime.llm import resolve_role_config

    data = S.load_local_settings()
    # 非法段落必须被丢弃（`llm: 123` / `roles: ["x"]` / `runtime: "nope"` 都不能活下来）；
    # 原来的 `data == {} or all(...)` 里的 `data == {}` 是多余的（空 dict 也满足 all()），
    # 且掩盖了真实意图：**只有 dict 段落允许保留**。
    assert isinstance(data, dict)
    assert all(isinstance(v, dict) for v in data.values()), data
    assert isinstance(data.get("llm", {}), dict) and isinstance(data.get("roles", {}), dict)
    cfg, _src = resolve_role_config("docking")
    assert cfg["model"]  # 仍然能解析出配置

    settings_file.write_text('{"llm": {"model": "ok-model"}}', encoding="utf-8")
    assert resolve_role_config("")[0]["model"] == "ok-model"


def test_clearing_setting_restores_env_baseline(settings_file, monkeypatch, clean_injected):
    """清空某项后必须撤销注入（不能残留上一轮的值），且重复调用幂等。"""
    import os

    from docking_agent import settings as S

    monkeypatch.delenv("DOCKING_WORKERS", raising=False)
    settings_file.write_text('{"runtime": {"docking_workers": 6}}', encoding="utf-8")
    assert S.apply_runtime_env() == ["DOCKING_WORKERS"]
    assert os.environ["DOCKING_WORKERS"] == "6"
    assert S.apply_runtime_env() == ["DOCKING_WORKERS"], "重复应用应幂等"
    assert os.environ["DOCKING_WORKERS"] == "6"

    settings_file.write_text("{}", encoding="utf-8")
    assert S.apply_runtime_env() == []
    assert os.environ.get("DOCKING_WORKERS") is None, "清空后不得残留"
    spec = S.SPEC_BY_PATH["runtime.docking_workers"]
    assert S.runtime_source(spec) == "内置默认"


def test_clearing_setting_restores_original_env_value(settings_file, monkeypatch, clean_injected):
    import os

    from docking_agent import settings as S

    monkeypatch.setenv("REPORT_TOP_N", "33")     # 外部（.env/shell）注入
    settings_file.write_text('{"runtime": {"report_top_n": 7}}', encoding="utf-8")
    S.apply_runtime_env()
    assert os.environ["REPORT_TOP_N"] == "7"
    settings_file.write_text("{}", encoding="utf-8")
    S.apply_runtime_env()
    assert os.environ["REPORT_TOP_N"] == "33", "应恢复到注入前的值"


def test_reset_keeps_external_env(client, settings_file, monkeypatch, clean_injected):
    """reset 只能回滚「界面注入」的键，不能误删 shell / CI 注入的键。"""
    import os

    monkeypatch.setenv("DOCKING_WORKERS", "32")   # 外部注入，不由界面写入
    client.put("/api/settings", json={"settings": {"runtime": {"sse_batch_size": 4}}})
    assert os.environ["SSE_BATCH_SIZE"] == "4"
    r = client.post("/api/settings/reset")
    assert r.status_code == 200
    assert os.environ.get("SSE_BATCH_SIZE") is None, "界面注入的键应被撤销"
    assert os.environ.get("DOCKING_WORKERS") == "32", "外部注入的键必须保留"


def test_deploy_protected_upload_cap_is_env_first(client, settings_file, monkeypatch,
                                                  clean_injected):
    """.env 显式设置了上限时，界面设置不得放宽它（保护机器）。"""
    import os

    monkeypatch.setenv("UPLOAD_MAX_MB", "50")
    r = client.put("/api/settings", json={"settings": {"deploy": {"upload_max_mb": 99999}}})
    assert r.status_code == 200
    from docking_agent import settings as S

    S.apply_runtime_env()
    assert os.environ["UPLOAD_MAX_MB"] == "50"
    spec = S.SPEC_BY_PATH["deploy.upload_max_mb"]
    assert S.runtime_effective(spec) == "50"


def test_readonly_port_is_rejected(client, settings_file):
    r = client.put("/api/settings", json={"settings": {"deploy": {"port": 8080}}})
    assert r.status_code == 400
    assert "界面不可修改" in r.json()["error_message"]


def test_connectivity_test_does_not_wipe_agent_models(client, settings_file, monkeypatch,
                                                      clean_injected):
    """「测试连通性」不能破坏已有角色的模型溯源信息。"""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    from docking_agent.api import app as app_mod
    from docking_agent.runtime import llm as llm_mod

    llm_mod.reset_llm_registry()
    llm_mod.build_chat_llm(None, role="property")     # 先有一个「已构建」的角色
    before = llm_mod.registry_entry("property")
    assert before and before.get("instance_id")

    class _Reply:
        content = "就绪"
        response_metadata = {"model_name": "server-model"}

    class _FakeLLM:
        def invoke(self, *a, **k):
            return _Reply()

    monkeypatch.setattr(llm_mod, "build_chat_llm", lambda ctx=None, role="": _FakeLLM())
    out = app_mod._test_role_llm("docking")
    assert out["status"] == "ok" and out["actual_model"] == "server-model"

    after = llm_mod.llm_registry()
    assert after.get("property") == before, "其他角色的登记必须原样保留"
    assert "docking" not in after, "测试用的临时实例不应留在登记表里"


def test_upstream_error_text_is_redacted(monkeypatch, settings_file):
    """上游错误体可能回显请求头：对外错误必须脱敏。"""
    import urllib.error

    monkeypatch.setenv("LLM_API_KEY", "sk-live-abcdef1234567890")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    from docking_agent.api import app as app_mod

    body = b'{"error":"bad key","echo":"Bearer sk-live-abcdef1234567890"}'

    def _boom(*args, **kwargs):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", None,
                                     __import__("io").BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    out = app_mod._fetch_endpoint_models()
    assert out["status"] == "error"
    assert "sk-live-abcdef1234567890" not in out["error"], "明文密钥不得出现在错误信息里"


def test_extra_headers_secrets_are_masked_but_preserved(client, settings_file):
    """额外请求头里的密钥只回显 ***，回传 *** 时保持原值。"""
    r = client.put("/api/settings", json={"settings": {"llm": {"extra_headers": {
        "X-Api-Key": "sk-header-secret-9876", "X-Org": "lab"}}}})
    assert r.status_code == 200
    data = _settings(client)
    headers = data["values"]["llm.extra_headers"]
    assert headers["X-Api-Key"] == "***"
    assert headers["X-Org"] == "lab"
    assert "sk-header-secret-9876" not in json.dumps(data, ensure_ascii=False)

    # 用户在界面上没改这一项，原样回传 *** → 后端保留真实值
    client.put("/api/settings", json={"settings": {"llm": {"extra_headers": headers}}})
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["llm"]["extra_headers"]["X-Api-Key"] == "sk-header-secret-9876"


def test_zero_and_false_are_not_treated_as_unset(client, settings_file):
    """0 / false 是有效值，不能被当成「未设置」丢掉。"""
    r = client.put("/api/settings", json={"settings": {
        "docking": {"max_ligands": 0, "save_poses": False},
        "llm": {"temperature": 0}}})
    assert r.status_code == 200
    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["docking"]["max_ligands"] == 0
    assert on_disk["docking"]["save_poses"] is False
    assert on_disk["llm"]["temperature"] == 0
    data = _settings(client)
    assert data["effective"]["llm.temperature"] == 0


def test_registry_counters_reset_per_run(monkeypatch, settings_file):
    """运行记录里的 calls 必须是「本次运行」的，不能随进程累计。"""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    from docking_agent.runtime import llm as llm_mod

    llm_mod.reset_llm_registry()
    try:
        llm_mod.build_chat_llm(None, role="docking")
        handler = llm_mod._ModelUsageHandler("docking")
        handler.on_llm_start({}, ["hi"])
        handler.on_llm_start({}, ["hi"])
        assert llm_mod.llm_registry()["docking"]["calls"] == 2
        before = llm_mod.registry_entry("docking")

        llm_mod.reset_registry_counters()
        after = llm_mod.llm_registry()["docking"]
        assert after.get("calls") is None, "计数应清零"
        assert after["model"] == before["model"], "实例信息（模型）必须保留"
        assert after["instance_id"] == before["instance_id"], "同一个进程里实例不变"
    finally:
        llm_mod.reset_llm_registry()
