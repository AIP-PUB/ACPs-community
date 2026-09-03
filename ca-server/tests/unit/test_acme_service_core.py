"""app.acme.service 核心辅助逻辑单元测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.acme.exception import AcmeException
from app.acme.model import AccountStatus, OrderStatus
from app.acme.schema import JWSRequest
from app.acme.service import (
    AccountService,
    JWKService,
    OrderService,
    ensure_account_is_active,
    get_account_by_jwk,
    get_account_from_request,
    parse_jws_request,
    verify_jws_signature,
    verify_jws_signature_with_jwk,
    verify_nonce,
)

_stub = cast("Any", SimpleNamespace)
_rsa_jwk = {"kty": "RSA", "n": "AQAB", "e": "AQAB"}


@pytest.mark.asyncio
async def test_verify_nonce_success() -> None:
    nonce_service = _stub(validate_and_consume_nonce=AsyncMock(return_value=True))

    await verify_nonce({"nonce": "n-1"}, nonce_service)

    nonce_service.validate_and_consume_nonce.assert_awaited_once_with("n-1")


@pytest.mark.asyncio
async def test_verify_nonce_missing_or_invalid() -> None:
    nonce_service = _stub(validate_and_consume_nonce=AsyncMock(return_value=False))

    with pytest.raises(AcmeException) as exc_info_missing:
        await verify_nonce({}, nonce_service)
    assert exc_info_missing.value.status_code == 400

    with pytest.raises(AcmeException) as exc_info_invalid:
        await verify_nonce({"nonce": "n-2"}, nonce_service)
    assert exc_info_invalid.value.status_code == 400


@pytest.mark.asyncio
async def test_parse_jws_request_success(monkeypatch: pytest.MonkeyPatch) -> None:
    nonce_service = _stub(validate_and_consume_nonce=AsyncMock(return_value=True))
    request_data = JWSRequest(protected="p", payload="q", signature="s")

    monkeypatch.setattr("app.acme.service.parse_protected_header", lambda _x: {"nonce": "n-1", "url": "u"})
    monkeypatch.setattr("app.acme.service.parse_payload", lambda _x: {"k": "v"})

    protected, payload, signature = await parse_jws_request(request_data, nonce_service, expected_url="u")

    assert protected["nonce"] == "n-1"
    assert payload == {"k": "v"}
    assert signature == "s"


@pytest.mark.asyncio
async def test_parse_jws_request_url_mismatch_and_bad_format(monkeypatch: pytest.MonkeyPatch) -> None:
    nonce_service = _stub(validate_and_consume_nonce=AsyncMock(return_value=True))
    request_data = JWSRequest(protected="p", payload="q", signature="s")

    monkeypatch.setattr("app.acme.service.parse_protected_header", lambda _x: {"nonce": "n-1", "url": "wrong"})
    monkeypatch.setattr("app.acme.service.parse_payload", lambda _x: {"k": "v"})

    with pytest.raises(AcmeException) as exc_info:
        await parse_jws_request(request_data, nonce_service, expected_url="expected")
    assert exc_info.value.status_code == 400

    monkeypatch.setattr("app.acme.service.parse_protected_header", lambda _x: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(AcmeException):
        await parse_jws_request(request_data, nonce_service)


@pytest.mark.asyncio
async def test_get_account_from_request_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    active_account = _stub(id=1, status=AccountStatus.VALID)
    service = _stub(
        get_account_by_id=AsyncMock(return_value=active_account),
        get_account_by_key_id=AsyncMock(return_value=active_account),
    )

    monkeypatch.setattr(JWKService, "compute_jwk_thumbprint", staticmethod(lambda _jwk: "kid-1"))

    account = await get_account_from_request({"kid": "https://ca/acct/1"}, service)
    assert account is active_account

    account2 = await get_account_from_request({"jwk": _rsa_jwk}, service)
    assert account2 is active_account


@pytest.mark.asyncio
async def test_get_account_from_request_error_cases() -> None:
    service = _stub(get_account_by_id=AsyncMock(return_value=None), get_account_by_key_id=AsyncMock(return_value=None))

    with pytest.raises(AcmeException):
        await get_account_from_request({}, service)

    with pytest.raises(AcmeException):
        await get_account_from_request({"kid": "https://ca/acct/404"}, service)


@pytest.mark.asyncio
async def test_get_account_by_jwk_behaviors(monkeypatch: pytest.MonkeyPatch) -> None:
    active_account = _stub(id=1, status=AccountStatus.VALID)
    deactivated_account = _stub(id=2, status=AccountStatus.DEACTIVATED)

    service = _stub(get_account_by_key_id=AsyncMock(return_value=active_account))
    monkeypatch.setattr(JWKService, "compute_jwk_thumbprint", staticmethod(lambda _jwk: "kid-1"))

    assert await get_account_by_jwk({"jwk": _rsa_jwk}, service) is active_account
    assert await get_account_by_jwk({"jwk": "not-dict"}, service) is None

    service.get_account_by_key_id = AsyncMock(return_value=deactivated_account)
    with pytest.raises(AcmeException):
        await get_account_by_jwk({"jwk": _rsa_jwk}, service)


def test_ensure_account_is_active_raises_for_deactivated() -> None:
    with pytest.raises(AcmeException):
        ensure_account_is_active(_stub(status=AccountStatus.DEACTIVATED))

    ensure_account_is_active(_stub(status=AccountStatus.VALID))


def test_verify_jws_signature_with_jwk_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    request = JWSRequest(protected="p", payload="q", signature="s")

    verifier_ok = SimpleNamespace(verify_jws_signature=lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr("app.acme.service.get_jws_verifier", lambda: verifier_ok)
    assert verify_jws_signature_with_jwk(request, {"kty": "RSA"}) is True

    verifier_fail = SimpleNamespace(
        verify_jws_signature=lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad sig"))
    )
    monkeypatch.setattr("app.acme.service.get_jws_verifier", lambda: verifier_fail)
    with pytest.raises(AcmeException):
        verify_jws_signature_with_jwk(request, {"kty": "RSA"})


def test_verify_jws_signature_uses_account_public_key(monkeypatch: pytest.MonkeyPatch) -> None:
    request = JWSRequest(protected="p", payload="q", signature="s")
    account = _stub(public_key=json.dumps({"kty": "RSA"}))

    monkeypatch.setattr("app.acme.service.verify_jws_signature_with_jwk", lambda _req, jwk: jwk["kty"] == "RSA")

    assert verify_jws_signature(request, {}, account) is True


@pytest.mark.asyncio
async def test_account_service_apply_key_change_success(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _stub()
    service = AccountService(session)

    account = _stub(id=10, public_key=json.dumps({"kty": "RSA", "n": "old", "e": "AQAB"}))

    monkeypatch.setattr(
        "app.acme.service.parse_protected_header",
        lambda _x: {"jwk": {"kty": "RSA", "n": "new", "e": "AQAB"}},
    )
    monkeypatch.setattr(
        "app.acme.service.parse_payload",
        lambda _x: {
            "account": "https://ca/acct/10",
            "oldKey": {"kty": "RSA", "n": "old", "e": "AQAB"},
        },
    )

    verifier = SimpleNamespace(verify_jws_signature=lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr("app.acme.service.get_jws_verifier", lambda: verifier)
    monkeypatch.setattr(JWKService, "compute_jwk_thumbprint", staticmethod(lambda _jwk: "new-key-id"))
    monkeypatch.setattr(service, "get_account_by_key_id", AsyncMock(return_value=None))

    async def _update(_account: Any, **kwargs: Any) -> Any:
        _account.key_id = kwargs["key_id"]
        _account.public_key = kwargs["public_key"]
        return _account

    monkeypatch.setattr(service, "update_account", _update)

    updated = await service.apply_key_change(
        account,
        {"protected": "p", "payload": "q", "signature": "s"},
        "https://ca",
    )

    assert updated.key_id == "new-key-id"
    assert json.loads(updated.public_key)["n"] == "new"


@pytest.mark.asyncio
async def test_account_service_apply_key_change_rejects_bad_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AccountService(_stub())
    account = _stub(id=1, public_key=json.dumps({"kty": "RSA"}))

    with pytest.raises(AcmeException):
        await service.apply_key_change(account, {}, "https://ca")

    with pytest.raises(AcmeException):
        await service.apply_key_change(account, {"protected": "p", "payload": "q", "signature": ""}, "https://ca")

    monkeypatch.setattr("app.acme.service.parse_protected_header", lambda _x: {})
    with pytest.raises(AcmeException):
        await service.apply_key_change(
            account,
            {"protected": "p", "payload": "q", "signature": "s"},
            "https://ca",
        )


@pytest.mark.asyncio
async def test_account_service_apply_key_change_rejects_conflicts_and_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AccountService(_stub())
    account = _stub(id=10, public_key=json.dumps({"kty": "RSA", "n": "old", "e": "AQAB"}))

    monkeypatch.setattr(
        "app.acme.service.parse_protected_header",
        lambda _x: {"jwk": {"kty": "RSA", "n": "new", "e": "AQAB"}},
    )
    monkeypatch.setattr(
        "app.acme.service.parse_payload",
        lambda _x: {
            "account": "https://ca/acct/999",
            "oldKey": {"kty": "RSA", "n": "old", "e": "AQAB"},
        },
    )
    verifier = SimpleNamespace(verify_jws_signature=lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr("app.acme.service.get_jws_verifier", lambda: verifier)
    monkeypatch.setattr(JWKService, "compute_jwk_thumbprint", staticmethod(lambda _jwk: "new-key-id"))

    with pytest.raises(AcmeException):
        await service.apply_key_change(
            account,
            {"protected": "p", "payload": "q", "signature": "s"},
            "https://ca",
        )

    monkeypatch.setattr(
        "app.acme.service.parse_payload",
        lambda _x: {
            "account": "https://ca/acct/10",
            "oldKey": {"kty": "RSA", "n": "DIFF", "e": "AQAB"},
        },
    )
    with pytest.raises(AcmeException):
        await service.apply_key_change(
            account,
            {"protected": "p", "payload": "q", "signature": "s"},
            "https://ca",
        )

    monkeypatch.setattr(
        "app.acme.service.parse_payload",
        lambda _x: {
            "account": "https://ca/acct/10",
            "oldKey": {"kty": "RSA", "n": "old", "e": "AQAB"},
        },
    )
    monkeypatch.setattr(service, "get_account_by_key_id", AsyncMock(return_value=_stub(id=999)))

    with pytest.raises(AcmeException):
        await service.apply_key_change(
            account,
            {"protected": "p", "payload": "q", "signature": "s"},
            "https://ca",
        )


@pytest.mark.asyncio
async def test_order_service_normalize_identifiers_and_helpers() -> None:
    service = OrderService(_stub())
    registry_client = _stub(validate_aic_and_get_info=AsyncMock(return_value=_stub(aic="AIC-001")))

    normalized, agents = await service.normalize_and_validate_agent_identifiers(
        [{"type": "agent", "value": "aic-001", "usage": "clientAuth"}],
        account_aic="AIC-001",
        registry_client=registry_client,
    )

    assert normalized == [{"type": "agent", "value": "AIC-001", "usage": "clientAuth"}]
    assert len(agents) == 1

    with pytest.raises(AcmeException):
        await service.normalize_and_validate_agent_identifiers([], "AIC-001", registry_client)

    with pytest.raises(AcmeException):
        await service.normalize_and_validate_agent_identifiers(
            [{"type": "dns", "value": "x"}], "AIC-001", registry_client
        )

    with pytest.raises(AcmeException):
        await service.normalize_and_validate_agent_identifiers([{"type": "agent"}], "AIC-001", registry_client)

    with pytest.raises(AcmeException):
        await service.normalize_and_validate_agent_identifiers(
            [{"type": "agent", "value": "AIC-OTHER"}],
            "AIC-001",
            registry_client,
        )

    registry_client.validate_aic_and_get_info = AsyncMock(return_value=None)
    with pytest.raises(AcmeException):
        await service.normalize_and_validate_agent_identifiers(
            [{"type": "agent", "value": "AIC-001"}],
            "AIC-001",
            registry_client,
        )

    registry_client.validate_aic_and_get_info = AsyncMock(return_value=_stub(aic="AIC-001"))
    with pytest.raises(AcmeException):
        await service.normalize_and_validate_agent_identifiers(
            [{"type": "agent", "value": "AIC-001", "usage": "bad"}],
            "AIC-001",
            registry_client,
        )


@pytest.mark.asyncio
async def test_order_service_notify_and_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    service = OrderService(_stub())
    registry_client = _stub(register_certificate_request=AsyncMock(return_value=True))

    agents = [_stub(aic="AIC-001"), _stub(aic="AIC-002")]
    await service.notify_certificate_requests(agents, "order-1", registry_client)

    assert registry_client.register_certificate_request.await_count == 2

    order = _stub(authorizations=[], status=OrderStatus.PENDING)

    async def _update_status(_order: Any, _status: OrderStatus) -> Any:
        _order.status = _status
        return _order

    monkeypatch.setattr(service, "update_order_status", _update_status)

    ready_order = await service.mark_ready_with_authorizations(order, ["authz-1"])
    assert ready_order.authorizations == ["authz-1"]
    assert ready_order.status == OrderStatus.READY
