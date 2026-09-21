#!/usr/bin/env python
"""本地部署自检脚本。

两类检查：
  1) 多 Agent 编排（用 Fake LLM 驱动：协调 Agent → 分发工具 → 子 Agent → 真实计算工具 → SSE 事件流）；
  2) 产物存储、真实子 Agent 对接、流式帧，
     不消耗任何真实模型额度。

用法：
    .venv/bin/python scripts/smoke_test.py              # 全部检查（含 1 次真实对接，约 20-40s）
    .venv/bin/python scripts/smoke_test.py --fast       # 跳过真实对接，只验证编排与 IO
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

from langchain_core.callbacks import CallbackManagerForLLMRun  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult  # noqa: E402
from pydantic import Field  # noqa: E402

# 各工具的最小合法入参（供 Fake LLM 构造 tool_call）
TOOL_ARGS = {
    "import_molecule_library": {"query_or_text": "CCO,CC(=O)Oc1ccccc1C(=O)O"},
    "run_property_assessment": {"molecules_json": '[{"name":"M1","smiles":"CCO"}]'},
    "molecular_property_assessment": {"molecules_json": '[{"name":"M1","smiles":"CCO"}]'},
    "run_docking": {"molecules_json": '[{"name":"M1","smiles":"CCO"}]', "receptor_sources": "thrombin"},
    "molecular_docking": {"molecules_json": '[{"name":"M1","smiles":"CCO"}]', "receptor_sources": "thrombin"},
    "run_binding_mode_analysis": {"molecules_json": '[{"name":"M1","smiles":"CCO"}]',
                                  "positive_control_smiles": "NC(=N)c1ccccc1"},
    "binding_mode_analysis": {"molecules_json": '[{"name":"M1","smiles":"CCO"}]',
                              "positive_control_smiles": "NC(=N)c1ccccc1"},
    "positive_control_similarity": {"molecules_json": '[{"name":"M1","smiles":"CCO"}]',
                                    "positive_control_smiles": "NC(=N)c1ccccc1"},
    "generate_screening_report": {"aggregated_json": json.dumps({
        "molecules": [{"name": "M1", "smiles": "CCO", "affinity_kcal_mol": -2.8,
                       "similarity_to_positive_control": 0.05}],
        "positive_control": {"name": "PC", "smiles": "NC(=N)c1ccccc1", "affinity_kcal_mol": -5.8},
    })},
}

RESULTS: List[tuple[bool, str]] = []


def check(ok: bool, label: str) -> bool:
    RESULTS.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return bool(ok)


def _embedded_json(text: str):
    """从可能带前后缀的文本中提取第一个完整 JSON 对象/数组。"""
    s = text or ""
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = s.find(open_ch), s.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(s[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None


class ScriptedChat(BaseChatModel):
    """脚本化假模型：首轮发起一次 tool_call，拿到工具结果后给出最终回答。"""

    force_tool: Optional[str] = None
    bound_tool_names: List[str] = Field(default_factory=list)
    call_count: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-smoke"

    def bind_tools(self, tools, **kwargs):  # type: ignore[override]
        names = []
        for t in tools:
            names.append(getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None))
        object.__setattr__(self, "bound_tool_names", [n for n in names if n])
        return self

    def _pick(self) -> str:
        if self.force_tool:
            return self.force_tool
        for name in self.bound_tool_names:
            if name in TOOL_ARGS:
                return name
        return self.bound_tool_names[0] if self.bound_tool_names else "noop"

    def _next_message(self, messages: List[BaseMessage]) -> AIMessage:
        self.call_count += 1
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        if tool_msgs:
            # 完整回显工具结果（不截断，保证内嵌 JSON 可解析）
            return AIMessage(content=f"[SMOKE-FINAL] {str(tool_msgs[-1].content)}")
        name = self._pick()
        return AIMessage(content="", tool_calls=[{
            "name": name, "args": TOOL_ARGS.get(name, {}), "id": f"call_{self.call_count}",
        }])

    def _generate(self, messages, stop=None, run_manager: Optional[CallbackManagerForLLMRun] = None,
                  **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next_message(list(messages)))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        msg = self._next_message(list(messages))
        yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content, tool_calls=msg.tool_calls))

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        msg = self._next_message(list(messages))
        yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content, tool_calls=msg.tool_calls))


def fake_llm_factory(force_tool: Optional[str] = None):
    def _factory(ctx=None, role: str = ""):
        return ScriptedChat(force_tool=force_tool)
    return _factory


def test_payload_and_store() -> None:
    print("\n[1/4] 入参与本地产物存储")
    from docking_agent.reporting.store import resolve_output, save_artifact
    from docking_agent.runtime.payload import normalize_agent_input

    p1 = normalize_agent_input({"messages": [{"role": "user", "content": "你好"}]})
    p2 = normalize_agent_input({"text": "你好"})
    p3 = normalize_agent_input({"type": "query", "content": {"query": {"prompt": [
        {"type": "text", "content": {"text": "你好"}}]}}})
    check(all(m["messages"][0].content == "你好" for m in (p1, p2, p3)), "三种入参格式均解析为同一条用户消息")

    rec = save_artifact(b"smoke", "smoke_check.txt", "text/plain")
    check(Path(rec["path"]).read_bytes() == b"smoke", "产物写入本地成功")
    check(resolve_output(rec["key"]) is not None, "产物可按 key 定位下载")
    check(resolve_output("../etc/passwd") is None, "路径穿越被拒绝")


def test_multi_agent(orchestration_tool: str) -> None:
    print(f"\n[3/5] 多 Agent 编排（Fake LLM 强制调用 {orchestration_tool}）")
    import docking_agent.agents.coordinator as agent_mod
    import docking_agent.agents.workers as workers_mod

    agent_mod.build_chat_llm = fake_llm_factory(force_tool=orchestration_tool)  # type: ignore[assignment]
    workers_mod.build_chat_llm = fake_llm_factory()  # 子 Agent 自动选择其唯一工具

    graph = agent_mod.build_agent(None)
    check(graph is not None, "协调 Agent 构建成功（含 3 个子 Agent 初始化）")
    check(workers_mod.get_property_agent() is not None, "子 Agent 已初始化：属性评估")
    check(workers_mod.get_docking_agent() is not None, "子 Agent 已初始化：Docking 执行")
    check(workers_mod.get_binding_agent() is not None, "子 Agent 已初始化：结合模式检测")

    result = graph.invoke(
        {"messages": [{"role": "user", "content": "请评估分子的物化性质"}]},
        config={"configurable": {"thread_id": "smoke-multi-agent"}, "recursion_limit": 20},
    )
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    check(bool(tool_msgs), "协调 Agent 成功调用了分发工具")
    if tool_msgs:
        payload = str(tool_msgs[0].content)
        parsed = _embedded_json(payload)
        check(isinstance(parsed, (dict, list)), "分发工具返回内嵌 JSON（真实子 Agent 结果）")
        if isinstance(parsed, dict):
            check(parsed.get("status") in ("ok", "no_molecules"), f"子 Agent 返回 status={parsed.get('status')}")
            assessment = parsed.get("assessment") or []
            check(bool(assessment) and "molecular_weight" in (assessment[0] or {}),
                  "子 Agent 完成了真实 RDKit 物化性质计算")
    check("[SMOKE-FINAL]" in str(result["messages"][-1].content), "图正常收敛到最终回答")


def test_worker_docking() -> None:
    """真实 Vina 对接：经「Docking 执行子 Agent」→ molecular_docking 工具。"""
    print("\n[3/4] Docking 子 Agent 真实对接（Fake LLM + 真实 Vina）")
    import docking_agent.agents.workers as workers_mod

    agent = workers_mod.get_docking_agent()
    raw = workers_mod.invoke_worker(
        agent,
        '请对以下分子执行真实对接（molecules_json）：[{"name":"M1","smiles":"CCO"}]；'
        "对接参数 exhaustiveness=16, n_poses=1, engine=vina；受体来源 receptor_sources=thrombin",
        thread_id="smoke-docking",
    )
    parsed = _embedded_json(raw)
    check(isinstance(parsed, dict), "Docking 子 Agent 返回内嵌 JSON")
    if not isinstance(parsed, dict):
        print(f"    raw 返回（前 400 字）：{raw[:400]!r}")
    if isinstance(parsed, dict):
        check(parsed.get("status") == "ok", f"对接状态 status={parsed.get('status')}")
        receptors = parsed.get("receptors") or []
        results = (receptors[0].get("results") if receptors else None) or []
        check(bool(results) and "affinity_kcal_mol" in results[0],
              f"返回真实亲和力 affinity={results[0].get('affinity_kcal_mol') if results else None} kcal/mol")
        if results:
            check(results[0].get("engine") == "vina", "使用了 Vina 引擎")


def test_streaming() -> None:
    print("\n[4/4] SSE 流式事件")
    import docking_agent.agents.coordinator as agent_mod
    import docking_agent.agents.workers as workers_mod
    from docking_agent.runtime.streaming import parse_sse_data, stream_agent_sse

    agent_mod.build_chat_llm = fake_llm_factory(force_tool="run_binding_mode_analysis")  # type: ignore[assignment]
    workers_mod.build_chat_llm = fake_llm_factory()  # type: ignore[assignment]
    graph = agent_mod.build_agent(None)

    async def _collect() -> List[dict]:
        events: List[dict] = []
        async for chunk in stream_agent_sse(graph, {"messages": [{"role": "user", "content": "分析结合模式"}]},
                                            {"configurable": {"thread_id": "smoke-stream"},
                                             "recursion_limit": 20}, "smoke-stream"):
            data = parse_sse_data(chunk)
            if data is not None:
                events.append(data)
        return events

    events = asyncio.run(_collect())
    kinds = {e.get("type") for e in events}
    check("start" in kinds, "收到 start 事件")
    check("tool_call" in kinds, "收到 tool_call 事件（子 Agent 调度可见）")
    check("final" in kinds, "收到 final 事件")
    check(bool(events) and events[-1].get("type") == "done", "以 done 事件结束")
    final = next((e for e in events if e.get("type") == "final"), {})
    check(bool(final.get("content")), "final 事件携带最终回答文本")

    # 结合模式工具必须返回真实完整结果（不能因内部导入失败而静默回退到轻量工具）
    binding_results = [e for e in events
                       if e.get("type") == "tool_result" and e.get("name") == "run_binding_mode_analysis"]
    check(bool(binding_results), "结合模式分发工具已被调用")
    if binding_results:
        payload = _embedded_json(str(binding_results[-1].get("content") or ""))
        check(isinstance(payload, dict) and payload.get("status") == "ok",
              "结合模式工具返回 status=ok（未回退）")
        rows = (payload or {}).get("results") or []
        check(bool(rows) and "structural_consistency" in rows[0],
              "结合模式结果含完整方法学字段（structural_consistency / binding_mode_hint）")


def main() -> int:
    ap = argparse.ArgumentParser(description="本地部署自检")
    ap.add_argument("--fast", action="store_true", help="跳过真实 Vina 对接")
    args = ap.parse_args()

    print("=" * 72)
    print("分子筛选多 Agent 本地部署自检")
    print("=" * 72)

    test_payload_and_store()
    test_multi_agent("run_property_assessment")
    if not args.fast:
        test_worker_docking()
    test_streaming()

    passed = sum(1 for ok, _ in RESULTS if ok)
    failed = [(ok, label) for ok, label in RESULTS if not ok]
    print("\n" + "=" * 72)
    print(f"结果：{passed}/{len(RESULTS)} 通过")
    for _ok, label in failed:
        print(f"  - 未通过：{label}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
