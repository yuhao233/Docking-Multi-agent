#!/usr/bin/env python
"""给前端「拍一张真实渲染的截图」—— 供人看，也供 AI 开发者在会话里读图核对视觉。

背景：本项目的界面校验长期只有 DOM 级（`scripts/ui_e2e.js` 用 jsdom），它能验证
「点了按钮会怎样」，验证不了「看起来对不对」。本脚本补上「真实渲染的像素」。

**重要前提（实测结论，别再重复踩）**：
  * 本沙箱内**无法自己拉起可见浏览器窗口**：`firefox --kiosk` 与普通 headful 启动都不会
    映射出窗口（等 60 s+ 也无窗口），因此"脚本自己开一个干净窗口再截图"这条路走不通；
  * `firefox --headless --screenshot` 可用，但它在 **load 事件**就抓图 —— 单页应用此刻还没渲染完，
    直接抓只会得到一片空背景（试过用 iframe + 慢资源把 load 推迟，实测迟迟不 settle）；
  * **可行路径**：桌面上已经开着的窗口可以精确抓取（`wmctrl` 定位矩形 + `ffmpeg x11grab` 抓该矩形），
    这就是本脚本默认的 window 模式 —— 实测可用，且只抓目标窗口、不涉及其它窗口。

用法（默认 window 模式：抓桌面上已打开的窗口）：

    # 你先把应用在浏览器里打开，然后：
    python scripts/shot.py                         # 默认按标题「分子对接」找窗口
    python scripts/shot.py --title DeepSeek        # 按别的标题找（如 DSH 自己的界面）
    python scripts/shot.py --mode screen           # 整屏（含其它窗口，仅在你同意时用）
    python scripts/shot.py --list                  # 只列出当前所有窗口，便于挑标题
    python scripts/shot.py --mode headless http://127.0.0.1:5095/   # 实验性：SPA 往往抓到空页面

输出默认 `var/tmp/shot/<时间戳>.png`，图底部带页脚（标题 / 时间 / 尺寸）；
打印出的绝对路径可直接交给 AI 会话的读图工具（`read_image`）查看。

更省事的替代方案（不需要本脚本）：自己截一张图（PrtSc 或 `gnome-screenshot -f x.png`），
把 PNG 放进工作区任意位置（如 `projects/var/tmp/shot/user.png`），把路径告诉我即可。
"""
from __future__ import annotations

import argparse
import http.server
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TITLE = "分子对接"

# --------------------------------------------------------------------------- #
# headless 兜底模式用：1×1 PNG + 一个故意慢的图片请求（把 load 事件推到应用渲染之后）
# --------------------------------------------------------------------------- #
_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)
# 包装页要点：
#   * 标题带**唯一标记**（SHOT-<token>）—— 沙箱内的 PID 与宿主 PID 不同名空间，
#     wmctrl 只能看到宿主 PID，所以必须靠窗口标题来认出「我们启动的那个窗口」；
#   * iframe 用 100vw/100vh 占满窗口，等价于直接打开应用；
#   * 慢图片把 load 事件推迟（headless 模式靠它，x11 模式用固定等待）。
_WRAPPER = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>html,body{{margin:0;padding:0;background:#0d1117;overflow:hidden}}
iframe{{display:block;width:100vw;height:100vh;border:0}}</style></head>
<body><iframe src="{target}"></iframe><img src="/slow?ms={wait_ms}" width="1" height="1" alt="">
</body></html>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    target = ""
    title = "shot harness"
    width = 1440
    height = 1000
    wait_ms = 5000

    def log_message(self, *args) -> None:  # noqa: D102
        pass

    def do_GET(self) -> None:  # noqa: N802, D102
        if self.path.startswith("/slow"):
            try:
                ms = int(self.path.split("ms=")[1].split("&")[0])
            except (IndexError, ValueError):
                ms = self.wait_ms
            time.sleep(max(0, ms) / 1000.0)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(_PNG_1PX)))
            self.end_headers()
            self.wfile.write(_PNG_1PX)
            return
        body = _WRAPPER.format(target=self.target, title=self.title,
                               width=self.width, height=self.height,
                               wait_ms=self.wait_ms).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# --------------------------------------------------------------------------- #
