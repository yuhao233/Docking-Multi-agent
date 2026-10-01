#!/usr/bin/env python3
"""把工作区组装成《代码提交要求》规定的提交包（目录 + zip）。

用法::

    .venv/bin/python scripts/make_submission.py            # 组装到 dist/ 并打包
    .venv/bin/python scripts/make_submission.py --no-zip   # 只组装目录

产物::

    dist/Docking-Multi-Agent/            # 提交目录（README / requirements / screen.py / src / ...）
    dist/Docking-Multi-Agent.zip         # 同上的压缩包

打包内容：`submission/` 下维护的提交材料（README、requirements.txt、示例数据、Model Card、
Notebook、结果与日志示例）+ 仓库源码 `src/docking_agent`（排除 __pycache__/.pyc）+ 顶层入口
`screen.py`。网页端（`web/`）与开发脚本不进入提交包：作品的核心计算与一键入口自包含，
完整工作区见代码仓库。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUBMISSION = PROJECT_ROOT / "submission"
DIST = PROJECT_ROOT / "dist"
PACKAGE_NAME = "Docking-Multi-Agent"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store", "*.egg-info")


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _copy_tree(src: Path, dst: Path) -> int:
    if not src.is_dir():
        return 0
    shutil.copytree(src, dst, ignore=IGNORE, dirs_exist_ok=True)
    return sum(1 for p in dst.rglob("*") if p.is_file())


def build(target: Path, *, with_zip: bool = True) -> Path:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    # 1) 提交材料（README / requirements / 数据 / 模型说明 / Notebook / 结果与日志示例）
    files = 0
    files += _copy_tree(SUBMISSION, target)
    # 2) 源码（核心计算 + 工具层 + Agent 编排 + 服务接口）
    files += _copy_tree(PROJECT_ROOT / "src" / "docking_agent", target / "src" / "docking_agent")
    # 3) 顶层一键入口
    _copy_file(PROJECT_ROOT / "screen.py", target / "screen.py")
    files += 1

    readme = target / "README.md"
    if not readme.is_file():
        raise SystemExit("提交包缺少 README.md（请在 submission/README.md 维护）")

    # 4) 打包时的自检：入口脚本语法、依赖清单、示例数据与结果是否齐全
    subprocess.run(["python3", "-m", "py_compile", str(target / "screen.py")], check=True)
    shutil.rmtree(target / "__pycache__", ignore_errors=True)   # py_compile 的副产物不进提交包
    for required in ("requirements.txt", "data/example/receptor_demo.pdb",
                     "data/example/ligands_demo.smi", "models/MODEL_CARD.md",
                     "notebooks/quickstart.ipynb", "results/results_example.csv"):
        if not (target / required).is_file():
            raise SystemExit(f"提交包缺少必备文件：{required}")

    archive = target.with_suffix(".zip")
    if with_zip:
        if archive.exists():
            archive.unlink()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(target.rglob("*")):
                if path.is_file():
                    zf.write(path, arcname=str(Path(PACKAGE_NAME) / path.relative_to(target)))
    print(f"提交目录：{target}（{files} 个文件）")
    if with_zip:
        size_mb = archive.stat().st_size / 1024 / 1024
        print(f"压缩包：{archive}（{size_mb:.1f} MB）")
    return archive if with_zip else target


def main() -> int:
    parser = argparse.ArgumentParser(description="组装提交包")
    parser.add_argument("--out", default=str(DIST), help="输出目录（默认 dist/）")
    parser.add_argument("--no-zip", action="store_true", help="只组装目录，不压缩")
    args = parser.parse_args()
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    build(out_root / PACKAGE_NAME, with_zip=not args.no_zip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
