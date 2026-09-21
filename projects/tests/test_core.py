"""本地运行时单元测试（快速，不触发网络与真实对接）。

运行： .venv/bin/python -m pytest -q
"""
from __future__ import annotations

import json
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


# --------------------------------------------------------------------------- #
# 入参归一化
# --------------------------------------------------------------------------- #
def test_payload_langchain_style():
    from docking_agent.runtime.payload import normalize_agent_input

    out = normalize_agent_input({"messages": [{"role": "user", "content": "你好"}]})
    assert out["messages"][0].content == "你好"


def test_payload_plain_text():
    from docking_agent.runtime.payload import normalize_agent_input

    assert normalize_agent_input({"text": "筛选分子"})["messages"][0].content == "筛选分子"
    assert normalize_agent_input("直接字符串")["messages"][0].content == "直接字符串"


def test_payload_coze_style():
    from docking_agent.runtime.payload import normalize_agent_input

    payload = {"type": "query", "content": {"query": {"prompt": [
        {"type": "text", "content": {"text": "帮我做分子对接"}}]}}}
    assert normalize_agent_input(payload)["messages"][0].content == "帮我做分子对接"


def test_payload_invalid():
    from docking_agent.runtime.payload import PayloadError, normalize_agent_input

    with pytest.raises(PayloadError):
        normalize_agent_input({"foo": "bar"})


# --------------------------------------------------------------------------- #
# 本地产物存储
# --------------------------------------------------------------------------- #
def test_artifact_roundtrip():
    from docking_agent.reporting.store import resolve_output, save_artifact

    rec = save_artifact(b"hello", "unit_test.txt", "text/plain")
    assert Path(rec["path"]).read_bytes() == b"hello"
    assert rec["url"].endswith(f"/files/{rec['key']}")
    assert resolve_output(rec["key"]) == Path(rec["path"])


@pytest.mark.parametrize("bad", ["../secret", "a/b.txt", "..", ""])
def test_artifact_traversal_rejected(bad):
    from docking_agent.reporting.store import resolve_output

    assert resolve_output(bad) is None


# --------------------------------------------------------------------------- #
# LLM 配置
# --------------------------------------------------------------------------- #
def test_llm_config_env_override(monkeypatch):
    from docking_agent.runtime.llm import load_llm_config

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:9999/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    cfg = load_llm_config()["config"]
    assert cfg["api_key"] == "test-key"
    assert cfg["base_url"] == "http://localhost:9999/v1"
    assert cfg["model"] == "test-model"


def test_build_llm_without_key_raises(monkeypatch):
    from docking_agent.runtime.llm import LLMConfigError, build_chat_llm

    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMConfigError):
        build_chat_llm(None)


def test_system_prompt_loaded():
    from docking_agent.runtime.llm import load_llm_config

    cfg = load_llm_config()
    assert "整体协调 Agent" in cfg.get("sp", "")


def test_coordinator_tool_list_matches_config(monkeypatch) -> None:
    """配置声明的工具清单必须与协调 Agent **实际绑定**的工具完全一致。

    旧实现用 `inspect.getsource` + 正则数 `tools=[...]` 的行，任何格式化/换行改动都会误伤；
    这里直接构建图并读出 ToolNode 真正绑定的工具名（同样零网络：假模型 + 假 checkpointer）。
    """
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from docking_agent.agents import coordinator
    from docking_agent.runtime.llm import load_llm_config

    class _Fake(GenericFakeChatModel):
        def bind_tools(self, *args: Any, **kwargs: Any) -> "_Fake":
            return self

    monkeypatch.setattr(coordinator, "init_workers", lambda ctx=None: None)
    monkeypatch.setattr(coordinator, "build_chat_llm",
                        lambda ctx=None, role="": _Fake(messages=iter(["ok"])))
    monkeypatch.setattr(coordinator, "get_memory_saver", lambda: None)

    graph = coordinator.build_agent(None)
    real = set(graph.nodes["tools"].bound.tools_by_name)
    declared = set(load_llm_config().get("tools") or [])
    assert declared, "配置里必须声明主管可用工具"
    assert declared == real, (
        f"配置与实际的工具集不一致：配置多 {sorted(declared - real)}、实际多 {sorted(real - declared)}")


