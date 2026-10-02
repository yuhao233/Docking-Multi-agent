#!/usr/bin/env python3
"""按《附件5-代码提交要求》逐条核对提交包是否与要求一致（只检查事实）。

用法::

    .venv/bin/python scripts/check_submission.py                 # 检查 dist/Docking-Multi-Agent
    .venv/bin/python scripts/check_submission.py <已解压的提交包目录>

检查内容：必备文件与目录、README 是否写明环境项与完整命令、依赖是否钉版本、
候选清单是否含必填字段、运行记录是否含种子/引擎版本/耗时、源码是否含硬编码绝对路径、
包内是否混入密钥或缓存。任一项不符合即退出码非 0。
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

if len(sys.argv) < 2:
    sys.argv.append(str(pathlib.Path(__file__).resolve().parents[1]
                        / "dist" / "Docking-Multi-Agent"))

import pathlib, sys

pkg = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.')
ok, bad = [], []

def need(cond: object, label: str) -> None:
    (ok if cond else bad).append(label)

# — 二、目录结构与必备文件 —
need((pkg/'README.md').is_file(), 'README.md 存在')
req = (pkg/'requirements.txt').read_text(encoding='utf-8')
need('vina==' in req and 'rdkit==' in req and 'meeko==' in req, 'requirements.txt 列明依赖版本')
readme = (pkg/'README.md').read_text(encoding='utf-8')
for key in ('Python', '操作系统', 'CUDA', '驱动', '系统库', '内存', '网络'):
    need(key in readme, f'README 说明环境项：{key}')
for cmd in ('python screen.py', 'python predict.py', 'pip install -r requirements.txt', 'bash run_web.sh'):
    need(cmd in readme, f'README 写明完整命令：{cmd}')
need((pkg/'screen.py').is_file() and (pkg/'predict.py').is_file(), '主运行入口 screen.py + predict.py')
need((pkg/'notebooks/quickstart.ipynb').is_file(), 'Notebook 存在且可解析')
need(bool(json.loads((pkg/'notebooks/quickstart.ipynb').read_text(encoding='utf-8'))['cells']),
     'Notebook 有可执行单元')
need('src/docking_agent' in str([p for p in pkg.glob('src/*')]), 'src/ 核心源码存在')
need((pkg/'data/README.md').is_file(), 'data/ 数据说明与许可')
need((pkg/'models/MODEL_CARD.md').is_file(), 'models/ Model Card')
need((pkg/'logs/run_example.json').is_file() and (pkg/'logs/run_example.log').is_file(),
     'logs/ 运行记录（含种子/环境/耗时）')
need((pkg/'results/results_example.csv').is_file(), 'results/ 候选清单示例')

# — 三、可复现与溯源 —
log = json.loads((pkg/'logs/run_example.json').read_text(encoding='utf-8'))
for key in ('seed', 'engine', 'engine_version', 'python', 'platform', 'elapsed_sec',
            'molecules_scored', 'box_center', 'box_size'):
    need(key in log, f'运行记录含 {key}')
need('许可' in (pkg/'data/README.md').read_text(encoding='utf-8'), 'data 说明含使用许可')
need('许可' in readme, 'README 含第三方来源与许可')
need('已知局限' in (pkg/'models/MODEL_CARD.md').read_text(encoding='utf-8'), 'Model Card 含已知局限')

# — 四、最终结果文件 —
csv_text = (pkg/'results/results_example.csv').read_text(encoding='utf-8')
header = csv_text.splitlines()[0].split(',')
for col in ('candidate_id', 'track', 'smiles', 'affinity_kcal_mol', 'engine_version', 'remark',
            'receptor_file', 'pose_file'):
    need(col in header, f'候选清单含必填字段：{col}')
need(len(csv_text.splitlines()) > 1, '候选清单含数据行')
need('结构文件' in readme or 'receptor_file' in readme, 'README 说明结构文件对应关系')

# — 五、非自训练说明与创新点 —
need('不训练' in readme or '未开展训练' in readme or '不训练新的深度学习模型' in readme,
     'README 说明未训练模型')
need('创新' in readme, 'README 含本项目创新与贡献')
need('llm_configured' in readme or '大模型' in readme, 'README 说明大模型调用方式与边界')

# — 六、代码规范 —
py_files = list((pkg/'src').rglob('*.py')) + [pkg/'screen.py', pkg/'predict.py']
hits = [str(f) for f in py_files
        if '/home/' in f.read_text(encoding='utf-8', errors='ignore')
        or 'C:\\Users' in f.read_text(encoding='utf-8', errors='ignore')]
need(not hits, f'源码无硬编码绝对路径（命中 {hits[:2]}）')
need('seed' in (pkg/'screen.py').read_text(encoding='utf-8'), '入口固定随机种子参数')

# — 包卫生 —
need(not list(pkg.rglob('__pycache__')), '无 __pycache__')
need(not list(pkg.rglob('*.pyc')), '无 .pyc')
secret = [str(f) for f in pkg.rglob('*') if f.is_file() and f.suffix in ('.py','.md','.json','.sh','.js')
          and re.search(r'sk-[A-Za-z0-9]{20,}', f.read_text(encoding='utf-8', errors='ignore'))]
need(not secret, f'包内无 API 密钥（命中 {secret[:2]}）')
need(not (pkg/'config/local_settings.json').read_text(encoding='utf-8').count('api_key'),
     'config/local_settings.json 已脱敏')

print(f'通过 {len(ok)} 项；不符合 {len(bad)} 项')
for b in bad:
    print('  ✗', b)
sys.exit(1 if bad else 0)
