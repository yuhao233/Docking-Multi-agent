"""架构回归测试：共享黑板、分发校验、横向核验、子 Agent 独立模型实例。

全部离线可跑（不依赖模型、不做真实对接）。
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


# --------------------------------------------------------------------------- #
# 共享黑板（P0.3）
# --------------------------------------------------------------------------- #
def test_blackboard_is_thread_safe_and_dedupes():
    from docking_agent.agents.blackboard import Blackboard

    board = Blackboard("R1")
    added = board.add_molecules([{"name": "乙醇", "smiles": "CCO"},
                                 {"name": "乙醇副本", "smiles": "OCC"},   # 同一分子不同写法
                                 {"name": "坏", "smiles": "not_a_smiles"}])
    assert len(added) == 1, "重复（规范化后相同）与无效分子都应被剔除"
    assert board.stats()["molecules"] == 1
    assert board.notes, "剔除无效项应留下协作备注"


def test_blackboard_enables_lateral_collaboration():
    """属性 Agent 写入 → 对接 Agent 读取 → 结合模式 Agent 交叉核验（横向协作链路）。"""
    from docking_agent.agents.blackboard import Blackboard, board_molecules_json, current_blackboard

    board = Blackboard("R2")
    token = current_blackboard.set(board)
    try:
        # ① 属性 Agent：规范化分子库
        board.add_molecules([{"name": "A", "smiles": "CCO"}, {"name": "B", "smiles": "CCC"}])
        # ② 对接 Agent：无需调用方再传 JSON，直接从黑板拿到规范化清单
        molecules = json.loads(board_molecules_json(""))
        assert {m["smiles"] for m in molecules} == {"CCO", "CCC"}
        # ③ 对接 Agent 写回结果
        board.set_docking([
            {"name": "A", "smiles": "CCO", "affinity_kcal_mol": -9.0, "engine": "vina"},
            {"name": "B", "smiles": "CCC", "affinity_kcal_mol": -4.0, "engine": "vina"},
        ])
        # ④ 结合模式 Agent 写回结果
        board.set_binding([{"name": "A", "smiles": "CCO", "similarity_to_positive_control": 0.1,
                            "anchor_match": False},
                           {"name": "B", "smiles": "CCC", "similarity_to_positive_control": 0.6,
                            "anchor_match": True}])
        assert board.stats() == {"molecules": 2, "properties": 0, "docking": 2,
                             "binding": 2, "pockets": 0}
    finally:
        current_blackboard.reset(token)


def test_cross_check_flags_inconsistencies():
    """check_binding_consistency 必须识别「强对接但骨架不像」「骨架像但对接弱」。"""
    from docking_agent.agents.blackboard import Blackboard, current_blackboard
    from docking_agent.tools.binding import check_binding_consistency

    board = Blackboard("R3")
    token = current_blackboard.set(board)
    try:
        board.add_molecules([{"name": "CTRL", "smiles": "NC(=N)c1ccccc1"},
                             {"name": "STRONG_ODD", "smiles": "CCO"},
                             {"name": "SIMILAR_WEAK", "smiles": "CCN"},
                             {"name": "ONLY_BINDING", "smiles": "CCC"}])
        board.set_docking([
            {"name": "CTRL", "smiles": "NC(=N)c1ccccc1", "affinity_kcal_mol": -5.8},
            {"name": "STRONG_ODD", "smiles": "CCO", "affinity_kcal_mol": -9.0},
            {"name": "SIMILAR_WEAK", "smiles": "CCN", "affinity_kcal_mol": -3.0},
        ])
        board.set_positive_control("NC(=N)c1ccccc1")
        board.set_binding([
            {"name": "CTRL", "smiles": "NC(=N)c1ccccc1", "similarity_to_positive_control": 1.0,
             "anchor_match": True},
            {"name": "STRONG_ODD", "smiles": "CCO", "similarity_to_positive_control": 0.1,
             "anchor_match": False},
            {"name": "SIMILAR_WEAK", "smiles": "CCN", "similarity_to_positive_control": 0.6,
             "anchor_match": True},
            {"name": "ONLY_BINDING", "smiles": "CCC", "similarity_to_positive_control": 0.2,
             "anchor_match": False},
        ])
        out = json.loads(check_binding_consistency.invoke({}))
        assert out["status"] == "ok"
        flags = out["flags"]
        assert flags["affinity_strong_low_similarity"] == 1, flags
        assert flags["similar_but_weak_docking"] == 1, flags
        assert flags["missing_docking"] == 1, flags  # ONLY_BINDING 没有对接结果
        verdicts = {r["name"]: r["verdict"] for r in out["rows"]}
        assert verdicts["STRONG_ODD"] == "affinity_strong_low_similarity"
        assert verdicts["SIMILAR_WEAK"] == "similar_but_weak_docking"
    finally:
        current_blackboard.reset(token)


def test_cross_check_without_blackboard_reports_clearly():
    from docking_agent.tools.binding import check_binding_consistency

    out = json.loads(check_binding_consistency.invoke({}))
    assert out["status"] == "error" and "共享黑板" in out["message"]


# --------------------------------------------------------------------------- #
# 分发边界校验 + 重试（P0.2）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,ok", [
    ('{"status":"ok","assessment":[]}', True),
    ('```json\n{"status":"ok","assessment":[1]}\n```', True),
    ('说明文字\n{"status":"ok","assessment":[1]}\n结束', True),
    ("这不是 JSON", False),
    ("[1,2,3]", False),
])
def test_json_extraction(raw, ok):
    from docking_agent.runtime.payload import parse_json_object

    assert (parse_json_object(raw) is not None) is ok


def test_dispatch_retries_once_then_succeeds(monkeypatch):
    """首次返回垃圾 → 自动带纠正提示重试一次 → 拿到合法 JSON。"""
    import docking_agent.tools.dispatch as dispatch

    replies = iter(["我不是 JSON", '{"status":"ok","assessment":[{"smiles":"CCO"}]}'])
    calls = []

    def fake_invoke(agent, content, thread_id):
        calls.append(thread_id)
        return next(replies)

    monkeypatch.setattr(dispatch, "invoke_worker", fake_invoke)
    out = json.loads(dispatch._invoke_checked(None, "msg", "property", "property"))
    assert out["status"] == "ok"
    assert len(calls) == 2 and calls[1].endswith("-retry")


def test_dispatch_reports_invalid_output_after_retry(monkeypatch):
    """两次都失败 → 返回显式的 agent_output_invalid（而不是把垃圾丢给协调 Agent）。"""
    import docking_agent.tools.dispatch as dispatch

    monkeypatch.setattr(dispatch, "invoke_worker", lambda *a, **k: "依然不是 JSON")
    out = json.loads(dispatch._invoke_checked(None, "msg", "docking", "docking"))
    assert out["status"] == "agent_output_invalid"
    assert out["role"] == "docking" and "receptors" in out["required_keys"]


def test_sub_agents_build_independent_llm_instances(monkeypatch):
    """3 个子 Agent 必须分别构建自己的 LLM 实例，并使用各自的 checkpointer。"""
    from docking_agent.agents import workers as w

    built = []

    class _FakeLLM:
        def __init__(self, role):
            self.role = role
            self.model_name = f"model-{role}"

    def _fake_build(ctx=None, role=""):
        llm = _FakeLLM(role)
        built.append(llm)
        return llm

    monkeypatch.setattr(w, "build_chat_llm", _fake_build)
    # `_make_agent` 现在带 name= （子图命名规范，P0）：假实现必须接受同名参数
    # `_make_agent` 现在带 name= （子图命名）与 structured=（结构化输出开关）
    monkeypatch.setattr(w, "_make_agent",
                        lambda llm, sp, tools, mem, name="", structured=True: {"llm": llm, "mem": mem})
    w.reset_workers()
    try:
        w.init_workers(None)
        assert [x.role for x in built] == ["property", "pocket", "docking", "binding"], \
            "每个角色各构建一次"
        assert len({id(x) for x in built}) == 4, "四个子 Agent 不能共用同一个模型实例"
        assert w.get_worker_llm("property") is not w.get_worker_llm("docking")
        assert w.get_worker_llm("pocket") is not w.get_worker_llm("docking")
        assert w.get_worker_llm("docking") is not w.get_worker_llm("binding")
        # 记忆隔离：每个子 Agent 一个 checkpointer
        assert len({id(w._worker_checkpointers[r]) for r in w.WORKER_ROLES}) == 4
        # 每个子 Agent 绑定的是自己的那个实例
        assert w.get_property_agent()["llm"] is w.get_worker_llm("property")
        assert w.get_pocket_agent()["llm"] is w.get_worker_llm("pocket")
        assert w.get_docking_agent()["llm"] is w.get_worker_llm("docking")
        assert w.get_binding_agent()["llm"] is w.get_worker_llm("binding")
        assert w.worker_llm_models() == {"property": "model-property",
                                         "pocket": "model-pocket",
                                         "docking": "model-docking",
                                         "binding": "model-binding"}
    finally:
        w.reset_workers()


def test_sub_agents_use_role_specific_models(monkeypatch):
    """按角色配置不同模型时，各子 Agent 真的拿到各自的模型。"""
    from docking_agent.agents import workers as w

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "base-model")
    monkeypatch.setenv("LLM_MODEL_DOCKING", "docking-model")
    monkeypatch.setenv("LLM_MODEL_PROPERTY", "property-model")
    monkeypatch.setenv("LLM_MODEL_POCKET", "pocket-model")
    monkeypatch.delenv("LLM_MODEL_BINDING", raising=False)
    monkeypatch.setattr(w, "_make_agent",
                        lambda llm, sp, tools, mem, name="", structured=True: {"llm": llm})
    w.reset_workers()
    try:
        w.init_workers(None)
        models = w.worker_llm_models()
        assert models["property"] == "property-model"
        assert models["docking"] == "docking-model"
        assert models["pocket"] == "pocket-model"
        assert models["binding"] == "base-model", "未单独配置的角色回落到全局模型"
    finally:
        w.reset_workers()
