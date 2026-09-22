"""「点名了具体受体」的回归测试（先自动解析；不确定才让用户选；不明确就绝不计算）。

真实缺陷：用户指令点名受体「植物去甲基化1酶」→ UniProt accession 直查 + 基因/蛋白名检索
都无匹配 → 系统却按「回退默认受体」**继续对接**了，用户拿到的是以凝血酶为受体的答非所问结果。

修复后的契约（v0.11：由「解析不了就问」升级为「先自动解析，再按置信度决定是否让用户选」）：
1. 受理层：点名但非注册表/PDB/UniProt 形状的受体 → `receptor.source == "named"`（**待解析**）
   且 `decision="run"` —— 第一步交给 `fetch_protein_structure` 自动去 UniProt/RCSB/AlphaFold 查；
2. 解析失败/歧义由工具**写回运行规约**：`fetch_protein_structure` 真正检索失败后把
   `receptor.source` 改成 `unresolved`，`run_docking` 护栏随即在调用任何对接引擎之前拦下
   （产品底线：计算对象不明确时绝不计算，绝不悄悄换默认受体）；
3. 多轮：哪怕上一轮已经问过、分子也已继承，本轮点名受体时照样先自动解析；
4. 不误伤：thrombin·trypsin·1DWC·PDB 号·accession / 上传受体文件 → 仍然 run；
   **完全没提受体 → `default` → ask（只提问，绝不用任何内建受体兜底）**。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

from docking_agent import intake  # noqa: E402
from docking_agent.api.schemas import AgentRequest  # noqa: E402

_NAMED_UNRESOLVED = "请把这个受体「植物去甲基化1酶」和乙醇 CCO 做对接"


def _req(**kwargs) -> AgentRequest:
    base = {"mode": "chat", "advanced": True, "message": "", "receptor": "thrombin",
            "ligands_text": ""}
    base.update(kwargs)
    return AgentRequest(**base)


# --------------------------------------------------------------------------- #
# ① 点名了非注册表受体 → named（待自动解析），不直接 ask
# --------------------------------------------------------------------------- #
def test_named_receptor_is_marked_named_for_auto_resolution() -> None:
    spec = intake.build_task_spec(_req(message=_NAMED_UNRESOLVED))
    assert spec["receptor"] == {"name": "植物去甲基化1酶", "file": "", "source": "named"}
    assert spec["decision"] == "run", "点名受体应交给主管 Agent 先自动解析，而不是一上来就问"

    message = intake.render_agent_message(spec)
    assert "decision=run" in message
    assert "fetch_protein_structure" in message, "必须明确要求第一步调用在线解析工具"
    assert "植物去甲基化1酶" in message
    assert "不要调用任何工具" not in message
    assert "ambiguous" in message and "not_found" in message
    assert "不得改用系统默认受体" in message or "不得改用系统默认受体开跑" in message

    joined = " ".join(spec["assumptions"])
    assert "在线自动解析" in joined and "named" not in joined


def test_uniprot_accession_in_choice_followup_is_used_verbatim() -> None:
    """用户点选候选后的追问（消息里带 accession）必须被当成明确指定的受体，直接解析。"""
    spec = intake.build_task_spec(
        _req(message="用 Q9SJQ6（拟南芥 ROS1 去甲基化酶）作为受体继续对接筛选",
             ligands_text="A:CCO"))
    assert spec["receptor"] == {"name": "Q9SJQ6", "file": "", "source": "user"}
    assert spec["decision"] == "run"


def test_llm_mentioned_receptor_that_cannot_be_resolved_becomes_named() -> None:
    """受理模型如实提取的受体名（原文逐字）不是注册表/accession 时 → named（待自动解析）。"""
    spec = intake.build_task_spec(_req(message="请用 EGFR 做对接，分子 A:CCO", ligands_text="A:CCO"))
    merged = intake.merge_llm_understanding(spec, {"mentioned_receptor": "EGFR"})
    assert merged["receptor"]["source"] == "named"
    assert merged["decision"] == "run"
    assert any("自动解析" in a for a in merged["assumptions"])


def test_llm_mentioned_uniprot_or_pdb_is_resolvable_and_runs() -> None:
    """UniProt accession 形状 / PDB 号是**可解析**的，不算「不可解析」，照常 run。"""
    spec = intake.build_task_spec(_req(message="请用 P00533 做对接，分子 A:CCO", ligands_text="A:CCO"))
    assert intake.merge_llm_understanding(spec, {"mentioned_receptor": "P00533"})["receptor"] \
        == {"name": "P00533", "file": "", "source": "user"}
    spec2 = intake.build_task_spec(_req(message="请用 7Z5X 做对接，分子 A:CCO", ligands_text="A:CCO"))
    assert intake.merge_llm_understanding(spec2, {"mentioned_receptor": "7Z5X"})["receptor"] \
        == {"name": "7Z5X", "file": "", "source": "user"}


def test_generic_demonstrative_is_not_treated_as_named_receptor() -> None:
    """「这个受体」「该受体」只是指代、没有点名 → 不得误判成 named/unresolved。

    注意：「没点名受体」本身也属于**必需项缺失**，所以最终仍然是 `ask`
    （受理层只提问、零工具调用），但它问的是「请指定受体」，而不是
    「您点名的受体解析不了」。"""
    spec = intake.build_task_spec(_req(message="帮我看看这个受体的成药性", ligands_text=""))
    assert spec["receptor"]["source"] == "default", spec["receptor"]
    assert spec["receptor"]["name"] == ""
    assert spec["decision"] == "ask"
    assert any("请指定受体" in q for q in spec["questions"]), spec["questions"]


@pytest.mark.parametrize("message", [
    "帮我把乙醇 CCO 做一次分子对接，未指定受体就用系统默认",
    "未指定受体就用系统默认",
    "用系统默认受体对接 CCO",
    "帮我用某个受体对接 CCO",
    "用其它受体对接 CCO",
    "没有指定受体，你看着办",
])
def test_indefinite_receptor_phrases_are_not_named_receptors(message: str) -> None:
    """「未指定/默认/某个/其它受体」是泛指，不是点名 → 不得误判成 named/unresolved（真实回归）。

    系统已经没有默认受体了，因此这些表述统一落到 `default` → **只提问**（零工具调用），
    而不是拿一个内置受体开跑。"""
    spec = intake.build_task_spec(_req(message=message, ligands_text=""))
    assert spec["receptor"]["source"] == "default", spec["receptor"]
    assert spec["decision"] == "ask"
    assert any("请指定受体" in q for q in spec["questions"]), spec["questions"]


# --------------------------------------------------------------------------- #
# ② 同一条消息里上传了受体文件 → run
# --------------------------------------------------------------------------- #
def test_named_but_unresolvable_receptor_with_uploaded_file_runs() -> None:
    spec = intake.build_task_spec(
        _req(message=_NAMED_UNRESOLVED, receptor_file="assets/receptor/rec.pdbqt"))
    assert spec["receptor"] == {"name": "", "file": "assets/receptor/rec.pdbqt", "source": "user"}
    assert spec["decision"] == "run"


# --------------------------------------------------------------------------- #
# ③ 注册表受体 / PDB 号 → run（不误伤）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("message,expected", [
    ("请用 thrombin 做筛选：A:CCO", "thrombin"),
    ("请用 1DWC 做筛选：A:CCO", "thrombin"),
    ("请用 trypsin 做筛选：A:CCO", "trypsin"),
    ("请用 1PTU 做筛选：A:CCO", "trypsin"),
    ("请用凝血酶做筛选：A:CCO", "thrombin"),      # 中文别名
    ("请用胰蛋白酶做筛选：A:CCO", "trypsin"),
])
def test_known_receptors_still_run(message: str, expected: str) -> None:
    spec = intake.build_task_spec(_req(message=message, ligands_text="A:CCO"))
    assert spec["receptor"] == {"name": expected, "file": "", "source": "user"}
    assert spec["decision"] == "run"


# --------------------------------------------------------------------------- #
# ④ 完全没提受体 → **只提问**（已无任何「默认受体」兜底）
# --------------------------------------------------------------------------- #
def test_no_receptor_mentioned_asks_instead_of_defaulting() -> None:
    spec = intake.build_task_spec(_req(message="帮我筛一下这两个分子 CCO", ligands_text=""))
    assert spec["receptor"]["source"] == "default"
    assert spec["decision"] == "ask", "受体是必需项；未指定就只提问，绝不替用户挑靶点"
    assert "receptor" in spec["missing"]
    msg = intake.render_agent_message(spec)
    assert "thrombin" not in msg and "1DWC" not in msg
    assert "不要回退任何默认受体" in msg


# --------------------------------------------------------------------------- #
# ⑤ 多轮：本轮点名受体 → 仍走自动解析（多轮守卫不改变计算对象纪律）
# --------------------------------------------------------------------------- #
def test_multi_turn_named_receptor_still_goes_through_auto_resolution() -> None:
    prior = [{"role": "user",
              "content": "帮我筛这两个分子：华法林 CC(=O)CC(c1ccccc1)c1c(O)c2ccccc2oc1=O"},
             {"role": "assistant", "content": "请问您想用哪个受体？"}]
    spec = intake.build_task_spec(_req(message="就用植物去甲基化1酶", ligands_text=""),
                                  prior_turns=prior)
    assert spec["ligands"]["source"] == "message", "分子已从上一轮继承"
    assert spec["receptor"]["source"] == "named"
    assert spec["decision"] == "run", "点名受体必须先自动解析；只有解析不确定时才回头问用户"


def test_multi_turn_molecule_choice_inherits_resolved_accession() -> None:
    """上一轮已把受体解析成 accession 时，本轮（分子点选后的追问）必须继承它而非回退默认。"""
    prior = [
        {"role": "user", "content": "从在线数据库中获取植物去甲基化酶ROS1，与小分子代森锰锌进行对接筛选"},
        {"role": "assistant",
         "content": "已解析受体 Q9SJQ6（Arabidopsis thaliana，DNA glycosylase/AP lyase ROS1）。"
                    "请在候选分子代表结构中选择一种。"},
    ]
    spec = intake.build_task_spec(
        _req(message="代森锰锌-EBDC S=C([S-])NCCNC(=S)[S-] （按最大有机片段/无金属代表结构继续对接）",
             ligands_text=""),
        prior_turns=prior)
    assert spec["receptor"] == {"name": "Q9SJQ6", "file": "", "source": "user"}
    assert spec["ligands"]["source"] == "message" and spec["ligands"]["count"] == 1
    assert spec["decision"] == "run"
    assert any("继承自上一轮已解析结果" in a for a in spec["assumptions"])


def test_multi_turn_inherits_pdb_id_when_prior_turn_mentioned_one() -> None:
    """上一轮助手回复里提到 PDB ID 时，按既有规则继承该 PDB（同样可解析）。"""
    prior = [
        {"role": "user", "content": "从在线数据库中获取植物去甲基化酶ROS1，与小分子代森锰锌进行对接筛选"},
        {"role": "assistant",
         "content": "已解析受体 Q9SJQ6，结构来源 RCSB 实验结构 7YHP（EM，3.1 Å）。请选择分子形式。"},
    ]
    spec = intake.build_task_spec(
        _req(message="代森锰锌-EBDC S=C([S-])NCCNC(=S)[S-] （按最大有机片段继续对接）", ligands_text=""),
        prior_turns=prior)
    assert spec["receptor"] == {"name": "7YHP", "file": "", "source": "user"}
    assert spec["decision"] == "run"


def test_our_own_receptor_question_is_never_inherited_as_user_receptor() -> None:
    """回归（2026-09-21 实测复现）：系统自己生成的「请指定受体…」提问（含示例 PDB/accession）
    绝不能被当成**用户点名**的受体。

    真实故障：第 1 轮问「请指定受体：① 提供 PDB 编号（如 4HHB）或 UniProt accession（如 P08922）…」，
    第 2 轮用户只回一句「好的，继续」→ 受理层从**助手回复**里抽到 4HHB 并当成用户指定，
    于是 decision=run、系统拿一个示例编号开跑。用户什么都没说却跑了别人的靶点。
    """
    from docking_agent.intake import _receptor_source_question

    # (a) 用**真实**的当前提问文案
    prior_real = [
        {"role": "user", "content": "帮我筛这两个分子 CCO、CCN"},
        {"role": "assistant", "content": _receptor_source_question()},
    ]
    spec = intake.build_task_spec(_req(message="好的，继续", ligands_text=""),
                                  prior_turns=prior_real)
    assert spec["receptor"]["source"] == "default", spec["receptor"]
    assert spec["decision"] == "ask"
    assert spec["ligands"]["source"] == "message", "分子仍应从上一轮继承"

    # (b) 真实形态：上一轮的「用户」消息其实是**受理层渲染后的整段机器文本**
    #     （checkpointer 里存的就是它）——里面的「先请用户指定受体」会被受体名抽取当成
    #     用户点名的受体，进而去扫助手回复里的示例编号。这是实测复现的泄漏路径。
    round1_spec = intake.build_task_spec(
        _req(message="帮我筛这两个分子 CCO、CCN，未指定受体", ligands_text=""))
    rendered_user_turn = intake.render_agent_message(round1_spec)
    assert "先请用户指定受体" in rendered_user_turn, "渲染块里确实有会被误抽的措辞"
    prior_rendered = [
        {"role": "user", "content": rendered_user_turn},
        {"role": "assistant",
         "content": "请指定受体：① 提供 PDB 编号（如 3ZBF）或 UniProt accession（如 P08922）；"
                    "② 上传受体结构文件（.pdb/.cif/.pdbqt）。"},
    ]
    spec2 = intake.build_task_spec(_req(message="好的，继续", ligands_text=""),
                                   prior_turns=prior_rendered)
    assert spec2["receptor"] == {"name": "", "file": "", "source": "default"}, spec2["receptor"]
    assert spec2["decision"] == "ask", spec2
    assert not any("3ZBF" in a for a in spec2["assumptions"]), spec2["assumptions"]

    # (b2) 提问文案里带具体示例编号（防止以后有人再加回示例）同样不得继承
    prior_with_example = [
        {"role": "user", "content": "帮我筛这两个分子 CCO、CCN"},
        {"role": "assistant", "content": _receptor_source_question() + "（如 4HHB / P08922）"},
    ]
    spec2b = intake.build_task_spec(_req(message="好的，继续", ligands_text=""),
                                    prior_turns=prior_with_example)
    assert spec2b["receptor"]["source"] == "default", spec2b["receptor"]
    assert spec2b["decision"] == "ask"
    assert not any("4HHB" in a for a in spec2b["assumptions"]), spec2b["assumptions"]

    # (c) 对照：上一轮**用户**确实点名过受体时，助手回复里的已解析 accession 仍可继承
    prior_user_named = [
        {"role": "user", "content": "从在线数据库中获取植物去甲基化酶ROS1，与小分子代森锰锌对接"},
        {"role": "assistant", "content": "已解析受体 Q9SJQ6（Arabidopsis thaliana）。"},
    ]
    spec3 = intake.build_task_spec(_req(message="好的，继续", ligands_text=""),
                                   prior_turns=prior_user_named)
    assert spec3["receptor"] == {"name": "Q9SJQ6", "file": "", "source": "user"}


# --------------------------------------------------------------------------- #
# ⑥ 分发护栏：只有**真正解析失败**（工具写回 unresolved）才拦下，且零对接调用
# --------------------------------------------------------------------------- #
def test_fetch_failure_marks_receptor_unresolved_and_guard_blocks(monkeypatch) -> None:
    """在线检索确实查不到时：工具把规约改成 unresolved，护栏拦下对接（产品底线）。"""
    from docking_agent.runs import Run, current_run
    from docking_agent.agents import dispatch
    from docking_agent.tools import online

    resolution = {"status": "not_found", "query": {"raw": "查不到的假受体ZZZQQ9"},
                  "selected": None, "candidates": [],
                  "attempts": [{"strategy": "gene", "query": "gene_exact:ZZZQQ9", "hits": 0},
                               {"strategy": "free-text", "query": "ZZZQQ9", "hits": 0}]}
    monkeypatch.setattr(online, "resolve_receptor_name", lambda src, **kw: resolution)

    with tempfile.TemporaryDirectory() as tmp:
        run = Run(Path(tmp), "R-NAME-FAIL", "agent", {"mode": "chat"})
        run.data["task_spec"] = {"receptor": {"name": "查不到的假受体ZZZQQ9", "file": "",
                                              "source": "named"}}
        token = current_run.set(run)
        try:
            out = json.loads(online.fetch_protein_structure.func("查不到的假受体ZZZQQ9"))
        finally:
            current_run.reset(token)

    assert out["status"] == "error" and out.get("reason") == "not_found"
    assert any("gene_exact:ZZZQQ9" in a for a in out["attempts"]), "必须逐条列出已尝试的检索"
    assert run.data["task_spec"]["receptor"]["source"] == "unresolved"
    guard = json.loads(dispatch.unresolved_receptor_message(run))
    assert guard["status"] == "needs_user_input"
    assert "gene_exact:ZZZQQ9" in guard["message"], "护栏回执也要带上已尝试的检索"


def test_fetch_ambiguity_publishes_choices_and_guard_blocks(monkeypatch) -> None:
    """检索到多个同样合理的候选：下发结构化 choices，且仍然阻断对接（不替用户选）。"""
    from docking_agent.runs import Run, current_run
    from docking_agent.agents import dispatch
    from docking_agent.tools import online

    cands = [
        {"accession": "P08922", "protein": "Proto-oncogene tyrosine-protein kinase ROS",
         "organism": "Homo sapiens", "organism_id": 9606, "gene_names": ["ROS1"],
         "reviewed": True, "score": 64.0, "pdb_ids": ["3ZBF"], "reasons": ["Swiss-Prot 已审阅 +40"]},
        {"accession": "Q9SJQ6", "protein": "DNA glycosylase/AP lyase ROS1",
         "organism": "Arabidopsis thaliana", "organism_id": 3702, "gene_names": ["ROS1"],
         "reviewed": True, "score": 64.0, "pdb_ids": ["7YHP"], "reasons": ["Swiss-Prot 已审阅 +40"]},
    ]
    resolution = {"status": "ambiguous", "query": {"raw": "ROS1", "genes": ["ROS1"]},
                  "selected": None, "candidates": cands,
                  "attempts": [{"strategy": "gene", "query": "gene_exact:ROS1", "hits": 8}]}
    monkeypatch.setattr(online, "resolve_receptor_name", lambda src, **kw: resolution)

    with tempfile.TemporaryDirectory() as tmp:
        run = Run(Path(tmp), "R-AMBIG", "agent", {"mode": "chat"})
        run.data["task_spec"] = {"receptor": {"name": "ROS1", "file": "", "source": "named"}}
        token = current_run.set(run)
        try:
            out = json.loads(online.fetch_protein_structure.func("ROS1"))
        finally:
            current_run.reset(token)

    assert out["status"] == "ambiguous"
    choices = run.data.get("choices") or []
    assert len(choices) >= 2, "歧义时必须给出 ≥2 个候选可选项"
    assert all(c.get("kind") == "receptor" for c in choices)
    assert choices[0]["value"] == "P08922" and choices[0]["prompt"]
    assert run.data["task_spec"]["receptor"]["source"] == "unresolved"
    assert json.loads(dispatch.unresolved_receptor_message(run))["status"] == "needs_user_input"


def test_run_docking_guard_returns_needs_user_input_with_zero_docking(monkeypatch) -> None:
    from docking_agent.runs import Run, current_run
    from docking_agent.agents import dispatch
    from docking_agent.tools import docking as docking_tool

    calls = {"agent": 0, "dock_library": 0, "molecular_docking": 0}

    def _no_agent():
        calls["agent"] += 1
        raise AssertionError("受体不可解析时不得创建/调用 Docking 子 Agent")

    monkeypatch.setattr(dispatch, "get_docking_agent", _no_agent)
    monkeypatch.setattr(docking_tool, "dock_library",
                        lambda *a, **k: calls.__setitem__("dock_library", calls["dock_library"] + 1))
    monkeypatch.setattr(docking_tool, "molecular_docking",
                        lambda *a, **k: calls.__setitem__("molecular_docking",
                                                          calls["molecular_docking"] + 1))

    with tempfile.TemporaryDirectory() as tmp:
        run = Run(Path(tmp), "R-UNRES", "agent", {"mode": "chat"})
        run.data["task_spec"] = {"receptor": {"name": "植物去甲基化1酶", "source": "unresolved",
                                              "resolution": {
                                                  "attempts": [{"strategy": "gene_exact",
                                                                "query": "gene_exact:ROS1",
                                                                "hits": 0}],
                                                  "candidates": [{"accession": "Q9SJQ6",
                                                                  "organism": "Arabidopsis thaliana",
                                                                  "protein": "DNA glycosylase/AP lyase ROS1",
                                                                  "pdb_ids": ["7YHP"], "score": 99.0}]}}}
        token = current_run.set(run)
        try:
            out = json.loads(dispatch.run_docking.func(
                molecules_json='[{"name":"A","smiles":"CCO"}]',
                receptor_sources="thrombin"))
        finally:
            current_run.reset(token)

    assert out["status"] == "needs_user_input"
    assert "植物去甲基化1酶" in out["message"]
    assert ".pdbqt" in out["message"] and "UniProt accession" in out["message"]
    assert "gene_exact:ROS1" in out["message"], "回执要列出已尝试的检索"
    assert "Q9SJQ6" in out["message"], "回执要列出找到的候选"
    assert calls == {"agent": 0, "dock_library": 0, "molecular_docking": 0}, calls


def test_run_docking_guard_blocks_default_and_passes_user_receptor() -> None:
    """护栏语义（新契约）：**未指定受体（default）同样被拦下**（系统没有默认受体），
    用户显式指定的受体不受影响。"""
    from docking_agent.agents import dispatch

    class _Run:
        def __init__(self, spec):
            self.data = {"task_spec": spec}

    blocked_default = json.loads(dispatch.unresolved_receptor_message(
        _Run({"receptor": {"name": "", "source": "default"}})))
    assert blocked_default["status"] == "needs_user_input"
    assert blocked_default["missing"] == ["receptor"]
    assert not blocked_default.get("choices"), "预置受体不得作为用户可选来源下发"
    assert "thrombin" not in blocked_default["message"]
    assert "1DWC" not in blocked_default["message"]

    assert dispatch.unresolved_receptor_message(
        _Run({"receptor": {"name": "trypsin", "source": "user"}})) == ""
    assert dispatch.unresolved_receptor_message(_Run({})) == ""
    assert dispatch.unresolved_receptor_message(None) == ""
    blocked = json.loads(dispatch.unresolved_receptor_message(
        _Run({"receptor": {"name": "植物去甲基化1酶", "source": "unresolved"}})))
    assert blocked["status"] == "needs_user_input"


def test_no_agent_binds_the_preset_receptor_catalog_tools() -> None:
    """契约：预置受体**只用于内部测试** —— 任何 Agent（协调层与子 Agent）都不得绑定
    「列出预置受体」的工具，否则模型会把它当成用户可选来源。"""
    from pathlib import Path

    from docking_agent.agents.workers import _WORKER_SPECS

    for role, (_sp_factory, tools_factory) in _WORKER_SPECS.items():
        names = {getattr(t, "name", "") for t in tools_factory()}
        assert "available_receptors" not in names, f"{role} 子 Agent 不得暴露预置受体清单"
        assert "list_known_receptors" not in names, f"{role} 子 Agent 不得暴露预置受体清单"

    coord_src = (Path(__file__).resolve().parent.parent
                 / "src/docking_agent/agents/coordinator.py").read_text(encoding="utf-8")
    assert "list_known_receptors" not in coord_src, "协调 Agent 的工具面不得再有预置受体清单"
    assert "available_receptors" not in coord_src


# --------------------------------------------------------------------------- #
# ④ 附件清单不得成为受体名来源（真实缺陷 20260917-122453-0404）
# --------------------------------------------------------------------------- #
_UPLOAD_PATH = ("/home/biolab/Tools/docking-agent/projects/assets/uploads/"
                "20260917-122453-c6b872-20260917-122255-ff6f3d-PGR_120.sdf")
_REF_BLOCK = ("对上传的小分子库做分子对接筛选，结果请带上每个小分子的 ID\n\n"
              "引用文件（本次对话已上传，可直接作为工具输入）：\n"
              f"- 20260917-122255-ff6f3d-PGR_120.sdf（小分子库，120 个分子）→ {_UPLOAD_PATH}")


def test_user_instruction_text_strips_machine_appended_file_block() -> None:
    """纯函数：剥掉机器拼接的「引用文件」清单，只留用户自己写的正文。"""
    text = intake.user_instruction_text(_REF_BLOCK)
    assert text == "对上传的小分子库做分子对接筛选，结果请带上每个小分子的 ID"
    assert "引用文件" not in text and "/home/" not in text
    assert intake.user_instruction_text("只要这一句") == "只要这一句"


def test_llm_instruction_view_hides_absolute_paths() -> None:
    """给受理模型看的指令里不得出现绝对路径（路径只走结构化字段）。"""
    view = intake._llm_instruction_view(_REF_BLOCK)
    assert "/home/" not in view and "c6b872" not in view, view
    assert "PGR_120.sdf" in view, "文件名本身要保留，模型才知道有附件"
    assert "不要据此推断受体名" in view


def test_receptor_hash_from_upload_path_is_not_named_receptor() -> None:
    """上传落盘名里的哈希片段被模型当成受体名时必须丢弃 → 回到「未指定受体」并按契约提问。"""
    spec = intake.build_task_spec(_req(message=_REF_BLOCK, molecule_file=_UPLOAD_PATH))
    assert spec["receptor"]["source"] == "default"
    assert spec["decision"] == "ask", "缺受体 = 必需项缺失 → 只提问"

    merged = intake.merge_llm_understanding(spec, {"mentioned_receptor": "C6B872"})
    assert merged["receptor"]["source"] == "default", merged["receptor"]
    assert merged["receptor"]["name"] != "C6B872"
    assert merged["decision"] == "ask", "丢弃伪受体名不会改变受理层结论"
    notes = " ".join(merged.get("llm_notes") or [])
    assert "C6B872" in notes and "附件路径" in notes, notes

    rendered = intake.render_agent_message(merged)
    assert "C6B872" not in rendered, "渲染给编排层的消息里不得再出现这个伪受体名"


def test_receptor_named_in_user_text_survives_attachment_filter() -> None:
    """对照：用户在正文里真点名了受体 → 附件清单的过滤不得误伤。"""
    raw = ("请用 EGFR 和上传的库做对接\n\n"
           "引用文件（本次对话已上传，可直接作为工具输入）：\n"
           f"- PGR_120.sdf（小分子库，120 个分子）→ {_UPLOAD_PATH}")
    spec = intake.build_task_spec(_req(message=raw, molecule_file=_UPLOAD_PATH))
    merged = intake.merge_llm_understanding(spec, {"mentioned_receptor": "EGFR"})
    assert merged["receptor"] == {"name": "EGFR", "file": "", "source": "named"}
    assert merged["decision"] == "run"


def test_llm_invented_receptor_is_still_dropped_with_reason() -> None:
    """模型凭空编造的受体名（原文里哪都没有）同样丢弃，并如实记下原因。"""
    spec = intake.build_task_spec(_req(message="用示例库做一次筛选", ligands_text=""))
    merged = intake.merge_llm_understanding(spec, {"mentioned_receptor": "SNAP25"})
    assert merged["receptor"]["source"] == "default"
    notes = " ".join(merged.get("llm_notes") or [])
    assert "SNAP25" in notes and "原文中不存在" in notes, notes


# --------------------------------------------------------------------------- #
# 跨面不变量：**「系统没有默认受体」必须在每一个用户可见/模型可见的面上成立**
# --------------------------------------------------------------------------- #
def test_no_user_facing_surface_offers_a_default_receptor() -> None:
    """跨面回归（2026-09-21 审计发现的真实漂移）。

    代码层（`intake` / `run_docking` 护栏 / `molecular_docking`）改成「未指定受体只提问」
    之后，另外三个「面」还留着「改用系统默认受体 凝血酶」：
      ① 协调 Agent 提示词（`config/agent_llm_config.json` 的 `sp`）—— 模型会照着说；
      ② 在线检索失败的回执与 choices（`tools/choices.py` / `tools/online.py`）—— 用户会照点；
      ③ 候选生成器（`core/resolve.py`）—— 无条件追加一个 `thrombin` 选项。

    后果是「用户什么都没说，系统却拿预置测试受体当研究靶点跑」。这里把四个面逐一钉住。
    """
    import json as _json

    cfg = _json.loads((PROJECT_ROOT / "config" / "agent_llm_config.json").read_text(encoding="utf-8"))
    sp = cfg.get("sp") or ""
    assert "按系统默认受体执行" not in sp, "协调提示词仍在指示「按系统默认受体执行」"
    assert "系统内建默认兜底" not in sp, "协调提示词仍宣称「系统内建默认兜底」"
    assert "系统没有默认受体" in sp, "协调提示词必须写明「系统没有默认受体」"

    src = PROJECT_ROOT / "src" / "docking_agent"
    choices_py = (src / "tools" / "choices.py").read_text(encoding="utf-8")
    assert "改用系统默认受体" not in choices_py, "choices 回执仍在提供「改用系统默认受体」"

    resolve_py = (src / "core" / "resolve.py").read_text(encoding="utf-8")
    assert "include_default" not in resolve_py, "候选生成器仍有 include_default 分支"
    assert "default-thrombin" not in resolve_py, "候选生成器仍在追加预置 thrombin 选项"

    online_py = (src / "tools" / "online.py").read_text(encoding="utf-8")
    assert "include_default" not in online_py, "在线解析仍在请求默认受体选项"

    prompts_py = (src / "agents" / "prompts.py").read_text(encoding="utf-8")
    for line in prompts_py.splitlines():
        if "默认受体" in line:
            assert any(k in line for k in ("不得", "没有", "不要", "绝不", "禁止", "只用于内部")), \
                f"子 Agent 提示词仍在正面描述默认受体：{line.strip()[:80]}"

