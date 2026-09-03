from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

import partners.utils as utils


class DummyServerSSLContext:
    def __init__(self, protocol: ssl._SSLMethod) -> None:
        self.protocol = protocol
        self.loaded_cert_chain: tuple[str, str] | None = None
        self.loaded_verify_locations: str | None = None
        self.minimum_version: ssl.TLSVersion | None = None
        self.verify_mode: ssl.VerifyMode | None = None

    def load_cert_chain(self, certfile: str, keyfile: str) -> None:
        self.loaded_cert_chain = (certfile, keyfile)

    def load_verify_locations(self, cafile: str) -> None:
        self.loaded_verify_locations = cafile


def _write_cert_files(base: Path) -> None:
    for filename in ("server.pem", "server.key", "ca.pem"):
        (base / filename).write_text("placeholder", encoding="utf-8")


def _mtls_config(*, verify_client: bool = False) -> dict[str, Any]:
    return {
        "mtls": {
            "tls_enabled": True,
            "cert_file": "server.pem",
            "key_file": "server.key",
            "ca_file": "ca.pem",
            "verify_client": verify_client,
        }
    }


def test_get_online_dir_prefers_env_and_project_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_online = tmp_path / "env-online"
    monkeypatch.setenv("PARTNERS_ONLINE_DIR", str(env_online))
    assert utils.get_online_dir() == env_online

    monkeypatch.delenv("PARTNERS_ONLINE_DIR")
    cwd = tmp_path / "project"
    cwd_online = cwd / "partners" / "online"
    cwd_online.mkdir(parents=True)
    monkeypatch.chdir(cwd)

    assert utils.get_online_dir() == cwd_online


def test_resolve_identity_binding_enabled_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIP_IDENTITY_BINDING_ENABLED", "off")
    assert utils.resolve_identity_binding_enabled({"app": {"identity_binding_enabled": True}}) is False

    monkeypatch.setenv("AIP_IDENTITY_BINDING_ENABLED", "yes")
    assert utils.resolve_identity_binding_enabled({"app": {"identity_binding_enabled": False}}) is True

    monkeypatch.setenv("AIP_IDENTITY_BINDING_ENABLED", "maybe")
    assert utils.resolve_identity_binding_enabled({"app": {"identity_binding_enabled": False}}) is False


def test_build_server_ssl_context_with_and_without_client_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_cert_files(tmp_path)
    created: list[DummyServerSSLContext] = []

    def _make_context(protocol: ssl._SSLMethod) -> DummyServerSSLContext:
        context = DummyServerSSLContext(protocol)
        created.append(context)
        return context

    monkeypatch.setattr(ssl, "SSLContext", _make_context)

    no_verify = cast("Any", utils.build_ssl_context(str(tmp_path), _mtls_config(verify_client=False)))
    verify = cast("Any", utils.build_ssl_context(str(tmp_path), _mtls_config(verify_client=True)))

    assert no_verify.loaded_cert_chain == (str(tmp_path / "server.pem"), str(tmp_path / "server.key"))
    assert no_verify.verify_mode == ssl.CERT_NONE
    assert verify.loaded_verify_locations == str(tmp_path / "ca.pem")
    assert verify.verify_mode == ssl.CERT_REQUIRED


def test_ssl_helpers_return_empty_when_tls_disabled(tmp_path: Path) -> None:
    cfg = {"mtls": {"tls_enabled": False}}

    assert utils.build_ssl_context(str(tmp_path), cfg) is None
    assert utils.build_client_ssl_context(str(tmp_path), cfg) is None
    assert cast("Any", utils).build_rabbitmq_ssl_context(str(tmp_path), cfg) is None
    assert utils.build_uvicorn_ssl_kwargs(str(tmp_path), cfg) == {}


def test_ssl_helpers_raise_for_missing_files(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="cert_file"):
        utils.build_uvicorn_ssl_kwargs(str(tmp_path), _mtls_config())


def test_build_uvicorn_ssl_kwargs_sets_client_cert_requirement(tmp_path: Path) -> None:
    _write_cert_files(tmp_path)

    kwargs = utils.build_uvicorn_ssl_kwargs(str(tmp_path), _mtls_config(verify_client=True))

    assert kwargs == {
        "ssl_certfile": str(tmp_path / "server.pem"),
        "ssl_keyfile": str(tmp_path / "server.key"),
        "ssl_ca_certs": str(tmp_path / "ca.pem"),
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }


def test_discover_agents_creates_missing_online_dir_and_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    online_dir = tmp_path / "online"
    monkeypatch.setenv("PARTNERS_ONLINE_DIR", str(online_dir))

    assert utils.discover_agents() == {}
    assert online_dir.exists()

    valid = online_dir / "valid"
    valid.mkdir()
    (valid / "acs.json").write_text("{}", encoding="utf-8")
    (valid / "config.toml").write_text("[server]\nport = 9101\n", encoding="utf-8")
    missing_config = online_dir / "missing_config"
    missing_config.mkdir()
    (missing_config / "acs.json").write_text("{}", encoding="utf-8")
    (online_dir / "plain-file").write_text("", encoding="utf-8")

    assert utils.discover_agents(["valid"]) == {"valid": str(valid)}


def test_read_agent_port_and_validate_ports(tmp_path: Path) -> None:
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "config.toml").write_text("[server]\nport = 9101\n", encoding="utf-8")
    (two / "config.toml").write_text("[server]\nport = 9101\n", encoding="utf-8")

    assert utils.read_agent_port(str(one)) == 9101
    with pytest.raises(ValueError, match="Port conflict"):
        utils.validate_ports({"one": str(one), "two": str(two)})


def test_terminate_processes_terminates_and_kills_stubborn_processes() -> None:
    graceful = Mock()
    graceful.is_alive.side_effect = [True, False]
    graceful.pid = 101
    stubborn = Mock()
    stubborn.is_alive.side_effect = [True, True]
    stubborn.pid = 102

    utils.terminate_processes({"graceful": graceful, "stubborn": stubborn})

    graceful.terminate.assert_called_once()
    stubborn.terminate.assert_called_once()
    graceful.kill.assert_not_called()
    stubborn.kill.assert_called_once()


def test_check_process_health_triggers_shutdown_and_exit() -> None:
    running = Mock()
    running.is_alive.return_value = True
    exited = Mock()
    exited.is_alive.return_value = False
    exited.exitcode = 7
    shutdown = Mock()

    with pytest.raises(SystemExit):
        utils.check_process_health({"running": running, "exited": exited}, shutdown)

    shutdown.assert_called_once()
