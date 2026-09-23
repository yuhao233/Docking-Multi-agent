"""API 请求模型（与 docs/api.md 契约一致）。"""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field

class AgentRequest(BaseModel):
    """多 Agent 请求。

    mode 决定「自然语言指令」与「表单参数」谁是权威，避免二者冲突：
      - manual（默认）：表单参数权威，message 仅作为目标描述/额外要求；
      - chat + advanced=False：纯对话，完全按指令执行，不注入任何参数；
      - chat + advanced=True：对话为主，参数作为「仅当指令未指定时生效」的默认值。
    """

    message: str = ""
    receptor_file: str = Field(default="", description="上传/提供的蛋白质受体文件路径（.pdb/.pdbqt，优先于 receptor）")
    mode: str = Field(default="manual", description="chat | manual")
    advanced: bool = Field(default=False, description="chat 模式下是否启用高级设置（参数作为默认值）")
    receptor: Optional[Any] = Field(
        default=None,
        description=("受体来源：PDB 编号 / UniProt accession / 基因或蛋白名称 / 结构文件路径。"
                     "**没有系统默认受体** —— 留空(None/\"\")即「用户未指定」，"
                     "受理层会据此只提问、不执行任何计算（预置受体仅内部测试用）。"))
    ligands_text: str = ""
    molecule_file: str = ""
    molecule_choice: str = Field(
        default="",
        description=("用户在界面**点选的代表结构**（多组分/配位聚合物时系统会下发 4 个选项）的 SMILES。"
                     "与 `positive_control` 一样属于「明确的用户决定」：受理层直接按它导入分子库，"
                     "不再按名称重新查询、也不再询问代表结构。"))
    molecule_choice_decision: str = Field(
        default="",
        description=("该代表结构的取法：`raw-mixture`（PubChem 原始多组分，金属与片段原样保留）/ "
                     "`metal-monomer`（金属-EBDC 单体）/ `organic-fragment`（最大有机片段、无金属）/ `custom`。"
                     "仅用于报告与运行记录追溯，不影响解析。"))
    molecule_choice_label: str = Field(
        default="",
        description="选项展示名（如「Mancozeb（CID 3034368）· PubChem 原始多组分结构」），写进报告与运行笔记。")
    positive_control: str = ""
    positive_control_decision: str = Field(
        default="",
        description=("共晶配体是否用作阳性对照的用户决定：use=用作对照"
                     "（此时 positive_control 为该配体的 SMILES）/ skip=不使用 / 空=未询问"))
    exhaustiveness: Optional[int] = Field(
        default=None,
        description="搜索强度；留空(None)=自动规划（界面勾「自动」时不发本字段），给值=用户显式指定")
    engine: str = Field(
        default="",
        description=("对接引擎；留空=跟随设置页「对接引擎（默认）」（出厂 auto：优先 Vina，"
                     "不可用时回退 AutoDock4 CPU）。可显式给 auto / vina / autodock / external"
                     "（external=设置页登记的外部引擎，如 AutoDock-GPU；未登记或未就绪会直接报错）。"))
    pocket_engine: str = Field(default="", description="结合位点来源引擎：''(默认 auto)/auto/p2rank/geometric/known_site")
    resume_run_id: str = Field(
        default="",
        description=("点选候选后**续跑同一个运行**：填该运行 id，服务端复用它（不新建 run），"
                     "把 message 作为答案注入线程并从 checkpoint 继续；留空=新运行"))
    resume_choice_kind: str = Field(
        default="", description="被回答的候选类型（如 positive_control）；续跑时只清这一问")
    site_center: Optional[List[float]] = None
    site_size: Optional[List[float]] = None
    n_poses: Optional[int] = Field(default=None, description="输出位姿数；留空=按任务默认（筛选 1 / 姿态 3）")
    max_ligands: Optional[int] = Field(default=None, description="最大分子数；留空=不限")
    save_poses: Optional[bool] = Field(default=None, description="是否保存位姿；留空=保存（系统默认）")
    protonation: str = Field(
        default="",
        description="质子化态策略：''=用设置页的值（默认 ph，目标 pH 7.4）/ ph=按目标 pH 分配 / "
                    "neutralize=仅中和带净电荷的分子 / "
                    "ph=按目标 pH 分配质子化态 / keep=保持输入")
    protonation_ph: float = Field(
        default=0.0,
        description="目标 pH（仅 protonation='ph' 时有意义；0=用设置页的值，默认 7.4；有效 0.5–14）")
    skip_positive_control: bool = Field(default=False, description="true 则跳过阳性对照（不做结合模式比较）")
    conversation_id: str = Field(
        default="",
        description="会话 id：同一 id 视为同一段对话（服务端据此延续上一轮上下文；留空 = 每次独立）")