# X11 辅助
# --------------------------------------------------------------------------- #
def _screen_size(display: str) -> tuple[int, int]:
    out = subprocess.run(["xdpyinfo", "-display", display], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "dimensions:" in line:
            dims = line.split("dimensions:")[1].split()[0]
            w, h = dims.split("x")
            return int(w), int(h)
    return 1920, 1080


def _windows(display: str) -> list[dict]:
    env = {**os.environ, "DISPLAY": display}
    out = subprocess.run(["wmctrl", "-lpG"], capture_output=True, text=True, env=env).stdout
    wins = []
    for line in out.splitlines():
        # wmctrl -lpG 列：id  desktop  pid  x  y  w  h  host  title…
        f = line.split(None, 8)
        if len(f) < 9:
            continue
        try:
            wins.append({"id": f[0], "pid": int(f[2]), "x": int(f[3]), "y": int(f[4]),
                         "w": int(f[5]), "h": int(f[6]), "title": f[8]})
        except ValueError:
            continue
    return wins


def _find_window(display: str, pid: int | None, title: str | None) -> dict | None:
    for w in _windows(display):
        if pid is not None and w["pid"] == pid:
            return w
    if title:
        for w in _windows(display):
            if title in w["title"]:
                return w
    return None


def _grab(display: str, geom: tuple[int, int, int, int], out: Path) -> bool:
    x, y, w, h = geom
    env = {**os.environ, "DISPLAY": display}
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "x11grab",
           "-video_size", f"{w}x{h}", "-i", f"{display}.0+{x},{y}", "-frames:v", "1", "-y", str(out)]
    proc = subprocess.run(cmd, capture_output=True, env=env, timeout=60)
    if proc.returncode != 0 or not out.exists():
        print("[shot] ffmpeg 抓屏失败：" + (proc.stderr or b"").decode(errors="replace")[:300],
              file=sys.stderr)
        return False
    return True


# --------------------------------------------------------------------------- #
# 页脚说明
# --------------------------------------------------------------------------- #
def _annotate(path: Path, caption: str, scale: float) -> None:
    try:
        from PIL import Image, ImageDraw
    except Exception as e:  # noqa: BLE001
        print(f"[shot] 跳过页脚（Pillow 不可用：{e}）", file=sys.stderr)
        return
    img = Image.open(path).convert("RGB")
    bar = 26
    canvas = Image.new("RGB", (img.width, img.height + bar), (13, 17, 23))
    canvas.paste(img, (0, 0))
    ImageDraw.Draw(canvas).text((8, img.height + 6), caption, fill=(150, 160, 175))
    if scale and scale != 1.0:
        canvas = canvas.resize((max(1, int(canvas.width * scale)),
                                max(1, int(canvas.height * scale))))
    canvas.save(path)


