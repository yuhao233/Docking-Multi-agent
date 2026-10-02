"""条件纪律段（`agents/prompt_blocks.py`）的回归。

该系统提示词由固定常量改为按运行事实抽取的子集，每次模型调用的固定开销因此下降，代价是
纪律段可能被抽掉。在缺少标记的实现中这会静默失效：该注入的特殊体系或受体纪律未注入，
模型自行编造。因此用例集中在安全方向，即事实未知时注入、解析失败时退回全文，而不考察
节省的 token 数量。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List


from docking_agent.agents import prompt_blocks as PB
from docking_agent.runtime import run_facts
from docking_agent.runtime.context import AgentContext

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG = PROJECT_ROOT / "config" / "agent_llm_config.json"


def _config_sp() -> str:
    return json.loads(CONFIG.read_text(encoding="utf-8"))["sp"]


# --------------------------------------------------------------------------- #
# 1) 配置：四个条件段都带成对标记（标记缺失会导致条件注入静默失效）
# --------------------------------------------------------------------------- #
def test_config_prompt_marks_every_conditional_block() -> None:
    sp = _config_sp()
    _body, found = PB.parse_blocks(sp)
    missing = sorted(set(PB.BLOCKS) - set(found))
    assert not missing, f"sp 缺少条件段标记 {missing}（会被当成「没有分段」而永不省开销）"
    extra = sorted(set(found) - set(PB.BLOCKS))
    assert not extra, f"sp 里有未在 BLOCKS 登记的条件段 {extra}"
    for key, chunk in found.items():
        assert len(chunk) > 200, f"条件段 {key} 只有 {len(chunk)} 字符，像是标记放错了位置"
    # 每个条件段都需要能被抽掉（标记位置写错时键存在但抽不动）
    assert len(PB.strip_blocks(sp, list(PB.BLOCKS))) < len(sp) - 2000


# --------------------------------------------------------------------------- #
# 2) 解析/剥离：只动标记内的内容，且标记必须成对
# --------------------------------------------------------------------------- #
def test_parse_and_strip_keep_other_blocks() -> None:
    text = ("A\n<!-- block:one -->\nONE\n<!-- end:one -->\n"
            "B\n<!-- block:two -->\nTWO\n<!-- end:two -->\nC\n")
    body, found = PB.parse_blocks(text)
    assert sorted(found) == ["one", "two"]
    assert body == text, "parse_blocks 不该改写正文（只做拆分）"
    out = PB.strip_blocks(text, ["one"])
    assert "ONE" not in out and "TWO" in out and "A" in out and "B" in out and "C" in out
    assert "block:one" not in out and "end:one" not in out, "被抽掉的段的标记必须一起删"
    assert "block:two" in out, "保留段先留着标记，交给 unwrap_blocks 统一去掉"
    assert PB.unwrap_blocks(out) == "A\nB\nTWO\nC\n"
    assert PB.strip_blocks(text, ["one", "two"]).strip() == "A\nB\nC".strip()


def test_unpaired_marker_is_never_dropped() -> None:
    """标记不成对（只有开始没有结束）时原样返回，即保留全文而不丢失纪律。"""
    broken = "A\n<!-- block:one -->\nONE\nB\n"
    assert PB.strip_blocks(broken, ["one"]) == broken
    # 嵌套或错配（`end` 的键不匹配）同样不做剥离
    mismatched = "A\n<!-- block:one -->\nONE\n<!-- end:two -->\nB\n"
    assert PB.strip_blocks(mismatched, ["one"]) == mismatched


def test_no_markers_means_full_prompt() -> None:
    """没有分段标记的配置必须原样使用全文，并在记录里写明原因。"""
    plain = "只有一段普通提示词。" * 40
    text, info = PB.assemble(plain, SimpleNamespace(data={}))
    assert text == plain
    assert info["blocks"] == "none" and "没有条件段标记" in info["reason"]


def test_assemble_falls_back_to_full_text_on_broken_run_object() -> None:
    """运行对象异常（取不到 `data`）时返回全文，而不是抛异常或返回空提示词。"""
    class _Boom:
        @property
        def data(self) -> Any:
            raise RuntimeError("boom")

    sp = _config_sp()
    text, info = PB.assemble(sp, _Boom())
    assert text == PB.unwrap_blocks(sp), "组装失败必须退回全文（且不带组装标记）"
    assert info["blocks"] == "none" and "组装失败" in info["reason"]


# --------------------------------------------------------------------------- #
# 3) 选择规则：安全方向（不知道就注入）
# --------------------------------------------------------------------------- #
def _facts(**kwargs: Any) -> Dict[str, Any]:
    base = {"has_upload": False, "receptor_pending": False, "library_size": 12,
            "has_plan": True, "docking_seen": True, "hetero_atoms": False,
            "special_chemistry": False}
    base.update(kwargs)
    return base


def test_unknown_facts_inject_the_safety_blocks() -> None:
    """关键事实未知时注入对应条件段：无法判定时多花 token，以避免少给纪律。"""
    keep = PB.select_blocks({})
    assert {"receptor_discipline", "special_systems"} <= keep, keep
    # 受体待解析时注入；库大小未知且没有规划时注入漏斗段
    assert PB.select_blocks(_facts(receptor_pending=None)) >= {"receptor_discipline"}
    assert PB.select_blocks(_facts(has_plan=False, library_size=0)) >= {"funnel_two_stage"}


def test_clean_run_skips_the_irrelevant_blocks() -> None:
    keep = PB.select_blocks(_facts())
    assert keep == set(), f"干净的小库运行不该注入任何条件段（实际 {sorted(keep)}）"


def test_dirty_or_big_run_keeps_the_matching_block() -> None:
    assert "special_systems" in PB.select_blocks(_facts(hetero_atoms=True))
    assert "special_systems" in PB.select_blocks(_facts(special_chemistry=True))
    # 只见过「尚未对接」的运行状态时注入（模型据此决定 `keep_hetatm` 与化学形式）
    assert "special_systems" in PB.select_blocks(_facts(docking_seen=False))
    # 大库且指令里没有自动规划时注入漏斗段
    assert "funnel_two_stage" in PB.select_blocks(
        _facts(has_plan=False, library_size=PB.funnel_min() + 1))
    # 有上传文件时注入上传处理段
    assert "upload_files" in PB.select_blocks(_facts(has_upload=True))


def test_collect_facts_reads_spec_plan_and_tool_facts() -> None:
    run = SimpleNamespace(data={
        "task_spec": {"ligands": {"source": "text", "count": 12},
                      "receptor": {"source": "user"}},
        "param_plan": {"exhaustiveness": 20, "library_size": 12},
        "prompt_facts": {"docking_seen": True, "hetero_atoms": True},
    })
    facts = PB.collect_facts(run)
    assert facts["has_upload"] is False and facts["receptor_pending"] is False
    assert facts["library_size"] == 12 and facts["has_plan"] is True
    assert facts["hetero_atoms"] is True, "工具记下的事实必须被读到"
    # 没有规约（CLI 或早期阶段）时取未知（None），注入与否由选择规则决定
    assert PB.collect_facts(SimpleNamespace(data={}))["receptor_pending"] is None


# --------------------------------------------------------------------------- #
# 4) 组装 + 记录：提示词真的变短，且注入清单落到 run.data
# --------------------------------------------------------------------------- #
def test_assemble_shortens_and_reports() -> None:
    sp = _config_sp()
    run = SimpleNamespace(data={
        "task_spec": {"ligands": {"source": "text", "count": 12},
                      "receptor": {"source": "user"}},
        "param_plan": {"exhaustiveness": 20},
        "prompt_facts": {"docking_seen": True, "hetero_atoms": False,
                         "special_chemistry": False},
    })
    text, info = PB.assemble(sp, run)
    assert len(text) < len(sp) - 2000, f"常见小库运行应显著变短（{len(text)} vs {len(sp)}）"
    assert info["injected"] == [] and info["skipped"], info
    assert info["used_chars"] == len(text) and info["full_chars"] == len(sp)
    # 抽掉的段里的独有字眼不得残留在提示词里（大库小节、特殊体系、受体纪律、上传处理）
    for needle in ("按固定分子数把库切成很多片", "unsupported_hetatm", "受体纪律",
                   "用户上传文件处理"):
        assert needle not in text, f"该省掉的段落残留：{needle}"
    assert "<!--" not in text, "给模型的提示词里不该出现组装用的标记"


def test_middleware_records_injection_into_run_data() -> None:
    """协调 Agent 的动态提示词中间件：返回组装后的提示词，并把清单记入 `run.data`。"""
    sp = _config_sp()
    run = SimpleNamespace(data={
        "task_spec": {"ligands": {"source": "text", "count": 12},
                      "receptor": {"source": "user"}},
        "param_plan": {"exhaustiveness": 20},
        "prompt_facts": {"docking_seen": True, "hetero_atoms": False,
                         "special_chemistry": False},
    })

    class _Request:
        def __init__(self, run_obj: Any) -> None:
            self.runtime = SimpleNamespace(context=AgentContext(run=run_obj))
            self.system_message = None

        def override(self, **kwargs: Any) -> "_Request":
            self.system_message = kwargs.get("system_message")
            return self

    mw = PB.coordinator_prompt_middleware(sp)
    seen: List[str] = []
    out = mw.wrap_model_call(_Request(run), lambda req: seen.append(req.system_message.content)
                             or req.system_message)
    assert out.content == seen[0]
    assert len(out.content) < len(sp)
    recorded = run.data.get("prompt_blocks") or {}
    assert recorded.get("skipped"), f"注入清单必须落到 run.data：{recorded}"
    assert recorded.get("used_chars") == len(out.content)


def test_middleware_without_run_uses_full_prompt() -> None:
    """取不到运行（Studio 或 CLI）时返回全文，以避免给出没有受体纪律的提示词。"""
    sp = _config_sp()
    mw = PB.coordinator_prompt_middleware(sp)
    req = SimpleNamespace(runtime=None, system_message=None,
                          override=lambda **kw: SimpleNamespace(
                              system_message=kw.get("system_message")))
    out = mw.wrap_model_call(req, lambda r: r.system_message)
    assert out.content == PB.unwrap_blocks(sp), "取不到运行时必须是全文（只是去掉组装标记）"


# --------------------------------------------------------------------------- #
# 5) 工具侧事实：只记布尔、负面信号粘住
# --------------------------------------------------------------------------- #
def test_docking_facts_extracts_hetero_and_special_chemistry() -> None:
    clean = {"receptors": [{"receptor_key": "A", "results": [
        {"name": "m", "smiles": "CCO", "affinity_kcal_mol": -5.0}]}]}
    facts = run_facts.docking_facts(clean)
    assert facts == {"docking_seen": True, "hetero_atoms": False, "special_chemistry": False}
    assert run_facts.docking_facts({}) == {}, "没有结果就不该下结论（保持未知）"

    dirty = {"receptors": [{"receptor_key": "A", "dropped_hetatm": {"ZN": 2}, "results": [
        {"name": "m", "smiles": "CCO", "removed_fragments": [{"smiles": "[Na+]"}]}]}]}
    facts = run_facts.docking_facts(dirty)
    assert facts["hetero_atoms"] is True and facts["special_chemistry"] is True
    assert run_facts.docking_facts({"receptors": [{"results": [
        {"name": "m", "ligand_facts": {"num_fragments": 3}}]}]})["special_chemistry"] is True
    assert run_facts.docking_facts({"receptors": [{"results": [
        {"name": "m", "ligand_facts": {"has_metal": True}}]}]})["special_chemistry"] is True
    assert run_facts.docking_facts({"receptors": [{"results": [
        {"name": "m", "protonation": {"applied": True}}]}]})["special_chemistry"] is True


def test_facts_are_sticky_once_true() -> None:
    """负面信号具有粘性：一次干净的精算轮次不会抹掉粗筛阶段留下的杂原子标记。"""
    run = SimpleNamespace(data={})
    run_facts.note(run, **{"docking_seen": True, "hetero_atoms": True})
    run_facts.note(run, **{"docking_seen": True, "hetero_atoms": False})
    assert run_facts.read(run)["hetero_atoms"] is True
    # 非粘性键按常规更新
    run_facts.note(run, library_size=10)
    run_facts.note(run, library_size=20)
    assert run_facts.read(run)["library_size"] == 20
    # 记账失败（`run` 没有 `data`）时静默返回，不影响调用方
    run_facts.note(SimpleNamespace(), hetero_atoms=True)
    assert run_facts.read(SimpleNamespace()) == {}


# --------------------------------------------------------------------------- #
# 6) 落盘：注入清单在 run.json 中的记录
# --------------------------------------------------------------------------- #
def test_prompt_blocks_land_in_result_json(run_ctx) -> None:
    """提示词不再是常量，因此需要能从产物中查出本次运行注入了哪些纪律段。"""
    from docking_agent.agents import persistence

    run, _board = run_ctx
    run.data["prompt_blocks"] = {"injected": ["receptor_discipline"],
                                 "skipped": ["funnel_two_stage", "special_systems",
                                             "upload_files"],
                                 "full_chars": 9956, "used_chars": 7000}
    run.data["task_spec"] = {"decision": "ask"}          # 零计算时走 no-op 分支（不产报告）
    persistence.persist_agent_run(run, [], "请补充受体")
    saved = json.loads((run.dir / "result.json").read_text(encoding="utf-8"))
    assert saved["prompt_blocks"]["injected"] == ["receptor_discipline"], saved.get("prompt_blocks")
    assert saved["prompt_blocks"]["used_chars"] == 7000


def test_receptor_provenance_recording_is_whitelisted_and_defensive() -> None:
    """只记录小白名单字段，且记账失败不影响解析结果。"""
    run = SimpleNamespace(data={})
    run_facts.note_receptor_provenance(run, {
        "accession": "Q9SJQ6", "organism": "Arabidopsis thaliana", "pdb_id": "7YHP",
        "structure_url": "https://files.rcsb.org/download/7YHP.pdb",   # 不进入运行小状态
        "downloaded_file": "/tmp/7YHP.pdb",                            # 同上（明细与路径）
        "empty": "", "none": None})
    recorded = run.data["receptor_provenance"]
    assert recorded["accession"] == "Q9SJQ6" and recorded["pdb_id"] == "7YHP"
    assert "structure_url" not in recorded and "downloaded_file" not in recorded
    assert "empty" not in recorded and "none" not in recorded
    # 没有 `data` 或传入非字典时静默返回
    run_facts.note_receptor_provenance(SimpleNamespace(), {"accession": "X"})
    run_facts.note_receptor_provenance(run, "not-a-dict")
    assert run.data["receptor_provenance"]["accession"] == "Q9SJQ6"
