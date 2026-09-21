#!/usr/bin/env python
"""上传 `.ent` 受体 → 对话模式确实使用上传受体的端到端取证。

背景（2026-09-16 用户报告的真实故障）：用户在对话模式上传 `pdb2gs3.ent`（GPX4 / RCSB 2GS3）
后写了「使用上传的蛋白质，用示例库进行筛选」，但：
  1. 前端 `payloadToBody` 在 `advanced=false` 时把 `receptor_file` 从请求体里丢掉；
  2. 即使路径到了后端，`resolve_receptor_specs()` 只认 `.pdb`，`.ent` 会「未识别受体 →
     回退默认 凝血酶(thrombin)」。
两条都会让「用户上传的蛋白质」根本没被用上，而日志还显示「受体 thrombin」。

本脚本用**真实服务**取证三条：

  C1  HTTP 全链路：`POST /api/uploads`（kind=receptor，真实 `.ent`）→ 得到准备好的 PDBQT →
      `POST /api/agent/stream`（mode=chat, advanced=false, receptor_file=该 PDBQT）→
      断言 `request.receptor_file`、`docking.json` 里最终使用的 `pdbqt` 都是上传产物，
      且 notes 里**没有**「未指定受体 → 默认凝血酶」。
  C2  真实浏览器（Playwright）：在对话页选文件上传 `.ent`（打真实上传端点）→ 点发送 →
      抓取浏览器**实际发出的请求体**，断言含 `receptor_file` 且等于上传产物。
      （为不重复烧一次多 Agent 运行，仅对 `/api/agent/stream` 的**响应**做桩；
        请求体本身是页面真实构造并真实发出的。）
  C3  回归：不带附件、不指定受体 → 仍走默认凝血酶，并在 notes 里明确写「未指定受体 ...」。

用法：
    export PLAYWRIGHT_BROWSERS_PATH=$PWD/var/cache/ms-playwright
    .venv/bin/python scripts/verify_receptor_upload_e2e.py http://127.0.0.1:5106
    .venv/bin/python scripts/verify_receptor_upload_e2e.py --only c2
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

# 用户报告里的那个文件（真实 .ent：GPX4 / RCSB 2GS3）
USER_ENT = PROJECT_ROOT / "assets" / "uploads" / "20260916-143529-cbf16a-pdb2gs3.ent"
FALLBACK_ENT = PROJECT_ROOT / "assets" / "receptors" / "structures" / "thrombin.pdb"

RESULTS: List[Tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  →  {detail}" if detail else ""))
    return bool(ok)


def _ent_path() -> Path:
    return USER_ENT if USER_ENT.is_file() else FALLBACK_ENT


def _upload_path(workdir: Path) -> Path:
    """把用户报告里的 .ent 复制成原名 `pdb2gs3.ent` 再上传，完全复刻用户的文件名。"""
    dest = workdir / "pdb2gs3.ent"
    if not dest.is_file():
        dest.write_bytes(_ent_path().read_bytes())
    return dest


# --------------------------------------------------------------------------- #
# C1：HTTP 全链路（真实上传 + 真实多 Agent 运行）
# --------------------------------------------------------------------------- #
def _sse_events(response: Any) -> Any:
    for raw in response.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data: "):
            continue
        try:
            yield json.loads(raw[len("data: "):])
        except json.JSONDecodeError:
            continue


def verify_c1(base: str, timeout_sec: int = 3600) -> Optional[str]:
    import requests

    workdir = PROJECT_ROOT / "var" / "verify"
    workdir.mkdir(parents=True, exist_ok=True)
    ent = _upload_path(workdir)
    print(f"\n--- C1 HTTP 全链路（真实运行；受体源文件 {ent.name}）---")
    with open(ent, "rb") as fh:
        up = requests.post(f"{base}/api/uploads",
                           files={"file": (ent.name, fh, "chemical/x-pdb")},
                           data={"kind": "receptor"}, timeout=900)
    check(up.status_code == 200, "POST /api/uploads(kind=receptor) 成功", f"HTTP {up.status_code}")
    if up.status_code != 200:
        check(False, "上传返回体", up.text[:300])
        return None
    uploaded: Dict[str, Any] = up.json()
    prepared = str(uploaded.get("receptor_file") or "")
    check(bool(prepared) and Path(prepared).suffix == ".pdbqt" and Path(prepared).is_file(),
          "上传 .ent 后返回可直接对接的 PDBQT", prepared)
    print(f"         box_center={uploaded.get('box_center')} site_source={uploaded.get('site_source')}")

    message = ("使用上传的蛋白质，用示例库进行筛选\n\n"
               "引用文件（本次对话已上传，可直接作为工具输入）：\n"
               f"- {ent.name}（受体）→ {uploaded.get('path')}")
    payload = {"mode": "chat", "advanced": False, "message": message,
               "conversation_id": "ent-1", "receptor_file": prepared}
    print(f"         请求体 receptor_file={prepared}")
    run_id: Optional[str] = None
    start_request: Dict[str, Any] = {}
    notes: List[str] = []
    t0 = time.time()
    with requests.post(f"{base}/api/agent/stream", json=payload, stream=True,
                       timeout=(30, timeout_sec)) as resp:
        check(resp.status_code == 200, "POST /api/agent/stream 已接受", f"HTTP {resp.status_code}")
        for event in _sse_events(resp):
            etype = event.get("type")
            if etype == "start":
                run_id = event.get("run_id")
                start_request = event.get("request") or {}
            elif etype == "tool_result":
                data = event.get("data")
                if isinstance(data, dict) and data.get("notes"):
                    notes = list(data["notes"])
            elif etype == "error":
                check(False, "运行过程中出现 error 事件", json.dumps(event, ensure_ascii=False)[:200])
            elif etype == "done":
                break
    elapsed = time.time() - t0
    check(bool(run_id), f"拿到 run_id（耗时 {elapsed:.0f}s）", str(run_id))
    if not run_id:
        return None

    check(start_request.get("receptor_file") == prepared,
          "run.data.request.receptor_file == 上传准备的 PDBQT",
          f"{start_request.get('receptor_file')!r}")

    run_dir = PROJECT_ROOT / "var" / "runs" / run_id
    req_file = run_dir / "request.json"
    docking_file = run_dir / "docking.json"
    check(req_file.is_file() and docking_file.is_file(),
          "运行目录存在 request.json / docking.json", str(run_dir))
    if not (req_file.is_file() and docking_file.is_file()):
        return run_id

    request_json = json.loads(req_file.read_text(encoding="utf-8"))
    check(request_json.get("receptor_file") == prepared,
          "落盘的 request.json 携带 receptor_file", str(request_json.get("receptor_file")))

    docking = json.loads(docking_file.read_text(encoding="utf-8"))
    blocks = docking.get("receptors") or []
    check(bool(blocks), "docking.json 有受体结果块", f"{len(blocks)} 个")
    if not blocks:
        return run_id
    used = str(blocks[0].get("pdbqt") or "")
    check(Path(used).resolve() == Path(prepared).resolve(),
          "最终对接所用受体 = 上传准备的 PDBQT（不是注册表默认血栓素）",
          f"used={used}")
    check("thrombin_1DWC" not in used,
          "最终所用受体不是注册表 thrombin_1DWC.pdbqt", used)

    all_notes = list(docking.get("notes") or [])
    joined = " ".join(all_notes)
    check("未指定受体" not in joined and "默认" not in joined,
          "notes 里没有任何「未指定受体 → 回退默认」说明", joined[:160])
    check(any("用户上传受体" in n for n in all_notes),
          "notes 明确记录「用户上传受体已准备/已就绪」",
          next((n for n in all_notes if "用户上传受体" in n), "")[:120])
    check(bool(blocks[0].get("results")), "确实完成了对接计算",
          f"{len(blocks[0].get('results') or [])} 个分子")
    return run_id


# --------------------------------------------------------------------------- #
# C2：真实浏览器抓取实际请求体（Playwright）
# --------------------------------------------------------------------------- #
def verify_c2(base: str) -> None:
    from playwright.sync_api import sync_playwright

    workdir = PROJECT_ROOT / "var" / "verify"
    workdir.mkdir(parents=True, exist_ok=True)
    ent = _upload_path(workdir)
    print(f"\n--- C2 Playwright 真实浏览器：抓取实际发出的请求体（{ent.name}）---")
    captured: List[Dict[str, Any]] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.on("request", lambda req: _capture_request(req, captured))
        # 只桩住「响应」，请求体仍是页面真实构造并真实发出的；
        # 这样 C2 不必再烧一次多 Agent 运行（真实服务链路已由 C1 覆盖）。
        page.route("**/api/agent/stream", lambda route: route.fulfill(
            status=200, content_type="text/event-stream",
            body="data: {\"type\":\"start\",\"run_id\":\"C2\"}\n\n"
                 "data: {\"type\":\"final\",\"content\":\"ok\"}\n\n"
                 "data: {\"type\":\"done\",\"run_id\":\"C2\",\"summary\":{\"status\":\"ok\"}}\n\n"))
        page.goto(f"{base}/#chat", wait_until="domcontentloaded")
        page.wait_for_function("window.__dshReady === true", timeout=60000)
        page.set_input_files("#chat-file-input", str(ent))
        page.wait_for_selector(".chat-attachment", timeout=120000)
        chip_title = page.get_attribute(".chat-attachment", "title") or ""
        check("已准备为 PDBQT" in chip_title, "界面提示写明已准备为 PDBQT", chip_title[:110])
        uploaded_pdbqt = page.evaluate(
            "() => (state.attachments.find(a => a.kind === 'receptor') || {}).receptorFile || ''")
        check(bool(uploaded_pdbqt) and str(uploaded_pdbqt).endswith(".pdbqt"),
              "浏览器里附件记录到上传产物 PDBQT 路径", str(uploaded_pdbqt))
        page.fill("#chat-input", "使用上传的蛋白质，用示例库进行筛选 @pdb2gs3.ent")
        page.click("#chat-send")
        for _ in range(120):
            if captured:
                break
            page.wait_for_timeout(500)
        browser.close()

    check(bool(captured), "捕获到发往 /api/agent/stream 的请求体", f"{len(captured)} 次")
    if not captured:
        return
    body = captured[0]
    print("         实际请求体关键字段：")
    print("           " + json.dumps({k: body.get(k) for k in
                                     ("mode", "advanced", "conversation_id", "receptor_file",
                                      "molecule_file") if k in body}, ensure_ascii=False))
    check(body.get("receptor_file") == uploaded_pdbqt,
          "请求体 receptor_file == 上传产物 PDBQT", str(body.get("receptor_file")))
    check(body.get("advanced") is False, "请求体 advanced=false（高级设置未展开）",
          str(body.get("advanced")))
    extra = [k for k in body if k not in ("mode", "message", "advanced", "conversation_id",
                                          "receptor_file", "molecule_file")]
    check(not extra, "除附件字段外不注入任何参数", ",".join(extra) or "（无）")
    check(ent.name in str(body.get("message") or ""), "指令里带「引用文件」清单（含原始 .ent）",
          [ln for ln in str(body.get("message") or "").splitlines() if "→" in ln][:1])


def _capture_request(req: Any, sink: List[Dict[str, Any]]) -> None:
    """Playwright request 事件回调：只关心发往 agent/stream 的那次。"""
    if "/api/agent/stream" not in req.url:
        return
    try:
        sink.append(json.loads(req.post_data or "{}"))
    except json.JSONDecodeError:
        sink.append({"raw": req.post_data})


# --------------------------------------------------------------------------- #
# C3：回归——不带附件、未指定受体仍走默认血栓素，且明确标注
# --------------------------------------------------------------------------- #
def verify_c3(base: str) -> None:
    import requests

    print("\n--- C3 回归：未指定受体 → 默认凝血酶，且 notes 明确标注 ---")
    # ① 确定性流水线（真实对接，1 个分子）：receptor 显式留空 = 未指定
    r = requests.post(f"{base}/api/pipeline/stream", json={
        "receptor": "", "ligands_text": "乙醇:CCO", "positive_control": "",
        "exhaustiveness": 1, "engine": "vina", "save_poses": False}, stream=True, timeout=900)
    run_id = None
    for event in _sse_events(r) if r.status_code == 200 else []:
        if event.get("type") == "start":
            run_id = event.get("run_id")
        if event.get("type") == "done":
            break
    check(bool(run_id), "流水线运行完成（未指定受体）", str(run_id))
    if run_id:
        docking = json.loads((PROJECT_ROOT / "var" / "runs" / run_id / "docking.json")
                             .read_text(encoding="utf-8"))
        joined = " ".join(docking.get("notes") or [])
        check("未指定受体" in joined and "thrombin" in joined,
              "notes 明确标注「未指定受体，已默认使用 凝血酶(thrombin, 1DWC)」",
              next((n for n in docking.get("notes") or [] if "未指定受体" in n), "")[:120])
        block = (docking.get("receptors") or [{}])[0]
        check("thrombin" in str(block.get("receptor_key") or ""),
              "回退到的确实是注册表默认受体", str(block.get("receptor_key")))

    # ② 受理层（chat，无附件、未点名受体）必须判成 default（前端据此如实打日志）
    from docking_agent import intake
    from docking_agent.api.schemas import AgentRequest

    spec = intake.build_task_spec(AgentRequest(mode="chat", advanced=False,
                                               message="使用示例库进行筛选", receptor="thrombin"))
    check(spec["receptor"]["source"] == "default" and spec["decision"] == "run",
          "受理层把「没指定受体」判成 source=default（而非 user）",
          json.dumps(spec["receptor"], ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="上传 .ent 受体端到端取证")
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:5106")
    parser.add_argument("--only", default="c1,c2,c3",
                        help="只做其中几项：c1,c2,c3（逗号分隔）")
    parser.add_argument("--timeout", type=int, default=3600, help="C1 真实运行的最长等待秒数")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    only = {x.strip().lower() for x in args.only.split(",") if x.strip()}

    print("=" * 74)
    print("上传 .ent 受体 → 对话模式确实使用上传受体 · 端到端取证")
    print(f"服务：{base}    受体源文件：{_ent_path()}")
    print("=" * 74)
    if "c1" in only:
        verify_c1(base, args.timeout)
    if "c2" in only:
        verify_c2(base)
    if "c3" in only:
        verify_c3(base)

    passed = sum(1 for ok, _l, _d in RESULTS if ok)
    print("\n" + "=" * 74)
    print(f"结果：{passed}/{len(RESULTS)} 通过")
    for ok, label, detail in RESULTS:
        if not ok:
            print(f"  [FAIL] {label}  →  {detail}")
    print("=" * 74)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
