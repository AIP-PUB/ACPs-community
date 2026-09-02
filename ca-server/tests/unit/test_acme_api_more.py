"""补充 app.acme.api 的单元覆盖。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import JSONResponse

from app.acme.api import (
    change_key,
    create_account,
    create_order,
    finalize_order,
    get_authorization,
    get_ca_certificate,
    get_certificate,
    get_directory,
    get_new_nonce,
    get_order,
    revoke_certificate,
    update_account,
)
from app.acme.exception import AcmeException
from app.acme.model import OrderStatus
from app.acme.schema import JWSRequest

_stub = cast("Any", SimpleNamespace)
_rsa_jwk = {"kty": "RSA", "n": "AQAB", "e": "AQAB"}


@pytest.fixture
def request_data() -> JWSRequest:
    return JWSRequest(protected="p", payload="q", signature="s")


@pytest.fixture
def settings() -> Any:
    return _stub(acme_directory_url="http://localhost:9003/acps-atr-v2/acme")


@pytest.fixture
def nonce_service() -> Any:
    return _stub(generate_nonce=AsyncMock(return_value="nonce-1"))


@pytest.fixture
def account() -> Any:
    return _stub(
        id=1,
        status="valid",
        contact=["mailto:test@example.com"],
        terms_of_service_agreed=True,
        aic="AIC-001",
        created_at=datetime.now(UTC),
        public_key='{"kty": "RSA"}',
    )


async def _acme_response(payload: dict[str, Any], _nonce: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=payload)


@pytest.mark.asyncio
async def test_update_account_success(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    account_service = SimpleNamespace(
        update_account=AsyncMock(return_value=account),
    )

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: account_service)
    monkeypatch.setattr(
        "app.acme.api.parse_jws_request",
        AsyncMock(return_value=({"kid": "k"}, {"contact": ["mailto:new@example.com"]}, "s")),
    )
    monkeypatch.setattr("app.acme.api.get_account_from_request", AsyncMock(return_value=account))
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.acme.api.get_configured_acme_base_url", lambda _settings: "https://ca.example.com/acme")
    monkeypatch.setattr("app.acme.api.create_acme_response", _acme_response)

    response = await update_account(1, request_data, _stub(), settings)

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_update_account_mismatch_raises(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: SimpleNamespace())
    monkeypatch.setattr("app.acme.api.parse_jws_request", AsyncMock(return_value=({"kid": "k"}, {}, "s")))
    monkeypatch.setattr("app.acme.api.ensure_post_as_get_uses_empty_payload", lambda _req: None)
    monkeypatch.setattr("app.acme.api.get_account_from_request", AsyncMock(return_value=_stub(id=2, status="valid")))
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)

    with pytest.raises(AcmeException):
        await update_account(1, request_data, _stub(), settings)


@pytest.mark.asyncio
async def test_create_order_success(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    order = SimpleNamespace(
        order_id="ord-1",
        status=OrderStatus.READY,
        expires=datetime.now(UTC),
        identifiers=[{"type": "agent", "value": "AIC-001"}],
        authorizations=["https://ca.example.com/acme/authz/a1"],
        not_before=None,
        not_after=None,
    )

    order_service = SimpleNamespace(
        normalize_and_validate_agent_identifiers=AsyncMock(
            return_value=([{"type": "agent", "value": "AIC-001"}], [_stub(aic="AIC-001")])
        ),
        create_order=AsyncMock(return_value=order),
        notify_certificate_requests=AsyncMock(return_value=None),
        mark_ready_with_authorizations=AsyncMock(return_value=order),
    )
    authorization_service = SimpleNamespace(
        create_valid_authorization_urls=AsyncMock(return_value=["https://ca.example.com/acme/authz/a1"])
    )

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: SimpleNamespace())
    monkeypatch.setattr("app.acme.api.get_order_service", lambda _session: order_service)
    monkeypatch.setattr("app.acme.api.get_authorization_service", lambda _session: authorization_service)
    monkeypatch.setattr("app.acme.api.get_registry_client", lambda: _stub())
    monkeypatch.setattr(
        "app.acme.api.parse_jws_request",
        AsyncMock(return_value=({"kid": "k"}, {"identifiers": [{"type": "agent", "value": "AIC-001"}]}, "s")),
    )
    monkeypatch.setattr("app.acme.api.get_account_from_request", AsyncMock(return_value=account))
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.acme.api.get_configured_acme_base_url", lambda _settings: "https://ca.example.com/acme")
    monkeypatch.setattr("app.acme.api.create_acme_response", _acme_response)

    response = await create_order(request_data, _stub(), _stub(), settings)

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_create_order_without_aic_raises(
    monkeypatch: pytest.MonkeyPatch, request_data: JWSRequest, settings: Any, nonce_service: Any
) -> None:
    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: _stub())
    monkeypatch.setattr("app.acme.api.get_order_service", lambda _session: _stub())
    monkeypatch.setattr("app.acme.api.get_authorization_service", lambda _session: _stub())
    monkeypatch.setattr("app.acme.api.get_registry_client", lambda: _stub())
    monkeypatch.setattr(
        "app.acme.api.parse_jws_request", AsyncMock(return_value=({"kid": "k"}, {"identifiers": []}, "s"))
    )
    monkeypatch.setattr("app.acme.api.get_account_from_request", AsyncMock(return_value=_stub(id=1, aic="")))
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)

    with pytest.raises(AcmeException):
        await create_order(request_data, _stub(), _stub(), settings)


@pytest.mark.asyncio
async def test_get_order_success(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    order = SimpleNamespace(
        order_id="ord-1",
        account_id=1,
        status=OrderStatus.VALID,
        expires=datetime.now(UTC),
        identifiers=[{"type": "agent", "value": "AIC-001"}],
        authorizations=["a1"],
        certificate="https://ca.example.com/acme/cert/c1",
    )

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: _stub())
    monkeypatch.setattr(
        "app.acme.api.get_order_service",
        lambda _session: SimpleNamespace(get_order_by_id=AsyncMock(return_value=order)),
    )
    monkeypatch.setattr("app.acme.api.parse_jws_request", AsyncMock(return_value=({"kid": "k"}, {}, "s")))
    monkeypatch.setattr("app.acme.api.ensure_post_as_get_uses_empty_payload", lambda _req: None)
    monkeypatch.setattr("app.acme.api.get_account_from_request", AsyncMock(return_value=account))
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.acme.api.get_configured_acme_base_url", lambda _settings: "https://ca.example.com/acme")
    monkeypatch.setattr("app.acme.api.create_acme_response", _acme_response)

    response = await get_order("ord-1", request_data, _stub(), settings)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_authorization_success_and_deactivate(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    authorization = _stub(order=_stub(account_id=1))
    authorization_service = SimpleNamespace(
        get_authorization_by_id=AsyncMock(return_value=authorization),
        deactivate_authorization=AsyncMock(return_value=authorization),
        build_authorization_response=lambda _auth, _base: {
            "status": "valid",
            "identifier": {"type": "agent", "value": "AIC-001"},
        },
    )

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: _stub())
    monkeypatch.setattr("app.acme.api.get_authorization_service", lambda _session: authorization_service)
    monkeypatch.setattr(
        "app.acme.api.parse_jws_request", AsyncMock(return_value=({"kid": "k"}, {"status": "deactivated"}, "s"))
    )
    monkeypatch.setattr("app.acme.api.get_account_from_request", AsyncMock(return_value=account))
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.acme.api.get_configured_acme_base_url", lambda _settings: "https://ca.example.com/acme")
    monkeypatch.setattr("app.acme.api.create_acme_response", _acme_response)

    response = await get_authorization("a1", request_data, _stub(), _stub(), settings)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_finalize_order_success(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    order = SimpleNamespace(
        order_id="ord-1",
        account_id=1,
        status=OrderStatus.READY,
        expires=datetime.now(UTC),
        identifiers=[{"type": "agent", "value": "AIC-001", "usage": "clientAuth"}],
        authorizations=["a1"],
        certificate=None,
    )
    certificates = [SimpleNamespace(cert_id="c1")]

    order_service = SimpleNamespace(
        get_order_by_id=AsyncMock(return_value=order),
        update_order_status=AsyncMock(
            side_effect=[SimpleNamespace(**order.__dict__), SimpleNamespace(**order.__dict__)]
        ),
    )
    certificate_service = SimpleNamespace(
        decode_csr_payload=lambda _csr: b"csr",
        validate_order_agents=AsyncMock(return_value=[_stub(aic="AIC-001")]),
        issue_certificate=AsyncMock(return_value=certificates),
        notify_issued_certificates=AsyncMock(return_value=None),
    )

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: _stub())
    monkeypatch.setattr("app.acme.api.get_order_service", lambda _session: order_service)
    monkeypatch.setattr("app.acme.api.get_certificate_service", lambda _session: certificate_service)
    monkeypatch.setattr("app.acme.api.get_registry_client", lambda: _stub())
    monkeypatch.setattr("app.acme.api.parse_jws_request", AsyncMock(return_value=({"kid": "k"}, {"csr": "x"}, "s")))
    monkeypatch.setattr("app.acme.api.get_account_from_request", AsyncMock(return_value=account))
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.acme.api.get_configured_acme_base_url", lambda _settings: "https://ca.example.com/acme")
    monkeypatch.setattr("app.acme.api.create_acme_response", _acme_response)

    response = await finalize_order("ord-1", request_data, _stub(), _stub(), settings)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_certificate_success(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    cert = SimpleNamespace(order_id=1, certificate_pem="CERT\n")
    order = SimpleNamespace(account_id=1)

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: _stub())
    monkeypatch.setattr(
        "app.acme.api.get_certificate_service",
        lambda _session: SimpleNamespace(get_certificate_by_id=AsyncMock(return_value=cert)),
    )
    monkeypatch.setattr(
        "app.acme.api.get_order_service",
        lambda _session: SimpleNamespace(get_order_by_pk=AsyncMock(return_value=order)),
    )
    monkeypatch.setattr("app.acme.api.parse_jws_request", AsyncMock(return_value=({"kid": "k"}, {}, "s")))
    monkeypatch.setattr("app.acme.api.ensure_post_as_get_uses_empty_payload", lambda _req: None)
    monkeypatch.setattr("app.acme.api.get_account_from_request", AsyncMock(return_value=account))
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.acme.api.get_ca_manager", lambda: SimpleNamespace(get_issuer_chain_pem=lambda: "CHAIN\n"))

    response = await get_certificate("c1", request_data, _stub(), _stub(), settings)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_revoke_certificate_success_and_missing_identity(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    cert_service = SimpleNamespace(
        decode_revoke_certificate_payload=lambda _x: b"cert-der",
        validate_revocation_reason=lambda _x: 1,
        get_revocable_certificate=AsyncMock(return_value=(_stub(id="acme-cert"), _stub(public_key=lambda: "pk"))),
        public_key_to_jwk=lambda _pk: {"kty": "RSA"},
        revoke_certificate=AsyncMock(return_value=None),
    )

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: _stub())
    monkeypatch.setattr("app.acme.api.get_certificate_service", lambda _session: cert_service)
    monkeypatch.setattr(
        "app.acme.api.parse_jws_request", AsyncMock(return_value=({"kid": "k"}, {"certificate": "x", "reason": 1}, "s"))
    )
    monkeypatch.setattr("app.acme.api.get_account_from_request", AsyncMock(return_value=account))
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)

    response = await revoke_certificate(request_data, _stub(), settings)
    assert response.status_code == 200

    monkeypatch.setattr(
        "app.acme.api.parse_jws_request", AsyncMock(return_value=({}, {"certificate": "x", "reason": 1}, "s"))
    )
    with pytest.raises(AcmeException):
        await revoke_certificate(request_data, _stub(), settings)


@pytest.mark.asyncio
async def test_change_key_success(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    account_service = SimpleNamespace(apply_key_change=AsyncMock(return_value=account))

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: account_service)
    monkeypatch.setattr("app.acme.api.parse_jws_request", AsyncMock(return_value=({"kid": "k"}, {"payload": "x"}, "s")))
    monkeypatch.setattr("app.acme.api.get_account_from_request", AsyncMock(return_value=account))
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.acme.api.get_configured_acme_base_url", lambda _settings: "https://ca.example.com/acme")
    monkeypatch.setattr("app.acme.api.create_acme_response", _acme_response)

    response = await change_key(request_data, _stub(), settings)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_change_key_missing_payload_raises(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: _stub())
    monkeypatch.setattr("app.acme.api.parse_jws_request", AsyncMock(return_value=({"kid": "k"}, {}, "s")))
    monkeypatch.setattr("app.acme.api.get_account_from_request", AsyncMock(return_value=account))
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)

    with pytest.raises(AcmeException):
        await change_key(request_data, _stub(), settings)


@pytest.mark.asyncio
async def test_get_directory_and_new_nonce(settings: Any, nonce_service: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = await get_directory(_stub(), settings)
    assert directory.newAccount.endswith("/new-account")

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    response = await get_new_nonce(_stub())
    assert response.status_code == 200
    assert response.headers["Replay-Nonce"] == "nonce-1"


@pytest.mark.asyncio
async def test_get_ca_certificate_success_and_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.acme.api.get_ca_manager",
        lambda: SimpleNamespace(get_ca_certificate_pem=lambda: "PEM"),
    )
    ok = await get_ca_certificate()
    assert ok.status_code == 200

    monkeypatch.setattr(
        "app.acme.api.get_ca_manager",
        lambda: SimpleNamespace(get_ca_certificate_pem=lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
    )
    with pytest.raises(AcmeException):
        await get_ca_certificate()


@pytest.mark.asyncio
async def test_create_account_only_return_existing(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    account_service = SimpleNamespace(get_account_by_key_id=AsyncMock(return_value=account))

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: account_service)
    monkeypatch.setattr("app.acme.api.get_registry_client", lambda: _stub())
    monkeypatch.setattr(
        "app.acme.api.parse_jws_request",
        AsyncMock(return_value=({"jwk": _rsa_jwk}, {"onlyReturnExisting": True}, "s")),
    )
    monkeypatch.setattr("app.acme.api.JWKService.compute_jwk_thumbprint", lambda _jwk: "thumb")
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.acme.api.get_configured_acme_base_url", lambda _settings: "https://ca.example.com/acme")
    monkeypatch.setattr("app.acme.api.create_acme_response", _acme_response)

    response = await create_account(request_data, _stub(), _stub(), settings)
    assert response.status_code in (200, 201)


@pytest.mark.asyncio
async def test_create_account_new_account_path(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    account_service = SimpleNamespace(create_account=AsyncMock(return_value=account))

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: account_service)
    monkeypatch.setattr("app.acme.api.get_registry_client", lambda: _stub())
    monkeypatch.setattr(
        "app.acme.api.parse_jws_request",
        AsyncMock(
            return_value=(
                {"jwk": _rsa_jwk, "url": "https://ca.example.com/acme/new-account"},
                {"externalAccountBinding": {"protected": "p", "payload": "q", "signature": "s"}},
                "s",
            )
        ),
    )
    monkeypatch.setattr("app.acme.api.JWKService.compute_jwk_thumbprint", lambda _jwk: "thumb")
    monkeypatch.setattr("app.acme.api.JWKService.verify_new_account_signature", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.acme.api.verify_eab_binding", AsyncMock(return_value="AIC-001"))
    monkeypatch.setattr("app.acme.api.get_configured_acme_base_url", lambda _settings: "https://ca.example.com/acme")
    monkeypatch.setattr("app.acme.api.create_acme_response", _acme_response)

    response = await create_account(request_data, _stub(), _stub(), settings)
    assert response.status_code in (200, 201)


@pytest.mark.asyncio
async def test_create_account_missing_jwk_raises(
    monkeypatch: pytest.MonkeyPatch, request_data: JWSRequest, settings: Any, nonce_service: Any
) -> None:
    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: _stub())
    monkeypatch.setattr("app.acme.api.get_registry_client", lambda: _stub())
    monkeypatch.setattr("app.acme.api.parse_jws_request", AsyncMock(return_value=({}, {}, "s")))

    with pytest.raises(AcmeException):
        await create_account(request_data, _stub(), _stub(), settings)


@pytest.mark.asyncio
async def test_create_account_only_return_existing_not_found_raises(
    monkeypatch: pytest.MonkeyPatch, request_data: JWSRequest, settings: Any, nonce_service: Any
) -> None:
    account_service = SimpleNamespace(get_account_by_key_id=AsyncMock(return_value=None))

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: account_service)
    monkeypatch.setattr("app.acme.api.get_registry_client", lambda: _stub())
    monkeypatch.setattr(
        "app.acme.api.parse_jws_request",
        AsyncMock(return_value=({"jwk": _rsa_jwk}, {"onlyReturnExisting": True}, "s")),
    )
    monkeypatch.setattr("app.acme.api.JWKService.compute_jwk_thumbprint", lambda _jwk: "thumb")

    with pytest.raises(AcmeException):
        await create_account(request_data, _stub(), _stub(), settings)


@pytest.mark.asyncio
async def test_get_order_not_found_raises(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: _stub())
    monkeypatch.setattr(
        "app.acme.api.get_order_service", lambda _session: SimpleNamespace(get_order_by_id=AsyncMock(return_value=None))
    )
    monkeypatch.setattr("app.acme.api.parse_jws_request", AsyncMock(return_value=({"kid": "k"}, {}, "s")))
    monkeypatch.setattr("app.acme.api.ensure_post_as_get_uses_empty_payload", lambda _req: None)
    monkeypatch.setattr("app.acme.api.get_account_from_request", AsyncMock(return_value=account))
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)

    with pytest.raises(AcmeException):
        await get_order("ord-404", request_data, _stub(), settings)


@pytest.mark.asyncio
async def test_finalize_order_not_ready_raises(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    order = SimpleNamespace(order_id="ord-1", account_id=1, status=OrderStatus.PENDING)

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: _stub())
    monkeypatch.setattr(
        "app.acme.api.get_order_service",
        lambda _session: SimpleNamespace(get_order_by_id=AsyncMock(return_value=order)),
    )
    monkeypatch.setattr("app.acme.api.get_certificate_service", lambda _session: _stub())
    monkeypatch.setattr("app.acme.api.get_registry_client", lambda: _stub())
    monkeypatch.setattr("app.acme.api.parse_jws_request", AsyncMock(return_value=({"kid": "k"}, {"csr": "x"}, "s")))
    monkeypatch.setattr("app.acme.api.get_account_from_request", AsyncMock(return_value=account))
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)

    with pytest.raises(AcmeException):
        await finalize_order("ord-1", request_data, _stub(), _stub(), settings)


@pytest.mark.asyncio
async def test_get_authorization_malformed_payload_raises(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    authorization = _stub(order=_stub(account_id=1))
    authorization_service = SimpleNamespace(
        get_authorization_by_id=AsyncMock(return_value=authorization),
        deactivate_authorization=AsyncMock(return_value=authorization),
        build_authorization_response=lambda _auth, _base: {"status": "valid"},
    )

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: _stub())
    monkeypatch.setattr("app.acme.api.get_authorization_service", lambda _session: authorization_service)
    monkeypatch.setattr(
        "app.acme.api.parse_jws_request", AsyncMock(return_value=({"kid": "k"}, {"status": "invalid"}, "s"))
    )
    monkeypatch.setattr("app.acme.api.get_account_from_request", AsyncMock(return_value=account))
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)

    with pytest.raises(AcmeException):
        await get_authorization("a1", request_data, _stub(), _stub(), settings)


@pytest.mark.asyncio
async def test_revoke_certificate_with_jwk_path(
    monkeypatch: pytest.MonkeyPatch, request_data: JWSRequest, settings: Any, nonce_service: Any
) -> None:
    cert_service = SimpleNamespace(
        decode_revoke_certificate_payload=lambda _x: b"cert-der",
        validate_revocation_reason=lambda _x: 1,
        get_revocable_certificate=AsyncMock(return_value=(_stub(id="acme-cert"), _stub(public_key=lambda: "pk"))),
        public_key_to_jwk=lambda _pk: {"kty": "RSA"},
        revoke_certificate=AsyncMock(return_value=None),
    )

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: _stub())
    monkeypatch.setattr("app.acme.api.get_certificate_service", lambda _session: cert_service)
    monkeypatch.setattr(
        "app.acme.api.parse_jws_request",
        AsyncMock(return_value=({"jwk": {"kty": "RSA"}}, {"certificate": "x", "reason": 1}, "s")),
    )
    monkeypatch.setattr("app.acme.api.get_account_by_jwk", AsyncMock(return_value=None))
    monkeypatch.setattr("app.acme.api.verify_jws_signature_with_jwk", lambda *_args, **_kwargs: True)

    response = await revoke_certificate(request_data, _stub(), settings)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_finalize_order_multi_certificates_branch(
    monkeypatch: pytest.MonkeyPatch,
    request_data: JWSRequest,
    settings: Any,
    account: Any,
    nonce_service: Any,
) -> None:
    order = SimpleNamespace(
        order_id="ord-2",
        account_id=1,
        status=OrderStatus.READY,
        expires=datetime.now(UTC),
        identifiers=[{"type": "agent", "value": "AIC-001", "usage": "clientAuth"}],
        authorizations=["a1"],
        certificate=None,
    )
    certificates = [SimpleNamespace(cert_id="c1"), SimpleNamespace(cert_id="c2")]

    order_service = SimpleNamespace(
        get_order_by_id=AsyncMock(return_value=order),
        update_order_status=AsyncMock(
            side_effect=[SimpleNamespace(**order.__dict__), SimpleNamespace(**order.__dict__)]
        ),
    )
    certificate_service = SimpleNamespace(
        decode_csr_payload=lambda _csr: b"csr",
        validate_order_agents=AsyncMock(return_value=[_stub(aic="AIC-001")]),
        issue_certificate=AsyncMock(return_value=certificates),
        notify_issued_certificates=AsyncMock(return_value=None),
    )

    monkeypatch.setattr("app.acme.api.get_nonce_service", lambda _session: nonce_service)
    monkeypatch.setattr("app.acme.api.get_account_service", lambda _session: _stub())
    monkeypatch.setattr("app.acme.api.get_order_service", lambda _session: order_service)
    monkeypatch.setattr("app.acme.api.get_certificate_service", lambda _session: certificate_service)
    monkeypatch.setattr("app.acme.api.get_registry_client", lambda: _stub())
    monkeypatch.setattr("app.acme.api.parse_jws_request", AsyncMock(return_value=({"kid": "k"}, {"csr": "x"}, "s")))
    monkeypatch.setattr("app.acme.api.get_account_from_request", AsyncMock(return_value=account))
    monkeypatch.setattr("app.acme.api.verify_jws_signature", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.acme.api.get_configured_acme_base_url", lambda _settings: "https://ca.example.com/acme")
    monkeypatch.setattr("app.acme.api.create_acme_response", _acme_response)

    response = await finalize_order("ord-2", request_data, _stub(), _stub(), settings)
    assert response.status_code == 200
