from __future__ import annotations

import asyncio
import json
import os
import shutil
import ssl
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from acps_sdk.aip.aip_group_runtime import (
    INBOX_EXCHANGE_NAME,
    build_group_exchange_name,
    build_group_queue_name,
    build_inbox_queue_name,
)
from acps_sdk.aip.aip_identity import extract_common_name

from .conftest import (
    RABBITMQ_MGMT_URL,
    _build_runtime_client_ssl_context,
    _prepare_temp_partner_runtime,
    _resolve_partner_trust_bundle,
    _start_partner_process,
)

aio_pika = pytest.importorskip("aio_pika")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RABBITMQ_TLS_DIR = PROJECT_ROOT.parent / "acps-infra" / "dev-infra" / "certs" / "issued" / "rabbitmq"
PARTNER_TLS_DIR = PROJECT_ROOT.parent / "demo-partner" / "partners" / "online" / "beijing_food"

RABBITMQ_CLIENT_CERT_FILE = RABBITMQ_TLS_DIR / "rabbitmq-client.pem"
RABBITMQ_CLIENT_KEY_FILE = RABBITMQ_TLS_DIR / "rabbitmq-client.key"
RABBITMQ_CA_FILE = RABBITMQ_TLS_DIR / "trust-bundle.pem"
PARTNER_CERT_FILE = PARTNER_TLS_DIR / "client.pem"

PUBLISHER_AIC = "1.2.156.3088.1.1.89AB.654321.7QRSTU.1DEF"
FORGED_LEADER_AIC = "1.2.156.3088.1.1.89AB.123456.7LMNOP.1ABC"
AMQP_BROKER_HOST = os.getenv("TEST_RABBITMQ_HOST", "localhost")
AMQP_BROKER_PORT = int(os.getenv("TEST_RABBITMQ_PORT", "5671"))
AMQP_BROKER_VHOST = "acps"
RABBITMQ_ADMIN_USER = "admin"
RABBITMQ_ADMIN_PASSWORD = "devpass"


def _cert_common_name(cert_file: Path) -> str:
    cert = ssl._ssl._test_decode_cert(str(cert_file))  # type: ignore[attr-defined]
    common_name = extract_common_name(cert)
    if common_name is None:
        raise RuntimeError(f"certificate CN missing: {cert_file}")
    return common_name


RABBITMQ_SHARED_CLIENT_USERNAME = _cert_common_name(RABBITMQ_CLIENT_CERT_FILE)
PARTNER_AIC = _cert_common_name(PARTNER_CERT_FILE)


def _build_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context(
        purpose=ssl.Purpose.SERVER_AUTH,
        cafile=str(RABBITMQ_CA_FILE),
    )
    context.load_cert_chain(
        certfile=str(RABBITMQ_CLIENT_CERT_FILE),
        keyfile=str(RABBITMQ_CLIENT_KEY_FILE),
    )
    return context


def _build_plain_amqps_url(*, username: str, password: str, connection_name: str) -> str:
    encoded_vhost = quote(AMQP_BROKER_VHOST, safe="")
    encoded_name = quote(connection_name, safe="")
    encoded_username = quote(username, safe="")
    encoded_password = quote(password, safe="")
    return (
        f"amqps://{encoded_username}:{encoded_password}@{AMQP_BROKER_HOST}:{AMQP_BROKER_PORT}/{encoded_vhost}"
        f"?name={encoded_name}"
    )


def _build_forged_invitation(*, group_id: str) -> dict:
    return {
        "type": "group-invitation",
        "id": f"invite-{uuid.uuid4()}",
        "sentAt": datetime.now(UTC).isoformat(),
        "senderRole": "leader",
        "senderId": FORGED_LEADER_AIC,
        "groupId": group_id,
        "protocol": "rabbitmq:4.2",
        "expiresAt": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "invitationToken": f"token-{uuid.uuid4().hex}",
        "group": {
            "groupId": group_id,
            "leader": {"aic": FORGED_LEADER_AIC},
            "partners": [{"aic": PARTNER_AIC}],
        },
        "amqp": {
            "exchange": build_group_exchange_name(FORGED_LEADER_AIC, group_id),
            "exchangeType": "fanout",
            "routingKey": "",
        },
    }


