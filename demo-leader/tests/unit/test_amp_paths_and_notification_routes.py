from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI


def test_amp_paths_honor_explicit_log_and_bootstrap_acs_roots(monkeypatch, tmp_path: Path) -> None:
    from assistant import amp_paths

    log_dir = tmp_path / "amp-logs"
    acs_file = tmp_path / "leader" / "atr" / "acs.json"
    acs_file.parent.mkdir(parents=True)
    acs_file.write_text('{"aic": "test"}', encoding="utf-8")

    monkeypatch.setenv("AMP_LOG_DIR", str(log_dir))
    monkeypatch.setenv("ACPS_APP_ROOT", str(tmp_path))
    monkeypatch.delenv("LEADER_RUNTIME_ROOT", raising=False)

    assert amp_paths.resolve_amp_log_dir() == log_dir
    assert amp_paths.resolve_leader_acs_file() == acs_file


def test_register_notification_routes_mounts_executor_receiver() -> None:
    from assistant.api.notification_routes import register_notification_routes

    class Receiver:
        def __init__(self) -> None:
            self.mounted: tuple[FastAPI, str] | None = None

        def mount(self, app: FastAPI, callback_path: str) -> None:
            self.mounted = (app, callback_path)

    class Executor:
        def __init__(self, receiver: Receiver) -> None:
            self.receiver = receiver

        def build_receiver(self) -> Receiver:
            return self.receiver

    app = FastAPI()
    receiver = Receiver()
    register_notification_routes(app, Executor(receiver), path_prefix="/callbacks")

    assert receiver.mounted == (app, "/callbacks/{task_id}")
