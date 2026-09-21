"""可复用的假 LLM 测试脚手架：脚本化假模型驱动**真实**多 Agent 编排。

从 `scripts/smoke_test.py`（保持原样，不修改）里的 `ScriptedChat` / `fake_llm_factory`
抽出并泛化，供各测试模块复用。特点：

- 不访问网络、不需要真实 LLM；
- 工具真实执行：真实 RDKit 计算、真实 Vina 对接、真实报告产物；
- 既能驱动协调 Agent 的**多步工具链**（导入库 → 性质 → 对接 → 结合模式 → 报告），
  也能让子 Agent 从分发指令里解析参数后调用自己的工具。

典型用法（配合标准 Agent Protocol 路径）::

    from support.fake_llm import agent_input_from_form, coordinator_script_for_form, run_agent

    def _run_agent(client, body, monkeypatch):
        return run_agent(client, coordinator_script_for_form(body), body, monkeypatch)

`run_agent` 会：装上假 LLM → `POST /threads` → `POST /threads/{tid}/runs/stream`
（assistant_id=coordinator）→ 从 SSE 帧里取回业务 `run_id`，随后可用
`GET /api/runs/{run_id}` 做原有断言。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

#: 各工具的最小合法入参（供假模型在无指令可解析时兜底构造 tool_call）
TOOL_ARGS: Dict[str, Dict[str, Any]] = {
    "import_molecule_library": {"query_or_text": "CCO,CC(=O)Oc1ccccc1C(=O)O"},
    "run_property_assessment": {},
    "molecular_property_assessment": {},
    "run_docking": {"molecules_json": "", "receptor_sources": "thrombin"},
    "molecular_docking": {"molecules_json": "", "receptor_sources": "thrombin"},
    "run_binding_mode_analysis": {"molecules_json": ""},
    "binding_mode_analysis": {"molecules_json": ""},
    "positive_control_similarity": {"molecules_json": ""},
    "predict_binding_pockets": {},
    "generate_screening_report": {"aggregated_json": ""},
}

#: 子 Agent 优先调用的工具（避免误选辅助工具或结构化输出工具）
_PREFERRED_TOOLS: Tuple[str, ...] = (
    "molecular_docking",
    "molecular_property_assessment",
    "binding_mode_analysis",
    "predict_binding_pockets",
)


def step(name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造一步脚本化的工具调用（供协调 Agent 假模型按序执行）。"""
    return {"name": name, "args": dict(args or {})}


# --------------------------------------------------------------------------- #
# 从分发指令里解析子 Agent 工具入参（模拟真实模型「照指令传参」）
# --------------------------------------------------------------------------- #
def _search(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.S)
    return match.group(1).strip() if match else ""


def parse_docking_instruction(text: str) -> Dict[str, Any]:
    """从 `run_docking` 下发给 Docking 子 Agent 的指令里解析出工具入参。"""
    args: Dict[str, Any] = {}
    molecules_json = _search(r"（molecules_json）：(.*?)；", text)
    params = re.search(r"对接参数 exhaustiveness=(\d+), n_poses=(\d+), engine=(\w+)", text)
    molecule_file = _search(r"molecule_file=\*\*(.+?)\*\*", text)
    receptor_file = _search(r"receptor_file=(.+?)（", text)
    receptor_sources = _search(r"receptor_sources=(.+?)（", text)
    control = _search(r"（SMILES=(.+?)）", text)
    top_from_previous = _search(r"top_from_previous=(\d+)", text)
    center = _search(r"site_center=\[([^\]]+)\]", text)
    size = _search(r"site_size=\[([^\]]+)\]", text)
    if molecules_json:
        args["molecules_json"] = molecules_json
    if params:
        args["exhaustiveness"] = int(params.group(1))
        args["n_poses"] = int(params.group(2))
        args["engine"] = params.group(3)
    if molecule_file:
        args["molecule_file"] = molecule_file
    if receptor_file:
        args["receptor_file"] = receptor_file
    elif receptor_sources:
        args["receptor_sources"] = receptor_sources
    if control:
        args["positive_control_smiles"] = control
    if top_from_previous:
        args["top_from_previous"] = int(top_from_previous)
    if center:
        args["site_center"] = [float(x) for x in center.replace(" ", "").split(",") if x]
    if size:
        args["site_size"] = [float(x) for x in size.replace(" ", "").split(",") if x]
    return args


def parse_binding_instruction(text: str) -> Dict[str, Any]:
    """从 `run_binding_mode_analysis` 下发的指令里解析出工具入参。"""
    args: Dict[str, Any] = {}
    molecule_file = _search(r"molecules_file=\*\*(.+?)\*\*", text)
    if molecule_file and not molecule_file.startswith("（"):
        args["molecules_file"] = molecule_file
    control = _search(r"阳性对照 (\S+) 的结合模式", text)
    if control:
        args["positive_control_smiles"] = control
    return args


