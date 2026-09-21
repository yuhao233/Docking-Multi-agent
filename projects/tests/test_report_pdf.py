"""PDF 报告导出、`downloads` 元信息与规范下载名的测试。

全部离线：只有一次小规模真实 Vina 对接（exhaustiveness=1，2 个分子），
不调用 LLM、不访问网络；其余断言直接对 `build_report_pdf` / `download_name` 做单元测试。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from support.fake_llm import coordinator_script_for_form, run_agent  # noqa: E402


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    from docking_agent.api.app import app

    with TestClient(app) as c:
        yield c


def _run_agent(client, body: dict, monkeypatch) -> str:
    """走标准 Agent Protocol 路径（假 LLM 驱动真实多 Agent 编排），返回业务 run_id。"""
    return run_agent(client, coordinator_script_for_form(body), body, monkeypatch)


def _pdf_pages(data: bytes) -> int:
    """不依赖第三方 PDF 库的页数统计：数 `/Type /Page` 对象（排除 `/Pages`）。"""
    return len(re.findall(rb"/Type\s*/Page[^s]", data))


def _download_filename(response) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r'filename="([^"]+)"', disposition)
    assert match, f"响应缺少规范下载文件名：{disposition!r}"
    return match.group(1)


# --------------------------------------------------------------------------- #
# download_name：纯函数单元测试
# --------------------------------------------------------------------------- #
def test_download_name_normal_case() -> None:
    from docking_agent.runs import download_name

    name = download_name("20260914-151116-3404", "report",
                         receptor="thrombin", molecules=8, ext="pdf")
    assert name == "dock_20260914-151116-3404_thrombin_8mols_report.pdf"


def test_download_name_sanitizes_cjk_space_and_slash() -> None:
    from docking_agent.runs import download_name

    name = download_name("R1", "data", receptor='凝血酶 / "Thrombin"\tX', molecules=3, ext="zip")
    assert re.fullmatch(r"dock_R1_[A-Za-z0-9._-]+_3mols_data\.zip", name), name
    for bad in (" ", "/", "\\", '"', "凝血"):
        assert bad not in name, f"文件名不应包含 {bad!r}：{name}"


def test_download_name_falls_back_when_receptor_unusable() -> None:
    from docking_agent.runs import download_name

    assert download_name("R1", "report", receptor="   ", molecules=1, ext="pdf") \
        == "dock_R1_receptor_1mols_report.pdf"
    # 截断到 24 字符：超长受体名不撑爆文件名
    long_name = download_name("R1", "report", receptor="A" * 60, molecules=1, ext="pdf")
    assert long_name == f"dock_R1_{'A' * 24}_1mols_report.pdf"


def test_download_name_missing_molecules_and_ext() -> None:
    from docking_agent.runs import download_name, download_prefix

    name = download_name("R1", "report", receptor="thrombin", ext="pdf")
    # 分子数未知时整段省略，不出现 namols 这种读不通的片段
    assert name == "dock_R1_thrombin_report.pdf"
    assert "namols" not in name and "mols" not in name
    assert download_prefix("R1", receptor="thrombin") == "dock_R1_thrombin"
    # ext 为空时不产生多余的点号
    assert download_name("R1", "report", receptor="thrombin") == "dock_R1_thrombin_report"


# --------------------------------------------------------------------------- #
# build_report_pdf：最小 result + 真实 PNG，离线可跑
# --------------------------------------------------------------------------- #
def test_build_report_pdf_is_real_pdf_with_embedded_chart(tmp_path) -> None:
    from docking_agent.reporting import build_report_pdf
    from docking_agent.reporting.charts import docking_bar_chart

    png = docking_bar_chart(
        [{"name": "乙醇", "smiles": "CCO", "affinity_kcal_mol": -3.2},
         {"name": "甲醇", "smiles": "CO", "affinity_kcal_mol": -2.1}],
        {"name": "阳性对照", "affinity_kcal_mol": -4.5})
    chart = tmp_path / "docking.png"
    chart.write_bytes(png)
    assert chart.stat().st_size > 1024, "图表 PNG 应真实非空"

    result = {
        "ranking": [{"name": "乙醇", "smiles": "CCO", "affinity_kcal_mol": -3.2,
                     "engine": "vina", "exhaustiveness": 1, "molecular_weight": 46.1,
                     "logP": -0.0, "tpsa": 20.2}],
        "molecules": [{"name": "乙醇", "smiles": "CCO"}],
        "positive_control": {"name": "阳性对照", "affinity_kcal_mol": -4.5},
        "task_spec": {"task_type": "screening", "decision": "run", "source": "rules"},
    }
    pdf = build_report_pdf(result, run_id="20260914-151116-3404", receptor_label="thrombin",
                           created_at="2026-09-14T15:11:16",
                           chart_paths={"docking_chart": chart})
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 5 * 1024, f"PDF 过小，可能没写入内容：{len(pdf)} 字节"
    assert _pdf_pages(pdf) >= 1


def test_tool_versions_cover_all_tools_with_unknown_fallback() -> None:
    """封面「工具版本」必须每个工具都有一行；取不到写「未知」而不是省略。"""
    from docking_agent.reporting.pdf import _tool_versions

    versions = {row[0]: row[1] for row in _tool_versions()}
    for tool in ("vina", "rdkit", "meeko", "p2rank", "python", "matplotlib"):
        assert tool in versions, f"工具版本缺少 {tool}"
        assert versions[tool], f"{tool} 版本不得为空"


def test_report_seed_line_falls_back_to_unknown() -> None:
    """没有 seed 字段时报告仍要写出这一行（写「未知」），保证复现信息不缺失。"""
    from docking_agent.reporting import build_markdown_report

    md = build_markdown_report(
        {"ranking": [{"name": "A", "smiles": "CCO", "affinity_kcal_mol": -5.0}]},
        run_id="R1")
    assert "随机种子" in md and "seed=未知" in md


def test_report_methods_limitations_and_failure_sections() -> None:
    """「方法与局限」「失败与跳过」两节必须有内容，且失败按原因分组、结论限定覆盖范围。"""
    from docking_agent.reporting import build_markdown_report

    result = {
        "molecules": [{"name": "A", "smiles": "CCO"}, {"name": "B", "smiles": "C1CC"},
                      {"name": "C", "smiles": "CCC"}],
        "ranking": [{"name": "A", "smiles": "CCO", "affinity_kcal_mol": -5.0,
                     "engine": "vina", "exhaustiveness": 1}],
        "positive_control": {"name": "PC", "affinity_kcal_mol": -4.0},
        "docking": {"receptors": [{
            "box_center": [1.0, 2.0, 3.0], "box_size": [22.0, 22.0, 22.0],
            "box_source": "实验位点（共晶配体质心）",
            "results": [
                {"name": "A", "smiles": "CCO", "affinity_kcal_mol": -5.0},
                {"name": "B", "smiles": "C1CC", "error": "配体化学解析失败：无法生成 3D 构象"},
                {"name": "C", "smiles": "CCC", "error": "docking 失败: 受体准备异常"},
            ]}]},
    }
    md = build_markdown_report(result, run_id="R1")
    assert "## 6. 方法与局限" in md
    assert "## 7. 失败与跳过" in md
    # 盒子效应实测引用（方法与局限的可比性依据）
    assert "1.34 kcal/mol" in md
    # 失败按原因分组，且明确结论覆盖范围
    assert "配体化学解析失败" in md and "对接失败" in md
    assert "只覆盖成功对接的分子" in md
    assert "66.7%" in md, "2/3 失败率应为 1 位小数的百分比"
    # 正文不出现裸地址
    assert "http://" not in md and "https://" not in md and "/files/" not in md


def test_ranking_csv_marks_status_and_appends_failures() -> None:
    """ranking.csv 必须带 status/error 列，并把失败行以 rank=FAIL 追加，避免「全成功」假象。"""
    from docking_agent.reporting import build_ranking_csv, collect_failures

    docking = {"receptors": [{"results": [
        {"name": "A", "smiles": "CCO", "affinity_kcal_mol": -5.0},
        {"name": "B", "smiles": "CCC", "error": "配体化学解析失败"},
    ]}]}
    csv_text = build_ranking_csv([{"name": "A", "smiles": "CCO", "affinity_kcal_mol": -5.0}],
                                 {}, failures=collect_failures(docking))
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    header = lines[0].split(",")
    assert "status" in header and "error" in header
    rows = [dict(zip(header, ln.split(","))) for ln in lines[1:]]
    assert any(r["status"] == "ok" for r in rows)
    assert any(r["rank"] == "FAIL" and r["status"] == "error" for r in rows)


def test_write_report_pdf_does_not_break_main_flow(tmp_path) -> None:
    """PDF 落盘失败时必须只记 warning、返回 False，而不是把主流程炸掉。"""
    from docking_agent.reporting.artifacts import write_report_pdf

    class BrokenRun:
        kind = "agent"
        id = "R1"
        dir = tmp_path
        data = {"created_at": "2026-09-14T15:11:16", "molecule_count": 1}

        def artifacts(self) -> list:
            return []

        def write_bytes(self, *args: Any, **kwargs: Any) -> None:
            raise OSError("磁盘已满")

    assert write_report_pdf(BrokenRun(), {"ranking": []}) is False


# --------------------------------------------------------------------------- #
# 真实 Agent 运行：PDF 产物 + 下载端点 + 规范文件名
# --------------------------------------------------------------------------- #
def test_agent_pdf_artifact_and_normalized_downloads(client, monkeypatch) -> None:
    run_id = _run_agent(client, {
        "receptor": "thrombin",
        "ligands_text": "乙醇:CCO,甲醇:CO",
        "positive_control": "NC(=N)c1ccccc1",
        "exhaustiveness": 1, "engine": "vina", "save_poses": True,
    }, monkeypatch)
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["run"]["status"] == "ok"
    assert detail["run"]["molecule_count"] == 2

    # ---- 产物清单里有真实 PDF ----
    artifact = next((a for a in detail["artifacts"] if a["name"] == "report_pdf"), None)
    assert artifact is not None, f"缺少 report_pdf 产物：{sorted(a['name'] for a in detail['artifacts'])}"
    assert artifact["content_type"] == "application/pdf"

    from docking_agent.runs import get_run_store

    pdf_path = get_run_store().artifact_path(run_id, "report_pdf")
    assert pdf_path is not None and pdf_path.is_file()
    data = pdf_path.read_bytes()
    assert data[:5] == b"%PDF-", "report.pdf 必须是真 PDF"
    assert len(data) > 5 * 1024, f"PDF 过小：{len(data)} 字节"
    assert _pdf_pages(data) >= 1

    # ---- downloads 字段（前端唯一事实源）----
    downloads = detail["downloads"]
    assert downloads["report_pdf"].startswith("dock_")
    assert downloads["report_pdf"].endswith("_report.pdf")
    assert downloads["prefix"].startswith("dock_")
    assert "thrombin" in downloads["prefix"] and "2mols" in downloads["prefix"]
    assert downloads["report_pdf"] == f"{downloads['prefix']}_report.pdf"

    # ---- /report.pdf 端点 ----
    resp = client.get(f"/api/runs/{run_id}/report.pdf")
    assert resp.status_code == 200, resp.text[:200]
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content[:5] == b"%PDF-"
    assert _pdf_pages(resp.content) >= 1
    pdf_name = _download_filename(resp)
    assert re.fullmatch(rf"dock_{re.escape(run_id)}_[A-Za-z0-9._-]+_2mols_report\.pdf", pdf_name), pdf_name
    assert pdf_name == downloads["report_pdf"]

    # ---- 其它下载端点也走同一套规范名 ----
    csv_resp = client.get(f"/api/runs/{run_id}/export.csv")
    assert csv_resp.status_code == 200
    csv_name = _download_filename(csv_resp)
    assert re.fullmatch(rf"dock_{re.escape(run_id)}_[A-Za-z0-9._-]+_2mols_ranking\.csv", csv_name), csv_name
    assert csv_name == downloads["ranking_csv"]

    # ---- 可复现信息：CSV 表头含 seed / seed_policy，报告正文也写明 ----
    csv_lines = [ln for ln in csv_resp.text.splitlines() if ln.strip()]
    header = csv_lines[0].split(",")
    assert "seed" in header and "seed_policy" in header, header
    first_row = dict(zip(header, csv_lines[1].split(",")))
    assert first_row["seed"] not in ("", None), f"结果行应带 seed：{csv_lines[1]}"
    assert first_row["seed_policy"], f"结果行应带 seed_policy：{csv_lines[1]}"
    assert "随机种子" in detail["report_markdown"]
    assert "seed_policy=" in detail["report_markdown"]

    # 单个产物下载（清单里的 download_url）同样用规范名
    artifact_resp = client.get(artifact["download_url"])
    assert artifact_resp.status_code == 200
    assert _download_filename(artifact_resp) == downloads["report_pdf"]

    zip_resp = client.get(f"/api/runs/{run_id}/download.zip")
    assert zip_resp.status_code == 200 and zip_resp.content[:2] == b"PK"
    zip_name = _download_filename(zip_resp)
    assert re.fullmatch(rf"dock_{re.escape(run_id)}_[A-Za-z0-9._-]+_2mols_data\.zip", zip_name), zip_name
    assert zip_name == downloads["data_zip"]

    poses_resp = client.get(f"/api/runs/{run_id}/poses.zip")
    poses_dir = get_run_store().root / run_id / "poses"
    if poses_resp.status_code == 200:
        assert poses_resp.content[:2] == b"PK"
        poses_name = _download_filename(poses_resp)
        assert re.fullmatch(rf"dock_{re.escape(run_id)}_[A-Za-z0-9._-]+_2mols_poses\.zip", poses_name), poses_name
        assert poses_name == downloads["poses_zip"]
    else:
        # 没开启保存位姿时，端点必须明确 404，而不是返回一个假的 zip
        assert not poses_dir.is_dir()
        assert poses_resp.status_code == 404


def test_report_pdf_endpoint_404_for_unknown_run(client) -> None:
    resp = client.get("/api/runs/20990101-000000-0000/report.pdf")
    assert resp.status_code == 404


def test_report_pdf_endpoint_generates_for_run_without_pdf_artifact(client) -> None:
    """已有 report.md 但没有 report_pdf 产物（如旧运行）时，端点应现场生成而不是 404。"""
    from docking_agent.runs import download_names, get_run_store

    store = get_run_store()
    run = store.new("agent", {"receptor": "thrombin"})
    run.write_text("report.md",
                   "# 分子对接筛选报告\n\n## 1. 任务与参数\n\n- 现场生成 PDF 的最小运行\n",
                   name="report_md", label="分析报告（Markdown）",
                   content_type="text/markdown; charset=utf-8")
    run.set(receptor_label="thrombin", molecule_count=0)
    run.finish("ok")
    assert store.artifact_path(run.id, "report_pdf") is None, "本用例要求初始没有 PDF 产物"

    resp = client.get(f"/api/runs/{run.id}/report.pdf")
    assert resp.status_code == 200, resp.text[:200]
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content[:5] == b"%PDF-"
    assert _pdf_pages(resp.content) >= 1
    assert _download_filename(resp) == download_names(run.id, store.meta(run.id))["report_pdf"]
