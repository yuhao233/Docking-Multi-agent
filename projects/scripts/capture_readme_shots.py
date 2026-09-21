#!/usr/bin/env python
"""抓取 README 用的界面截图（可复现：内容来自真实运行，样式来自真实前端）。

用法：
    .venv/bin/python scripts/capture_readme_shots.py --run-id 20260921-095530-0405
    .venv/bin/python scripts/capture_readme_shots.py --base http://127.0.0.1:5106 --out ../docs/images

四张图（写入 docs/images/，WebP 压缩，宽度统一 1400）：
    ui-chat.webp       对话模式：运行参数条 + 气泡（含参数小票）+ 运行详情
    ui-results.webp    结果总览：KPI + 排序推荐表（含分子 ID 列）
    ui-report.webp     报告：协调 Agent 的「0. 本次要求与响应」+ 分子的 2D 结构卡
    ui-artifacts.webp  中间数据：产物清单（报告 / 排序 CSV / 图表 / 位姿 / 受体结构）

对话那张用**桩数据**渲染（避免每次抓图都真跑一次 LLM），内容取自真实运行；
其余三张走真实运行的产物与接口，因此图里出现的数值都能在 `var/runs/<run_id>/` 里核对。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = PROJECT_ROOT.parent / "docs" / "images"
TARGET_WIDTH = 1400

#: 对话截图的内容：与真实运行 20260921-095530-0405（PGR137/PGR042 × thrombin）一致
CHAT_USER = "用上传的分子库对接 thrombin，报告里带上小分子的 ID。exhaustiveness=1、n_poses=1，直接执行。"
CHAT_REPLY = """## 筛选结论（thrombin · PGR 库）

按综合分（亲和力 × 配体效率 × 类药性 × 理化性质窗口）排序，**两个分子的等级均封顶为 C**：

| # | 分子 ID | 名称 | 亲和力 (kcal/mol) | 配体效率 LE | 等级 |
|:--|:--|:--|--:|--:|:--|
| 1 | PGR042 | 布洛芬 | -5.97 | 0.656 | C |
| 2 | PGR137 | 阿司匹林 | -5.73 | 0.626 | C |