_INSTRUCTION_PARSERS: Dict[str, Callable[[str], Dict[str, Any]]] = {
    "molecular_docking": parse_docking_instruction,
    "binding_mode_analysis": parse_binding_instruction,
}


# --------------------------------------------------------------------------- #
# 脚本化假模型
# --------------------------------------------------------------------------- #
class ScriptedChat(BaseChatModel):
    """脚本化假模型。

    - 协调 Agent（role="coordinator"）：按 `script` 里的工具名**逐步**调用，每一步拿到
      工具回执后再发下一步；脚本走完回一个最终回答（完整回显最后一个工具结果）。
    - 子 Agent：优先调用 `_PREFERRED_TOOLS` 里它绑定的那个工具；可从最后一条人类指令里
      解析参数（如 `molecular_docking` 的受体/分子/参数），拿不到就用兜底入参。
    """

    script: List[Dict[str, Any]] = Field(default_factory=list)
    role: str = ""
    force_tool: Optional[str] = None
    final_prefix: str = "[FAKE-FINAL]"
    bound_tool_names: List[str] = Field(default_factory=list)
    call_count: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-fake-llm"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChat":
        names: List[str] = []
        for tool_obj in tools:
            name = getattr(tool_obj, "name", None)
            if name is None and isinstance(tool_obj, dict):
                name = tool_obj.get("name")
            if name:
                names.append(str(name))
        object.__setattr__(self, "bound_tool_names", names)
        return self

    def _pick(self) -> str:
        if self.force_tool:
            return self.force_tool
        for name in _PREFERRED_TOOLS:
            if name in self.bound_tool_names:
                return name
        for name in self.bound_tool_names:
            if name in TOOL_ARGS:
                return name
        return self.bound_tool_names[0] if self.bound_tool_names else "noop"

    def _args_for(self, name: str, messages: Sequence[BaseMessage]) -> Dict[str, Any]:
        parser = _INSTRUCTION_PARSERS.get(name)
        if parser is not None:
            instruction = ""
            for message in reversed(messages):
                if type(message).__name__ == "HumanMessage":
                    instruction = str(getattr(message, "content", "") or "")
                    break
            parsed = parser(instruction)
            if parsed:
                return parsed
        return dict(TOOL_ARGS.get(name) or {})

    def _tool_call(self, name: str, args: Dict[str, Any]) -> AIMessage:
        return AIMessage(content="", tool_calls=[
            {"name": name, "args": args, "id": f"call_{self.call_count}"},
        ])

    def _next_message(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.call_count += 1
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        if self.role == "coordinator" and self.script:
            index = len(tool_msgs)
            if index < len(self.script):
                current = self.script[index]
                return self._tool_call(str(current.get("name") or ""),
                                       dict(current.get("args") or {}))
            payload = str(tool_msgs[-1].content) if tool_msgs else "（无工具结果）"
            return AIMessage(content=f"{self.final_prefix} {payload}")
        if tool_msgs:
            return AIMessage(content=f"{self.final_prefix} {tool_msgs[-1].content}")
        name = self._pick()
        return self._tool_call(name, self._args_for(name, messages))

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None,
                  **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next_message(list(messages)))])

    def _stream(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                run_manager: Optional[Any] = None, **kwargs: Any):  # type: ignore[override]
        message = self._next_message(list(messages))
        yield ChatGenerationChunk(message=AIMessageChunk(content=message.content,
                                                         tool_calls=message.tool_calls))

    async def _astream(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                       run_manager: Optional[Any] = None, **kwargs: Any):  # type: ignore[override]
        message = self._next_message(list(messages))
        yield ChatGenerationChunk(message=AIMessageChunk(content=message.content,
                                                         tool_calls=message.tool_calls))


def fake_llm_factory(*, script: Optional[List[Dict[str, Any]]] = None,
                     force_tool: Optional[str] = None) -> Callable[..., ScriptedChat]:
    """返回 `build_chat_llm` 的替身工厂（签名与真实实现兼容：ctx/role 关键字）。"""

    def _factory(ctx: Any = None, role: str = "", **kwargs: Any) -> ScriptedChat:
        if role == "coordinator":
            return ScriptedChat(role="coordinator", script=[dict(s) for s in (script or [])],
                                force_tool=force_tool)
        return ScriptedChat(role=role, force_tool=force_tool)

    return _factory


