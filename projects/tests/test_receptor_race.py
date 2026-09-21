"""受体准备缓存的**并发隔离**回归测试。

背景（真实缺陷，可复现）：受体准备产物原先只按源文件的 basename 命名
（`<base>_prot.pdb` / `<base>.pdbqt` / `<base>.site.json`），而缓存目录是全局共享的。
于是两个并发运行（或两个都叫 `receptor.pdb` 的上传）会写同一组文件、互相覆盖，
出现「我请求保留 ZN，拿回来的却是别人的 HEM+ZN 结果」这种静默串数据——
它也正是全量 pytest 里那条偶发失败（`test_docking_notes_report_untemplatable_kept_residues`）的根因：
两个 pytest 进程同时准备同名受体。

修法：准备产物改为**内容寻址**（文件名带源文件内容哈希 + keep 集哈希），
不同输入天然落到不同文件；对外展示名仍然是原始 basename。
"""
from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from docking_agent.config import ensure_runtime_env  # noqa: E402

ensure_runtime_env()

from test_ligand_receptor_chemistry import _receptor_pdb_text  # noqa: E402


def _prepare_in_thread(tag: str, keep: tuple, sink: dict, barrier: threading.Barrier) -> None:
    from docking_agent.core.receptors import prepare_user_receptor

    work_dir = Path(tempfile.mkdtemp(prefix=f"receptor-race-{tag}-",
                                     dir=str(PROJECT_ROOT / "var" / "tmp")))
    # 关键：三个并发请求用**完全同名**的源文件，模拟真实上传/多运行场景
    src = work_dir / "receptor.pdb"
    src.write_text(_receptor_pdb_text(), encoding="utf-8")
    barrier.wait(timeout=30)          # 尽量让三次准备真正重叠
    spec = prepare_user_receptor(str(src), keep_hetatm=keep)
    sink[tag] = spec


def test_concurrent_prepare_same_filename_is_isolated() -> None:
    """同名受体 + 不同 keep_hetatm 并发准备：各自结果必须与自己的请求一致。"""
    sink: dict = {}
    barrier = threading.Barrier(3)
    cases = {"keep_zn": ("ZN",), "keep_hem_zn": ("HEM", "ZN"), "keep_none": ()}
    threads = [threading.Thread(target=_prepare_in_thread, args=(tag, keep, sink, barrier))
               for tag, keep in cases.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)

    assert len(sink) == 3, f"有线程未完成：{sorted(sink)}"
    assert sink["keep_zn"]["kept_hetatm"] == {"ZN": 1}, sink["keep_zn"]["kept_hetatm"]
    assert sink["keep_none"]["kept_hetatm"] == {} and sink["keep_none"]["unsupported_hetatm"] == []
    hem = sink["keep_hem_zn"]
    assert hem["kept_hetatm"] == {"ZN": 1}, hem["kept_hetatm"]
    assert hem["unsupported_hetatm"] == ["HEM"], hem["unsupported_hetatm"]

    # 结果文件必须互不相同（内容寻址），否则就是又回到了共享同名文件
    files = {Path(s["pdbqt"]).name for s in sink.values()}
    assert len(files) == 3, f"准备产物发生文件名碰撞：{sorted(files)}"
    # 对外展示名仍然是原始 basename（不能把内容哈希泄露到 receptor_key / 报告里）
    assert {s["key"] for s in sink.values()} == {"receptor"}, [s["key"] for s in sink.values()]


def test_pdbqt_spec_strips_content_address_suffix() -> None:
    """只拿到内容寻址的 .pdbqt 时，展示用的 base 要去掉哈希后缀。"""
    from docking_agent.core.receptors import _pdbqt_spec

    work_dir = Path(tempfile.mkdtemp(prefix="receptor-name-", dir=str(PROJECT_ROOT / "var" / "tmp")))
    src = work_dir / "my_receptor.pdb"
    src.write_text(_receptor_pdb_text(), encoding="utf-8")
    from docking_agent.core.receptors import prepare_user_receptor

    spec = prepare_user_receptor(str(src))
    again = _pdbqt_spec(spec["pdbqt"])
    assert again["key"] == "my_receptor", again["key"]
    assert spec["key"] == "my_receptor"
    # sidecar 仍能被找到（盒子来源不能因为改名而退化成蛋白质心）
    assert (again.get("site") or {}).get("source"), again.get("site")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
