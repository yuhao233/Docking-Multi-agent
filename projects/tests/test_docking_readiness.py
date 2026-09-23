"""对接的前置条件由**工具**守：信息没确认就不计算（不靠提示词求模型自觉）。"""
from __future__ import annotations

import json
from typing import Any

from docking_agent.agents.guards import docking_readiness, readiness_payload


class _Run:
    def __init__(self, **data: Any) -> None:
        self.data: dict = {"request": {}, "task_spec": {}, **data}


def test_missing_receptor_blocks_docking() -> None:
    run = _Run(request={"molecule_file": "/tmp/lib.sdf"})
    assert "receptor" in docking_readiness(run)
    body = json.loads(readiness_payload(run))
    assert body["status"] == "precondition_missing"
    assert "受体" in body["message"]


def test_site_and_library_are_not_hard_preconditions() -> None:
    """位点由对接路径现场确定；配体可能来自 ligands_text/黑板 → 两者都不该拦住开跑。"""
    run = _Run(request={"receptor_file": "/tmp/r.pdb", "ligands_text": "乙醇:CCO"})
    assert docking_readiness(run) == []


def test_pending_positive_control_blocks_docking() -> None:
    run = _Run(request={"receptor_file": "/tmp/r.pdb", "molecule_file": "/tmp/lib.sdf"})
    run.data["choices"] = [{"id": "positive_control:none", "kind": "positive_control", "value": ""}]
    run.data["choices_blocking"] = "positive_control"
    missing = docking_readiness(run)
    assert missing == ["positive_control"], missing
    assert json.loads(readiness_payload(run))["status"] == "needs_user_input"


def test_everything_ready_passes() -> None:
    run = _Run(request={"receptor_file": "/tmp/r.pdb", "molecule_file": "/tmp/lib.sdf"})
    assert readiness_payload(run) == ""
