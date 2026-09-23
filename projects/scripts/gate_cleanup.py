"""门禁脚本的**自清理**：删掉本次门禁运行真实创建的那些运行记录。

为什么需要：`scripts/ui_e2e.js` 与 `scripts/browser_check.py` 在真跑模式下会通过标准协议
创建真实运行记录（每跑一次十几条）。它们会留在 `var/runs` 里，长期把用户的历史列表挤满
（实测：一次会话下来 5534 条运行里绝大多数是夹具运行）。清理策略保守且可核对：

* 只删「跑之前不存在、跑完新出现」的 run_id —— 绝不碰用户已有的记录；
* 通过 `DELETE /api/runs/{id}` 走服务端删除（远程服务同样适用，不直接动文件系统）；
* 删除失败只打印，不影响门禁结论；条数会作为一条 check 报告出来。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

LIST_LIMIT = 300


def run_ids(base: str, *, timeout: int = 20) -> set:
    """当前服务端历史里的 run_id 集合（取不到时返回空集 → 调用方退化为「不删」）。"""
    try:
        with urllib.request.urlopen(f"{base}/api/runs?limit={LIST_LIMIT}", timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 读不到历史就不清理，不能因此让门禁失败
        print("门禁自清理：读取历史失败（跳过清理）：", exc)
        return set()
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = (data or {}).get("runs") if isinstance(data, dict) else None
    if rows is None and isinstance(payload, dict):
        rows = payload.get("runs")
    return {str(r.get("run_id")) for r in (rows or []) if isinstance(r, dict) and r.get("run_id")}


def cleanup(base: str, before: set, *, timeout: int = 20) -> tuple:
    """删除本次新增的运行记录，返回 `(deleted, created)` 两个条数。"""
    created = sorted(run_ids(base) - set(before))
    deleted = 0
    for run_id in created:
        request = urllib.request.Request(
            f"{base}/api/runs/{urllib.parse.quote(run_id)}", method="DELETE")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                if resp.status == 200:
                    deleted += 1
        except Exception as exc:  # noqa: BLE001 - 清理失败只报告，不影响门禁结论
            print(f"门禁自清理：删除 {run_id} 失败：{exc}")
    print(f"门禁自清理：删除本次创建的运行记录 {deleted}/{len(created)} 条")
    return deleted, len(created)