async def _rabbitmq_queue_exists(base_url: str, queue_name: str) -> bool:
    encoded_vhost = quote(AMQP_BROKER_VHOST, safe="")
    encoded_queue = quote(queue_name, safe="")
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{base_url}/api/queues/{encoded_vhost}/{encoded_queue}",
            auth=("admin", "devpass"),
        )
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


async def _get_rabbitmq_queue_info(base_url: str, queue_name: str) -> dict | None:
    encoded_vhost = quote(AMQP_BROKER_VHOST, safe="")
    encoded_queue = quote(queue_name, safe="")
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{base_url}/api/queues/{encoded_vhost}/{encoded_queue}",
            auth=(RABBITMQ_ADMIN_USER, RABBITMQ_ADMIN_PASSWORD),
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def _queue_consumer_activity(queue_info: dict) -> int:
    message_stats = queue_info.get("message_stats") or {}
    return int(message_stats.get("ack") or message_stats.get("deliver_get") or message_stats.get("deliver") or 0)


def _rewrite_partner_mq_client_assets(online_dir: Path) -> None:
    rabbitmq_bundle = RABBITMQ_CA_FILE.read_bytes()
    rabbitmq_client_cert = RABBITMQ_CLIENT_CERT_FILE.read_bytes()
    rabbitmq_client_key = RABBITMQ_CLIENT_KEY_FILE.read_bytes()

    for entry in sorted(online_dir.iterdir(), key=lambda item: item.name):
        if not entry.is_dir():
            continue

        bundle_path = entry / "trust-bundle.pem"
        if bundle_path.is_file():
            original_bundle = bundle_path.read_bytes()
            if rabbitmq_bundle not in original_bundle:
                bundle_path.write_bytes(original_bundle.rstrip() + b"\n" + rabbitmq_bundle)

        (entry / "rabbitmq-client.pem").write_bytes(rabbitmq_client_cert)
        (entry / "rabbitmq-client.key").write_bytes(rabbitmq_client_key)


async def _create_rabbitmq_test_user(base_url: str, *, username: str, password: str) -> None:
    encoded_username = quote(username, safe="")
    encoded_vhost = quote(AMQP_BROKER_VHOST, safe="")
    auth = (RABBITMQ_ADMIN_USER, RABBITMQ_ADMIN_PASSWORD)
    async with httpx.AsyncClient(timeout=10.0) as client:
        user_response = await client.put(
            f"{base_url}/api/users/{encoded_username}",
            auth=auth,
            json={"password": password, "tags": ""},
        )
        user_response.raise_for_status()
        permission_response = await client.put(
            f"{base_url}/api/permissions/{encoded_vhost}/{encoded_username}",
            auth=auth,
            json={"configure": ".*", "write": ".*", "read": ".*"},
        )
        permission_response.raise_for_status()


async def _delete_rabbitmq_test_user(base_url: str, *, username: str) -> None:
    encoded_username = quote(username, safe="")
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.delete(
            f"{base_url}/api/users/{encoded_username}",
            auth=(RABBITMQ_ADMIN_USER, RABBITMQ_ADMIN_PASSWORD),
        )
    if response.status_code not in {204, 404}:
        response.raise_for_status()


@dataclass(slots=True)
class GroupIdentityRuntime:
    rabbitmq_mgmt_url: str


