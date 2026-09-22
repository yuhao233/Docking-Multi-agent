"""共享黑板（blackboard）：多 Agent 协作的公共工作区。

为什么需要它：原先子 Agent 之间完全隔离，所有中间结果只能靠「把 JSON 塞进消息字符串」
在协调 Agent 那里中转——既浪费 token，也让子 Agent 无法互相协作（例如 Docking Agent
拿不到属性 Agent 规范化后的分子表，Binding Agent 拿不到对接结果）。

黑板提供一份**运行级、线程安全**的共享状态，任何 Agent/工具都可以读写：

    receptor/site      受体与已知位点
    molecules          规范化去重后的分子库（smiles -> {name, smiles}）
    properties         理化性质（smiles -> {...}）
    docking            对接结果（smiles -> {...}）
    binding            结合模式结果（smiles -> {...}）
    notes              协作备注（各 Agent 的修正与判断）

实现为 ContextVar，由 API 层在每次运行时注入，工具函数用 `current_blackboard.get()` 取用。

**模块位置**：本模块属 `runtime/` 层（跨 Agent 的运行级服务），原先在 `agents/` 下 ——
那会让最被依赖的 `runtime.context` 反向 import `agents`（审计 V3）。
"""
from __future__ import annotations

import logging
import threading
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from rdkit import Chem

logger = logging.getLogger(__name__)


def canonical_key(smiles: str) -> str:
    """分子身份键：SMILES → 规范形式（**同一个物质的任何写法都必须落成同一个键**）。

    真实缺陷（2026-09-22 用户实测）：一次运行只给了 1 个分子，却对接出 2 行 ——
    `add_molecules` 用**规范** SMILES 做键，而 `set_properties` / `set_docking` / `set_binding`
    用**原始** SMILES 做键。同一物质（PubChem 原始写法 `S=C([S-])NCC…` 与用户点选的
    `C(CNC(=S)[S-])…`，canonical/InChIKey 完全相同）因此各占一个键，黑板里出现两个「分子」，
    对接与排行也就出现两行。
    无法解析时退回原文（保持可追溯，不丢数据）。
    """
    text = str(smiles or "").strip()
    if not text:
        return ""
    mol = Chem.MolFromSmiles(text)
    if mol is None:
        return text
    try:
        return Chem.MolToSmiles(mol) or text
    except Exception:  # noqa: BLE001 - 特殊价态/金属：退回原文
        return text


#: 黑板在 store 里的命名空间前缀与字段键（**按字段存**：不同字段并发写不会互相覆盖）
STORE_NS_PREFIX = "blackboard"
_STORE_FIELDS = ("receptor", "site", "molecules", "properties", "docking", "binding",
                 "positive_control", "pockets", "pocket_engine", "site_pinned", "notes")


