"""文档一致性回归：校验文档描述与代码行为一致。

校验项限于能机器判定的事实（不含数字类取值）：
1. 已删除的角色名（critic）不出现在文档与配置里；
2. 文档与配置里出现的 Agent 角色名都来自 `ROLES`；
3. 代码读取的环境变量都在 `.env.example` 里登记；
4. README 指向开发手册（docs/architecture.md），且该文件存在；
5. 文档列出的门禁脚本与用例数等清单真实存在。

文档与代码不一致的典型来源是代码改动后未同步文档，
因此 README / docs/api.md / docs/architecture.md 均纳入校验。
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

# 文档可讨论「已删除的角色」（变更说明），但不作为当前角色使用
REMOVED_ROLE = "critic"


def _doc_texts() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in DOCS if p.exists()}


def test_removed_role_does_not_come_back() -> None:
    """critic 已随独立复核流程一并删除（ROLES 里没有它）。

    变更说明中可以提到该角色，但不把它写进当前角色清单与配置。
    """
    from docking_agent.runtime.llm import ROLES

    assert REMOVED_ROLE not in ROLES
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    assert REMOVED_ROLE not in (cfg.get("roles") or {}), "配置文件里不该再有 critic 角色"
    for name, text in _doc_texts().items():
        # 正文行：允许出现在变更说明里，不允许出现在角色清单式表述中
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
    """代码读到的环境变量都在 `.env.example` 里登记（否则部署方无从查证）。"""
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
    """README 指向开发手册，供后续开发者查阅架构与规范。"""
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/architecture.md" in readme
    handbook = PROJECT_ROOT / "docs" / "architecture.md"
    assert handbook.exists() and handbook.stat().st_size > 5000
    text = handbook.read_text(encoding="utf-8")
    for section in ("## 2. 架构总览", "## 5. 关键抽象与不变量", "## 10. 开发规范",
                    "## 11. 扩展指南", "## 12. 测试与验收体系", "## 15. 目标差距与改进路线"):
        assert section in text, f"开发手册缺少关键章节：{section}"


def test_gate_scripts_and_referenced_files_exist() -> None:
    """文档与 README 里列出的门禁脚本真实存在。"""
    scripts = PROJECT_ROOT / "scripts"
    for name in ("smoke_test.py", "verify_docking.py", "check_web.py", "snapshot_ids.py",
                 "intake_eval.py", "ui_e2e.js", "lint_local.py", "kill_leftovers.sh",
                 "shot.py", "ui_shot.py"):
        assert (scripts / name).exists(), f"缺少门禁脚本 scripts/{name}"
    assert (scripts / "lint_baseline.json").exists(), "lint 门禁需要 baseline 文件"


def test_docs_do_not_promise_removed_autocomplete() -> None:
    """服务端不做自动补齐与事后接管，文档不承诺该行为。"""
    # 只拦「承诺式」表述：变更说明里讨论该行为是允许的
    allowed_context = ("不", "没有", "已移除", "删除", "修复", "审计", "历史", "漂移", "回归")
    for name, text in _doc_texts().items():
        for line in text.splitlines():
            if "自动补齐" in line and not any(k in line for k in allowed_context):
                pytest.fail(f"{name} 仍在承诺「自动补齐」：{line.strip()[:80]}")


# --------------------------------------------------------------------------- #
# 生成物（PDF/DOCX）与其 Markdown 源保持同步
# --------------------------------------------------------------------------- #
REPO_ROOT = PROJECT_ROOT.parent
#: (sidecar 路径, 说明)，sidecar 由生成器写出（见 scripts/doc_stamp.py）
GENERATED_STAMPS = [
    (REPO_ROOT / "docs" / ".generated.json",
     "docs/技术文档.md → 技术文档.docx / 技术文档.pdf（scripts/build_tech_doc_pdf.py）"),
    (PROJECT_ROOT / "docs" / ".generated.json",
     "docs/技术报告.md → 技术报告.docx / 技术报告.pdf（scripts/build_report_docx.py）"),
]


def _load_stamp(path: Path) -> list:
    try:
        import json as _json

        data = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = (data or {}).get("artifacts") or {}
    return [(name, info) for name, info in sorted(entries.items()) if isinstance(info, dict)]


def test_generated_documents_match_their_sources() -> None:
    """PDF/DOCX 由当前的 Markdown 源生成。

    判据是内容哈希而不是 mtime：干净克隆 / CI 检出 / `git checkout` 都会重写 mtime，
    按时间判断既会误报也会漏报。生成器在产出时把源文件 sha256 写进
    `<dir>/.generated.json`，这里只比哈希。

    源文件改动后未重出产物时，提交的 `.docx`/`.pdf` 内容陈旧，
    而 PDF 是评审最先读的形态，因此该情况按不一致处理。
    """
    import sys as _sys

    _sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from doc_stamp import sha256_of  # noqa: PLC0415

    problems: list[str] = []
    checked = 0
    for stamp_path, hint in GENERATED_STAMPS:
        if not stamp_path.is_file():
            problems.append(f"缺少生成物溯源戳 {stamp_path.relative_to(REPO_ROOT)}"
                            f"（先跑生成器；{hint}）")
            continue
        entries = _load_stamp(stamp_path)
        if not entries:
            problems.append(f"{stamp_path.relative_to(REPO_ROOT)} 里没有任何条目（重新跑生成器）")
            continue
        for name, info in entries:
            source = stamp_path.parent / str(info.get("source") or "")
            output = stamp_path.parent / name
            if not source.is_file():
                problems.append(f"{stamp_path.name}: 源文件不存在 {source.name}")
                continue
            if not output.is_file():
                problems.append(f"{stamp_path.name}: 产物不存在 {name}")
                continue
            checked += 1
            actual = sha256_of(source)
            if actual != info.get("sha256"):
                problems.append(
                    f"{name} 与源 {source.name} 不同步（源已改但未重出产物）")
    assert not problems, "生成物与源不一致：\n  - " + "\n  - ".join(problems)
    assert checked >= 4, f"至少应校验 4 个产物（2 份文档 × docx/pdf），实际 {checked}"


def test_report_figures_are_committed() -> None:
    """技术报告的配图须入库。

    FIGURE_MAP 里的 13 张配图若指向被 gitignore 的 `var/` 运行产物，
    干净克隆上重跑生成器只会得到 13 个红色「［缺图］」占位符，产物无法从仓库复现。
    生成器缺图时默认拒绝出文档（见 `--allow-missing-figures`），本用例保证图片本身在仓库里。
    """
    script = (PROJECT_ROOT / "scripts" / "build_report_docx.py").read_text(encoding="utf-8")
    block = re.search(r"FIGURE_MAP: list\[Figure\] = \[(.*?)\n\]", script, re.S)
    assert block, "未找到 FIGURE_MAP（生成器的插图映射）"
    paths = re.findall(r'Figure\([^)]*?"([^"]+\.png)"', block.group(1), re.S)
    assert len(paths) >= 10, f"配图数量异常：{len(paths)}"
    assert all("{" not in path for path in paths), \
        f"配图路径不得用 f-string（克隆后无法解析）：{[p for p in paths if '{' in p]}"
    missing = [path for path in paths if not (PROJECT_ROOT / path).is_file()]
    assert missing == [], f"配图未入库（干净克隆无法复现 DOCX/PDF）：{missing}"


def test_report_generator_refuses_to_build_without_figures() -> None:
    """缺图时生成器拒绝出文档（否则会产出带红色占位符的无效产物）。"""
    pytest.importorskip("docx")            # python-docx 是生成器的开发工具依赖
    import sys as _sys
    scripts = str(PROJECT_ROOT / "scripts")
    if scripts not in _sys.path:
        _sys.path.insert(0, scripts)
    import build_report_docx as builder

    assert builder.missing_figure_files(PROJECT_ROOT) == [], "仓库内配图必须齐全"
    fake = [builder.Figure(anchor=r"^1\.", after=1,
                           path="docs/report-media/__definitely_missing__.png", caption="x")]
    assert builder.missing_figure_files(PROJECT_ROOT, figures=fake) == \
        ["docs/report-media/__definitely_missing__.png"]