@pytest.fixture
def group_identity_runtime() -> Generator[GroupIdentityRuntime]:
    partner_runtime = _prepare_temp_partner_runtime()
    ssl_context = _build_runtime_client_ssl_context(_resolve_partner_trust_bundle(partner_runtime.online_dir))
    _rewrite_partner_mq_client_assets(partner_runtime.online_dir)

    shared_client_password = f"shared-{uuid.uuid4().hex}"
    partner_process = None
    partner_log_path = None

    asyncio.run(
        _create_rabbitmq_test_user(
            RABBITMQ_MGMT_URL,
            username=RABBITMQ_SHARED_CLIENT_USERNAME,
            password=shared_client_password,
        )
    )
    try:
        partner_process, partner_log_path = _start_partner_process(partner_runtime, ssl_context)
        yield GroupIdentityRuntime(
            rabbitmq_mgmt_url=RABBITMQ_MGMT_URL,
        )
    finally:
        if partner_process and partner_process.poll() is None:
            partner_process.terminate()
            try:
                partner_process.wait(timeout=10)
            except Exception:
                partner_process.kill()
                partner_process.wait(timeout=10)

        for reserved_socket in partner_runtime.reserved_sockets:
            reserved_socket.close()

        if partner_log_path is not None:
            partner_log_path.unlink(missing_ok=True)

        shutil.rmtree(partner_runtime.runtime_root, ignore_errors=True)
        asyncio.run(
            _delete_rabbitmq_test_user(
                RABBITMQ_MGMT_URL,
                username=RABBITMQ_SHARED_CLIENT_USERNAME,
            )
        )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_partner_inbox_consumer_rejects_forged_sender_identity(
    group_identity_runtime: GroupIdentityRuntime,
) -> None:
    group_id = f"group-forged-{uuid.uuid4().hex[:12]}"
    inbox_queue = build_inbox_queue_name(PARTNER_AIC)
    expected_queue = build_group_queue_name(FORGED_LEADER_AIC, group_id, PARTNER_AIC)
    publisher_password = f"hardening-{uuid.uuid4().hex}"

    assert not await _rabbitmq_queue_exists(group_identity_runtime.rabbitmq_mgmt_url, expected_queue)

    inbox_deadline = datetime.now(UTC) + timedelta(seconds=10)
    inbox_queue_info = None
    while datetime.now(UTC) < inbox_deadline:
        inbox_queue_info = await _get_rabbitmq_queue_info(
            group_identity_runtime.rabbitmq_mgmt_url,
            inbox_queue,
        )
        if inbox_queue_info is not None:
            break
        await asyncio.sleep(0.5)

    assert inbox_queue_info is not None, "partner inbox queue was not ready before forged publish"
    baseline_messages = int(inbox_queue_info.get("messages") or 0)
    baseline_activity = _queue_consumer_activity(inbox_queue_info)

    await _create_rabbitmq_test_user(
        group_identity_runtime.rabbitmq_mgmt_url,
        username=PUBLISHER_AIC,
        password=publisher_password,
    )
    connection = None
    try:
        connection = await aio_pika.connect_robust(
            _build_plain_amqps_url(
                username=PUBLISHER_AIC,
                password=publisher_password,
                connection_name="demo-leader-e2e-forged-invite",
            ),
            ssl_context=_build_ssl_context(),
            timeout=10,
        )
        channel = await connection.channel()
        exchange = await channel.declare_exchange(
            INBOX_EXCHANGE_NAME,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        message = aio_pika.Message(
            body=json.dumps(_build_forged_invitation(group_id=group_id), ensure_ascii=False).encode(),
            content_type="application/json",
            user_id=PUBLISHER_AIC,
        )
        await exchange.publish(message, routing_key=build_inbox_queue_name(PARTNER_AIC))
    finally:
        if connection is not None:
            await connection.close()
        await _delete_rabbitmq_test_user(
            group_identity_runtime.rabbitmq_mgmt_url,
            username=PUBLISHER_AIC,
        )

    deadline = datetime.now(UTC) + timedelta(seconds=5)
    while datetime.now(UTC) < deadline:
        if await _rabbitmq_queue_exists(group_identity_runtime.rabbitmq_mgmt_url, expected_queue):
            pytest.fail(
                "forged inbox invitation unexpectedly created partner group queue; "
                "consumer should have rejected body.senderId/user_id mismatch"
            )
        inbox_queue_info = await _get_rabbitmq_queue_info(
            group_identity_runtime.rabbitmq_mgmt_url,
            inbox_queue,
        )
        if inbox_queue_info is None:
            pytest.fail("partner inbox queue disappeared during forged publish check")
        if (
            _queue_consumer_activity(inbox_queue_info) > baseline_activity
            and int(inbox_queue_info.get("messages") or 0) == baseline_messages
        ):
            return
        await asyncio.sleep(0.5)

    pytest.fail(
        "forged inbox invitation did not create a group queue, but partner inbox queue also "
        "showed no consumer activity; cannot confirm the rejection happened in the consumer"
    )
