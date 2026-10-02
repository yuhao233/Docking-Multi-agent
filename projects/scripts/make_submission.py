#!/usr/bin/env python3
"""把工作区组装成《代码提交要求》规定的提交包（目录 + zip）。

用法::

    .venv/bin/python scripts/make_submission.py            # 组装到 dist/ 并打包
    .venv/bin/python scripts/make_submission.py --no-zip   # 只组装目录

产物::

    dist/Docking-Multi-Agent/            # 提交目录（README / requirements / screen.py / src / ...）
    dist/Docking-Multi-Agent.zip         # 同上的压缩包

打包内容：`submission/` 下维护的提交材料（README、requirements.txt、示例数据、Model Card、
Notebook、结果与日志示例、网页端说明）+ 仓库源码 `src/docking_agent`（排除 __pycache__/.pyc）
+ 顶层入口 `screen.py` 与 `run_web.sh` + 网页端运行所需资源（`web/` 前端、`config/` 配置、
`assets/` 内置受体库与示例分子库）。**不打包**：使用者上传数据 `assets/uploads`、缓存
`assets/cache`、第三方工具二进制 `assets/tools`、含密钥的 `.env`、`var/` 运行记录。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUBMISSION = PROJECT_ROOT / "submission"
DIST = PROJECT_ROOT / "dist"
PACKAGE_NAME = "Docking-Multi-Agent"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store", "*.egg-info")
#: 网页端运行所需、但**不能**进提交包的内容：
#  `assets/uploads` 是使用者上传的数据、`assets/cache` 与 `assets/tools` 是缓存与第三方二进制、
#  `.env` 含密钥。提交包只带配置模板（`.env.example`）与内置资源（受体库/示例库/提示词）。
RUNTIME_SKIP = {"uploads", "cache", "tools", ".venv", "var", "dist", "__pycache__"}


def _copy_runtime_dir(src: Path, dst: Path) -> int:
    """按白名单复制运行期目录（web/ config/ assets/ scripts/），跳过数据与缓存。"""
    if not src.is_dir():
        return 0
    count = 0
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if any(part in RUNTIME_SKIP for part in rel.parts):
            continue
        if path.is_file():
            if path.name == ".env" or path.suffix in (".pyc", ".pyo"):
                continue
            if path.name == "local_settings.json":
                continue          # 本机设置含 API 密钥，只用下面的脱敏模板
            _copy_file(path, dst / rel)
            count += 1
    if (src / "local_settings.json").is_file():
        # 脱敏模板：保留非敏感默认值（如对接引擎偏好），**清空密钥**
        try:
            data = json.loads((src / "local_settings.json").read_text(encoding="utf-8"))
            if isinstance(data.get("llm"), dict):
                data["llm"].pop("api_key", None)
            # 对接引擎回落为 auto（内置 Vina）：本机若登记过外部引擎，提交包在新环境会直接失败
            if isinstance(data.get("docking"), dict):
                data["docking"]["engine"] = "auto"
            text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            # 两个文件：`local_settings.json` 会被启动时自动加载（开箱即用）；
            # `.example` 供使用者对照字段含义后改成自己的配置。
            (dst / "local_settings.json").write_text(text, encoding="utf-8")
            (dst / "local_settings.example.json").write_text(text, encoding="utf-8")
            count += 2
        except Exception as exc:  # noqa: BLE001 - 模板生成失败不影响打包
            print(f"提示：本机设置脱敏模板未生成（{exc}），提交包内不含该模板")
    return count


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
    # 3) 顶层一键入口 + 网页端启动脚本
    _copy_file(PROJECT_ROOT / "screen.py", target / "screen.py")
    _copy_file(PROJECT_ROOT / "start.sh", target / "start.sh")
    _copy_file(SUBMISSION / "run_web.sh", target / "run_web.sh")
    files += 3
    # 4) 网页端与运行所需资源：web/（前端）、config/（含密钥的 .env 不复制）、
    #    assets/（内置受体库、示例分子库、提示词；跳过 uploads/cache/tools）
    for name in ("web", "config", "assets"):
        files += _copy_runtime_dir(PROJECT_ROOT / name, target / name)

    readme = target / "README.md"
    if not readme.is_file():
        raise SystemExit("提交包缺少 README.md（请在 submission/README.md 维护）")

    # 4) 打包时的自检：入口脚本语法、依赖清单、示例数据与结果是否齐全
    subprocess.run(["python3", "-m", "py_compile", str(target / "screen.py")], check=True)
    shutil.rmtree(target / "__pycache__", ignore_errors=True)   # py_compile 的副产物不进提交包
    for required in ("requirements.txt", "data/example/receptor_demo.pdb",
                     "data/example/ligands_demo.smi", "models/MODEL_CARD.md",
                     "notebooks/quickstart.ipynb", "results/results_example.csv",
                     "web/simple.html", "web/index.html", "web/app.js",
                     "config/agent_llm_config.json", "config/local_settings.example.json",
                     ".env.example",
                     "run_web.sh", "WEB.md"):
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