# --------------------------------------------------------------------------- #
# 按角色配置：每个 Agent 一个独立模型实例
# --------------------------------------------------------------------------- #
AGENT_ROLES = {"intake", "coordinator", "property", "pocket", "docking", "binding"}


def test_role_config_precedence(monkeypatch):
    """优先级：角色环境变量 > 角色文件项(roles) > 全局环境变量 > 全局文件项(config)。"""
    from docking_agent.runtime import llm as llm_mod

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "global-model")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.9")
    for name in ("LLM_MODEL_DOCKING", "LLM_TEMPERATURE_DOCKING", "LLM_TEMPERATURE_PROPERTY"):
        monkeypatch.delenv(name, raising=False)

    # 角色文件项不被全局环境变量压掉（否则 .env 里的 LLM_TEMPERATURE 会让 roles 段失效）
    assert llm_mod.effective_config("property")["temperature"] == 0.0
    assert llm_mod.effective_config("docking")["temperature"] == 0.1
    # 未覆盖的角色继承全局配置
    assert llm_mod.effective_config("coordinator")["model"] == "global-model"
    assert llm_mod.effective_config("coordinator")["temperature"] == 0.9

    # 角色环境变量优先于角色文件项
    monkeypatch.setenv("LLM_MODEL_DOCKING", "docking-model")
    monkeypatch.setenv("LLM_TEMPERATURE_DOCKING", "0.5")
    docking = llm_mod.effective_config("docking")
    assert docking["model"] == "docking-model"
    assert docking["temperature"] == 0.5
    # 其他角色不受影响
    assert llm_mod.effective_config("property")["model"] == "global-model"


def test_describe_roles_covers_every_agent_role(monkeypatch):
    from docking_agent.runtime import llm as llm_mod

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    roles = llm_mod.describe_roles()
    assert set(roles) == set(llm_mod.ROLES) == AGENT_ROLES
    assert all(r.get("model") for r in roles.values())
    assert all("temperature" in r for r in roles.values())


def test_build_chat_llm_creates_distinct_instance_per_role(monkeypatch):
    """每个角色都必须拿到**独立**的模型实例，且登记表能区分它们。"""
    from docking_agent.runtime import llm as llm_mod

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    llm_mod.reset_llm_registry()
    try:
        built = [llm_mod.build_chat_llm(None, role=r) for r in ("property", "docking", "binding")]
        assert len({id(x) for x in built}) == 3, "三个子 Agent 不能共用同一个模型对象"

        reg = llm_mod.llm_registry()
        assert set(reg) == {"property", "docking", "binding"}
        assert len({v["instance_id"] for v in reg.values()}) == 3
        # 各自记录了自己真实使用的模型
        assert all(v.get("model") for v in reg.values())
    finally:
        llm_mod.reset_llm_registry()


def test_llm_usage_handler_records_server_confirmed_model(monkeypatch):
    """回调要能把服务端回执的模型名按角色记下来（证明「实际用了什么模型」）。"""
    from docking_agent.runtime import llm as llm_mod

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    llm_mod.reset_llm_registry()
    try:
        llm_mod.build_chat_llm(None, role="docking")

        class _Msg:
            response_metadata = {"model_name": "server-model-x"}

        class _Gen:
            message = _Msg()

        class _Resp:
            generations = [[_Gen()]]
            llm_output = {"model_name": "server-model-x"}

        handler = llm_mod._ModelUsageHandler("docking")
        handler.on_llm_start({}, ["hi"])
        handler.on_llm_end(_Resp())

        meta = llm_mod.llm_registry()["docking"]
        assert meta["calls"] == 1
        assert meta["actual_model"] == "server-model-x"
        assert meta["actual_models"] == ["server-model-x"]
    finally:
        llm_mod.reset_llm_registry()


def test_report_shows_each_agent_model():
    """报告首表要写明各 Agent 用的模型（按角色独立实例，可溯源）。"""
    from docking_agent.reporting import build_markdown_report

    md = build_markdown_report(
        {"ranking": [], "molecules": [], "binding": {}},
        kind="agent", run_id="R1",
        agent_models={"coordinator": {"model": "m-a", "actual_model": "m-a"},
                      "docking": {"model": "m-b"},
                      "property": {"model": None}},
    )
    assert "各 Agent 模型" in md
    assert "对接执行 `m-b`" in md and "协调 `m-a`" in md