def install_fake_llm(monkeypatch: Any, *,
                     script: Optional[List[Dict[str, Any]]] = None,
                     force_tool: Optional[str] = None) -> None:
    """把假 LLM 装到协调 Agent 与子 Agent 上，并让产物图在本次用例里重建。

    - 重置子 Agent（`reset_workers`）与 API 缓存的图，确保本次脚本生效；
    - `monkeypatch` 会在用例结束后自动还原模块属性与图缓存。
    """
    from docking_agent.agents import coordinator as coordinator_mod
    from docking_agent.agents import workers as workers_mod
    from docking_agent.api import app as app_mod

    monkeypatch.setattr(coordinator_mod, "build_chat_llm",
                        fake_llm_factory(script=script, force_tool=force_tool))
    monkeypatch.setattr(workers_mod, "build_chat_llm",
                        fake_llm_factory(force_tool=force_tool))
    workers_mod.reset_workers()
    monkeypatch.setattr(app_mod.state, "graph", None)


# --------------------------------------------------------------------------- #
# 表单 → 假 LLM 脚本 / 标准面入参
# --------------------------------------------------------------------------- #
def molecules_json_from_text(ligands_text: str) -> str:
    """把「名称:SMILES,名称:SMILES」文本解析成分子 JSON（模拟模型照指令转 JSON）。"""
    from docking_agent.core import parse_smiles_text

    return json.dumps(parse_smiles_text(ligands_text), ensure_ascii=False)


def coordinator_script_for_form(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把「原流水线表单」翻译成协调 Agent 的多步工具脚本。

    语义与原表单一致：分子来源（`ligands_text` / `molecule_file`）、受体来源
    （`receptor_file` 优先，其次 `receptor`）、`positive_control`、`exhaustiveness`、
    `engine`、`n_poses`。有阳性对照时才跑结合模式对照分析。
    """
    steps: List[Dict[str, Any]] = []
    molecule_file = str(body.get("molecule_file") or "")
    ligands_text = str(body.get("ligands_text") or "")
    if molecule_file:
        steps.append(step("import_molecule_library", {"molecule_file": molecule_file}))
    elif ligands_text:
        # `import_molecule_library` 的归一化层不认「名称:SMILES,名称:SMILES」这种混合文本，
        # 真实模型会把指令里的清单转成 JSON 再传（这里用确定性解析器模拟）。
        steps.append(step("import_molecule_library",
                          {"query_or_text": molecules_json_from_text(ligands_text)}))
    else:
        steps.append(step("import_molecule_library", {}))
    steps.append(step("run_property_assessment", {}))
    docking: Dict[str, Any] = {
        "exhaustiveness": int(body.get("exhaustiveness") or 1),
        "n_poses": int(body.get("n_poses") or 1),
        "engine": str(body.get("engine") or "vina"),
    }
    receptor_file = str(body.get("receptor_file") or "")
    receptor = body.get("receptor")
    if receptor_file:
        docking["receptor_file"] = receptor_file
    elif receptor:
        docking["receptor_sources"] = str(receptor)
    control = str(body.get("positive_control") or "")
    if control:
        docking["positive_control_smiles"] = control
    steps.append(step("run_docking", docking))
    if control:
        steps.append(step("run_binding_mode_analysis",
                          {"molecules_json": "", "positive_control_smiles": control}))
    steps.append(step("generate_screening_report", {"aggregated_json": ""}))
    return steps


def agent_input_from_form(body: Dict[str, Any], *, message: str = "") -> Dict[str, Any]:
    """把表单语义包成标准 `input`（`mode="manual"`，参数即权威）。"""
    payload = dict(body)
    payload.setdefault("mode", "manual")
    payload.setdefault("message", message or "请按表单参数完成一次完整的分子筛选。")
    payload["messages"] = [{"type": "human", "content": payload["message"]}]
    return payload


def business_run_id_from_sse(text: str) -> str:
    """从标准 SSE 报文里取业务 run id（`start` 事件的 `run_id` / `business_run_id`）。"""
    fallback = ""
    for line in (text or "").splitlines():
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[len("data: "):])
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "start" and event.get("run_id"):
            return str(event["run_id"])
        if event.get("business_run_id"):
            fallback = fallback or str(event["business_run_id"])
        if event.get("run_id") and not str(event["run_id"]).startswith("run_"):
            fallback = fallback or str(event["run_id"])
    return fallback


def run_agent(client: Any, script: List[Dict[str, Any]], body: Dict[str, Any],
              monkeypatch: Any, *, assistant_id: str = "coordinator",
              message: str = "") -> str:
    """装上假 LLM → 建线程 → 标准面流式执行 → 返回业务 run_id。"""
    install_fake_llm(monkeypatch, script=script)
    thread_id = client.post("/threads", json={}).json()["thread_id"]
    resp = client.post(
        f"/threads/{thread_id}/runs/stream",
        json={"assistant_id": assistant_id,
              "stream_mode": ["messages", "updates", "custom"],
              "input": agent_input_from_form(body, message=message)},
    )
    assert resp.status_code == 200, resp.text[:500]
    run_id = business_run_id_from_sse(resp.text)
    assert run_id, f"未从标准 SSE 帧取到业务 run_id：{resp.text[:500]}"
    return run_id
