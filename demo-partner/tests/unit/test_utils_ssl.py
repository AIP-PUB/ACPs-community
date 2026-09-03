import ssl
from pathlib import Path

import pytest

from partners import utils as partner_utils


class DummySSLContext:
    def __init__(self) -> None:
        self.loaded_cert_chain: tuple[str, str] | None = None
        self.minimum_version: ssl.TLSVersion | None = None

    def load_cert_chain(self, certfile: str, keyfile: str) -> None:
        self.loaded_cert_chain = (certfile, keyfile)


def test_build_client_ssl_context_uses_server_identity_cert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for filename in (
        "server.pem",
        "server.key",
        "client.pem",
        "client.key",
        "trust-bundle.pem",
    ):
        (tmp_path / filename).write_text("placeholder", encoding="utf-8")

    dummy_ctx = DummySSLContext()
    monkeypatch.setattr(
        partner_utils.ssl,  # type: ignore[attr-defined]
        "create_default_context",
        lambda purpose, cafile: dummy_ctx,
    )

    cfg = {
        "mtls": {
            "tls_enabled": True,
            "cert_file": "server.pem",
            "key_file": "server.key",
            "ca_file": "trust-bundle.pem",
        }
    }

    ctx = partner_utils.build_client_ssl_context(str(tmp_path), cfg)

    assert ctx is dummy_ctx  # type: ignore[comparison-overlap]
    assert dummy_ctx.loaded_cert_chain == (
        str(tmp_path / "client.pem"),
        str(tmp_path / "client.key"),
    )
    assert dummy_ctx.minimum_version == ssl.TLSVersion.TLSv1_3


def test_build_client_ssl_context_falls_back_to_server_identity_cert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for filename in (
        "server.pem",
        "server.key",
        "trust-bundle.pem",
    ):
        (tmp_path / filename).write_text("placeholder", encoding="utf-8")

    dummy_ctx = DummySSLContext()
    monkeypatch.setattr(
        partner_utils.ssl,  # type: ignore[attr-defined]
        "create_default_context",
        lambda purpose, cafile: dummy_ctx,
    )

    cfg = {
        "mtls": {
            "tls_enabled": True,
            "cert_file": "server.pem",
            "key_file": "server.key",
            "ca_file": "trust-bundle.pem",
        }
    }

    ctx = partner_utils.build_client_ssl_context(str(tmp_path), cfg)

    assert ctx is dummy_ctx  # type: ignore[comparison-overlap]
    assert dummy_ctx.loaded_cert_chain == (
        str(tmp_path / "server.pem"),
        str(tmp_path / "server.key"),
    )
    assert dummy_ctx.minimum_version == ssl.TLSVersion.TLSv1_3


def test_build_rabbitmq_ssl_context_prefers_mq_client_cert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for filename in (
        "server.pem",
        "server.key",
        "rabbitmq-client.pem",
        "rabbitmq-client.key",
        "client.pem",
        "client.key",
        "trust-bundle.pem",
    ):
        (tmp_path / filename).write_text("placeholder", encoding="utf-8")

    dummy_ctx = DummySSLContext()
    monkeypatch.setattr(
        partner_utils.ssl,  # type: ignore[attr-defined]
        "create_default_context",
        lambda purpose, cafile: dummy_ctx,
    )

    cfg = {
        "mtls": {
            "tls_enabled": True,
            "cert_file": "server.pem",
            "key_file": "server.key",
            "ca_file": "trust-bundle.pem",
        }
    }

    ctx = partner_utils.build_rabbitmq_ssl_context(str(tmp_path), cfg)

    assert ctx is dummy_ctx  # type: ignore[comparison-overlap]
    assert dummy_ctx.loaded_cert_chain == (
        str(tmp_path / "rabbitmq-client.pem"),
        str(tmp_path / "rabbitmq-client.key"),
    )
    assert dummy_ctx.minimum_version == ssl.TLSVersion.TLSv1_3


def test_build_rabbitmq_ssl_context_falls_back_to_agent_client_cert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for filename in (
        "server.pem",
        "server.key",
        "client.pem",
        "client.key",
        "trust-bundle.pem",
    ):
        (tmp_path / filename).write_text("placeholder", encoding="utf-8")

    dummy_ctx = DummySSLContext()
    monkeypatch.setattr(
        partner_utils.ssl,  # type: ignore[attr-defined]
        "create_default_context",
        lambda purpose, cafile: dummy_ctx,
    )

    cfg = {
        "mtls": {
            "tls_enabled": True,
            "cert_file": "server.pem",
            "key_file": "server.key",
            "ca_file": "trust-bundle.pem",
        }
    }

    ctx = partner_utils.build_rabbitmq_ssl_context(str(tmp_path), cfg)

    assert ctx is dummy_ctx  # type: ignore[comparison-overlap]
    assert dummy_ctx.loaded_cert_chain == (
        str(tmp_path / "client.pem"),
        str(tmp_path / "client.key"),
    )
    assert dummy_ctx.minimum_version == ssl.TLSVersion.TLSv1_3
