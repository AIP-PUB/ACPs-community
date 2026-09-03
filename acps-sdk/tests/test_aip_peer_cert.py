from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from acps_sdk.aip import (
    AipPeerCertificateMiddleware,
    InvalidPeerCertificateError,
    PeerCertificateRegistry,
    extract_peer_identity,
    get_request_peer_aic,
)

VALID_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5"


def _cert(
    *,
    common_name: str | None = VALID_AIC,
    san_aic: str | None = None,
) -> dict:
    cert: dict = {}
    if common_name is not None:
        cert["subject"] = ((("commonName", common_name),),)
    if san_aic is not None:
        cert["subjectAltName"] = (("URI", f"acps://{san_aic}"),)
    return cert


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/rpc",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 50123),
            "server": ("127.0.0.1", 8443),
        }
    )


def test_registry_register_lookup_unregister() -> None:
    registry = PeerCertificateRegistry()
    registry.register(("127.0.0.1", 1), ("127.0.0.1", 2), _cert())
    info = registry.lookup(("127.0.0.1", 1), ("127.0.0.1", 2))
    assert info is not None
    assert info.peer_aic == VALID_AIC
    registry.unregister(("127.0.0.1", 1), ("127.0.0.1", 2))
    assert registry.lookup(("127.0.0.1", 1), ("127.0.0.1", 2)) is None


def test_registry_stores_peer_identity() -> None:
    registry = PeerCertificateRegistry()
    registry.register(("127.0.0.1", 11), ("127.0.0.1", 22), _cert(san_aic=VALID_AIC))
    info = registry.lookup(("127.0.0.1", 11), ("127.0.0.1", 22))
    assert info is not None
    assert info.peer_identity == extract_peer_identity(_cert(san_aic=VALID_AIC))
    assert info.peer_aic == VALID_AIC


def test_registry_preserves_identity_error() -> None:
    registry = PeerCertificateRegistry()
    registry.register(
        ("127.0.0.1", 111),
        ("127.0.0.1", 222),
        _cert(common_name=VALID_AIC, san_aic=OTHER_AIC),
    )
    info = registry.lookup(("127.0.0.1", 111), ("127.0.0.1", 222))
    assert info is not None
    assert isinstance(info.identity_error, InvalidPeerCertificateError)
    assert info.peer_identity is None
    assert info.common_name == VALID_AIC


@pytest.mark.asyncio
async def test_middleware_injects_peer_cert_state() -> None:
    registry = PeerCertificateRegistry()
    registry.register(("127.0.0.1", 50123), ("127.0.0.1", 8443), _cert(san_aic=VALID_AIC))
    middleware = AipPeerCertificateMiddleware(app=lambda scope, receive, send: None)
    request = _request()

    async def call_next(req: Request):
        assert req.state.peer_certificate == _cert(san_aic=VALID_AIC)
        assert req.state.peer_common_name == VALID_AIC
        assert req.state.peer_identity is not None
        assert req.state.peer_aic == VALID_AIC
        assert req.state.peer_identity_error is None
        return JSONResponse({"ok": True})

    from acps_sdk.aip import aip_peer_cert

    original_registry = aip_peer_cert.registry
    aip_peer_cert.registry = registry
    try:
        response = await middleware.dispatch(request, call_next)
    finally:
        aip_peer_cert.registry = original_registry
    assert response.status_code == 200


def test_get_request_peer_aic_raises_identity_error() -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(
            peer_identity_error=InvalidPeerCertificateError("bad cert"),
            peer_aic=None,
            peer_identity=None,
        )
    )
    with pytest.raises(InvalidPeerCertificateError):
        get_request_peer_aic(request)  # type: ignore[arg-type]


def test_get_request_peer_aic_returns_none_without_state() -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(
            peer_identity_error=None,
            peer_aic=None,
            peer_identity=None,
        )
    )
    assert get_request_peer_aic(request) is None  # type: ignore[arg-type]
