"""API 请求模型（与 docs/api.md 契约一致）。"""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field



class PipelineRequest(BaseModel):
    receptor: Optional[Any] = Field(default="thrombin", description="受体：预置 key / PDB 路径 / URL / 多受体数组")
    receptor_file: str = Field(default="", description="上传/提供的蛋白质受体文件路径（.pdb/.pdbqt，优先于 receptor）")
    site_center: Optional[List[float]] = None
    site_size: Optional[List[float]] = None
    ligands_text: str = ""
    molecule_file: str = ""
    # 默认**不**使用内置示例库：只有调用方明确要求（界面「示例库」来源 / 用户明确要求）才置 True。
    allow_example_fallback: bool = False
    positive_control: str = ""
    exhaustiveness: Optional[int] = Field(
        default=None, description="留空(null)=按任务/库/盒自动规划；给出数值=用户显式指定，不自动改")
    n_poses: Optional[int] = Field(
        default=None, description="留空(null)=自动（筛选 1 / 姿态分析 3）；给出数值=用户显式指定")
    engine: str = "vina"
    pocket_engine: str = Field(default="", description="结合位点来源引擎：''(默认 auto)/auto/p2rank/geometric/known_site")
    save_poses: bool = True
    max_ligands: int = 0
    protonation: str = Field(
        default="",
        description="质子化态策略：''=用设置页的值（默认 ph，目标 pH 7.4）/ ph=按目标 pH 分配 / "
                    "neutralize=仅中和带净电荷的分子 / "
                    "ph=按目标 pH 分配质子化态 / keep=保持输入")
    protonation_ph: float = Field(
        default=0.0,
        description="目标 pH（仅 protonation='ph' 时有意义；0=用设置页的值，默认 7.4；有效 0.5–14）")
    dock_positive_control: bool = True
    skip_positive_control: bool = Field(default=False, description="true 则跳过阳性对照（不做结合模式比较）")
    conversation_id: str = Field(
        default="", description="会话 id：同一 id = 同一段对话（流水线无多轮记忆，仅作记录）")


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
    receptor: Optional[Any] = "thrombin"
    ligands_text: str = ""
    molecule_file: str = ""
    positive_control: str = ""
    # 「留空 = 自动」：客户端没改动就不发这些字段，服务端按自动规划/系统默认处理；
    # 一旦给了数值即视为用户显式指定（不再被自动规划改写）。实测背景：界面上打开过
    # 「高级设置」又关掉时，旧实现会把这些默认值当成用户参数下发。
    exhaustiveness: Optional[int] = Field(
        default=None,
        description="搜索强度；留空(None)=自动规划（界面勾「自动」时不发本字段），给值=用户显式指定")
    engine: str = Field(default="", description="对接引擎；留空=跟随系统默认（vina）/ auto / autodock")
    pocket_engine: str = Field(default="", description="结合位点来源引擎：''(默认 auto)/auto/p2rank/geometric/known_site")
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


