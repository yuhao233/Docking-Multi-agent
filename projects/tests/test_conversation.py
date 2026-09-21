"""多轮会话（conversation_id）回归测试。

背景（用户报告的缺陷）：
    提交任务后系统向用户提问，用户回答了，系统却**又开了一段新对话**，没有延续上一轮。

根因：`api/app.py` 三处都用 `thread_id = run.id`，而 `run.id` 每次提交都是新的
（`runs.new_run_id()`），LangGraph checkpointer 的记忆永远命中不到上一轮；
前端也没有任何「会话 id」概念，服务端无从知道「这是同一个对话」。

修复分三层，本文件逐一验证：
  1. API 层：`conversation_id` 贯穿为 `thread_id`（同一 id → 同一 thread → 命中历史）；
  2. 受理层：`prior_turns` 让受理层看见上一轮，确定性继承分子/受体且不再重复判 ask；
  3. 兼容性：不传 `conversation_id` 时行为与修复前一致（`thread_id = run.id`）。

全部离线：用 FakeGraph 顶掉真实图（不建模型、不联网），stub 掉结果落盘。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

from docking_agent import intake  # noqa: E402
from docking_agent.api.schemas import AgentRequest  # noqa: E402

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


# --------------------------------------------------------------------------- #
# 离线替身：FakeGraph + 受理层探针
# --------------------------------------------------------------------------- #
class _FakeGraph:
    """最小可用的「带 checkpointer 的图」替身。

    - `astream` 记录每次调用的 (thread_id, 入参消息)，把消息写进该 thread 的历史，
      并回一条 AI 消息（模拟真实 agent 的一轮）；
    - `aget_state` 返回该 thread 的历史消息 —— 与真实 LangGraph + MemorySaver 的
      「同一 thread 累积、不同 thread 隔离」语义一致，足以验证 API 层的会话串联。
    """

    def __init__(self) -> None:
        from langchain_core.messages import AIMessage, HumanMessage

        self._Human = HumanMessage
        self._AI = AIMessage
        self.history: Dict[str, List[Any]] = {}
        self.calls: List[Dict[str, Any]] = []

    async def astream(self, payload: Dict[str, Any], config: Optional[Dict[str, Any]] = None,
                      stream_mode: Any = None, context: Any = None) -> AsyncIterator[Any]:
        from langchain_core.messages import AIMessageChunk

        thread = ((config or {}).get("configurable") or {}).get("thread_id")
        incoming = list(payload.get("messages") or [])
        self.calls.append({"thread_id": thread, "messages": incoming})
        msgs = self.history.setdefault(thread, [])
        for m in incoming:
            content = m.get("content") if isinstance(m, dict) else str(m)
            msgs.append(self._Human(content=str(content)))
        reply = self._AI(content="已收到：" + (str(incoming[-1].get("content")) if incoming else ""))
        yield "messages", (AIMessageChunk(content=reply.content), {"langgraph_node": "model"})
        msgs.append(reply)
        yield "updates", {"model": {"messages": [reply]}}

    async def aget_state(self, config: Optional[Dict[str, Any]] = None) -> Any:
        thread = ((config or {}).get("configurable") or {}).get("thread_id")
        return SimpleNamespace(values={"messages": list(self.history.get(thread, []))})


def _sse_events(text: str):
    for line in text.splitlines():
        if line.startswith("data: "):
            try:
                yield json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                continue


@pytest.fixture()
def client() -> Iterator[Any]:
    from docking_agent.api.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def offline_graph(monkeypatch) -> Iterator[Any]:
    """把服务端的图换成 FakeGraph，并探针记录受理层实际拿到的 prior_turns。

    - `graph`：FakeGraph 实例；
    - `seen`：每次 `build_task_spec` 收到的 prior_turns（名单顺序 = 请求顺序）。
    """
    import docking_agent.api.app as app_mod

    graph = _FakeGraph()
    monkeypatch.setattr(app_mod.state, "get_graph", lambda: graph)

    original = intake.build_task_spec
    seen: List[Optional[List[Dict[str, str]]]] = []

    def _spy(req, *, raw_request="", prior_turns=None):  # noqa: ANN001
        seen.append(prior_turns)
        return original(req, raw_request=raw_request, prior_turns=prior_turns)

    monkeypatch.setattr(intake, "build_task_spec", _spy)

    # 结果落盘与本次断言无关：stub 掉，避免每个用例都跑图表/PDF
    import docking_agent.agents.persistence as persistence

    monkeypatch.setattr(persistence, "persist_agent_run",
                        lambda run, messages, final_text: {"ranking": [], "molecules": []})
    return SimpleNamespace(graph=graph, seen=seen)


def _post_agent(client, **body) -> Dict[str, Any]:
    """发一次 /api/agent/stream，返回 {run_id, conversation_id, events}。"""
    payload = {"mode": "chat", "advanced": True, "message": "筛选一下", "ligands_text": "A:CCO"}
    payload.update(body)
    resp = client.post("/api/agent/stream", json=payload)
    assert resp.status_code == 200, resp.text
    events = list(_sse_events(resp.text))
    kinds = [e.get("type") for e in events]
    assert kinds and kinds[-1] == "done", kinds
    start = next(e for e in events if e.get("type") == "start")
    return {"run_id": start["run_id"], "start": start, "events": events}


def _run_detail(client, run_id: str) -> Dict[str, Any]:
    detail = client.get(f"/api/runs/{run_id}").json()
    return detail["run"]


# --------------------------------------------------------------------------- #
# 1) API 级连续性：同一 conversation_id → 同一 thread → 第二轮看得见第一轮
# --------------------------------------------------------------------------- #
def test_same_conversation_id_continues_previous_turn(client, offline_graph) -> None:
    first = _post_agent(client, conversation_id="conv-continue", message="帮我筛这两个分子 CCO")
    second = _post_agent(client, conversation_id="conv-continue", message="用 trypsin")

    # 两次运行的 conversation_id 一致（Req 1）
    assert first["start"]["conversation_id"] == "conv-continue"
    assert second["start"]["conversation_id"] == "conv-continue"
    d1 = _run_detail(client, first["run_id"])
    d2 = _run_detail(client, second["run_id"])
    assert d1["conversation_id"] == "conv-continue"
    assert d2["conversation_id"] == "conv-continue"
    assert d1["thread_id"] == d2["thread_id"] == "conv-continue"

    # 两次用的是同一个 thread（根因就是这里曾经用 run.id）
    calls = offline_graph.graph.calls
    assert calls[0]["thread_id"] == calls[1]["thread_id"] == "conv-continue"
    assert d1["run_id"] != d2["run_id"], "运行记录仍然每次独立（只有会话是共享的）"

    # checkpointer 状态里确实累积了第一轮（agent 下一轮能读到的历史）
    history = offline_graph.graph.history["conv-continue"]
    contents = [getattr(m, "content", "") for m in history]
    assert any("帮我筛这两个分子 CCO" in c for c in contents), "第二轮执行时历史里必须含第一轮用户消息"
    assert any(c.startswith("已收到：") for c in contents), "第一轮的助手回复也应在历史里"

    # 受理层在第二轮**确实收到了**第一轮的历史（第一轮没有历史）
    assert not offline_graph.seen[0], "第一轮不应有 prior_turns"
    prior2 = offline_graph.seen[1]
    assert prior2, "第二轮必须把上一轮历史传给受理层"
    joined = json.dumps(prior2, ensure_ascii=False)
    assert "帮我筛这两个分子 CCO" in joined
    assert any(t["role"] == "assistant" for t in prior2)


def test_prior_history_ignores_tool_messages(client, offline_graph, monkeypatch) -> None:
    """历史里只取 human/ai，tool 消息不得进入受理上下文。"""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    thread = "conv-tools"
    offline_graph.graph.history[thread] = [
        HumanMessage(content="先导入库"),
        ToolMessage(content="{\"ok\":true}", tool_call_id="t1", name="import_molecule_library"),
        AIMessage(content="已导入"),
    ]
    _post_agent(client, conversation_id=thread, message="继续")
    prior = offline_graph.seen[-1]
    assert [t["role"] for t in prior] == ["user", "assistant"]
    assert "import_molecule_library" not in json.dumps(prior, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 2) 不同会话隔离
# --------------------------------------------------------------------------- #
def test_different_conversation_ids_are_isolated(client, offline_graph) -> None:
    first = _post_agent(client, conversation_id="conv-A", message="会话 A 的分子 CCO")
    _post_agent(client, conversation_id="conv-B", message="会话 B 的问题")

    assert offline_graph.seen[1] in (None, []), "新会话不得看到别的会话的历史"
    assert "会话 A 的分子 CCO" not in json.dumps(offline_graph.seen[1], ensure_ascii=False)

    # 两个 thread 各自独立
    a = offline_graph.graph.history["conv-A"]
    b = offline_graph.graph.history["conv-B"]
    assert any("会话 A" in getattr(m, "content", "") for m in a)
    assert not any("会话 A" in getattr(m, "content", "") for m in b)

    # 回到 A 时又能看到 A 的历史（隔离而非覆盖）
    _post_agent(client, conversation_id="conv-A", message="回到 A")
    assert any("会话 A 的分子 CCO" in json.dumps(t, ensure_ascii=False)
               for t in offline_graph.seen[-1])
    assert first["run_id"]


# --------------------------------------------------------------------------- #
# 3) 兼容性：不传 conversation_id → thread_id = run.id（与修复前一致）
# --------------------------------------------------------------------------- #
def test_absent_conversation_id_keeps_legacy_behavior(client, offline_graph) -> None:
    first = _post_agent(client, message="第一次")
    second = _post_agent(client, message="第二次")

    d1 = _run_detail(client, first["run_id"])
    d2 = _run_detail(client, second["run_id"])
    assert d1["conversation_id"] == "" and d2["conversation_id"] == ""
    assert d1["thread_id"] == d1["run_id"], "缺省时 thread_id 必须回落到 run.id（兼容）"
    assert d2["thread_id"] == d2["run_id"]
    assert d1["thread_id"] != d2["thread_id"]
    # 两次都是新 thread → 都拿不到上一轮
    assert not offline_graph.seen[0]
    assert not offline_graph.seen[1]


def test_blank_conversation_id_is_treated_as_absent(client, offline_graph) -> None:
    first = _post_agent(client, conversation_id="   ", message="空会话 id")
    detail = _run_detail(client, first["run_id"])
    assert detail["conversation_id"] == ""
    assert detail["thread_id"] == detail["run_id"]


# --------------------------------------------------------------------------- #
# 4) 受理层守卫：看见上一轮 → 继承分子/受体，且不再重复判 ask
# --------------------------------------------------------------------------- #
def _req(**kwargs) -> AgentRequest:
    base = {"mode": "chat", "advanced": True, "message": "", "receptor": "thrombin",
            "ligands_text": ""}
    base.update(kwargs)
    return AgentRequest(**base)


_PRIOR_WITH_MOLECULES = [
    {"role": "user", "content": "帮我筛这两个分子：华法林 CC(=O)CC(c1ccccc1)c1c(O)c2ccccc2oc1=O"},
    {"role": "assistant", "content": "请问您想用哪个受体？"},
]


def test_answer_to_previous_question_is_run_and_receptor_is_recognized() -> None:
    """用户回答「用 trypsin」→ decision=run 且受体命中 trypsin（不再当成新对话）。"""
    prior = [{"role": "assistant", "content": "请问您想用哪个受体？"}]
    spec = intake.build_task_spec(_req(message="用 trypsin"), prior_turns=prior)
    assert spec["decision"] == "run"
    assert spec["receptor"]["name"] == "trypsin"
    assert spec["prior_turn_count"] == 1

    # 无 prior_turns 时同样的 message 不因这条守卫变成 run（保持既有语义：
    # 受理模型若仍要求补充且无可识别分子来源，就还是 ask）
    base = intake._finalize({**intake.build_task_spec(_req(message="用 trypsin")),
                             "needs_user_input": True})
    assert base["decision"] == "ask"
    forced = intake._finalize({**intake.build_task_spec(_req(message="用 trypsin"), prior_turns=prior),
                               "needs_user_input": True})
    assert forced["decision"] == "ask", "上一轮也没给出分子 → 允许再问一次"


def test_prior_molecules_are_inherited_and_suppress_repeated_ask() -> None:
    """上一轮给了 SMILES、这一轮只回答受体：分子继承下来 → 不得再判 ask。"""
    spec = intake.build_task_spec(_req(message="用 trypsin 筛一下"), prior_turns=_PRIOR_WITH_MOLECULES)
    assert spec["ligands"]["source"] == "message"
    assert spec["ligands"].get("inherited") == "prior_turns"
    assert "华法林:" in "；".join(spec["ligands"]["molecules"])
    assert spec["receptor"]["name"] == "trypsin"
    assert any("继承自上一轮" in a for a in spec["assumptions"])

    forced = intake._finalize({**spec, "needs_user_input": True})
    assert forced["decision"] == "run", "已从上一轮继承到分子，不能重复追问"

    # 无 prior_turns：同样的 message 缺分子来源 → 既有语义仍是 ask
    bare = intake._finalize({**intake.build_task_spec(_req(message="用 trypsin 筛一下")),
                             "needs_user_input": True})
    assert bare["decision"] == "ask"


def test_prior_receptor_is_inherited_when_current_message_omits_it() -> None:
    prior = [{"role": "user", "content": "请用 trypsin 做一次筛选"},
             {"role": "assistant", "content": "请问您想分析哪些分子？"}]
    spec = intake.build_task_spec(_req(message="用示例库就行"), prior_turns=prior)
    assert spec["receptor"] == {"name": "trypsin", "file": "", "source": "user"}
    assert any("受体继承自上一轮" in a for a in spec["assumptions"])


def test_prior_turns_are_passed_to_intake_llm_context(monkeypatch) -> None:
    """受理模型必须能看见上一轮（只读），但不得据此产出参数。"""
    monkeypatch.setenv("INTAKE_LLM", "on")
    from docking_agent.runtime import llm as llm_mod

    calls: List[Any] = []

    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    class _Stub:
        def invoke(self, messages, *a, **k) -> Any:
            calls.append(messages)
            return _Reply(json.dumps({"goal": "继续筛选", "confidence": 0.9,
                                      "exhaustiveness": 32, "params": {"exhaustiveness": 32}},
                                     ensure_ascii=False))

    monkeypatch.setattr(llm_mod, "build_chat_llm", lambda ctx=None, role="": _Stub())
    spec = intake.build_task_spec(_req(message="用 trypsin 筛一下"), prior_turns=_PRIOR_WITH_MOLECULES)
    refined = intake.refine_task_spec(spec, prior_turns=_PRIOR_WITH_MOLECULES)
    sent = json.loads(calls[0][1].content)
    assert "上一轮对话（只读上下文，不要修改任何参数）" in sent
    joined = json.dumps(sent, ensure_ascii=False)
    assert "华法林" in joined
    assert refined["params"]["exhaustiveness"] is None, "上一轮上下文不能成为参数来源；未给值就是留空"
    assert refined["decision"] == "run"


def test_build_message_accepts_prior_turns_without_breaking_signature() -> None:
    """既有调用方式（不传 prior_turns）必须保持可用。"""
    message, spec = intake.build_message(_req(message="筛一下", ligands_text="A:CCO"))
    assert spec["prior_turn_count"] == 0
    assert "decision=run" in message
    message2, spec2 = intake.build_message(_req(message="筛一下"), prior_turns=_PRIOR_WITH_MOLECULES)
    assert spec2["prior_turn_count"] == 2
