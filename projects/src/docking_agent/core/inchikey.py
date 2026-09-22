"""InChIKey → 结构：内置表 + 本地缓存 + PubChem 在线反查（可关）。

## 为什么需要这一层

InChIKey 是**单向哈希**，离线无法反解。用户上传的表格里带 InChIKey 列时，旧实现只认内置的
22 个常见化合物，其余整行跳过 —— 用户看到的就是「解析不出来」。这里补两件事：

1. **在线反查**：PubChem PUG REST 支持按 InChIKey 精确检索（`compound/inchikey/<key>/...`），
   拿到 SMILES 后**必须回算 InChIKey 与查询值逐字比对**才接受（防止命中错误/数据不一致）；
2. **本地缓存**：命中过的键落盘到 `assets/cache/inchikey/<KEY>.json`，之后完全离线可用。

## 边界与诚实性

- 网络默认开（`INCHIKEY_ONLINE=on`），可关；单键超时 `INCHIKEY_TIMEOUT`（默认 8 s）。
- 失败**绝不编造**：返回空并给出可操作原因（网络不可用 / 未收录 / 校验不一致）。
- 结构来源（builtin / cache / pubchem）全程可查：`lookup_source()` 与 `resolutions()`。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from docking_agent.config import env_bool, env_int
from docking_agent.paths import cache_dir

logger = logging.getLogger(__name__)

PUBCHEM_INCHIKEY = ("https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/inchikey/{}/property/"
                    "{}/JSON")
#: PubChem 不同版本返回的属性名不一样，按「信息量」从多到少取第一个可用的
_SMILES_KEYS = ("IsomericSMILES", "SMILES", "CanonicalSMILES", "ConnectivitySMILES")
_USER_AGENT = "docking-agent/0.16 (+local inchikey resolve)"

#: 本次进程内「这个键的结构从哪来」的记录（builtin / cache / pubchem），供溯源落盘使用
_resolutions: Dict[str, str] = {}
#: 失败原因（网络不可达 / 未收录 / 校验不一致…）：上层要把它写进「跳过原因」，不能只说"解析不出"
_errors: Dict[str, str] = {}
_resolutions_lock = threading.Lock()


def online_enabled() -> bool:
    """是否允许联网反查（设置页面/环境变量可关；测试默认关）。"""
    return env_bool("INCHIKEY_ONLINE", True)


def _timeout() -> int:
    return max(1, min(60, env_int("INCHIKEY_TIMEOUT", 8)))


def cache_file(key: str) -> Path:
    return cache_dir() / "inchikey" / f"{key.upper()}.json"


def _remember(key: str, source: str) -> None:
    with _resolutions_lock:
        _resolutions[key.upper()] = source
        _errors.pop(key.upper(), None)


def _remember_error(key: str, error: str) -> None:
    with _resolutions_lock:
        _errors[key.upper()] = str(error)


def resolution_error(key: str) -> str:
    """该键最近一次失败的原因（没有失败记录则空串）。"""
    with _resolutions_lock:
        return _errors.get(str(key or "").upper(), "")


def lookup_source(key: str) -> str:
    """该键的来源：builtin / cache / pubchem / ''（未知）。"""
    k = str(key or "").upper()
    if k in _resolutions:
        return _resolutions[k]
    from docking_agent.core.normalize_io import _INCHIKEY_MAP  # 局部导入避免循环依赖

    if k in _INCHIKEY_MAP:
        return "builtin"
    if cache_file(k).is_file():
        return "cache"
    return ""


def resolutions() -> Dict[str, str]:
    """本进程内所有已解析键的来源快照（只读副本）。"""
    with _resolutions_lock:
        return dict(_resolutions)


def _cache_get(key: str) -> Optional[str]:
    path = cache_file(key)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    smiles = str(data.get("smiles") or "").strip()
    return smiles or None


def _cache_put(key: str, smiles: str, cid: Any = None) -> None:
    path = cache_file(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"inchikey": key.upper(), "smiles": smiles, "cid": cid,
                                    "source": "pubchem", "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:  # 缓存写不进去不影响本次解析
        logger.debug("InChIKey 缓存写入失败（%s）：%s", path, e)


def _fail(key: str, error: str) -> Dict[str, Any]:
    """统一的失败返回：记下原因（供跳过原因/溯源引用），结构一律留空。"""
    _remember_error(key, error)
    return {"smiles": "", "source": "", "error": error}


def _http_json(url: str, timeout: int) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8")) if body.strip() else {}


def _smiles_from_payload(payload: Any) -> tuple:
    """从 PubChem property 响应里取最具体的 SMILES，并带上 CID。"""
    props = (((payload or {}).get("PropertyTable") or {}).get("Properties") or [])
    if not props:
        return "", None
    row = props[0] or {}
    for name in _SMILES_KEYS:
        value = str(row.get(name) or "").strip()
        if value:
            return value, row.get("CID")
    return "", row.get("CID")


def _verify(key: str, smiles: str) -> bool:
    """回算 InChIKey 必须与查询键逐字一致 —— 「解析到」不等于「解析对」。"""
    try:
        from rdkit import Chem

        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return False
        return Chem.MolToInchiKey(mol).upper() == key.upper()
    except Exception as e:  # noqa: BLE001 - 校验失败按不通过处理，绝不接受未校验结构
        logger.debug("InChIKey 回算校验异常（%s）：%s", key, e)
        return False


def resolve_inchikey(key: str, *, allow_network: Optional[bool] = None) -> Dict[str, Any]:
    """把一个 InChIKey 解析成 SMILES。

    返回 `{"smiles": str, "source": "builtin|cache|pubchem", "cid": ...}`
    或 `{"smiles": "", "source": "", "error": "<可操作原因>"}`。
    **顺序**：内置表 → 本地缓存 → 在线（可关）。在线结果必须通过回算校验才落盘与采用。
    """
    text = str(key or "").strip().upper()
    if not text:
        return {"smiles": "", "source": "", "error": "InChIKey 为空"}
    from docking_agent.core.normalize_io import _INCHIKEY_MAP  # 局部导入避免循环依赖

    builtin = _INCHIKEY_MAP.get(text)
    if builtin:
        _remember(text, "builtin")
        return {"smiles": builtin, "source": "builtin"}

    cached = _cache_get(text)
    if cached:
        _remember(text, "cache")
        return {"smiles": cached, "source": "cache"}

    enabled = online_enabled() if allow_network is None else bool(allow_network)
    if not enabled:
        return _fail(text, "离线模式（INCHIKEY_ONLINE=off）且内置表/本地缓存都没有该键；"
                            "可在设置里打开「InChIKey 在线反查」")

    url = PUBCHEM_INCHIKEY.format(urllib.parse.quote(text, safe=""), "SMILES,ConnectivitySMILES")
    try:
        payload = _http_json(url, _timeout())
    except urllib.error.HTTPError as e:
        if e.code in (404, 400):
            return _fail(text, f"PubChem 未收录该 InChIKey（HTTP {e.code}）")
        return _fail(text, f"PubChem 查询失败（HTTP {e.code}）")
    except Exception as e:  # noqa: BLE001 - 网络问题不能中断整库解析
        return _fail(text, f"PubChem 查询失败：{type(e).__name__}: {e}")

    smiles, cid = _smiles_from_payload(payload)
    if not smiles:
        return _fail(text, "PubChem 返回里没有 SMILES")
    if not _verify(text, smiles):
        return _fail(text, "PubChem 返回的结构回算 InChIKey 与查询值不一致，已拒绝采用")
    _cache_put(text, smiles, cid)
    _remember(text, "pubchem")
    return {"smiles": smiles, "source": "pubchem", "cid": cid}


def provenance_notes(records: Any) -> List[str]:
    """给归一化结果生成 InChIKey 溯源说明：逐条点名结构来自 内置表 / 本地缓存 / 在线还原。

    InChIKey 是单向哈希，用户有权知道「这个结构到底从哪来」——因此不允许默默填上。
    """
    from docking_agent.core.normalize_io import _inchikeys_in, _shorten_names  # 局部导入避免循环

    by_source: Dict[str, List[str]] = {"builtin": [], "cache": [], "pubchem": []}
    for rec in records or []:
        for key in _inchikeys_in(rec.get("raw") or ""):
            source = lookup_source(key)
            if source in by_source and rec.get("id") not in by_source[source]:
                by_source[source].append(str(rec.get("id") or ""))
    notes: List[str] = []
    if by_source["builtin"]:
        notes.append(f"以下分子的结构来自**内置 InChIKey→结构映射表**："
                     f"{_shorten_names(by_source['builtin'])}")
    if by_source["cache"]:
        notes.append(f"以下分子的结构来自**本地 InChIKey 缓存**"
                     f"（上次在线反查过，本次离线命中）：{_shorten_names(by_source['cache'])}")
    if by_source["pubchem"]:
        notes.append(f"以下分子的结构由**在线反查 PubChem 还原**（InChIKey 是单向哈希，"
                     f"离线无法反解；结果已回算校验一致并写入本地缓存）："
                     f"{_shorten_names(by_source['pubchem'])}")
    if not any(by_source.values()):
        failed = [str(r.get("id") or "") for r in records or []
                  if "InChIKey" in str(r.get("raw") or "")]
        if failed:
            notes.append(f"以下分子声明了 InChIKey 但未能还原结构：{_shorten_names(failed)}")
    logger.debug("InChIKey 来源统计：%s（本进程共解析 %s 个）",
                 {k: len(v) for k, v in by_source.items()}, len(resolutions()))
    return notes
