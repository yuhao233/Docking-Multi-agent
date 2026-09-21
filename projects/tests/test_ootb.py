"""开箱即用（OOTB）与交付边界：打包规则、能力自检、标准面定位。

本轮（P0）修复的真实缺陷与确立的守卫：
1. **打包**：`scripts/pack.sh` 的排除项写的是 `assets/receptor/cache`，导致 tar 实测把
   `assets/cache`（786 MB）与 `assets/uploads`（245 MB，**含用户上传数据**）打进了"源码包"，
   体积 1.4 GB。现在排除项必须与真实目录对齐，并有体积上限与包内硬校验。
2. **能力自检**：`scripts/doctor.sh` 把"缺什么 → 降级成什么"一次列清（此前只写在
   `.env.example` 注释里，用户要跑到某一步才知道降级了）。
3. **标准面定位**：`docs/adr/0001-agent-surface.md`（路线 A：产品面 / 平台面），
   并写清"能力只加一处"的硬规则。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACK_SH = PROJECT_DIR / "scripts" / "pack.sh"
DOCTOR_SH = PROJECT_DIR / "scripts" / "doctor.sh"
ADR = PROJECT_DIR / "docs" / "adr" / "0001-agent-surface.md"


def _run(args: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(PROJECT_DIR), capture_output=True, text=True,
                          timeout=timeout)


# --------------------------------------------------------------------------- #
# 1) 打包：排除项对齐真实目录 + 不夹带用户数据
# --------------------------------------------------------------------------- #
def test_pack_excludes_reference_existing_paths_or_globs() -> None:
    """排除项里不允许出现"不存在的路径"——上次的缺陷正是这个（assets/receptor/cache 之外
    真正的大目录没被排除）。glob（*.pyc）与"当前确实不存在"的项目要显式豁免。"""
    text = PACK_SH.read_text(encoding="utf-8")
    excludes = re.findall(r"--exclude=([^\s'\"]+)", text)
    # 脚本里 exclude 是数组元素，形如 'assets/cache'；两种写法都收
    excludes += re.findall(r"^\s*'([^']+)',?\s*#?", text, re.M)
    excludes = [e.strip().strip("'\"") for e in excludes]
    paths = [e for e in excludes if "/" in e or e.startswith("assets") or e.startswith("config")]
    missing = [e for e in sorted(set(paths)) if not (PROJECT_DIR / e).exists()]
    assert missing == [], f"排除项指向不存在的路径：{missing}（规则腐坏，需与真实目录对齐）"


def test_pack_covers_heavy_and_private_dirs() -> None:
    """必须排除的重目录/私密路径：缓存、用户上传、外部工具、运行历史、本地设置与 .env。"""
    text = PACK_SH.read_text(encoding="utf-8")
    for needed in ("assets/cache", "assets/uploads", "assets/tools", "var",
                   "config/local_settings.json", ".env"):
        assert needed in text, f"pack.sh 未排除 {needed}"


def test_pack_list_shows_no_user_data_and_keeps_receptors() -> None:
    """`--list` 预演：体积受控（< 80 MB），不含 cache/uploads/tools，且带上受体注册表。"""
    proc = _run(["bash", "scripts/pack.sh", "--list"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    included = out.split("会包含的条目：", 1)[1]
    for banned in ("assets/cache", "assets/uploads", "assets/tools"):
        assert banned not in included, f"{banned} 不应出现在包含清单里：\n{included}"
    assert "assets/receptors" in included, "已准备的受体必须随包（离线可用）"
    assert "assets/receptors/registry" in included, "registry 需显式可见"
    size_mb = int(re.search(r"预计体积：约 (\d+) MB", out).group(1))
    assert size_mb < 80, f"源码包预计 {size_mb} MB，超过阈值（缓存/上传又被带进来了？）"


# --------------------------------------------------------------------------- #
# 2) 能力自检：矩阵 + 退出码分级
# --------------------------------------------------------------------------- #
def test_doctor_reports_capability_matrix() -> None:
    proc = _run(["bash", "scripts/doctor.sh", "--json"])
    assert proc.returncode in (0, 2), f"doctor 退出码应为 0/2（本机环境），实际 {proc.returncode}\n{proc.stderr}"
    report: Dict[str, Any] = json.loads(proc.stdout)
    assert report["exit_code"] == proc.returncode
    keys = {item["key"] for item in report["items"]}
    for needed in ("python", "dep:rdkit", "dep:vina", "port", "java", "p2rank",
                   "pdb2pqr", "autodock", "external_engine", "cjk_font", "llm"):
        assert needed in keys, f"doctor 缺少能力项 {needed}：{sorted(keys)}"
    for item in report["items"]:
        assert item["label"] and item["detail"], item
        if not item["ok"]:
            # 缺了必须能说清"影响什么 / 怎么补"（不静默）
            assert item.get("degrade") or item.get("hint"), f"缺项未说明影响与补齐方式：{item}"
    # 必需项全绿（本机已装好）；若失败则说明环境真的缺东西
    assert report["required_failed"] == [], report["required_failed"]


def test_doctor_probe_is_reusable_module() -> None:
    """探测逻辑放在 python 模块里（bash 只做解释器选择），便于 CI 与其它脚本复用。"""
    sys.path.insert(0, str(PROJECT_DIR / "scripts"))
    try:
        import doctor_probe  # type: ignore[import-not-found]

        report = doctor_probe.collect(online=False)
        assert report["exit_code"] in (0, 2)
        assert doctor_probe.render(report).startswith("=")
        assert isinstance(doctor_probe.default_port(), int)
        # 端口检查：被占用不算故障（start.sh 会顺延），用真实端口验证语义
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        busy = sock.getsockname()[1]
        try:
            item = doctor_probe.check_port(busy, window=3)
            assert item["required"] is False, "端口是可选能力（start.sh 会顺延）"
        finally:
            sock.close()
    finally:
        sys.path.remove(str(PROJECT_DIR / "scripts"))


# --------------------------------------------------------------------------- #
# 3) 标准面定位：ADR 必须说清两个面与"能力只加一处"
# --------------------------------------------------------------------------- #
def test_adr_documents_both_surfaces_and_single_home_rule() -> None:
    assert ADR.is_file(), "缺少 ADR-0001（产品面 / 平台面定位）"
    text = ADR.read_text(encoding="utf-8")
    for needed in ("产品面", "平台面", "能力只加一处", "langgraph-deploy",
                   "api/agent_service.py", "docs/api.md", "architecture.md"):
        assert needed in text, f"ADR 未提及 {needed}"
    assert "路线 A" in text and "已采纳" in text, "ADR 必须写明已采纳的路线"


def test_root_readme_links_and_images_exist() -> None:
    """根 README 是 GitHub 门面：本地链接与图片必须真实存在（含架构图与界面截图）。"""
    repo_root = PROJECT_DIR.parent
    text = (repo_root / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\]\(([^)#][^)]*)\)", text)
    local = [l for l in links if not l.startswith(("http://", "https://", "mailto:", "#"))
             and not l.startswith("<")]
    missing = [l for l in sorted(set(local)) if not (repo_root / l).exists()]
    assert missing == [], f"根 README 引用了不存在的路径：{missing}"
    for needed in ("docs/images/architecture.png", "docs/images/lifecycle.png",
                   "docs/images/ui-chat.webp", "docs/images/ui-results.webp",
                   "docs/images/ui-report.webp", "docs/images/ui-artifacts.webp"):
        assert (repo_root / needed).is_file(), f"缺少 README 图片 {needed}"
    doc = (repo_root / "docs" / "技术文档.md")
    assert doc.is_file(), "缺少技术文档 docs/技术文档.md"
    doc_text = doc.read_text(encoding="utf-8")
    assert "```mermaid" in text or "```mermaid" in doc_text, \
        "架构应附 Mermaid 版本（GitHub 可直接渲染、可复制）"
    # 技术文档的内链（图片与其它文档）必须存在
    doc_links = re.findall(r"\]\(([^)#][^)]*)\)", doc_text)
    doc_local = [l for l in doc_links if not l.startswith(("http://", "https://", "mailto:"))]
    doc_missing = [l for l in sorted(set(doc_local)) if not (doc.parent / l).exists()]
    assert doc_missing == [], f"技术文档引用了不存在的路径：{doc_missing}"
    for needed in ("images/layers.png", "images/dataflow.png"):
        assert (doc.parent / needed).is_file(), f"技术文档缺少配图 {needed}"
    # 图由脚本生成/抓取：脚本必须随仓库提供，否则图无法随代码更新
    for script in ("scripts/render_architecture.py", "scripts/capture_readme_shots.py"):
        assert (PROJECT_DIR / script).is_file(), f"缺少图像生成脚本 {script}"


def test_architecture_and_api_point_to_adr() -> None:
    architecture = (PROJECT_DIR / "docs" / "architecture.md").read_text(encoding="utf-8")
    api = (PROJECT_DIR / "docs" / "api.md").read_text(encoding="utf-8")
    assert "adr/0001-agent-surface.md" in architecture, "architecture.md 应链接 ADR-0001"
    assert "adr/0001-agent-surface.md" in api, "api.md 应链接 ADR-0001"
    assert "产品面" in api and "平台面" in api, "api.md 顶部应说明本文档覆盖哪个面"
    readme = (PROJECT_DIR / "README.md").read_text(encoding="utf-8")
    assert "scripts/doctor.sh" in readme and "scripts/pack.sh" in readme, "README 应写清两条命令"