# --------------------------------------------------------------------------- #
# 分子核心计算
# --------------------------------------------------------------------------- #
def test_parse_smiles_text():
    from docking_agent.core import parse_smiles_text

    mols = parse_smiles_text("阿司匹林:CC(=O)Oc1ccccc1C(=O)O，乙醇:CCO；无效片段:not_a_smiles")
    names = {m["name"]: m["smiles"] for m in mols}
    assert len(mols) == 2
    assert names["阿司匹林"] == "CC(=O)Oc1ccccc1C(=O)O"
    assert names["乙醇"] == "CCO"


def test_compute_properties():
    from docking_agent.core import compute_properties

    # 默认策略是 ph(7.4)：羧酸按生理 pH 去质子化，分子式因此是阴离子形式
    p = compute_properties("CC(=O)Oc1ccccc1C(=O)O", protonation="keep")  # 阿司匹林
    assert p["formula"] == "C9H8O4"
    assert 179 < p["molecular_weight"] < 181
    assert p["hba"] >= 3
    assert p["hbd"] == 1
    assert p["drug_likeness_pass"] is True


def test_receptor_registry_paths_exist():
    from docking_agent.core import RECEPTOR_REGISTRY

    for key, spec in RECEPTOR_REGISTRY.items():
        assert Path(spec["pdbqt"]).is_file(), f"{key} 受体文件缺失: {spec['pdbqt']}"
        assert len(spec["center"]) == 3 and len(spec["size"]) == 3


def test_tanimoto_and_binding_report():
    from docking_agent.core import compute_binding_report, compute_tanimoto

    assert compute_tanimoto("CCO", "CCO") == pytest.approx(1.0)
    assert compute_tanimoto("CCO", "NC(=N)c1ccccc1") < 0.2
    report = compute_binding_report(
        [{"name": "a", "smiles": "NC(=N)c1ccccc1"}, {"name": "b", "smiles": "CCO"}], "NC(=N)c1ccccc1"
    )
    assert report["rows"][0]["name"] == "a"
    assert report["rows"][0]["similarity_to_positive_control"] == pytest.approx(1.0)


def test_merge_and_rank_orders_by_affinity():
    from docking_agent.core import merge_and_rank

    props = [{"smiles": "CCO", "name": "a"}, {"smiles": "CCC", "name": "b"}]
    docks = [{"smiles": "CCO", "name": "a", "affinity_kcal_mol": -3.0},
             {"smiles": "CCC", "name": "b", "affinity_kcal_mol": -7.5}]
    binding = {"rows": [{"smiles": "CCO", "similarity_to_positive_control": 0.2},
                        {"smiles": "CCC", "similarity_to_positive_control": 0.5}]}
    ranked = merge_and_rank(props, docks, binding)
    assert [r["name"] for r in ranked] == ["b", "a"]


def test_example_library_and_positive_control():
    from docking_agent.pipeline import load_positive_control, resolve_molecules

    mols, source = resolve_molecules("", "", allow_example_fallback=True)
    assert source == "example-library"
    assert len(mols) >= 5
    assert load_positive_control() == "NC(=N)c1ccccc1"


def test_resolve_molecules_no_fallback():
    from docking_agent.pipeline import resolve_molecules

    mols, source = resolve_molecules("", "", allow_example_fallback=False)
    assert mols == [] and source == "none"


def test_serialize_result_handles_messages():
    from langchain_core.messages import AIMessage, HumanMessage

    from docking_agent.api.app import _serialize

    out = _serialize({"messages": [HumanMessage(content="hi"), AIMessage(content="yo")]})
    assert out["messages"][0] == {"role": "user", "content": "hi"}
    assert out["messages"][1]["role"] == "assistant"


def test_receptor_registry_has_known_site():
    from docking_agent.core import DEFAULT_RECEPTOR, RECEPTOR_REGISTRY, list_receptors

    assert DEFAULT_RECEPTOR in RECEPTOR_REGISTRY
    infos = {r["key"]: r for r in list_receptors()}
    assert "thrombin" in infos
    site = infos["thrombin"]["site"]
    assert len(site["center"]) == 3 and len(site["size"]) == 3
    assert infos["thrombin"]["available"] is True


