"""报告 Markdown 的 golden 快照回归（离线，不需要引擎）。

覆盖范围：`tests/test_report_pdf.py` 只校验 PDF 魔数、体积与页数，
Markdown 侧此前只被零散断言过几个关键词，报告缺节、表头变化、
数值丢失一类回归缺少断言覆盖。

本用例用一份合成 result 渲染报告，并与提交的快照逐字节比对；
数据固定、时间固定、不联网、不调用 LLM，可在 CI 的离线组里运行。

快照过期时（报告版式或章节变更）用下面命令重出并比对 diff；
重出后按 git diff 确认改动范围。

```bash
cd projects && REGEN_REPORT_GOLDEN=1 .venv/bin/python -m pytest -q tests/test_report_golden.py
```
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

GOLDEN = Path(__file__).resolve().parent / "golden" / "report_min.md"

#: 固定输入：两次运行得到的报告逐字节一致（时间、run_id、受体标签均写死）
GOLDEN_RESULT: Dict[str, Any] = {
    "ranking": [
        {"name": "华法林", "smiles": "CC(=O)CC(c1ccccc1)c1c(O)c2ccccc2oc1=O",
         "affinity_kcal_mol": -8.4, "ligand_efficiency": -0.28, "lipinski_violations": 0,
         "logP": 2.7, "tpsa": 63.6, "molecular_weight": 308.3},
        {"name": "乙醇", "smiles": "CCO", "affinity_kcal_mol": -2.1,
         "ligand_efficiency": -0.35, "lipinski_violations": 0,
         "logP": -0.1, "tpsa": 20.2, "molecular_weight": 46.07},
    ],
    "molecules": [
        {"name": "华法林", "smiles": "CC(=O)CC(c1ccccc1)c1c(O)c2ccccc2oc1=O",
         "molecular_weight": 308.3, "logP": 2.7, "tpsa": 63.6, "h_donors": 1, "h_acceptors": 4,
         "rotatable_bonds": 4, "lipinski_violations": 0},
        {"name": "乙醇", "smiles": "CCO", "molecular_weight": 46.07, "logP": -0.1,
         "tpsa": 20.2, "h_donors": 1, "h_acceptors": 1, "rotatable_bonds": 0,
         "lipinski_violations": 0},
    ],
    "docking": {
        "receptors": [{
            "receptor_key": "thrombin", "receptor": "thrombin(1DWC)",
            "box_center": [31.5, 13.74, 24.36], "box_size": [22.0, 22.0, 22.0],
            "box_source": "实验位点（共晶配体质心）",
            "results": [
                {"name": "华法林", "smiles": "CC(=O)CC(c1ccccc1)c1c(O)c2ccccc2oc1=O",
                 "affinity_kcal_mol": -8.4, "engine": "vina", "exhaustiveness": 16,
                 "box_group": "main", "pass": "fine"},
                {"name": "乙醇", "smiles": "CCO", "affinity_kcal_mol": -2.1,
                 "engine": "vina", "exhaustiveness": 16, "box_group": "main", "pass": "fine"},
            ],
        }],
    },
    "binding": {"rows": [
        {"name": "华法林", "smiles": "CC(=O)CC(c1ccccc1)c1c(O)c2ccccc2oc1=O",
         "similarity": 0.42, "consistency": "consistent"},
    ]},
    "positive_control": {"name": "苯甲脒", "smiles": "NC(=N)c1ccccc1", "affinity_kcal_mol": -5.6},
    "task_spec": {"task_type": "screening", "authority": "chat", "decision": "run",
                  "source": "rules", "assumptions": ["未提供阳性对照 → 跳过对照分子对接"]},
    "notes": ["本次运行使用上传的分子库（2 个分子）。"],
}

GOLDEN_ARTIFACTS: List[Dict[str, Any]] = [
    {"name": "docking_chart", "path": "charts/docking_chart.png", "content_type": "image/png"},
    {"name": "ranking_csv", "path": "ranking.csv", "content_type": "text/csv; charset=utf-8"},
]


def build_golden_markdown() -> str:
    """用固定输入渲染报告（时间、run_id、标签均写死，逐字节可复现）。"""
    from docking_agent.reporting import build_markdown_report

    return build_markdown_report(
        GOLDEN_RESULT, kind="agent", run_id="GOLDEN-RUN-0001",
        receptor_label="thrombin", created_at="2026-01-02 03:04:05",
        artifacts=GOLDEN_ARTIFACTS)


def test_report_markdown_matches_golden_snapshot() -> None:
    """报告 Markdown 与提交的快照一致（章节、表头、数字都在快照里）。"""
    rendered = build_golden_markdown()
    if os.environ.get("REGEN_REPORT_GOLDEN") in ("1", "true", "yes"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(rendered, encoding="utf-8")
        import warnings

        warnings.warn(f"已重出报告快照 {GOLDEN}：请人工 review git diff", stacklevel=1)
        return
    assert GOLDEN.is_file(), f"缺少报告快照 {GOLDEN}（用 REGEN_REPORT_GOLDEN=1 生成）"
    expected = GOLDEN.read_text(encoding="utf-8")
    if rendered != expected:
        import difflib

        diff = "\n".join(list(difflib.unified_diff(
            expected.splitlines(), rendered.splitlines(),
            fromfile="golden/report_min.md", tofile="渲染结果", lineterm=""))[:80])
        raise AssertionError(
            "报告内容与快照不一致（若是有意改版式，请 REGEN_REPORT_GOLDEN=1 重出并 review diff）：\n"
            + diff)


def test_report_golden_covers_every_section() -> None:
    """快照本身覆盖报告骨架的关键小节（防止快照退化成"只剩标题"）。"""
    text = GOLDEN.read_text(encoding="utf-8") if GOLDEN.is_file() else build_golden_markdown()
    for section in ("任务来源与受理", "受体、位点与对接盒", "对接参数", "工具与版本",
                    "推荐化合物排行", "结合口袋分析", "失败与跳过", "数据与产物"):
        assert section in text, f"报告缺少关键小节：{section}"
    # 关键数字来自输入，不是占位值或被丢弃的值
    assert "-8.4" in text and "-2.1" in text, "报告未包含真实亲和力数值"
    assert "华法林" in text and "乙醇" in text


def test_report_golden_is_deterministic() -> None:
    """同一输入渲染两次逐字节一致（否则快照无意义）。"""
    assert build_golden_markdown() == build_golden_markdown()
