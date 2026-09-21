#!/usr/bin/env python
"""真浏览器（Playwright + Chromium）下的界面截图 / 视觉回归 / 布局体检。

与 `scripts/shot.py` 的分工：
  * `shot.py`  —— 无需额外依赖，抓**桌面上已打开**的窗口（人肉挡不住时用它）；
  * 本脚本    —— 用可编程浏览器**自己打开页面**：能等元素、能点、能滚、能多页批量、
                 还能做「改前/改后」像素对比。改前端时的主力工具。

首次准备（一次性，约 150 MB，装在 var/cache/ms-playwright，已 gitignore）：
    uv pip install playwright
    PLAYWRIGHT_BROWSERS_PATH=$PWD/var/cache/ms-playwright .venv/bin/python -m playwright install chromium
（本脚本会自动把 PLAYWRIGHT_BROWSERS_PATH 指到 var/cache/ms-playwright，无需每次设置。）

用法：
    # 1) 单张截图（可等元素、可点击、可滚动）
    python scripts/ui_shot.py shot --url http://127.0.0.1:5095/#/settings --out var/tmp/shot/settings.png
    python scripts/ui_shot.py shot --page settings --scale 1 --full-page

    # 2) 整页体检：控制台错误 / 横向溢出 / 图片加载失败（真实浏览器，不是静态扫描）
    python scripts/ui_shot.py check --base http://127.0.0.1:5095

    # 3) 视觉回归：先存基线，改完前端再比对
    python scripts/ui_shot.py baseline --base http://127.0.0.1:5095
    python scripts/ui_shot.py diff     --base http://127.0.0.1:5095     # 只比对，不改基线
    python scripts/ui_shot.py diff     --update                          # 确认无误后更新基线

输出目录：`var/tmp/ui_shots/`（截图）、`var/tmp/ui_baseline/`（基线）、
`var/tmp/ui_diff/`（差异图：左右并排 + 红色高亮差异像素）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BROWSERS_PATH = PROJECT_ROOT / "var" / "cache" / "ms-playwright"
SHOT_DIR = PROJECT_ROOT / "var" / "tmp" / "ui_shots"
BASELINE_DIR = PROJECT_ROOT / "var" / "tmp" / "ui_baseline"
DIFF_DIR = PROJECT_ROOT / "var" / "tmp" / "ui_diff"

# 需要覆盖的页面集合（改前端时按这套对基线）。
#   hash  —— 顶层视图 `#/settings`、工作台子页 `#chat` / `#manual`（见 web/app.js 的 PAGE_HASH）
#   steps —— 需要点击/等待才能到达的状态（历史列表与运行详情都是工作台内的「标签页」而非路由）
PAGES: list[dict] = [
    {"name": "chat", "hash": "#chat", "desc": "工作台·对话模式（默认视图）"},
    {"name": "manual", "hash": "#manual", "desc": "工作台·参数模式表单"},
    {"name": "history", "hash": "#chat", "desc": "工作台·历史运行列表（结果面板标签页）",
     "steps": [{"click": '[data-tab="history"]'}, {"wait": "#history-tbody tr"}]},
    {"name": "run-detail", "hash": "#chat", "desc": "工作台·某次运行的结果卡片/图表/产物",
     "steps": [{"click": '[data-tab="history"]'}, {"wait": "#history-tbody tr"},
               {"click": "#history-tbody tr"}, {"wait": "#run-summary"}]},
    {"name": "settings", "hash": "#/settings", "desc": "设置页（schema 驱动表单）"},
    {"name": "chat-narrow", "hash": "#chat", "desc": "窄屏（响应式布局）",
     "width": 520, "height": 900},
]



# 视觉回归要的是「布局与样式变化」，不能因为时钟/运行数据变化就报警。
# 这些容器里的内容会随时间和运行记录变化，截图前统一隐藏（visibility 保留占位）。
VOLATILE_SELECTORS = [
    "#run-status", "#run-meta", "#live-progress", "#progress-bar-fill",
    "#log-stream", "#history-tbody", "#history-table", "#history-hint",
    "#molecule-tbody", "#chat-history", "#result-summary", "#stage-log",
    "#elapsed", "#eta", "#run-summary", "#ranking-tbody",
]


def _freeze_dynamic(page) -> int:
    """隐藏随时间/数据变化的节点，返回实际隐藏的数量。"""
    return int(page.evaluate(
        """(sels) => {
            let n = 0;
            for (const s of sels) {
                for (const el of document.querySelectorAll(s)) {
                    el.style.visibility = 'hidden';
                    n++;
                }
            }
            return n;
        }""",
        VOLATILE_SELECTORS,
    ))

def _run_steps(page, spec: dict, args) -> None:
    """执行某页面的到达步骤（点击 / 等待 / 输入 / 固定延时）。"""
    for step in spec.get("steps") or []:
        if "click" in step:
            page.click(step["click"], timeout=args.timeout)
        if "wait" in step:
            page.wait_for_selector(step["wait"], timeout=args.timeout)
        if "fill" in step:
            sel, _, value = step["fill"].partition("=")
            page.fill(sel, value)
        if "ms" in step:
            page.wait_for_timeout(int(step["ms"]))


def _ensure_browser_path() -> None:
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BROWSERS_PATH))


def _require_playwright():
    _ensure_browser_path()
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        print("[ui] 未安装 playwright（uv pip install playwright），或浏览器未下载："
              f"{e}\n    提示：PLAYWRIGHT_BROWSERS_PATH={BROWSERS_PATH} "
              ".venv/bin/python -m playwright install chromium", file=sys.stderr)
        raise SystemExit(2)
    return sync_playwright


def _latest_run_id(base: str) -> str:
    """取一个真实运行 id，用于截图「结果页」（没有运行就返回空串，页面会走空态）。"""
    try:
        with urlopen(f"{base}/api/runs?limit=1", timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        runs = data.get("runs") or data.get("items") or []
        if runs:
            return str(runs[0].get("run_id") or runs[0].get("id") or "")
    except Exception as e:  # noqa: BLE001
        print(f"[ui] 取运行 id 失败（结果页将显示空态）：{e}", file=sys.stderr)
    return ""


def _url_for(base: str, page: dict, run_id: str) -> str:
    return base.rstrip("/") + "/" + page["hash"].format(run_id=run_id or "none")


# --------------------------------------------------------------------------- #
# 体检：控制台错误 / 横向溢出 / 图片加载失败
# --------------------------------------------------------------------------- #
def _probe(page) -> dict:
    return page.evaluate(
        """() => {
            const de = document.documentElement;
            const imgs = Array.from(document.images || []);
            const broken = imgs.filter(i => i.complete && i.naturalWidth === 0)
                                .map(i => (i.currentSrc || i.src || '').slice(-80));
            // 找出「明显超出视口右边界」的元素（横向溢出的元凶，便于定位）
            const overflowing = [];
            for (const el of document.querySelectorAll('body *')) {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.right > window.innerWidth + 2 &&
                    getComputedStyle(el).position !== 'fixed') {
                    overflowing.push((el.tagName.toLowerCase() +
                        (el.id ? '#' + el.id : '') +
                        (el.className && typeof el.className === 'string'
                          ? '.' + el.className.split(/\\s+/)[0] : '')).slice(0, 60));
                    if (overflowing.length >= 5) break;
                }
            }
            return {
                title: document.title,
                viewport: [window.innerWidth, window.innerHeight],
                scrollWidth: de.scrollWidth,
                scrollHeight: de.scrollHeight,
                horizontalOverflow: de.scrollWidth > window.innerWidth + 2,
                brokenImages: broken,
                overflowing,
                visibleText: (document.body.innerText || '').slice(0, 200),
            };
        }"""
    )


def cmd_check(args) -> int:
    sync_playwright = _require_playwright()
    run_id = _latest_run_id(args.base)
    out_dir = SHOT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    rows: list[tuple[str, str, str]] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=(args.launch_args or []))
        for spec in PAGES:
            width = spec.get("width", args.width)
            height = spec.get("height", args.height)
            page = browser.new_page(viewport={"width": width, "height": height},
                                    device_scale_factor=args.scale)
            errors: list[str] = []
            page.on("console", lambda m: errors.append(f"{m.type}: {m.text[:160]}")
                    if m.type in ("error",) else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {str(e)[:160]}"))
            url = _url_for(args.base, spec, run_id)
            page.goto(url, wait_until="networkidle", timeout=args.timeout)
            _run_steps(page, spec, args)
            page.wait_for_timeout(args.settle)
            if getattr(args, "freeze", False):
                _freeze_dynamic(page)
                page.wait_for_timeout(150)
            info = _probe(page)
            shot = out_dir / f"check-{spec['name']}.png"
            page.screenshot(path=str(shot), full_page=False)
            page.close()
            status = "OK"
            if info["horizontalOverflow"]:
                status = "横向溢出"
                problems.append(f"{spec['name']}: 横向溢出 {info['scrollWidth']}px > "
                                f"视口 {info['viewport'][0]}px；疑似：{info['overflowing']}")
            if info["brokenImages"]:
                status = "图片失败"
                problems.append(f"{spec['name']}: {len(info['brokenImages'])} 张图片加载失败 "
                                f"{info['brokenImages'][:2]}")
            if errors:
                status = "控制台错误"
                problems.append(f"{spec['name']}: 控制台 {len(errors)} 条 —— {errors[:2]}")
            rows.append((spec["name"], status, f"{info['title'][:28]} | "
                                               f"{info['viewport'][0]}x{info['viewport'][1]}"))

    print("=" * 88)
    print("真实浏览器界面体检（Playwright + Chromium）")
    print("=" * 88)
    for name, status, extra in rows:
        mark = "PASS" if status == "OK" else "FAIL"
        print(f"  [{mark}] {name:<12} {extra}")
    print("-" * 88)
    for p in problems:
        print("  ! " + p)
    print(f"结果：{len(rows) - len(problems)}/{len(rows)} 页通过（截图在 {out_dir}）")
    return 1 if problems else 0


# --------------------------------------------------------------------------- #
# 截图
# --------------------------------------------------------------------------- #
def cmd_shot(args) -> int:
    sync_playwright = _require_playwright()
    spec = next((p for p in PAGES if p["name"] == args.page), None)
    if args.page and spec is None:
        print(f"未知页面 {args.page}；可选：{[p['name'] for p in PAGES]}", file=sys.stderr)
        return 2
    url = args.url or _url_for(args.base, spec or PAGES[0], _latest_run_id(args.base))
    out = Path(args.out) if args.out else SHOT_DIR / f"{args.page or 'shot'}.png"
    out = out if out.is_absolute() else PROJECT_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    width = args.width or (spec or {}).get("width") or 1440
    height = args.height or (spec or {}).get("height") or 1000

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=(args.launch_args or []))
        page = browser.new_page(viewport={"width": width, "height": height},
                                device_scale_factor=args.scale)
        console: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text[:160]}"))
        page.goto(url, wait_until="networkidle", timeout=args.timeout)
        if spec:
            _run_steps(page, spec, args)
        if args.freeze:
            hidden = _freeze_dynamic(page)
            print(f"[ui] 已冻结 {hidden} 个动态节点（--no-freeze 可关闭）")
        if args.wait_selector:
            page.wait_for_selector(args.wait_selector, timeout=args.timeout)
        for sel in args.click or []:
            page.click(sel, timeout=args.timeout)
        for item in args.fill or []:
            sel, _, value = item.partition("=")
            page.fill(sel, value)
        for sel in args.hover or []:
            page.hover(sel, timeout=args.timeout)
        if args.hover:
            # 留出气泡淡入 + 视口自适应定位的时间
            page.wait_for_timeout(400)
        if args.scroll:
            page.mouse.wheel(0, args.scroll)
        page.wait_for_timeout(args.settle)
        page.screenshot(path=str(out), full_page=args.full_page)
        info = _probe(page)
        browser.close()

    print(f"[ui] 截图：{out}")
    print(f"[ui] 标题：{info['title']}  视口：{info['viewport'][0]}x{info['viewport'][1]}"
          f"  页面高度：{info['scrollHeight']}px")
    if info["horizontalOverflow"]:
        print(f"[ui] ⚠ 横向溢出：{info['scrollWidth']}px > {info['viewport'][0]}px"
              f"，疑似 {info['overflowing']}")
    bad = [c for c in console if c.startswith("error")]
    if bad:
        print(f"[ui] ⚠ 控制台错误 {len(bad)} 条：{bad[:3]}")
    return 0


# --------------------------------------------------------------------------- #
# 视觉回归：baseline / diff
# --------------------------------------------------------------------------- #
def _capture_set(base: str, out_dir: Path, args) -> dict[str, Path]:
    sync_playwright = _require_playwright()
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = _latest_run_id(base)
    shots: dict[str, Path] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=(args.launch_args or []))
        for spec in PAGES:
            width = spec.get("width", args.width)
            height = spec.get("height", args.height)
            page = browser.new_page(viewport={"width": width, "height": height},
                                    device_scale_factor=1)
            page.goto(_url_for(base, spec, run_id), wait_until="networkidle",
                      timeout=args.timeout)
            _run_steps(page, spec, args)
            page.wait_for_timeout(args.settle)
            if getattr(args, "freeze", False):
                _freeze_dynamic(page)
                page.wait_for_timeout(150)
            p = out_dir / f"{spec['name']}.png"
            page.screenshot(path=str(p), full_page=False)
            page.close()
            shots[spec["name"]] = p
        browser.close()
    return shots


def _diff_images(a: Path, b: Path, out: Path, threshold: int = 24) -> tuple[float, int]:
    from PIL import Image, ImageChops
    ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    if ia.size != ib.size:
        ib = ib.resize(ia.size)
    diff = ImageChops.difference(ia, ib).convert("L").point(lambda v: 255 if v > threshold else 0)
    changed = sum(1 for px in diff.getdata() if px)
    total = ia.width * ia.height
    # 差异图：左原图 / 右新图 / 第三格红色高亮
    canvas = Image.new("RGB", (ia.width * 3 + 20, ia.height), (10, 12, 16))
    canvas.paste(ia, (0, 0))
    canvas.paste(ib, (ia.width + 10, 0))
    red = Image.new("RGB", ia.size, (255, 40, 40))
    canvas.paste(Image.composite(red, ib, diff), (ia.width * 2 + 20, 0))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    return (changed / total * 100.0), changed


def cmd_visual(args, update: bool) -> int:
    target_dir = BASELINE_DIR if update else SHOT_DIR / "current"
    shots = _capture_set(args.base, target_dir, args)
    print(f"[ui] 已抓取 {len(shots)} 页 → {target_dir}")
    if update:
        print("[ui] 基线已更新。下次改动后用 `diff` 比对。")
        return 0

    if not BASELINE_DIR.exists() or not any(BASELINE_DIR.glob("*.png")):
        print("[ui] 还没有基线：先运行 `baseline` 子命令", file=sys.stderr)
        return 2

    rows = []
    changed_pages = []
    for name, cur in shots.items():
        base = BASELINE_DIR / f"{name}.png"
        if not base.exists():
            rows.append((name, "新页面", 0.0))
            continue
        pct, px = _diff_images(base, cur, DIFF_DIR / f"{name}-diff.png")
        verdict = "一致" if pct < args.tolerance else "有差异"
        if verdict != "一致":
            changed_pages.append(name)
        rows.append((name, f"{verdict}（{pct:.3f}%，{px} px）", pct))

    print("=" * 88)
    print("视觉回归比对（基线 vs 当前；tolerance=%.3f%%）" % args.tolerance)
    print("=" * 88)
    for name, status, _ in rows:
        print(f"  {name:<12} {status}")
    print("-" * 88)
    if changed_pages:
        print(f"结果：{len(changed_pages)} 页与基线不同 —— 差异图（左=改前 中=改后 右=红色高亮）：")
        for n in changed_pages:
            print(f"    {DIFF_DIR / (n + '-diff.png')}")
        print("确认新样式无误后运行 `diff --update` 更新基线。")
        return 1
    print("结果：全部与基线一致 ✅")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="真浏览器界面截图 / 视觉回归 / 布局体检")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p) -> None:
        p.add_argument("--base", default="http://127.0.0.1:5095", help="应用地址")
        p.add_argument("--width", type=int, default=1440)
        p.add_argument("--height", type=int, default=1000)
        p.add_argument("--scale", type=float, default=1.0, help="devicePixelRatio（截图清晰度）")
        p.add_argument("--settle", type=int, default=1200, help="页面稳定后再截的等待毫秒")
        p.add_argument("--timeout", type=int, default=30000, help="单步超时毫秒")
        p.add_argument("--freeze", dest="freeze", action="store_true", default=True,
                       help="截图前隐藏随时间/数据变化的节点（默认开）")
        p.add_argument("--no-freeze", dest="freeze", action="store_false",
                       help="保留动态内容（用于人工查看真实页面）")
        p.add_argument("--launch-args", nargs="*", default=[],
                       help="透传给 Chromium 的启动参数（如 --no-sandbox）")

    p_shot = sub.add_parser("shot", help="截一张图")
    common(p_shot)
    p_shot.add_argument("--url", default="", help="直接给完整 URL（优先于 --page）")
    p_shot.add_argument("--page", default="", choices=[""] + [p["name"] for p in PAGES])
    p_shot.add_argument("--out", default="")
    p_shot.add_argument("--full-page", action="store_true")
    p_shot.add_argument("--wait-selector", default="")
    p_shot.add_argument("--click", action="append", default=[], help="点击选择器（可重复）")
    p_shot.add_argument("--fill", action="append", default=[], help='填表：selector=value（可重复）')
    p_shot.add_argument("--hover", action="append", default=[],
                        help="悬停选择器后再截图（可重复；用于拍工具提示气泡）")
    p_shot.add_argument("--scroll", type=int, default=0, help="向下滚动像素")

    p_check = sub.add_parser("check", help="整页体检：控制台/溢出/图片")
    common(p_check)

    p_base = sub.add_parser("baseline", help="抓一套基线（覆盖所有页面）")
    common(p_base)

    p_diff = sub.add_parser("diff", help="与基线比对（--update 更新基线）")
    common(p_diff)
    p_diff.add_argument("--update", action="store_true", help="用当前渲染更新基线")
    p_diff.add_argument("--tolerance", type=float, default=0.05,
                        help="可接受差异百分比（默认 0.05%%）")

    args = ap.parse_args()
    if args.cmd == "shot":
        return cmd_shot(args)
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "baseline":
        return cmd_visual(args, update=True)
    return cmd_visual(args, update=args.update)


if __name__ == "__main__":
    sys.exit(main())
