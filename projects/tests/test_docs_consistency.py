"""文档一致性回归：让「文档说的」与「代码做的」不再漂移。

为什么值得一组测试：本项目的历史经验是——**错误文档比没有文档更糟**。
审计（2026-09-14）在 README / docs/api.md 里查出 30 处与代码不符：已删除的 `critic` 角色、
并不存在的「服务端自动补齐」、阳性对照默认行为、环境变量默认值、角色数量、测试项数……
这些都是「代码改了、文档忘了改」的典型。

本文件只检查**能机器判定**的事实（不查数字类实测值）：
1. 已删除的角色名（critic）不得再出现在文档/配置里；
2. 文档与配置里出现的 Agent 角色名必须都来自 `ROLES`；
3. 代码读取的环境变量必须在 `.env.example` 里登记；
4. README 必须指向开发手册（docs/architecture.md），且该文件存在；
5. 门禁脚本/用例数等清单必须真实存在。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS = [PROJECT_ROOT / "README.md", PROJECT_ROOT / "docs" / "api.md",
        PROJECT_ROOT / "docs" / "architecture.md"]
CONFIG = PROJECT_ROOT / "config" / "agent_llm_config.json"

# 允许在文档里讨论「已删除的角色」（修复记录/历史说明），但不得作为当前角色使用
REMOVED_ROLE = "critic"


def _doc_texts() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in DOCS if p.exists()}


def test_removed_role_does_not_come_back() -> None:
    """critic 已随独立复核流程一并删除（ROLES 里没有它）。

    修复记录里可以提到历史，但**不得**再把它写进当前角色清单/配置。
    """
    from docking_agent.runtime.llm import ROLES

    assert REMOVED_ROLE not in ROLES
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    assert REMOVED_ROLE not in (cfg.get("roles") or {}), "配置文件里不该再有 critic 角色"
    for name, text in _doc_texts().items():
        # 章节标签之外的正文：允许出现在「修复记录」叙述里，但不允许出现在角色清单式表述中
        offenders = [line for line in text.splitlines()
                     if REMOVED_ROLE in line and not any(
                         k in line for k in ("修复", "删除", "移除", "不再", "回归", "历史"))]
        assert not offenders, f"{name} 仍把已删除角色 {REMOVED_ROLE} 当作现有角色：{offenders[:3]}"


def test_documented_roles_come_from_roles_tuple() -> None:
    """文档/配置里出现的角色名必须都是 ROLES 里真实存在的（防止凭空写角色）。"""
    from docking_agent.runtime.llm import ROLES

    allowed = set(ROLES)
    # 文档里以反引号或 JSON 形式出现的角色名候选
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    for role in (cfg.get("roles") or {}):
        if role.startswith("_"):        # `_doc` 之类的说明性键不是角色
            continue
        assert role in allowed, f"配置里有未知角色 {role}"
    for name, text in _doc_texts().items():
        for m in re.finditer(r"角色(?:为|：|:)?\s*`?([a-z_]{3,12})`?", text):
            role = m.group(1)
            if role in ("roles", "role", "coordinator") or "_" in role:
                continue
            assert role in allowed or role in ("intake",), f"{name} 提到未知角色 {role}"


def test_env_vars_read_by_code_are_documented() -> None:
    """代码读到的环境变量必须在 `.env.example` 里登记（否则部署方无从发现）。"""
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?\s*([A-Z_][A-Z0-9_]*)=", env_example, re.M))
    read: set[str] = set()
    for f in (PROJECT_ROOT / "src").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        read |= set(re.findall(r'(?:env|env_int|env_bool|env_float|getenv)\(\s*"([A-Z_][A-Z0-9_]*)"',
                               text))
    # 角色级变量由 LLM_<字段>_<角色> 动态拼出，正则只能看到全局名；这里只校验显式读取的全局名
    missing = sorted(read - documented)
    assert not missing, f"以下环境变量被代码读取但 .env.example 未登记：{missing}"


def test_readme_points_to_developer_handbook() -> None:
    """README 必须指向开发手册，否则后续 AI/开发者找不到架构与规范。"""
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/architecture.md" in readme
    handbook = PROJECT_ROOT / "docs" / "architecture.md"
    assert handbook.exists() and handbook.stat().st_size > 5000
    text = handbook.read_text(encoding="utf-8")
    for section in ("## 2. 架构总览", "## 5. 关键抽象与不变量", "## 10. 开发规范",
                    "## 11. 扩展指南", "## 12. 测试与验收体系", "## 15. 目标差距与改进路线"):
        assert section in text, f"开发手册缺少关键章节：{section}"


def test_gate_scripts_and_referenced_files_exist() -> None:
    """文档/README 里承诺的门禁脚本必须真实存在（不能让新人对着一堆不存在的命令跑）。"""
    scripts = PROJECT_ROOT / "scripts"
    for name in ("smoke_test.py", "verify_docking.py", "check_web.py", "snapshot_ids.py",
                 "intake_eval.py", "ui_e2e.js", "lint_local.py", "kill_leftovers.sh",
                 "shot.py", "ui_shot.py"):
        assert (scripts / name).exists(), f"缺少门禁脚本 scripts/{name}"
    assert (scripts / "lint_baseline.json").exists(), "lint 门禁需要 baseline 文件"


def test_docs_do_not_promise_removed_autocomplete() -> None:
    """服务端不做「自动补齐/事后接管」——文档不得再承诺它（历史审计发现的矛盾点）。"""
    # 只拦「承诺式」表述：修复记录/审计说明里讨论历史行为是允许的
    allowed_context = ("不", "没有", "已移除", "删除", "修复", "审计", "历史", "漂移", "回归")
    for name, text in _doc_texts().items():
        for line in text.splitlines():
            if "自动补齐" in line and not any(k in line for k in allowed_context):
                pytest.fail(f"{name} 仍在承诺「自动补齐」：{line.strip()[:80]}")
