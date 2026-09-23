"""本地设置（界面可编辑）—— `config/local_settings.json`。

设置页面的后端支撑。设计目标：
  1. **单一事实来源**：所有可配置项集中在本模块的字段表（SPECS）里，
     接口、校验、前端渲染都由它驱动，避免前后端字段走样；
  2. **每个字段都能回答「当前值是多少、来自哪里」**（sources），
     避免出现「改了没生效却不知道为什么」的情况；
  3. **不破坏既有部署方式**：没有本文件时行为与之前完全一致。

优先级（越具体越优先）：

    LLM 字段：
        LLM_<字段>_<角色> 环境变量
      > local_settings.json → roles.<角色>
      > 内置角色默认 agent_llm_config.json → roles.<角色>
      > local_settings.json → llm
      > LLM_<字段> 环境变量（.env）
      > agent_llm_config.json → config

    运行类字段：
        界面设置(local_settings.json) > .env > 内置默认
      例外（部署保护项，.env 优先且界面不建议改）：
        PORT / DOCKING_MAX_LIGANDS / UPLOAD_MAX_MB

运行类字段通过 `apply_runtime_env()` 写入进程环境变量，
从而复用各模块既有的 env_int/env_bool/... 读取逻辑（零改动接入）。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from docking_agent.paths import project_root

logger = logging.getLogger(__name__)

SETTINGS_REL = "config/local_settings.json"
# 允许用环境变量指定设置文件位置（测试与多实例部署使用）
ENV_OVERRIDE = "LOCAL_SETTINGS_PATH"

# 部署保护项：由 Spec.env_priority 派生（**唯一事实源**是字段规格，避免两处清单漂移）
# 这些键由 .env 决定（部署方策略），界面只读展示。

ROLES: Tuple[str, ...] = ("intake", "coordinator", "property", "pocket", "docking", "binding")
ROLE_LABEL = {
    "intake": "任务受理 Agent（理解用户要什么）",
    "coordinator": "整体协调 Agent（编排）",
    "property": "分子属性评估 Agent",
    "pocket": "口袋分析 Agent",
    "docking": "Docking 执行 Agent",
    "binding": "结合模式检测 Agent",
}


# --------------------------------------------------------------------------- #
# 字段规格
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Spec:
    """一个可配置字段的完整描述（接口 / 校验 / 前端渲染共用）。"""

    path: str                     # 点分路径（local_settings.json 内的位置）
    label: str
    group: str
    kind: str = "str"             # str|int|float|bool|enum|json|secret
    default: Any = None
    choices: Tuple[Any, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    env: Optional[str] = None     # 映射到的环境变量（运行类字段用）
    env_priority: bool = False    # True = .env 优先（部署保护项）
    needs_restart: bool = False   # 改后需重启进程
    readonly: bool = False        # 界面只读（改动无意义/由启动参数决定）
    role: str = ""                # 角色字段所属角色
    field: str = ""               # 角色字段的字段名
    help: str = ""
    placeholder: str = ""


GROUPS: Tuple[Dict[str, str], ...] = (
    {"id": "llm", "label": "LLM 接入（全局）",
     "help": "所有 Agent 的默认端点与调用参数。API Key 只写不读（界面不回显明文）。"},
    {"id": "models", "label": "模型管理",
     "help": "从当前端点拉取可用模型列表，再分配给各 Agent。"},
    {"id": "roles", "label": "各 Agent 模型与调用参数",
     "help": "每个 Agent 持有独立模型实例；留空表示继承全局设置（占位符显示继承到的值）。"},
    {"id": "docking", "label": "对接默认值（界面表单预填）",
     "help": "设置页保存的默认值会在打开工作台时预填表单；单次运行仍以表单/指令为准。"},
    {"id": "external", "label": "外部工具（自行安装）",
     "help": "这些工具本项目不分发：填了路径并且「检测」通过才会被使用；"
             "填了但检测不通过，运行会直接失败并提示，不会静默改用别的实现。"},
    {"id": "runtime", "label": "运行与性能参数",
     "help": "保存后对新的运行立即生效（进程内环境变量会被刷新）。"},
    {"id": "deploy", "label": "部署级参数",
     "help": "端口由启动命令决定（界面改不了）；其余项若 .env 已显式设置则以 .env 为准，"
             "未设置时可由界面调整。"},
)

# ---- LLM 全局字段 ----
LLM_SPECS: Tuple[Spec, ...] = (
    Spec("llm.base_url", "API Base URL", "llm", "str", placeholder="https://api.deepseek.com",
         help="任意 OpenAI 兼容端点；留空则用内置默认。"),
    Spec("llm.api_key", "API Key", "llm", "secret", placeholder="留空 = 不修改",
         help="只写入本地文件，接口不会回显明文。"),
    Spec("llm.model", "默认模型", "llm", "str", placeholder="deepseek-flash",
         help="未单独配置的 Agent 使用这个模型。"),
    Spec("llm.temperature", "温度 temperature", "llm", "float", default=0.2,
         minimum=0, maximum=2, step=0.05),
    Spec("llm.top_p", "top_p", "llm", "float", default=0.9, minimum=0, maximum=1, step=0.05),
    Spec("llm.timeout", "超时（秒）", "llm", "float", default=600, minimum=5, step=10),
    Spec("llm.max_tokens", "最大输出 tokens", "llm", "int", minimum=1, step=256,
         help="留空则用端点默认。"),
    Spec("llm.thinking", "思考模式", "llm", "enum", choices=("disabled", "enabled"),
         help="豆包等模型会发送 {\"thinking\":{\"type\":...}}。"),
    Spec("llm.extra_headers", "额外请求头（JSON）", "llm", "json",
         placeholder='{"X-Org":"lab"}',
         help="值为密钥类字段名（*key*/*token*/*auth*…）时只回显 ***，不会明文下发；"
              "界面上原样保留 *** 即表示不修改。"),
    Spec("llm.extra_body", "额外请求体（JSON）", "llm", "json",
         placeholder='{"top_k":40}'),
)

# ---- 各 Agent 角色字段（path = roles.<role>.<field>）----
# 注意：第三个位置参数是 group，第四个才是 kind，写全避免退化成 str。
ROLE_FIELD_SPECS: Tuple[Spec, ...] = (
    Spec("model", "模型", "roles", "str", placeholder="继承全局"),
    Spec("temperature", "温度", "roles", "float", minimum=0, maximum=2, step=0.05),
    Spec("top_p", "top_p", "roles", "float", minimum=0, maximum=1, step=0.05),
    Spec("max_tokens", "最大 tokens", "roles", "int", minimum=1, step=256),
    Spec("timeout", "超时（秒）", "roles", "float", minimum=5, step=10),
    Spec("thinking", "思考模式", "roles", "enum", choices=("", "disabled", "enabled")),
    Spec("base_url", "Base URL（独立端点，可选）", "roles", "str", placeholder="继承全局"),
    Spec("api_key", "API Key（独立密钥，可选）", "roles", "secret", placeholder="留空 = 继承全局"),
    Spec("structured_output", "结构化输出", "roles", "enum", choices=("", "auto", "on", "off"),
         help="auto（默认）= 先按「强制 tool_choice」试，被供应商拒绝后记住该能力并改用 JSON 模式/"
              "文本契约，后续运行不再重试；on = 强制结构化输出（供应商不支持会失败）；"
              "off = 直接用文本 JSON 契约。"),
)


def _role_specs() -> Tuple[Spec, ...]:
    out: List[Spec] = []
    for role in ROLES:
        for spec in ROLE_FIELD_SPECS:
            out.append(Spec(
                path=f"roles.{role}.{spec.path}",
                label=spec.label,
                group="roles",
                kind=spec.kind,
                choices=spec.choices,
                minimum=spec.minimum,
                maximum=spec.maximum,
                step=spec.step,
                role=role,
                field=spec.path,
                help=spec.help,
                placeholder=spec.placeholder,
            ))
    return tuple(out)


# ---- 对接默认值（界面表单预填；不映射环境变量）----
DOCKING_SPECS: Tuple[Spec, ...] = (
    Spec("docking.engine", "对接引擎（默认）", "docking", "enum",
         choices=("auto", "vina", "autodock", "external"), default="auto",
         help="对话/表单没有显式指定引擎时用这里的默认值。"
              "auto=优先内置 Vina、不可用时回退 AutoDock4 CPU；vina=内置 Vina（CPU）；"
              "autodock=经典 AutoDock4（需要 autodock4+autogrid4）；"
              "external=设置页登记的外部引擎（如 GPU 版 AutoDock-GPU）——未登记或探测不通过会直接报错。"),
    Spec("docking.exhaustiveness", "搜索强度 exhaustiveness", "docking", "int",
         default=16, minimum=1, maximum=64,
         help="每个分子的采样次数，默认 16（平衡档）。越大越准越慢；"
              "对话/参数模式里显式给出数值即为用户参数（source=user），不再被自动规划改写。"),
    Spec("docking.n_poses", "输出位姿数 n_poses", "docking", "int", default=1,
         minimum=1, maximum=50),
    Spec("docking.save_poses", "保存对接位姿文件", "docking", "bool", default=True),
    Spec("docking.max_ligands", "最大分子数（0=不限）", "docking", "int", default=0, minimum=0),
    Spec("docking.protonation", "质子化态策略", "docking", "enum",
         env="LIGAND_PROTONATION", choices=("ph", "neutralize", "keep"), default="ph",
         help="ph（默认，推荐）= 按「目标 pH」分配质子化态（羧酸/胺/脒/胍/咪唑等，内置 pKa 规则表，"
              "默认 7.4 生理 pH，对接的通行假设）；"
              "neutralize = 只把带净电荷的分子中和，中性分子一字不改；"
              "keep = 完全保持输入形式，仅在结果里告警。策略是运行级的（同一批分子同口径），"
              "逐分子记录净电荷前/后与命中规则，原始 SMILES 始终保留。"),
    Spec("docking.protonation_ph", "目标 pH（质子化）", "docking", "float",
         env="LIGAND_PROTONATION_PH", default=7.4, minimum=0.5, maximum=14.0, step=0.1,
         placeholder="7.4",
         help="仅当质子化态策略 = ph 时生效。常用：胃酸 1.5 / 溶酶体 4.5 / 生理 7.4。"
              "处理方式：先中和到中性形式，再按内置 pKa 规则表加到目标 pH 的状态"
              "（规则近似，不是 pKa 预测；命中的规则会逐分子记录在报告与产物里）。"),
    Spec("docking.positive_control", "默认阳性对照 SMILES", "docking", "str",
         placeholder="留空 = 不做对照分析",
         help="仅用于预填表单；留空时运行不做阳性对照分析。"),
)

# ---- 运行与性能（映射环境变量，保存后立即生效）----
# ---- 外部工具（用户自行安装；env 映射后 core/ 侧照旧用 env() 读取）----
EXTERNAL_SPECS: Tuple[Spec, ...] = (
    Spec("external.autogrid4_bin", "AutoGrid4 可执行文件", "external", "str",
         env="AUTOGRID4_BIN", placeholder="/path/to/autogrid4",
         help="生成格点能量图（.maps.fld）：经典 AutoDock4 与外部 AutoDock-GPU 都要用；"
              "留空则查找 PATH 上的 autogrid4。"),
    Spec("external.autodock4_bin", "AutoDock4 可执行文件", "external", "str",
         env="AUTODOCK4_BIN", placeholder="/path/to/autodock4",
         help="经典 AutoDock4 CPU 引擎（engine=autodock）与 AutoGrid4 配套；留空则查找 PATH。"),
    Spec("external.vina_bin", "AutoDock Vina CLI 路径（仅登记）", "external", "str",
         env="VINA_BIN", placeholder="/path/to/vina",
         help="只用于登记与探测（设置页「检测」/ doctor / 报告标注），**不改变对接由谁执行**；"
              "留空则自动查找 PATH 上的 vina。要真正改用它跑对接，请填下面的 EXTERNAL_DOCKING_BIN。"),
    Spec("external.docking_bin", "GPU 对接引擎可执行文件", "external", "str",
         env="EXTERNAL_DOCKING_BIN", placeholder="/path/to/unidock",
         help="Vina 兼容的对接工具：识别 Uni-Dock / Vina-GPU / AutoDock-GPU 三类**以及 CPU 版 "
              "AutoDock Vina CLI**。它是 vina 引擎的另一种执行器，不改变打分函数；"
              "留空则用内置 CPU Vina（同版本、支持每分子盒子/分批/取消）。"),
    Spec("external.gpu_device", "GPU 设备序号", "external", "int",
         env="GPU_DEVICE", default=0, minimum=0, maximum=15,
         help="多卡机器指定用哪块卡（对应 CUDA_VISIBLE_DEVICES / OpenCL 设备序号）。"),
    Spec("external.gpu_batch_size", "单批配体数", "external", "int",
         env="GPU_BATCH_SIZE", default=100, minimum=1, maximum=2000,
         help="一次提交给 GPU 引擎的配体数量；显存不足时调小。"),
    Spec("external.p2rank_home", "P2Rank 安装目录", "external", "str",
         env="P2RANK_HOME", placeholder="/path/to/p2rank_2.5.1",
         help="留空则依次查找 assets/tools/p2rank* 与 PATH；缺失时口袋分析退化为内置几何法。"),
    Spec("external.pdb2pqr_bin", "pdb2pqr 可执行文件", "external", "str",
         env="PDB2PQR_BIN", placeholder="/path/to/pdb2pqr",
         help="留空则查找 PATH 与 PyMOL 自带副本；缺失时受体不按目标 pH 重算质子化态。"),
)


RUNTIME_SPECS: Tuple[Spec, ...] = (
    Spec("runtime.pocket_engine", "口袋预测引擎", "runtime", "enum",
         env="POCKET_ENGINE", choices=("auto", "p2rank", "geometric", "known_site"),
         default="auto",
         help="auto = 优先 P2Rank（需本地部署），不可用时回退内置几何法；"
              "known_site = 只用受体已知位点（不跑预测）。"),
    Spec("runtime.pocket_top_n", "保留候选口袋数", "runtime", "int",
         env="POCKET_TOP_N", minimum=1, maximum=50, default=10),
    Spec("runtime.pocket_padding", "口袋转盒子的外扩量（Å）", "runtime", "float",
         env="POCKET_PADDING", minimum=0, maximum=12, step=0.5, default=4.0),
    Spec("runtime.pocket_min_size", "盒边长下限（Å）", "runtime", "float",
         env="POCKET_MIN_SIZE", minimum=8, maximum=40, step=1, default=18.0,
         help="Vina 盒子过小会漏掉结合模式，过大会显著变慢。"),
    Spec("runtime.pocket_max_size", "盒边长上限（Å）", "runtime", "float",
         env="POCKET_MAX_SIZE", minimum=12, maximum=60, step=1, default=30.0),
    Spec("runtime.inchikey_online", "InChIKey 在线反查", "runtime", "bool",
         env="INCHIKEY_ONLINE", default=True,
         help="InChIKey 是单向哈希：on=未收录的键自动去 PubChem 反查（回算校验一致才采用，"
              "并写入本地缓存，之后离线可用）；off=只用内置表与本地缓存。"),
    Spec("runtime.inchikey_timeout", "InChIKey 反查超时（秒）", "runtime", "int",
         env="INCHIKEY_TIMEOUT", minimum=1, maximum=60, step=1, default=8),
    Spec("runtime.box_span_enabled", "库级配体感知下限（C 方案）", "runtime", "bool",
         env="BOX_SPAN_ENABLED", default=True,
         help="on=用整库配体 3D 跨度的 P95+10 Å 抬高盒子下限，避免大配体被欠采样；"
              "off=退回纯口袋驱动的盒子（旧行为）。"),
    Spec("runtime.box_span_sample", "库级下限抽样分子数 K", "runtime", "int",
         env="BOX_SPAN_SAMPLE", minimum=20, maximum=1000, default=200,
         help="按重原子数降序取前 K 个生成 3D 算跨度（库小于 K 时全算）；"
              "避免为全库生成 3D 的成本。"),
    Spec("runtime.box_group_margin", "超限分组判据余量（Å）", "runtime", "float",
         env="BOX_GROUP_MARGIN", minimum=0, maximum=30, step=0.5, default=10.0,
         help="某分子「3D 跨度 + 该值 > 主盒对应边」时划入 box_group=large，"
              "用同一中心的更大盒子单独重跑。"),
    Spec("runtime.box_large_padding", "大配体组盒子余量（Å）", "runtime", "float",
         env="BOX_LARGE_PADDING", minimum=0, maximum=30, step=0.5, default=12.0,
         help="large 组盒子尺寸 = 组内最大 3D 跨度 + 该值（中心与主组完全相同）。"),
    Spec("runtime.agent_funnel_min", "启用两阶段漏斗的分子数阈值", "runtime", "int",
         env="AGENT_FUNNEL_MIN", minimum=0, maximum=100000, default=500,
         help="候选数 ≥ 该值时，应先全库粗筛（低搜索强度）再对头部精算；0=关闭漏斗。"),
    Spec("runtime.agent_refine_top_n", "精算头部条数上限", "runtime", "int",
         env="AGENT_REFINE_TOP_N", minimum=1, maximum=5000, default=200,
         help="漏斗第二阶段只精算亲和力最好的前 N 个（按片调参的粒度）。"),
    Spec("runtime.agent_coarse_exhaustiveness", "粗筛搜索强度", "runtime", "int",
         env="AGENT_COARSE_EXHAUSTIVENESS", minimum=1, maximum=32, default=1),
    Spec("runtime.agent_fine_exhaustiveness", "精算搜索强度", "runtime", "int",
         env="AGENT_FINE_EXHAUSTIVENESS", minimum=1, maximum=64, default=16),
    # ---- 参数自动规划（core/params.py）：运行级/阶段级参数，绝不逐分子 ----
    Spec("runtime.auto_param_enabled", "对接参数自动规划", "runtime", "bool",
         env="AUTO_PARAM_ENABLED", default=True,
         help="on=按任务类型/库柔性/盒体积自动规划 exhaustiveness 与 n_poses 并写进报告；"
              "off=沿用旧默认（exhaustiveness=16 / n_poses=1）。"),
    Spec("runtime.auto_param_pilot", "pilot 预算护栏", "runtime", "bool",
         env="AUTO_PARAM_PILOT", default=True,
         help="on=大库先用最贵的少数分子以 exhaustiveness=1 真实试跑，外推总耗时并在超预算时降级；"
              "pilot 只测量，不影响结果。"),
    Spec("runtime.auto_param_pilot_n", "pilot 试跑分子数", "runtime", "int",
         env="AUTO_PARAM_PILOT_N", minimum=1, maximum=10, default=3),
    Spec("runtime.auto_param_pilot_min", "pilot 触发的最小库规模", "runtime", "int",
         env="AUTO_PARAM_PILOT_MIN", minimum=0, maximum=100000, default=50),
    Spec("runtime.auto_param_budget_ratio", "预算占运行超时比例", "runtime", "float",
         env="AUTO_PARAM_BUDGET_RATIO", minimum=0.05, maximum=1.0, step=0.05, default=0.6,
         help="预算 = 该比例 × RUN_TIMEOUT_SECONDS；超出则按精度优先降级。"),
    # ---- 推荐化合物排行（reporting/recommend.py）----
    Spec("runtime.recommend_top_n", "推荐排行条数", "runtime", "int",
         env="RECOMMEND_TOP_N", minimum=1, maximum=100, default=10,
         help="报告「推荐化合物排行」与 2D 结构图展示前 N 个；理化性质空间图也只画这 N 个"
              "（与排序一致，避免整库散点糊成一团）。"),
    Spec("runtime.rank_weights", "推荐综合分权重", "runtime", "str",
         env="RANK_WEIGHTS", default="0.45,0.20,0.20,0.15",
         placeholder="0.45,0.20,0.20,0.15",
         help="按顺序 = 对接亲和力 / 配体效率 LE / 类药性(Lipinski) / 理化性质窗口(logP,TPSA)，"
              "会自动归一化；也可写 affinity=0.5,le=0.2,lipinski=0.2,physchem=0.1。"
              "非法输入回退默认并在报告里注明。"),
    Spec("runtime.auto_param_exh_min", "exhaustiveness 下限", "runtime", "int",
         env="AUTO_PARAM_EXH_MIN", minimum=1, maximum=32, default=2),
    Spec("runtime.auto_param_exh_max", "exhaustiveness 上限", "runtime", "int",
         env="AUTO_PARAM_EXH_MAX", minimum=2, maximum=128, default=32),
    Spec("runtime.auto_param_base_screening", "筛选基准搜索强度", "runtime", "int",
         env="AUTO_PARAM_BASE_SCREENING", minimum=2, maximum=64, default=16,
         help="自动规划里「筛选任务」的基准强度，默认 16（与表单默认一致）。"),
    Spec("runtime.auto_param_base_binding", "姿态/结合模式基准搜索强度", "runtime", "int",
         env="AUTO_PARAM_BASE_BINDING", minimum=2, maximum=64, default=16),
    Spec("runtime.auto_param_stats_sample", "库统计抽样分子数", "runtime", "int",
         env="AUTO_PARAM_STATS_SAMPLE", minimum=20, maximum=5000, default=500,
         help="按重原子数取前 N 个算可旋转键（2D 描述符），用于库柔性 P90。"),
    Spec("runtime.auto_param_flex_divisor", "柔性系数基准（可旋转键）", "runtime", "float",
         env="AUTO_PARAM_FLEX_DIVISOR", minimum=1, maximum=20, step=0.5, default=5.0),
    Spec("runtime.auto_param_flex_min", "柔性系数下限", "runtime", "float",
         env="AUTO_PARAM_FLEX_MIN", minimum=0.1, maximum=5, step=0.05, default=0.75),
    Spec("runtime.auto_param_flex_max", "柔性系数上限", "runtime", "float",
         env="AUTO_PARAM_FLEX_MAX", minimum=0.5, maximum=10, step=0.1, default=2.5),
    Spec("runtime.auto_param_box_ref", "盒体积系数参考边长（Å）", "runtime", "float",
         env="AUTO_PARAM_BOX_REF", minimum=5, maximum=60, step=1, default=22.0),
    Spec("runtime.auto_param_box_factor_max", "盒体积系数上限", "runtime", "float",
         env="AUTO_PARAM_BOX_FACTOR_MAX", minimum=1, maximum=5, step=0.1, default=2.0),
    Spec("runtime.auto_param_refine_top_min", "超预算时精算头部下限", "runtime", "int",
         env="AUTO_PARAM_REFINE_TOP_MIN", minimum=1, maximum=5000, default=100),
    Spec("runtime.auto_param_refine_top_large", "大库精算头部上调值", "runtime", "int",
         env="AUTO_PARAM_REFINE_TOP_LARGE", minimum=1, maximum=5000, default=300),
    Spec("runtime.auto_param_large_lib", "触发精算头部上调的库规模", "runtime", "int",
         env="AUTO_PARAM_LARGE_LIB", minimum=0, maximum=1000000, default=5000,
         help="库 ≥ 该值且预算允许时，精算头部从 AGENT_REFINE_TOP_N 上调到 "
              "AUTO_PARAM_REFINE_TOP_LARGE。"),
    Spec("runtime.auto_param_n_poses_binding", "姿态分析 n_poses", "runtime", "int",
         env="AUTO_PARAM_N_POSES_BINDING", minimum=1, maximum=20, default=3),
    Spec("runtime.auto_param_n_poses_max", "n_poses 上限", "runtime", "int",
         env="AUTO_PARAM_N_POSES_MAX", minimum=1, maximum=50, default=5),
    Spec("runtime.agent_shard_max", "分片数上限", "runtime", "int",
         env="AGENT_SHARD_MAX", minimum=1, maximum=64, default=8,
         help="按分子数切片调度时的最大片数（防 LLM 开销与上下文随片数膨胀）；"
              "推荐按阶段分片而不是按数量切分。"),
    Spec("runtime.agent_tool_top_n", "回传给模型的明细条数上限", "runtime", "int",
         env="AGENT_TOOL_TOP_N", minimum=1, maximum=500, default=20,
         help="只影响**给模型看的视图大小**（大库时明细落盘、只回传摘要+前 N 条），"
              "不限制对接多少个分子——对接规模由任务与算力决定。"),
    Spec("runtime.docking_workers", "对接并行进程数", "runtime", "int",
         env="DOCKING_WORKERS", minimum=1, maximum=128,
         help="默认 min(CPU//2, 16)；分子数很少时自动串行。"),
    Spec("runtime.vina_cpu", "每个进程的线程数 VINA_CPU", "runtime", "int",
         env="VINA_CPU", minimum=1, maximum=64, default=1),
    Spec("runtime.pose_save_max", "位姿保存上限（全局）", "runtime", "int",
         env="POSE_SAVE_MAX", minimum=0, default=5000),
    Spec("runtime.pose_top_n", "位姿保存前 N 名", "runtime", "int",
         env="POSE_TOP_N", minimum=0, default=200),
    Spec("runtime.pose_artifact_max", "位姿产物登记上限", "runtime", "int",
         env="POSE_ARTIFACT_MAX", minimum=0, default=200),
    Spec("runtime.result_inline_limit", "接口内联明细上限", "runtime", "int",
         env="RESULT_INLINE_LIMIT", minimum=10, default=200),
    Spec("runtime.report_top_n", "报告排序表行数", "runtime", "int",
         env="REPORT_TOP_N", minimum=1, default=50),
    Spec("runtime.chart_top_n", "图表展示前 N 名", "runtime", "int",
         env="CHART_TOP_N", minimum=1, default=20),
    Spec("runtime.chart_bar_max", "直方图/柱图上限", "runtime", "int",
         env="CHART_BAR_MAX", minimum=1, default=50),
    Spec("runtime.sse_batch_size", "实时事件批量大小", "runtime", "int",
         env="SSE_BATCH_SIZE", minimum=1, maximum=500, default=25),
    Spec("runtime.sse_flush_ms", "实时事件刷新间隔（ms）", "runtime", "int",
         env="SSE_FLUSH_MS", minimum=20, maximum=5000, default=200),
    Spec("runtime.sse_progress_ms", "进度心跳间隔（ms）", "runtime", "int",
         env="SSE_PROGRESS_MS", minimum=100, maximum=10000, default=1000),
    # 默认值必须与 runtime/streaming.py 的读取默认值一致（2000），否则「设置页显示的默认」
    # 与「不设置时的实际行为」会不一致（审计发现的漂移点）
    Spec("runtime.stream_tool_result_chars", "工具结果流式截断字符数", "runtime", "int",
         env="STREAM_TOOL_RESULT_CHARS", minimum=200, default=2000),
    Spec("runtime.run_timeout_seconds", "单次运行超时（秒）", "runtime", "int",
         env="RUN_TIMEOUT_SECONDS", minimum=30, default=900),
    # 默认值必须与 `config.DEFAULT_RECURSION_LIMIT` 一致（settings 只依赖 envs.py，
    # 不反向导入 config → 这里写字面量，由 tests/test_recursion_limit.py 看护两者不发散）
    Spec("runtime.recursion_limit", "图递归上限", "runtime", "int",
         env="RECURSION_LIMIT", minimum=10, maximum=500, default=120),
    Spec("runtime.log_level", "日志级别", "runtime", "enum",
         env="LOG_LEVEL", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO"),
    Spec("runtime.checkpoint_backend", "会话检查点后端", "runtime", "enum",
         env="CHECKPOINT_BACKEND", choices=("memory", "sqlite"), default="memory",
         needs_restart=True),
    Spec("runtime.artifact_base_url", "产物下载地址前缀", "runtime", "str",
         env="ARTIFACT_BASE_URL", needs_restart=True,
         placeholder="http://127.0.0.1:<PORT>"),
)

# ---- 部署保护项（只读展示）----
DEPLOY_SPECS: Tuple[Spec, ...] = (
    Spec("deploy.port", "服务端口 PORT", "deploy", "int", env="PORT", env_priority=True,
         default=5000, readonly=True,
         help="端口由启动命令决定（bash start.sh --port N），运行中改不了。"),
    Spec("deploy.docking_max_ligands", "单次对接分子数上限 DOCKING_MAX_LIGANDS", "deploy",
         "int", env="DOCKING_MAX_LIGANDS", env_priority=True, default=0,
         help="0=不限。部署方用 .env 设定后，界面修改不生效（保护机器）。"),
    Spec("deploy.upload_max_mb", "上传大小上限（MB）", "deploy", "int",
         env="UPLOAD_MAX_MB", env_priority=True, default=200),
)

SPECS: Tuple[Spec, ...] = (LLM_SPECS + _role_specs() + DOCKING_SPECS
                           + EXTERNAL_SPECS + RUNTIME_SPECS + DEPLOY_SPECS)
SPEC_BY_PATH = {s.path: s for s in SPECS}
# 运行类字段（写入环境变量）
# 对接默认值里的质子化态策略同样映射环境变量：它既是表单默认值，也是核心配体处理策略。
ENV_SPECS = tuple(s for s in DOCKING_SPECS + EXTERNAL_SPECS + RUNTIME_SPECS
                 + DEPLOY_SPECS if s.env)
DEPLOY_PROTECTED: Tuple[str, ...] = tuple(
    s.env for s in ENV_SPECS if s.env_priority and s.env)


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def local_settings_path() -> Path:
    override = os.getenv(ENV_OVERRIDE)
    return Path(override) if override else project_root() / SETTINGS_REL


def load_local_settings() -> Dict[str, Any]:
    """读取本地设置。

    文件不存在、JSON 损坏、或**结构不符合预期**（例如手改成 `{"llm": 123}`）时
    都退化为 {}，绝不因为一个坏掉的设置文件让服务起不来。
    """
    path = local_settings_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("本地设置文件读取失败(%s)：%s", path, e)
        return {}
    if not isinstance(data, dict):
        logger.warning("本地设置文件格式异常(%s)：顶层不是对象，已忽略", path)
        return {}
    return _sanitize(data, path)


def _sanitize(data: Dict[str, Any], path: Any) -> Dict[str, Any]:
    """逐层校验结构：非字典的中间节点直接丢弃（并告警），避免上游 dict()/get() 抛错。"""
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            logger.warning("设置项 %s 结构异常（应为对象），已忽略：%r", key, value)
            continue
        if key in ("llm", "roles", "docking", "runtime", "deploy"):
            if key == "roles":
                roles: Dict[str, Any] = {}
                for role, fields in value.items():
                    if isinstance(fields, dict):
                        roles[role] = fields
                    else:
                        logger.warning("设置项 roles.%s 结构异常，已忽略：%r", role, fields)
                out[key] = roles
            else:
                out[key] = value
        else:
            out[key] = value
    return out


def save_local_settings(data: Dict[str, Any]) -> Path:
    path = local_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2,
                              sort_keys=True) + "\n", encoding="utf-8")
    return path


def clear_local_settings() -> bool:
    path = local_settings_path()
    if path.is_file():
        path.unlink()
        return True
    return False


def get_dotted(data: Dict[str, Any], path: str, default: Any = None) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_dotted(data: Dict[str, Any], path: str, value: Any) -> None:
    """写入点分路径；value 为 None 时保留 None（由 merge_settings 解释为删除）。"""
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def delete_dotted(data: Dict[str, Any], path: str) -> None:
    parts = path.split(".")
    node: Any = data
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return
        node = node[part]
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def prune_empty(data: Dict[str, Any]) -> Dict[str, Any]:
    """去掉空字典/空字符串，避免设置文件里堆一堆无意义的空值。"""
    def _clean(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: _clean(v) for k, v in node.items()}
            return {k: v for k, v in out.items() if v not in ({}, None, "")}
        return node

    cleaned = _clean(data)
    return cleaned if isinstance(cleaned, dict) else {}


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
class SettingsError(ValueError):
    """设置校验失败：携带逐字段错误。"""

    def __init__(self, errors: List[Dict[str, str]]):
        super().__init__("；".join(f"{e['path']}: {e['message']}" for e in errors))
        self.errors = errors


def _coerce(spec: Spec, raw: Any) -> Any:
    """按规格转换并校验单个值；None/空字符串 = 清除（回到继承/默认）。"""
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip() == "" and spec.kind != "json":
        return None
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise SettingsError([{"path": spec.path, "message": "需要布尔值（true/false）"}])
    if spec.kind == "int":
        try:
            value: Any = int(float(raw))
        except (TypeError, ValueError):
            raise SettingsError([{"path": spec.path, "message": f"需要整数，收到 {raw!r}"}]) from None
    elif spec.kind == "float":
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise SettingsError([{"path": spec.path, "message": f"需要数字，收到 {raw!r}"}]) from None
    elif spec.kind == "json":
        if isinstance(raw, (dict, list)):
            value = raw
        else:
            text = str(raw).strip()
            if text == "":
                return None
            try:
                value = json.loads(text)
            except json.JSONDecodeError as e:
                raise SettingsError([{"path": spec.path, "message": f"不是合法 JSON：{e}"}]) from None
        if not isinstance(value, dict):
            raise SettingsError([{"path": spec.path, "message": "需要 JSON 对象（{...}）"}])
    elif spec.kind == "enum":
        value = str(raw).strip()
        allowed = [str(c) for c in spec.choices]
        if value not in allowed:
            raise SettingsError([{"path": spec.path,
                                  "message": f"取值必须是 {'/'.join(allowed)} 之一"}])
    else:  # str / secret
        value = str(raw).strip()
    if spec.minimum is not None and isinstance(value, (int, float)) and value < spec.minimum:
        raise SettingsError([{"path": spec.path, "message": f"不得小于 {spec.minimum}"}])
    if spec.maximum is not None and isinstance(value, (int, float)) and value > spec.maximum:
        raise SettingsError([{"path": spec.path, "message": f"不得大于 {spec.maximum}"}])
    return value


def _flatten_input(data: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """把提交内容摊平成 {路径: 值}；json 类字段（extra_body）整体视作叶子。"""
    out: Dict[str, Any] = {}
    for key, value in (data or {}).items():
        path = f"{prefix}.{key}" if prefix else str(key)
        spec = SPEC_BY_PATH.get(path)
        if isinstance(value, dict) and not (spec is not None and spec.kind == "json"):
            out.update(_flatten_input(value, path))
        else:
            out[path] = value
    return out


def reject_readonly(updates: Dict[str, Any]) -> List[Dict[str, str]]:
    """只读项（如 PORT）不接受界面写入。"""
    errors: List[Dict[str, str]] = []
    for path in _flatten_input(updates):
        spec = SPEC_BY_PATH.get(path)
        if spec is not None and spec.readonly:
            errors.append({"path": path, "message": "该项由启动参数决定，界面不可修改"})
    return errors


_SECRET_KEY_RE = None


def _looks_secret(key: str) -> bool:
    global _SECRET_KEY_RE
    if _SECRET_KEY_RE is None:
        import re as _re

        _SECRET_KEY_RE = _re.compile(r"(?i)key|token|secret|auth|password|credential")
    return bool(_SECRET_KEY_RE.search(str(key)))


def mask_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    """额外请求头里的敏感值（*key* / *token* / *auth* …）只回显 ***。"""
    return {str(k): ("***" if _looks_secret(k) and v not in (None, "") else v)
            for k, v in (headers or {}).items()}


def unmask_headers(submitted: Any, current: Any) -> Dict[str, Any]:
    """提交里值为 *** 的头保持原值（前端看到的掩码不是真值）。"""
    out = dict(submitted) if isinstance(submitted, dict) else {}
    stored = current if isinstance(current, dict) else {}
    for key, value in list(out.items()):
        if value == "***" and key in stored:
            out[key] = stored[key]
    return out


def normalize_updates(updates: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """把前端提交的内容规整成可写入的嵌套结构。

    既接受扁平写法 `{"llm.model": "x"}`，也接受嵌套写法 `{"llm": {"model": "x"}}`。
    """
    if not isinstance(updates, dict):
        return {}, [{"path": "-", "message": "请求体需要 JSON 对象"}]
    clean: Dict[str, Any] = {}
    errors: List[Dict[str, str]] = []
    for path, raw in _flatten_input(updates).items():
        spec = SPEC_BY_PATH.get(path)
        if spec is None:
            errors.append({"path": path, "message": "未知配置项"})
            continue
        try:
            value = _coerce(spec, raw)
        except SettingsError as e:
            errors.extend(e.errors)
            continue
        set_dotted(clean, path, value)
    return clean, errors


def merge_settings(current: Dict[str, Any], clean: Dict[str, Any]) -> Dict[str, Any]:
    """把规整后的更新合并进现有设置（值为 None = 清除该项，回到继承/默认）。"""
    merged = json.loads(json.dumps(current)) if current else {}
    for path, value in flatten_paths(clean).items():
        if value is None:
            delete_dotted(merged, path)
        else:
            set_dotted(merged, path, value)
    return prune_empty(merged)


def flatten_paths(data: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """把嵌套设置摊平成 {点分路径: 值}。"""
    out: Dict[str, Any] = {}
    for key, value in (data or {}).items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten_paths(value, path))
        else:
            out[path] = value
    return out


# --------------------------------------------------------------------------- #
# 生效：运行类字段 → 环境变量
# --------------------------------------------------------------------------- #
def _serialize(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# 本进程「由设置页面注入」的环境变量基线：{键: 注入前的值（None = 之前不存在）}。
# 用于正确撤销：用户清空某项时恢复到基线，而不是让上一轮注入的值残留；
# 同时保证 reset 不会误删 shell / CI 外部注入的环境变量。
_INJECTED: Dict[str, Optional[str]] = {}


def apply_runtime_env() -> List[str]:
    """把界面设置里的运行类字段写入进程环境变量，返回被应用的键。

    - 一般项：界面设置覆盖 .env（用户刚在界面改的值应当生效）；
    - 部署保护项：环境变量已显式设置时保持环境变量优先；
    - 被清除的项：恢复到注入前的基线值（而不是残留旧值）。
    """
    from docking_agent.envs import load_env

    load_env()
    data = load_local_settings()
    applied: List[str] = []
    for spec in ENV_SPECS:
        assert spec.env
        name = spec.env
        value = get_dotted(data, spec.path)
        protect = spec.env_priority and os.getenv(name) not in (None, "")
        if value is None or protect:
            # 不再由界面提供该值：若之前是我们注入的，恢复基线
            if name in _INJECTED:
                baseline = _INJECTED.pop(name)
                if baseline is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = baseline
            continue
        if name not in _INJECTED:
            _INJECTED[name] = os.environ.get(name)   # 记录基线，供后续撤销
        try:
            os.environ[name] = _serialize(value)
            applied.append(name)
        except Exception as e:  # noqa: BLE001
            logger.warning("应用设置 %s 失败：%s", spec.path, e)
    return applied


def injected_env_keys() -> List[str]:
    """当前由设置页面注入的环境变量（reset 时只回滚这些）。"""
    return sorted(_INJECTED)


def runtime_effective(spec: Spec) -> Any:
    """运行类字段的当前生效值（环境变量优先规则与 apply_runtime_env 一致）。"""
    from docking_agent.envs import env

    data = load_local_settings()
    value = get_dotted(data, spec.path)
    env_value = env(spec.env) if spec.env else None
    if spec.env_priority and env_value not in (None, ""):
        return env_value
    if value is not None:
        return value
    if env_value not in (None, ""):
        return env_value
    return spec.default


def runtime_source(spec: Spec) -> str:
    """该运行类字段生效值的来源（区分「界面设置」与「.env / 外部环境变量」）。"""
    from docking_agent.envs import env

    data = load_local_settings()
    if spec.env_priority and spec.env and env(spec.env) not in (None, ""):
        return f"环境变量 {spec.env}"
    if get_dotted(data, spec.path) is not None:
        return "界面设置"
    if spec.env and env(spec.env) not in (None, ""):
        if spec.env in _INJECTED:
            return "界面设置"
        return f"环境变量 {spec.env}"
    return "内置默认"


def settings_meta() -> Dict[str, Any]:
    from docking_agent.envs import load_env
    from docking_agent.paths import project_root as _root

    load_env()
    path = local_settings_path()
    return {
        "path": str(path),
        "relative": SETTINGS_REL,
        "exists": path.is_file(),
        "env_file": (_root() / ".env").is_file(),
        "env_override": os.getenv(ENV_OVERRIDE, ""),
    }


def spec_to_dict(spec: Spec) -> Dict[str, Any]:
    return {
        "path": spec.path,
        "label": spec.label,
        "group": spec.group,
        "kind": spec.kind,
        "default": spec.default,
        "choices": list(spec.choices),
        "min": spec.minimum,
        "max": spec.maximum,
        "step": spec.step,
        "env": spec.env,
        "env_priority": spec.env_priority,
        "needs_restart": spec.needs_restart,
        "readonly": spec.readonly,
        "role": spec.role,
        "role_label": ROLE_LABEL.get(spec.role, ""),
        "field": spec.field,
        "help": spec.help,
        "placeholder": spec.placeholder,
    }