**要点**：两者都结合到 S1 口袋，但亲和力弱于 -6.0 kcal/mol 门槛 → 建议作为弱结合参考，
或提高 exhaustiveness 复算；报告已按你的要求带上 `id` 列（覆盖 2/2）。"""


#: 抓图前的脱敏脚本：文档与仓库里不出现本机绝对路径与具体模型/端点名。
#: 只改**页面文本**（DOM 文本节点），不改任何真实数据与接口。
SANITIZE_JS = r"""
() => {
  const mask = (text) => String(text)
    .replace(/\/home\/[^\s'\")]+/g, '（运行目录）')
    .replace(/\/Users\/[^\s'\")]+/g, '（运行目录）')
    .replace(/api\.deepseek\.com|openai\.com|localhost:\d+|127\.0\.0\.1:\d+/g, '（LLM 端点）')
    .replace(/deepseek[-\w.]*/gi, '（模型）');
  const walk = (node) => {
    if (node.nodeType === 3) {
      const next = mask(node.nodeValue);
      if (next !== node.nodeValue) node.nodeValue = next;
      return;
    }
    if (node.nodeType !== 1) return;
    const tag = node.tagName.toLowerCase();
    if (['script', 'style', 'textarea'].indexOf(tag) >= 0) return;
    if (node.hasAttribute && node.hasAttribute('title')) {
      node.setAttribute('title', mask(node.getAttribute('title')));
    }
    Array.prototype.forEach.call(node.childNodes, walk);
  };
  walk(document.body);
}
"""


def _stub_events() -> List[Dict[str, Any]]:
    return [
        {"type": "start", "run_id": "SHOT-1",
         "task_spec": {"task_type": "screening", "authority": "chat+advanced"},
         "request": {"mode": "chat"}},
        {"type": "update", "node": "docking", "keys": ["messages"]},
        {"type": "final", "content": CHAT_REPLY},
        {"type": "done", "run_id": "SHOT-1", "summary": {"status": "ok", "molecule_count": 2}},
    ]


def _sse(events: List[Dict[str, Any]]) -> str:
    out = []
    for ev in events:
        kind = ev.get("type")
        if kind == "final":
            out.append("event: messages/complete\ndata: "
                       + json.dumps([{"type": "AIMessage", "content": ev["content"]}],
                                    ensure_ascii=False) + "\n\n")
        elif kind == "update":
            out.append("event: updates\ndata: " + json.dumps({ev["node"]: {"keys": ev["keys"]}}) + "\n\n")
        elif kind == "done":
            out.append("event: values\ndata: " + json.dumps(
                {"run_id": ev["run_id"], "summary": ev["summary"]}) + "\n\n")
        else:
            out.append("event: metadata\ndata: " + json.dumps(
                {"run_id": "run_shot", "thread_id": "shot"}) + "\n\n")
            out.append("event: custom\ndata: " + json.dumps(ev, ensure_ascii=False) + "\n\n")
    out.append("event: end\ndata: {}\n\n")
    return "".join(out)


def _compress(png: Path, webp: Path, *, width: int = TARGET_WIDTH) -> None:
    """统一宽度并转 WebP（仓库里不放 2 MB 级 PNG）。"""
    from PIL import Image

    with Image.open(png) as img:
        if img.width > width:
            height = round(img.height * width / img.width)
            img = img.resize((width, height), Image.LANCZOS)
        img.convert("RGB").save(webp, "WEBP", quality=84, method=5)
    png.unlink(missing_ok=True)


def capture(base: str, run_id: str, out_dir: Path, *, live_chat_run: str = "") -> List[Path]:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "_tmp"
    tmp.mkdir(exist_ok=True)
    written: List[Path] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1500, "height": 1000}, device_scale_factor=2,
                                locale="zh-CN")

        # ---------- 1) 对话模式（桩数据） ----------
        def handle_threads(route: Any) -> None:
            route.fulfill(status=200, headers={"Content-Type": "application/json"},
                          body=json.dumps({"thread_id": "shot-thread"}))

        def handle_stream(route: Any) -> None:
            route.fulfill(status=200, headers={"Content-Type": "text/event-stream"},
                          body=_sse(_stub_events()))

        def handle_run(route: Any) -> None:
            route.fulfill(status=200, headers={"Content-Type": "application/json"},
                          body=json.dumps({
                              "run": {"run_id": "SHOT-1", "status": "ok", "molecule_count": 2,
                                      "kind": "agent"},
                              "result": {"aggregates": {"total": 2}, "ranking_total": 2,
                                         "ranking": [{"rank": 1, "id": "PGR042", "name": "布洛芬",
                                                      "affinity_kcal_mol": -5.97}],
                                         "molecules": []},
                              "artifacts": [{"name": "report.md", "label": "分析报告（Markdown）"}],
                              "report_markdown": "# 分子对接筛选报告\n", "downloads": {},
                              "log": [], "collaboration": {}}, ensure_ascii=False))
        page.route("**/threads", handle_threads)
        page.route("**/runs/stream", handle_stream)
        page.route("**/api/runs/SHOT-1*", handle_run)

        page.goto(base + "/#chat", wait_until="domcontentloaded")
        page.wait_for_selector("#chat-input")
        page.fill("#chat-input", CHAT_USER)
        page.click("#chat-send")
        page.wait_for_function(
            "() => { const b = document.querySelector('.chat-msg.assistant .chat-body');"
            " return b && b.textContent.indexOf('筛选结论') >= 0; }", timeout=25000)
        page.wait_for_timeout(600)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
        png = tmp / "ui-chat.png"
        page.evaluate(SANITIZE_JS)
        page.wait_for_timeout(120)
        page.screenshot(path=str(png))
        _compress(png, out_dir / "ui-chat.webp")
        written.append(out_dir / "ui-chat.webp")

        # ---------- 2~4) 真实运行的结果 / 报告 / 中间数据 ----------
        def clear_routes() -> None:
            for pattern in ("**/threads", "**/runs/stream", "**/api/runs/SHOT-1*"):
                page.unroute(pattern)

        clear_routes()
        page.goto(base + "/#chat", wait_until="domcontentloaded")
        page.wait_for_selector("#chat-input")
        page.evaluate(f"loadRun('{run_id}', {{silent: true}})")
        page.wait_for_timeout(1800)

        # 结果总览（KPI + 排序表）
        page.evaluate("switchTab('overview')")
        page.wait_for_timeout(1200)
        page.evaluate("""() => {
            const t = document.querySelector('#run-kpis');
            if (t) t.scrollIntoView({block: 'start'});
        }""")
        page.wait_for_timeout(600)
        png = tmp / "ui-results.png"
        page.evaluate(SANITIZE_JS)
        page.wait_for_timeout(120)
        page.screenshot(path=str(png))
        _compress(png, out_dir / "ui-results.webp")
        written.append(out_dir / "ui-results.webp")

        # 报告：§0 要求与响应 + 结构卡
        page.evaluate("switchTab('report')")
        page.wait_for_timeout(1500)
        page.evaluate("""() => {
            const img = document.querySelector("#report-box img[src*='recommend_card']");
            if (img) img.scrollIntoView({block: 'center'});
        }""")
        page.wait_for_timeout(1500)
        png = tmp / "ui-report.png"
        page.evaluate(SANITIZE_JS)
        page.wait_for_timeout(120)
        page.screenshot(path=str(png))
        _compress(png, out_dir / "ui-report.webp")
        written.append(out_dir / "ui-report.webp")

        # 中间数据：产物清单
        page.evaluate("switchTab('artifacts')")
        page.wait_for_timeout(1200)
        page.evaluate("""() => {
            const t = document.querySelector('#artifact-grid') || document.querySelector('#tab-artifacts');
            if (t) t.scrollIntoView({block: 'start'});
        }""")
        page.wait_for_timeout(700)
        png = tmp / "ui-artifacts.png"
        page.evaluate(SANITIZE_JS)
        page.wait_for_timeout(120)
        page.screenshot(path=str(png))
        _compress(png, out_dir / "ui-artifacts.webp")
        written.append(out_dir / "ui-artifacts.webp")

        browser.close()
    tmp.rmdir()
    return written


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取 README 界面截图（WebP）")
    parser.add_argument("--base", default="http://127.0.0.1:5106")
    parser.add_argument("--run-id", required=True, help="用于结果/报告/产物截图的真实运行 id")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    for path in capture(args.base, args.run_id, Path(args.out)):
        print(f"[shot] 已生成 {path}（{path.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