def test_run_store_roundtrip(tmp_path):
    from docking_agent.runs import RunStore

    store = RunStore(tmp_path / "runs")
    run = store.new("pipeline", {"x": 1})
    run.write_json("molecules", [{"name": "a", "smiles": "CCO"}], label="库")
    run.write_text("ranking.csv", "a,b\n1,2\n", name="ranking_csv", label="CSV")
    run.finish("ok", molecule_count=1)

    assert store.exists(run.id)
    detail = store.detail(run.id)
    assert detail["run"]["molecule_count"] == 1
    names = {a["name"] for a in detail["artifacts"]}
    assert {"molecules", "ranking_csv"} <= names
    assert store.artifact_path(run.id, "ranking_csv").read_text(encoding="utf-8").startswith("a,b")
    assert store.artifact_path(run.id, "../run.json") is None
    zipped = store.zip(run.id)
    assert zipped and zipped.is_file()
    assert run.id in [r["run_id"] for r in store.list()]


def test_json_dump_pipeline_result(tmp_path, monkeypatch):
    from docking_agent.pipeline import run_pipeline

    # 仅验证无分子时的明确提示（不触发对接）
    result = run_pipeline(ligands_text="", allow_example_fallback=False)
    assert result["status"] == "no_molecules"
    assert "SMILES" in result["message"]
    json.dumps(result, ensure_ascii=False)  # 可序列化


# --------------------------------------------------------------------------- #
# 回归：结合模式检测（曾因拆分 core 时漏导出 _fingerprint 而静默回退）
# --------------------------------------------------------------------------- #
def test_binding_mode_analysis_tool_returns_full_result():
    import json

    from docking_agent.tools.binding import binding_mode_analysis

    mols = json.dumps([{"name": "苯甲脒", "smiles": "NC(=N)c1ccccc1"},
                       {"name": "阿司匹林", "smiles": "CC(=O)Oc1ccccc1C(=O)O"}], ensure_ascii=False)
    out = json.loads(binding_mode_analysis.invoke(
        {"molecules_json": mols, "positive_control_smiles": "NC(=N)c1ccccc1"}))

    assert out["status"] == "ok", f"结合模式工具不应报错：{out}"
    rows = {r["name"]: r for r in out["results"]}
    # 与阳性对照自身：相似度必须为 1
    assert rows["苯甲脒"]["similarity_to_positive_control"] == 1.0
    assert rows["苯甲脒"]["structural_consistency"] == "high"
    # 完整方法学字段必须齐全（不能退化成只有相似度）
    for key in ("morgan_tanimoto", "maccs_tanimoto", "combined_similarity",
                "structural_consistency", "binding_mode_hint", "pharmacophore",
                "anchor_match", "mw_delta"):
        assert key in rows["阿司匹林"], f"缺少字段 {key}"
    # 真实 SMARTS 药效团：苯甲脒含脒基，阿司匹林不含
    assert rows["苯甲脒"]["pharmacophore"]["cationic_anchor"] is True
    assert rows["阿司匹林"]["pharmacophore"]["cationic_anchor"] is False
    assert "control_pharmacophore" in out


def test_binding_tools_are_consistent():
    """两个结合模式工具必须口径一致，避免子 Agent 选谁结果不同。"""
    import json

    from docking_agent.tools.binding import binding_mode_analysis, positive_control_similarity

    mols = json.dumps([{"name": "A", "smiles": "NC(=N)c1ccc2cc(OC(=O)c3ccc(NC(=N)N)cc3)ccc2c1"},
                       {"name": "B", "smiles": "CCO"}], ensure_ascii=False)
    args = {"molecules_json": mols, "positive_control_smiles": "NC(=N)c1ccccc1"}
    full = json.loads(binding_mode_analysis.invoke(args))
    lite = json.loads(positive_control_similarity.invoke(args))
    assert full["status"] == "ok" and lite["status"] == "ok"
    assert ([r["similarity_to_positive_control"] for r in full["results"]]
            == [r["similarity_to_positive_control"] for r in lite["results"]])


def test_binding_tools_handle_bad_input():
    import json

    from docking_agent.tools.binding import binding_mode_analysis

    bad = json.loads(binding_mode_analysis.invoke({"molecules_json": "[]", "positive_control_smiles": "CCO"}))
    assert bad["status"] == "error"
    nomol = json.loads(binding_mode_analysis.invoke(
        {"molecules_json": '[{"name":"x"}]', "positive_control_smiles": "CCO"}))
    assert nomol["status"] == "error"


