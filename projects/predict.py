#!/usr/bin/env python3
"""一键生成候选清单（与 `screen.py` 等价入口，参数完全一致）。

按《代码提交要求》中"predict.py 等脚本"的命名习惯提供；评测平台可直接调用：

    python predict.py --receptor receptor.pdb --ligands library.sdf \
        --center 31.5,13.74,24.36 --exhaustiveness 8 --seed 42 --out results/results.csv

参数与输出说明见 README.md §3/§4。
"""
from screen import main

if __name__ == "__main__":
    raise SystemExit(main())
