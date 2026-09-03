"""tests/unit/test_system_setup.py — system_setup.py 单例单元测试（ES5）。"""

from __future__ import annotations

import sys
from pathlib import Path

_current_dir = Path(__file__).parent
_tests_root = _current_dir.parent
_project_root = _tests_root.parent
_leader_dir = _project_root / "leader"

if str(_leader_dir) not in sys.path:
    sys.path.insert(0, str(_leader_dir))
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


def test_leader_system_emitter_configuration() -> None:
    import assistant.system_setup as system_setup
    from acps_sdk.amp import SystemEmitter

    assert isinstance(system_setup.LEADER_SYSTEM_EMITTER, SystemEmitter)
    assert system_setup.LEADER_SERVICE_NAME == "demo-leader"
    assert str(system_setup._SYSTEM_LOG_FILE).endswith("logs/amp_system.jsonl")
    assert system_setup.LEADER_SYSTEM_EMITTER._aic
    assert system_setup.LEADER_SYSTEM_EMITTER._resource["service.name"] == system_setup.LEADER_SERVICE_NAME
