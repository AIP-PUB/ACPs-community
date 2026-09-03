"""真实 RabbitMQ validated user-id 行为的 e2e 验证。"""

from __future__ import annotations

import os
import ssl
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

import pytest

aio_pika = pytest.importorskip("aio_pika")

if TYPE_CHECKING:
    from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractRobustConnection

_TEST_AIC = os.environ.get("E2E_TEST_AIC", "")
_FORGED_AIC = os.environ.get(
    "E2E_FORGED_AIC",
    "1.2.156.3088.1.1.89AB.123456.7LMNOP.1ABC",
)


def _build_ssl_context(
    *,
    tls_cert_file: Path,
    tls_key_file: Path,
    tls_ca_cert_file: Path,
) -> ssl.SSLContext:
    ctx = ssl.create_default_context(
        purpose=ssl.Purpose.SERVER_AUTH,
        cafile=str(tls_ca_cert_file),
    )
    ctx.load_cert_chain(
        certfile=str(tls_cert_file),
        keyfile=str(tls_key_file),
    )
    return ctx


def _build_amqps_url(*, host: str, port: int, vhost: str, connection_name: str) -> str:
    encoded_vhost = quote(vhost, safe="")
    encoded_name = quote(connection_name, safe="")
    return f"amqps://{host}:{port}/{encoded_vhost}?auth=external&name={encoded_name}"


async def _open_connection(
    *,
    tls_cert_file: Path,
    tls_key_file: Path,
    tls_ca_cert_file: Path,
) -> AbstractRobustConnection:
    host = os.environ.get("AMQP_BROKER_HOST", "localhost")
    port = int(os.environ.get("AMQP_BROKER_PORT", "5671"))
    vhost = os.environ.get("AMQP_BROKER_VHOST", "acps")
    ssl_context = _build_ssl_context(
        tls_cert_file=tls_cert_file,
        tls_key_file=tls_key_file,
        tls_ca_cert_file=tls_ca_cert_file,
    )
    try:
        return await aio_pika.connect_robust(
            _build_amqps_url(
                host=host,
                port=port,
                vhost=vhost,
                connection_name="mq-auth-e2e-user-id",
            ),
            ssl_context=ssl_context,
            timeout=10,
        )
    except Exception as exc:
        pytest.skip(f"RabbitMQ broker unavailable for validated user-id e2e: {exc}")


async def _ensure_inbox_exchange(channel: AbstractChannel) -> AbstractExchange:
    """Ensure the shared inbox exchange exists in local/dev brokers."""
    return await channel.declare_exchange(
        "inbox.topic",
        aio_pika.ExchangeType.TOPIC,
        durable=True,
        passive=False,
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_validated_user_id_allows_matching_authenticated_username(
    mtls_configured: bool,
    tls_cert_file: Path | None,
    tls_key_file: Path | None,
    tls_ca_cert_file: Path | None,
) -> None:
    if not _TEST_AIC:
        pytest.skip("E2E_TEST_AIC 未配置 — 跳过 RabbitMQ validated user-id e2e")

    assert mtls_configured
    assert tls_cert_file is not None
    assert tls_key_file is not None
    assert tls_ca_cert_file is not None

    connection = await _open_connection(
        tls_cert_file=tls_cert_file,
        tls_key_file=tls_key_file,
        tls_ca_cert_file=tls_ca_cert_file,
    )
    try:
        channel = await connection.channel()
        exchange = await _ensure_inbox_exchange(channel)
        message = aio_pika.Message(
            body=b'{"type":"identity-check"}',
            content_type="application/json",
            user_id=_TEST_AIC,
        )
        await exchange.publish(message, routing_key=f"inbox_{_TEST_AIC}")
    finally:
        await connection.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_validated_user_id_rejects_forged_user_id(
    mtls_configured: bool,
    tls_cert_file: Path | None,
    tls_key_file: Path | None,
    tls_ca_cert_file: Path | None,
) -> None:
    if not _TEST_AIC:
        pytest.skip("E2E_TEST_AIC 未配置 — 跳过 RabbitMQ validated user-id e2e")
    if _FORGED_AIC == _TEST_AIC:
        pytest.skip("E2E_FORGED_AIC 与 E2E_TEST_AIC 相同，无法验证 forged user_id")

    assert mtls_configured
    assert tls_cert_file is not None
    assert tls_key_file is not None
    assert tls_ca_cert_file is not None

    connection = await _open_connection(
        tls_cert_file=tls_cert_file,
        tls_key_file=tls_key_file,
        tls_ca_cert_file=tls_ca_cert_file,
    )
    try:
        channel = await connection.channel()
        exchange = await _ensure_inbox_exchange(channel)
        forged = aio_pika.Message(
            body=b'{"type":"identity-check"}',
            content_type="application/json",
            user_id=_FORGED_AIC,
        )
        with pytest.raises(Exception) as exc_info:
            await exchange.publish(forged, routing_key=f"inbox_{_TEST_AIC}")

        exc_text = str(exc_info.value)
        assert (
            "user_id" in exc_text
            or "authenticated user" in exc_text
            or "PRECONDITION_FAILED" in exc_text
            or "ACCESS_REFUSED" in exc_text
        )
    finally:
        await connection.close()
