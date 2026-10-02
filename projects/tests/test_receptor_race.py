"""受体准备缓存的并发隔离回归测试。

此前的实现按源文件 basename 命名受体准备产物
（`<base>_prot.pdb` / `<base>.pdbqt` / `<base>.site.json`），而缓存目录全局共享，
两个并发运行（或两个同名 `receptor.pdb` 上传）会写同一组文件并互相覆盖，
出现「请求保留 ZN，返回的却是其它请求的 HEM+ZN 结果」这类静默串数据；
该行为也是全量 pytest 中偶发失败（`test_docking_notes_report_untemplatable_kept_residues`）的技术原因，
两个 pytest 进程会同时准备同名受体。

现实现把准备产物改为内容寻址（文件名带源文件内容哈希与 keep 集哈希），
不同输入落到不同文件，对外展示名仍为原始 basename。
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
    # 三个并发请求共用同名源文件，模拟同名上传与多运行场景
    src = work_dir / "receptor.pdb"
    src.write_text(_receptor_pdb_text(), encoding="utf-8")
    barrier.wait(timeout=30)          # 三次准备尽量重叠执行
    spec = prepare_user_receptor(str(src), keep_hetatm=keep)
    sink[tag] = spec


def test_concurrent_prepare_same_filename_is_isolated() -> None:
    """同名受体、不同 keep_hetatm 并发准备：各结果与自身请求的参数一致。"""
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

    # 结果文件互不相同（内容寻址），否则会退化为共享同名文件
    files = {Path(s["pdbqt"]).name for s in sink.values()}
    assert len(files) == 3, f"准备产物发生文件名碰撞：{sorted(files)}"
    # 对外展示名仍为原始 basename（内容哈希不得进入 receptor_key 与报告）
    assert {s["key"] for s in sink.values()} == {"receptor"}, [s["key"] for s in sink.values()]


def test_pdbqt_spec_strips_content_address_suffix() -> None:
    """只拿到内容寻址的 .pdbqt 时，展示用的 base 去掉哈希后缀。"""
    from docking_agent.core.receptors import _pdbqt_spec

    work_dir = Path(tempfile.mkdtemp(prefix="receptor-name-", dir=str(PROJECT_ROOT / "var" / "tmp")))
    src = work_dir / "my_receptor.pdb"
    src.write_text(_receptor_pdb_text(), encoding="utf-8")
    from docking_agent.core.receptors import prepare_user_receptor

    spec = prepare_user_receptor(str(src))
    again = _pdbqt_spec(spec["pdbqt"])
    assert again["key"] == "my_receptor", again["key"]
    assert spec["key"] == "my_receptor"
    # sidecar 仍可被找到（盒子来源不因改名退化为蛋白质中心）
    assert (again.get("site") or {}).get("source"), again.get("site")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