def test_all_internal_imports_resolvable():
    """永久回归：所有 docking_agent 内部导入都必须能解析。

    这条测试用于防止「拆分模块后漏导出」这类静默失败（例如 core._fingerprint）。
    """
    import ast
    import importlib
    from pathlib import Path

    root = PROJECT_ROOT
    files = (list((root / "src" / "docking_agent").rglob("*.py"))
             + list((root / "tests").rglob("*.py"))
             + list((root / "scripts").rglob("*.py")))
    problems = []
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ImportFrom) and node.module
                    and node.module.startswith("docking_agent")):
                continue
            try:
                mod = importlib.import_module(node.module)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{f}:{node.lineno} 模块导入失败 {node.module}: {e}")
                continue
            for alias in node.names:
                if alias.name != "*" and not hasattr(mod, alias.name):
                    problems.append(f"{f}:{node.lineno} {node.module} 没有 {alias.name!r}")
    assert problems == [], "存在无法解析的内部导入：\n" + "\n".join(problems)


def test_pose_artifact_fallback_resolution(tmp_path):
    """大库时位姿不逐个进清单，但仍要能按产物名直接下载。"""
    from docking_agent.runs import RunStore

    store = RunStore(tmp_path / "runs")
    run = store.new("pipeline", {})
    poses = run.dir / "poses"
    poses.mkdir(parents=True, exist_ok=True)
    (poses / "pose_aspirin.pdbqt").write_text("POSE\n", encoding="utf-8")
    run.finish("ok")

    path = store.artifact_path(run.id, "pose_aspirin")
    assert path is not None and path.read_text(encoding="utf-8") == "POSE\n"
    # 不存在的位姿名必须返回 None，且不得越权访问
    assert store.artifact_path(run.id, "pose_不存在") is None
    assert store.artifact_path(run.id, "pose_../run") is None


def test_detail_caps_inline_payload(tmp_path, monkeypatch):
    """内联的分子库/性质/对接明细必须按上限截断，避免上万分子撑爆响应。"""
    monkeypatch.setenv("RESULT_INLINE_LIMIT", "2")
    from docking_agent.runs import RunStore

    store = RunStore(tmp_path / "runs")
    run = store.new("pipeline", {})
    mols = [{"name": f"M{i}", "smiles": "CCO"} for i in range(5)]
    run.write_json("molecules", mols, label="库")
    run.write_json("properties", mols, label="性质")
    run.write_json("docking", {"status": "ok", "receptors": [{"receptor_key": "t", "results": mols}]},
                   label="对接")
    run.write_ranking(mols)
    run.finish("ok")

    detail = store.detail(run.id)
    assert len(detail["result"]["molecules"]) == 2
    assert detail["result"]["molecules_total"] == 5
    assert len(detail["result"]["properties"]) == 2
    assert len(detail["result"]["docking"]["receptors"][0]["results"]) == 2
    assert detail["result"]["docking"]["receptors"][0]["results_total"] == 5
    assert detail["result"]["ranking_total"] == 5


def test_parse_smiles_text_accepts_name_and_smiles_separated_by_space():
    """聊天里最常见的写法是「名称 SMILES」（空格分隔），必须能解析。"""
    from docking_agent.core import parse_smiles_text

    mols = parse_smiles_text("华法林 CC(=O)CC(c1ccccc1)c1c(O)c2ccccc2oc1=O；布洛芬 CCC(C)Cc1ccc(cc1)C(C)C(=O)O")
    names = {m["name"]: m["smiles"] for m in mols}
    assert len(mols) == 2, mols
    assert "华法林" in names and "布洛芬" in names


def test_parse_smiles_text_ignores_prose_but_keeps_embedded_molecules():
    """一句话里夹带的分子要抽出来，散文部分忽略（不能把整句当 SMILES 丢掉）。"""
    from docking_agent.core.ligands import extract_smiles, parse_smiles_text

    text = ("帮我筛一下这两个分子：华法林 CC(=O)CC(c1ccccc1)c1c(O)c2ccccc2oc1=O；"
            "布洛芬 CCC(C)Cc1ccc(cc1)C(C)C(=O)O")
    mols = parse_smiles_text(text)
    assert len(mols) == 2, mols
    assert {m["name"] for m in mols} == {"华法林", "布洛芬"}, mols
    # 纯散文（无 SMILES）不应抽出任何分子
    assert extract_smiles("帮我看看这个受体的成药性怎么样") == []