def dedupe_molecules(molecules: List[Dict[str, Any]]) -> "tuple[List[Dict[str, Any]], int]":
    """按**化学身份**去重（保持输入顺序）：返回 `(去重后的清单, 去掉的条数)`。

    与 `Blackboard.add_molecules` 用同一个 `canonical_key`：同一物质的两种 SMILES 写法
    （PubChem 原始写法 / 用户点选写法 / 大小写与原子顺序差异）只保留第一条。
    """
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for m in molecules or []:
        key = canonical_key(str((m or {}).get("smiles") or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out, len(list(molecules or [])) - len(out)


class Blackboard:
    """运行级共享工作区（线程安全）。

    **两种后端**（P2-c：接入 LangGraph `store`）：

    | 后端 | 何时使用 | 语义 |
    | --- | --- | --- |
    | 进程内对象（默认，`store=None`） | CLI / 单测 / 不经图的调用 | 与历史行为完全一致（RLock 保护） |
    | LangGraph `store`（`store=...`） | 图调用链路（父图与 4 个子 Agent 共享同一个 store） | 同一 run 的黑板在**同一命名空间**下；与 checkpointer 对齐、平台可见、跨进程可读 |

    为了不丢更新，store 后端**按字段写**（每个字段一个 key），而不是整档覆写；
    同一字段的并发写为 last-write-wins（与进程内语义一致）。本地 `_cache` 仍是热路径。
    """

    def __init__(self, run_id: str = "", store: Any = None):
        self.run_id = run_id
        self.store = store
        self._ns = (STORE_NS_PREFIX, run_id or "default")
        self._lock = threading.RLock()
        self.receptor: Optional[Dict[str, Any]] = None
        self.site: Optional[Dict[str, Any]] = None
        self._molecules: Dict[str, Dict[str, Any]] = {}
        self._properties: Dict[str, Dict[str, Any]] = {}
        self._docking: Dict[str, Dict[str, Any]] = {}
        self._binding: Dict[str, Dict[str, Any]] = {}
        self.positive_control: str = ""      # 阳性对照 SMILES（交叉核验需要它来判定「强对接」）
        # 口袋分析 Agent 的预测结果与「选定的对接盒」；_site_pinned 表示 site 由 Agent 明确选择，
        # 后续 set_receptor 不得覆盖（横向协作交接的语义保证）
        self._pockets: List[Dict[str, Any]] = []
        self.pocket_engine: str = ""
        self._site_pinned: bool = False
        self.notes: List[str] = []
        # 接入 store 时先做一次 hydrate：同一 run 的第二个视图（另一个进程/子图）立刻能看到已有状态
        if self.store is not None:
            self._hydrate()

    # ---------------- store 直通（按字段） ----------------
    def _mirror(self, field: str, value: Any) -> None:
        """把某个字段写进 store（调用方已持锁）。"""
        if self.store is None:
            return
        try:
            self.store.put(self._ns, field, {"value": value})
        except Exception:  # noqa: BLE001 - 黑板是协作加速器，store 写失败不影响本次计算
            logger.warning("黑板字段 %s 写入 store 失败（忽略）", field, exc_info=True)

    def _mirror_all(self) -> None:
        self._mirror("receptor", self.receptor)
        self._mirror("site", self.site)
        self._mirror("molecules", self._molecules)
        self._mirror("properties", self._properties)
        self._mirror("docking", self._docking)
        self._mirror("binding", self._binding)
        self._mirror("positive_control", self.positive_control)
        self._mirror("pockets", self._pockets)
        self._mirror("pocket_engine", self.pocket_engine)
        self._mirror("site_pinned", self._site_pinned)
        self._mirror("notes", self.notes)

    def _read_field(self, field: str, default: Any) -> Any:
        if self.store is None:
            return default
        try:
            item = self.store.get(self._ns, field)
        except Exception:  # noqa: BLE001
            return default
        if item is None:
            return default
        value = getattr(item, "value", None)
        return value.get("value", default) if isinstance(value, dict) else default

    def _hydrate(self) -> None:
        """从 store 读取已有状态（构造时调用一次）；本地已有的非空值不覆盖。"""
        with self._lock:
            mapping = {
                "receptor": "receptor", "site": "site", "molecules": "_molecules",
                "properties": "_properties", "docking": "_docking", "binding": "_binding",
                "positive_control": "positive_control", "pockets": "_pockets",
                "pocket_engine": "pocket_engine", "site_pinned": "_site_pinned", "notes": "notes",
            }
            for field, attr in mapping.items():
                current = getattr(self, attr)
                if current not in (None, "", [], {}):
                    continue
                value = self._read_field(field, None)
                if value not in (None, "", [], {}):
                    setattr(self, attr, value)

    # ---------------- 受体 ----------------
    def set_receptor(self, spec: Dict[str, Any]) -> None:
        with self._lock:
            self.receptor = {
                "key": spec.get("key"), "name": spec.get("name"),
                "pdb": spec.get("pdb"), "protein": spec.get("protein"),
                "pdbqt": spec.get("pdbqt"),
            }
            if not self._site_pinned:
                self.site = {"center": list(spec.get("center") or []),
                             "size": list(spec.get("size") or []),
                             "source": (spec.get("site") or {}).get("source", "")}
            self._mirror("receptor", self.receptor)
            self._mirror("site", self.site)

    def get_receptor(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self.receptor) if self.receptor else None

    def get_site(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self.site) if self.site else None

    def set_site(self, *, center: List[float], size: Optional[List[float]] = None,
                 source: str = "", chosen_by: str = "pocket_agent",
                 pocket: Optional[Dict[str, Any]] = None,
                 validation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """口袋分析 Agent 选定对接盒（提交给 Docking Agent）。

        置 _site_pinned 后，后续 set_receptor 不再用受体默认位点覆盖它 —— 这就是
        「口袋 Agent 决定盒子 → Docking Agent 按此对接」的交接语义。
        """
        with self._lock:
            self.site = {
                "center": [float(c) for c in center],
                "size": [float(s) for s in (size or [22.0, 22.0, 22.0])],
                "source": source or "口袋分析 Agent 选定",
                "chosen_by": chosen_by,
                "pocket": pocket or {},
                "validation": validation or {},
            }
            self._site_pinned = True
            self._mirror("site", self.site)
            self._mirror("site_pinned", True)
            return dict(self.site)

    def set_pockets(self, pockets: List[Dict[str, Any]], *, engine: str = "",
                    receptor_key: str = "") -> None:
        with self._lock:
            self._pockets = [dict(p) for p in (pockets or [])]
            self.pocket_engine = engine or self.pocket_engine
            if receptor_key:
                for pocket in self._pockets:
                    pocket.setdefault("receptor_key", receptor_key)
            self._mirror("pockets", self._pockets)
            self._mirror("pocket_engine", self.pocket_engine)

    def get_pockets(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(p) for p in self._pockets]

    # ---------------- 分子库（规范化 + 去重）----------------
    def add_molecules(self, molecules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """加入分子（自动规范化 SMILES 并去重），返回本次新增的部分。"""
        added: List[Dict[str, Any]] = []
        with self._lock:
            for m in molecules or []:
                smiles = str((m or {}).get("smiles") or "").strip()
                if not smiles:
                    continue
                mol = Chem.MolFromSmiles(smiles)
                if mol is None:
                    self.notes.append(f"跳过无法解析的 SMILES：{smiles[:40]}")
                    continue
                canonical = canonical_key(smiles)
                if canonical in self._molecules:
                    continue
                entry = {"name": (m.get("name") or canonical), "smiles": canonical}
                self._molecules[canonical] = entry
                added.append(entry)
            self._mirror("molecules", self._molecules)
            self._mirror("notes", self.notes)
        return added

    def molecules(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._molecules.values()]

    def smiles_list(self) -> List[str]:
        with self._lock:
            return list(self._molecules.keys())

    # ---------------- 中间结果 ----------------
    def set_properties(self, properties: List[Dict[str, Any]]) -> None:
        with self._lock:
            for p in properties or []:
                s = str((p or {}).get("smiles") or "")
                if not s:
                    continue
                key = canonical_key(s)
                self._properties[key] = p
                if key not in self._molecules:
                    self._molecules[key] = {"name": p.get("name") or s, "smiles": s}
            self._mirror("properties", self._properties)
            self._mirror("molecules", self._molecules)

    def properties(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._properties.values()]

    def get_property(self, smiles: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            key = canonical_key(smiles)
            if key in self._properties:
                return dict(self._properties[key])
            # 兜底：调用方可能给的是原始写法而键是规范写法（或反之）
            return dict(self._properties[smiles]) if smiles in self._properties else None

    def set_docking(self, results: List[Dict[str, Any]], receptor: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            if receptor:
                self.set_receptor(receptor)
            for r in results or []:
                s = str((r or {}).get("smiles") or "")
                if s:
                    self._docking[canonical_key(s)] = r
            self._mirror("docking", self._docking)

    def docking(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._docking.values()]

    def set_binding(self, rows: List[Dict[str, Any]]) -> None:
        with self._lock:
            for r in rows or []:
                s = str((r or {}).get("smiles") or "")
                if s:
                    self._binding[canonical_key(s)] = r
            self._mirror("binding", self._binding)

    def binding(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._binding.values()]

    def set_positive_control(self, smiles: str) -> None:
        if smiles:
            with self._lock:
                self.positive_control = str(smiles).strip()
                self._mirror("positive_control", self.positive_control)

    def add_note(self, message: str) -> None:
        if message:
            with self._lock:
                self.notes.append(str(message))
                self._mirror("notes", self.notes)

    # ---------------- 视图 ----------------
    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"molecules": len(self._molecules), "properties": len(self._properties),
                    "docking": len(self._docking), "binding": len(self._binding),
                    "pockets": len(self._pockets)}

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"run_id": self.run_id, "receptor": self.receptor, "site": self.site,
                    "pockets": self._pockets[:20], "pocket_engine": self.pocket_engine,
                    "positive_control": self.positive_control,
                    "molecule_count": len(self._molecules),
                    "molecules": [dict(v) for v in self._molecules.values()][:50],
                    "properties": [dict(v) for v in self._properties.values()][:50],
                    "docking": [dict(v) for v in self._docking.values()][:50],
                    "binding": [dict(v) for v in self._binding.values()][:50],
                    "notes": list(self.notes)}


# 当前运行的共享黑板（由 API 层注入）
current_blackboard: ContextVar[Optional[Blackboard]] = ContextVar("current_blackboard", default=None)


def get_blackboard() -> Optional[Blackboard]:
    return current_blackboard.get()


# --------------------------------------------------------------------------- #
# LangGraph store 后端（P2-c）：父图与 4 个子 Agent 共享同一个 store 实例
# --------------------------------------------------------------------------- #
_shared_store: Any = None
_store_boards: Dict[tuple, Blackboard] = {}

#: 视图缓存上限（护栏）：正常运行会在每个 run 结束时 `forget_store_blackboard()`，
#: 这里是兜底 —— 任何漏掉的路径（例如 CLI 直调 `active_blackboard()`）也不会让
#: 缓存无限增长（FIFO 淘汰最旧视图）。
_STORE_BOARDS_MAX = 32


def shared_store() -> Any:
    """进程内共享的 `InMemoryStore`（图构建时传给 create_agent 的 `store=`）。

    为什么用单例：协调 Agent 与 4 个子 Agent 是**分别编译的图**，只有共享同一个 store
    实例，子 Agent 写进黑板的受体/位点盒/分子库才能被父图与其它子 Agent 看到。
    """
    global _shared_store
    if _shared_store is None:
        from langgraph.store.memory import InMemoryStore  # 延迟导入

        _shared_store = InMemoryStore()
        logger.info("黑板已接入 LangGraph store（InMemoryStore 单例）")
    return _shared_store


def store_blackboard(store: Any, run_id: str = "") -> Blackboard:
    """取某个 (store, run) 的黑板视图（同一 run 反复取到同一个对象）。"""
    key = (id(store), run_id or "default")
    board = _store_boards.get(key)
    if board is None:
        board = Blackboard(run_id, store=store)
        _store_boards[key] = board
        while len(_store_boards) > _STORE_BOARDS_MAX:
            _store_boards.pop(next(iter(_store_boards)))
    return board


def forget_store_blackboard(run_id: str) -> int:
    """运行结束时丢弃该 run 的 store 黑板视图，返回移除条数。

    为什么必须显式丢弃：`_store_boards` 以 `(id(store), run_id)` 为键缓存视图，
    视图持有该 run 的分子/性质/对接等运行级状态；服务长时间运行时每个 run 都会留下
    一条，属于**真实的进程内泄漏**（审计 §2.1）。运行收尾处（API/标准面/兼容面）
    都应调用本函数；`_STORE_BOARDS_MAX` 只是兜底护栏。
    """
    target = run_id or "default"
    keys = [k for k in _store_boards if k[1] == target]
    for key in keys:
        _store_boards.pop(key, None)
    return len(keys)


def reset_store_blackboards() -> None:
    """清空全部 store 视图缓存（测试 / 配置热更新用）。"""
    _store_boards.clear()


def board_molecules_json(fallback_json: str = "") -> str:
    """工具用：优先用黑板里的分子库（规范化去重过），否则回退到调用方传入的 JSON。"""
    board = get_blackboard()
    if board is not None:
        molecules = board.molecules()
        if molecules:
            import json

            return json.dumps(molecules, ensure_ascii=False)
    return fallback_json