def _out_path(raw: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(raw) if raw else PROJECT_ROOT / "var" / "tmp" / "shot" / f"{stamp}.png"
    out = out if out.is_absolute() else PROJECT_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


# --------------------------------------------------------------------------- #
# 三种模式
# --------------------------------------------------------------------------- #
def shot_window(args) -> int:
    """抓桌面上**已打开的**窗口：wmctrl 定位矩形 → ffmpeg 只抓该矩形。"""
    display = args.display
    out = _out_path(args.out)
    win = _find_window(display, None, args.title)
    if win is None:
        print(f"[shot] 没找到标题包含「{args.title}」的窗口。先看有哪些窗口：", file=sys.stderr)
        for w in _windows(display):
            print(f"   {w['id']}  {w['w']}x{w['h']}  {w['title'][:70]}", file=sys.stderr)
        return 1
    screen_w, screen_h = _screen_size(display)
    if not args.no_raise:
        # 先把这个窗口抬到最前：ffmpeg 抓的是**屏幕像素**，被别的窗口压住就会拍到别人的内容
        subprocess.run(["wmctrl", "-ia", win["id"]],
                       env={**os.environ, "DISPLAY": display}, capture_output=True)
        time.sleep(args.raise_wait / 1000.0)
        win = _find_window(display, None, args.title) or win
    x, y = max(0, win["x"]), max(0, win["y"])
    w, h = min(win["w"], screen_w - x), min(win["h"], screen_h - y)
    print(f"[shot] 抓窗口：{win['title'][:50]}  {w}x{h} @({x},{y})")
    if not _grab(display, (x, y, w, h), out):
        return 1
    _annotate(out, f"{win['title'][:60]}  |  {time.strftime('%Y-%m-%d %H:%M:%S')}  |  {w}x{h}",
              args.scale)
    print(f"[shot] 已保存：{out}（{out.stat().st_size / 1024:.1f} KB）")
    return 0


def shot_screen(args) -> int:
    """抓整屏（会包含桌面上其它窗口，仅在明确同意时使用）。"""
    display = args.display
    out = _out_path(args.out)
    w, h = _screen_size(display)
    print(f"[shot] 抓整屏：{w}x{h}")
    if not _grab(display, (0, 0, w, h), out):
        return 1
    _annotate(out, f"[screen]  {time.strftime('%Y-%m-%d %H:%M:%S')}  |  {w}x{h}", args.scale)
    print(f"[shot] 已保存：{out}（{out.stat().st_size / 1024:.1f} KB）")
    return 0


def list_windows(args) -> int:
    print(f"[shot] 显示 {args.display} 当前窗口：")
    for w in _windows(args.display):
        print(f"  {w['id']}  {w['w']:>5}x{w['h']:<5} @({w['x']:>5},{w['y']:>5})  pid={w['pid']:<8} "
              f"{w['title'][:70]}")
    return 0


def shot_headless(args) -> int:
    out = _out_path(args.out)
    _Handler.target = args.url
    _Handler.title = f"SHOT-headless-{os.getpid()}"
    _Handler.width = args.width or 1440
    _Handler.height = args.height or 1000
    _Handler.wait_ms = args.wait
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = Path(tempfile.mkdtemp(prefix="shot-home-", dir=str(PROJECT_ROOT / "var" / "tmp")))
    env = {**os.environ, "HOME": str(home), "MOZ_HEADLESS": "1", "DISPLAY": ""}
    print(f"[shot] headless 模式：{_Handler.width}x{_Handler.height}，等待 {args.wait} ms")
    try:
        proc = subprocess.run(
            ["firefox", "--headless", "--no-remote", "--new-instance",
             "--profile", str(home / "profile"),
             "--window-size", f"{_Handler.width},{_Handler.height}",
             "--screenshot", str(out), f"http://127.0.0.1:{port}/"],
            env=env, capture_output=True, timeout=args.startup + args.wait / 1000 + 30)
    except subprocess.TimeoutExpired:
        print("[shot] headless 超时：目标页可能保持长连接（SSE/轮询）导致迟迟不 settle，"
              "建议改用默认的 x11 模式", file=sys.stderr)
        server.shutdown()
        return 1
    finally:
        server.shutdown()
        shutil.rmtree(home, ignore_errors=True)
    if not out.exists() or out.stat().st_size == 0:
        print("[shot] 截图失败：" + ((proc.stderr or b"")[-400:]).decode(errors="replace"),
              file=sys.stderr)
        return 1
    parsed = urlparse(args.url)
    _annotate(out, f"{parsed.netloc}{parsed.path}  |  {time.strftime('%Y-%m-%d %H:%M:%S')}"
                   f"  |  {_Handler.width}x{_Handler.height}", args.scale)
    print(f"[shot] 已保存：{out}（{out.stat().st_size / 1024:.1f} KB）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取真实渲染的前端界面（X11 窗口 / 整屏）")
    ap.add_argument("url", nargs="?", default="",
                    help="仅 headless 模式使用（默认 http://127.0.0.1:5095/）")
    ap.add_argument("--mode", choices=("window", "screen", "headless"), default="window",
                    help="window=抓已打开的窗口（默认，实测可用）；screen=整屏；headless=实验性")
    ap.add_argument("--title", default=DEFAULT_TITLE, help="window 模式的窗口标题关键字")
    ap.add_argument("--display", default=os.environ.get("DISPLAY") or ":0")
    ap.add_argument("--out", default="", help="输出 PNG（默认 var/tmp/shot/<时间戳>.png）")
    ap.add_argument("--width", type=int, default=1440, help="headless 模式视口宽")
    ap.add_argument("--height", type=int, default=1000, help="headless 模式视口高")
    ap.add_argument("--wait", type=int, default=6000, help="headless 模式渲染等待毫秒")
    ap.add_argument("--startup", type=int, default=90, help="headless 模式超时上限（秒）")
    ap.add_argument("--scale", type=float, default=1.0, help="输出缩放（0.5 便于整屏查看）")
    ap.add_argument("--list", action="store_true", help="只列出当前窗口，便于挑 --title")
    ap.add_argument("--no-raise", action="store_true",
                    help="不把目标窗口抬到最前（默认会抬，避免抓到压在上面的其它窗口）")
    ap.add_argument("--raise-wait", type=int, default=700,
                    help="抬窗后等待毫秒（默认 700）")
    args = ap.parse_args()

    if args.list:
        return list_windows(args)
    if args.mode == "screen":
        return shot_screen(args)
    if args.mode == "headless":
        args.url = args.url or "http://127.0.0.1:5095/"
        return shot_headless(args)
    return shot_window(args)


if __name__ == "__main__":
    sys.exit(main())
