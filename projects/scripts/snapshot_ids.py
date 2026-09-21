#!/usr/bin/env python
"""DOM id 清单快照 / 比对：重构前后确认没有 id 被误删。

用法：
    .venv/bin/python scripts/snapshot_ids.py save     # 保存当前 web/index.html 的 id 清单
    .venv/bin/python scripts/snapshot_ids.py diff     # 与快照比对，列出被移除/新增的 id
"""
from __future__ import annotations

import json
import sys
from html.parser import HTMLParser
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
SNAP = Path(__file__).resolve().parent.parent / "var" / "tmp" / "id_baseline.json"


class _Ids(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k == "id" and v:
                self.ids.append(v)


def current_ids() -> list[str]:
    parser = _Ids()
    parser.feed((WEB / "index.html").read_text(encoding="utf-8"))
    return sorted(set(parser.ids))


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "diff"
    ids = current_ids()
    if mode == "save":
        SNAP.parent.mkdir(parents=True, exist_ok=True)
        SNAP.write_text(json.dumps(ids, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已保存 {len(ids)} 个 id 到 {SNAP}")
        return 0
    if not SNAP.is_file():
        print("没有基线快照，请先执行 save", file=sys.stderr)
        return 2
    base = set(json.loads(SNAP.read_text(encoding="utf-8")))
    cur = set(ids)
    removed, added = sorted(base - cur), sorted(cur - base)
    print(f"基线 {len(base)} 个 → 当前 {len(cur)} 个")
    if removed:
        print(f"❌ 被移除 {len(removed)} 个 id（需确认是否仍被动态引用）：{removed}")
    if added:
        print(f"＋ 新增 {len(added)} 个 id：{added[:20]}{' ...' if len(added) > 20 else ''}")
    if not removed:
        print("✅ 没有 id 被移除")
    return 1 if removed else 0


if __name__ == "__main__":
    sys.exit(main())
