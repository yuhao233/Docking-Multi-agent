"""上传、上传后对接、固定报告格式与报告内嵌图片的测试。

包含真实对接（每个用例 1-3 个分子，exhaustiveness=1），耗时可控。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

LIGAND_FILE = PROJECT_ROOT / "assets" / "libraries" / "ligands_user.sdf"
RECEPTOR_FILE = PROJECT_ROOT / "assets" / "receptors" / "structures" / "thrombin.pdb"


@pytest.fixture(scope="module")
def client():
    from docking_agent.api.app import app

    with TestClient(app) as c:
        yield c


def _upload(client, path: Path, kind: str = "auto"):
    """上传（v0.22 起**只保存文件**，不解析、不准备）。"""
    with open(path, "rb") as fh:
        return client.post("/api/uploads", files={"file": (path.name, fh, "application/octet-stream")},
                           data={"kind": kind})


def _upload_ready(client, path: Path, kind: str = "auto"):
    """上传 + 用户主动「校验文件」→ 得到解析/准备结果（等价于旧的 /api/uploads 返回）。"""
    up = _upload(client, path, kind=kind)
    assert up.status_code == 200, up.text[:300]
    payload = up.json()
    assert payload.get("pending") is True, payload
    return client.post("/api/uploads/inspect",
                       json={"path": payload["path"], "kind": payload.get("kind") or kind})


def _run_pipeline(client, body: dict):
    r = client.post("/api/pipeline/stream", json=body)
    assert r.status_code == 200, r.text[:300]
    run_id = None
    for line in r.text.splitlines():
        if line.startswith("data: "):
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if event.get("type") == "start":
                run_id = event["run_id"]
    assert run_id, "未收到 start 事件"
    return run_id


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #
def test_upload_ligand_file_only_stores_until_user_starts(client) -> None:
    """用户要求：**不要一上传就开始处理文件**。

    上传只落盘并回 `path`（`pending=True`，不含 count/molecules）；
    解析发生在用户点「校验文件」（inspect）或真正开始运行时。
    """
    assert LIGAND_FILE.is_file(), f"缺少测试用分子文件：{LIGAND_FILE}"
    r = _upload(client, LIGAND_FILE)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["kind"] == "ligand" and body["pending"] is True
    assert Path(body["path"]).is_file(), "上传文件应真实落盘"
    assert "count" not in body and "molecules" not in body, "上传阶段不得解析"
    # 显式校验后才给出解析结果
    ready = client.post("/api/uploads/inspect", json={"path": body["path"], "kind": "ligand"}).json()
    assert ready["pending"] is False and ready["count"] >= 1 and ready["molecules"]


def test_upload_receptor_file_prepares_site_on_inspect(client) -> None:
    assert RECEPTOR_FILE.is_file()
    r = _upload_ready(client, RECEPTOR_FILE)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["kind"] == "receptor"
    assert Path(body["receptor_file"]).is_file(), "应生成 PDBQT"
    # 凝血酶 1DWC 的已知位点盒（来自共晶配体质心）
    assert abs(body["box_center"][0] - 31.5) < 1.0
    assert len(body["box_size"]) == 3
    # 受体质子化必须与配体同一目标 pH（v0.22）
    prot = body.get("receptor_protonation") or {}
    assert prot.get("policy") == "ph" and prot.get("ph") == 7.4, prot
    assert prot.get("applied") is True, prot
    assert prot.get("his_states"), prot


def test_upload_rejects_unparsable_file(client):
    bad = PROJECT_ROOT / "var" / "tmp" / "not_a_molecule.txt"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("这不是分子文件", encoding="utf-8")
    up = _upload(client, bad)
    assert up.status_code == 200 and up.json()["pending"] is True, "上传阶段不解析"
    r = client.post("/api/uploads/inspect", json={"path": up.json()["path"], "kind": "ligand"})
    assert r.status_code == 400
    assert "分子" in str(r.json().get("detail", ""))


# --------------------------------------------------------------------------- #
# 用上传的文件真实对接
# --------------------------------------------------------------------------- #
def test_docking_with_uploaded_files(client):
    lig = _upload(client, LIGAND_FILE).json()
    rec = _upload(client, RECEPTOR_FILE).json()
    # 运行时才准备：这里提交的是**原始上传路径**
    assert "receptor_file" not in rec and rec["pending"] is True

    run_id = _run_pipeline(client, {
        "receptor_file": rec["path"],
        "molecule_file": lig["path"],
        "positive_control": "NC(=N)c1ccccc1",
        "exhaustiveness": 1, "engine": "vina", "save_poses": False,
    })
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["run"]["status"] == "ok"

    blocks = (detail["result"].get("docking") or {}).get("receptors") or []
    assert blocks, "应有对接结果"
    # 关键：上传受体后必须仍用活性位点盒，而不是退化成全蛋白质心
    assert abs(blocks[0]["box_center"][0] - 31.5) < 1.0, \
        f"位点盒被错误地替换成蛋白质心：{blocks[0]['box_center']}"
    # 位点来源：仍以实验位点（共晶配体质心）为准，但会附带口袋工具的独立验证结论。
    # 括号里必须写清是哪个共晶配体（如「共晶配体(MIT)质心」），否则用户无法核对盒子依据。
    site_source = (blocks[0].get("site") or {}).get("source") or ""
    assert site_source.startswith("实验位点（共晶配体"), site_source
    assert "质心" in site_source, site_source
    assert "预测" in site_source and "Å" in site_source, site_source
    assert blocks[0].get("box_source") == site_source, "结果块要带上盒子溯源"
    assert detail["result"]["ranking"], "应产生排序结果"


# --------------------------------------------------------------------------- #
# 固定报告格式与内嵌图片
# --------------------------------------------------------------------------- #
def test_report_has_fixed_sections_and_embedded_images(client):
    run_id = _run_pipeline(client, {
        "receptor": "thrombin",
        "ligands_text": "乙醇:CCO,甲醇:CO",
        "positive_control": "NC(=N)c1ccccc1",
        "exhaustiveness": 1, "engine": "vina", "save_poses": False,
    })
    detail = client.get(f"/api/runs/{run_id}").json()
    md = detail["report_markdown"]

    # 章节顺序固定（表 1 抬头 + 9 个二级章节）
    expected = ["# 分子对接筛选报告", "## 1. 任务与参数", "## 2. 结果排序", "## 3. 推荐分子",
                "## 4. 理化性质", "## 5. 结合模式与阳性对照比较", "## 6. 方法与局限",
                "## 7. 失败与跳过", "## 8. 结论与建议", "## 9. 数据与产物"]
    positions = [md.find(h) for h in expected]
    assert all(p >= 0 for p in positions), f"缺少章节：{[h for h, p in zip(expected, positions) if p < 0]}"
    assert positions == sorted(positions), "章节顺序必须固定"

    # 图/表带编号与题注；图片用相对路径内嵌，正文不得出现裸 URL
    assert "**表 1 " in md and "**图 1 " in md
    assert "http://" not in md and "https://" not in md and "/files/" not in md
    images = re.findall(r"!\[([^\]]*)\]\((charts/[^)]+\.png)\)", md)
    assert len(images) >= 5, f"报告应内嵌至少 5 张相对路径图片，实际 {len(images)}"
    artifact_names = {a["name"] for a in detail["artifacts"]}
    from docking_agent.runs import get_run_store

    run_dir = get_run_store().root / run_id
    for alt, rel in images:
        name = rel.rsplit("/", 1)[-1][:-4]
        assert name in artifact_names, f"报告引用了不存在的产物 {name}（{alt}）"
        assert (run_dir / rel).is_file(), f"内嵌图片文件不存在：{rel}"
        assert (run_dir / rel).stat().st_size > 500, f"内嵌图片疑似空文件：{rel}"
        assert alt.startswith("图 "), f"图题应带编号：{alt}"


def test_report_without_positive_control_skips_control_sections(client):
    run_id = _run_pipeline(client, {
        "receptor": "thrombin", "ligands_text": "乙醇:CCO",
        "positive_control": "", "exhaustiveness": 1, "engine": "vina", "save_poses": False,
    })
    md = client.get(f"/api/runs/{run_id}").json()["report_markdown"]
    assert "## 5. 结合模式与阳性对照比较" in md
    assert "未提供阳性对照" in md
    # 无对照时不应出现结合模式对照图
    assert "binding_scatter" not in md


def test_report_takes_only_agent_conclusions_not_the_whole_narrative() -> None:
    """第 8 节只摘录协调 Agent 的**结论/建议**类小节，且标题降级、代码块不被改动。

    契约（v0.20）：过去把整份 Agent 报告贴进第 8 节 → 同一批数字在报告里出现两遍
    （用户明确要求"报告中不要重复内容"）。数据复读小节（参数摘要/分子列表/属性评估/
    可视化/对照数据/数据可用性）必须丢弃；结论类小节保留。
    """
    from docking_agent.reporting import build_markdown_report

    md = build_markdown_report(
        {"ranking": [{"name": "A", "smiles": "CCO", "affinity_kcal_mol": -5.0}],
         "molecules": [{"name": "A", "smiles": "CCO"}]},
        kind="agent", run_id="R1",
        agent_narrative=("## 分子对接筛选报告\n\n### 1. 系统与参数配置摘要\n内容\n"
                         "### 2. 筛选后分子列表及排序\n| 分子 | 分数 |\n| --- | --- |\n| A | -5 |\n"
                         "### 6. 优化建议\n\n1. 建议提高搜索强度复算。\n\n"
                         "```\n## 代码里的井号\n```"))

    # 固定章节仍为二级标题且顺序正确
    assert "## 1. 任务与参数" in md
    assert "## 8. 结论与建议" in md
    # 数据复读小节被丢弃（否则就是"报告里出现两遍"）
    assert "系统与参数配置摘要" not in md
    assert "筛选后分子列表及排序" not in md
    # 结论类小节保留，且模型标题被整体降一级（不与固定章节冲突）
    assert re.search(r"^###+ .*优化建议", md, re.M) and "建议提高搜索强度复算" in md
    assert not re.search(r"^## 优化建议", md, re.M), "模型标题必须降级，不能与固定章节同级"
    # 代码块内容不被改动、围栏不被切坏
    assert "## 代码里的井号" in md and md.count("```") % 2 == 0
