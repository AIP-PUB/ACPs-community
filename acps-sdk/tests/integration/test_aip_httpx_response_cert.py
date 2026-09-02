from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from acps_sdk.aip.aip_identity import extract_peer_aic_from_httpx_response

from ._tls_test_utils import (
    LEADER_CLIENT_CERT,
    LEADER_CLIENT_KEY,
    LEADER_TRUST_BUNDLE,
    PARTNER_AIC,
    PARTNER_SERVER_CERT,
    PARTNER_SERVER_KEY,
    PARTNER_TRUST_BUNDLE,
    build_client_ssl_context,
    build_server_ssl_context,
    run_tls_app,
)


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/json")
    async def json_endpoint() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/fail")
    async def fail_endpoint() -> PlainTextResponse:
        return PlainTextResponse("boom", status_code=500)

    @app.get("/stream")
    async def stream_endpoint() -> StreamingResponse:
        async def _gen():
            yield b"chunk-1\n"
            await asyncio.sleep(0)
            yield b"chunk-2\n"

        return StreamingResponse(_gen(), media_type="text/plain")

    return app


@pytest.mark.asyncio
async def test_extract_peer_aic_from_real_tls_responses() -> None:
    app = _build_app()
    server_ssl = build_server_ssl_context(
        cert_file=PARTNER_SERVER_CERT,
        key_file=PARTNER_SERVER_KEY,
        ca_file=PARTNER_TRUST_BUNDLE,
        require_client_cert=False,
    )
    client_ssl = build_client_ssl_context(
        cert_file=LEADER_CLIENT_CERT,
        key_file=LEADER_CLIENT_KEY,
        ca_file=LEADER_TRUST_BUNDLE,
    )

    with run_tls_app(app, ssl_context=server_ssl) as base_url:
        async with httpx.AsyncClient(verify=client_ssl, timeout=10.0) as client:
            ok_response = await client.get(f"{base_url}/json")
            fail_response = await client.get(f"{base_url}/fail")
            reuse_response = await client.get(f"{base_url}/json")

            assert extract_peer_aic_from_httpx_response(ok_response) == PARTNER_AIC
            assert extract_peer_aic_from_httpx_response(fail_response) == PARTNER_AIC
            assert extract_peer_aic_from_httpx_response(reuse_response) == PARTNER_AIC

            async with client.stream("GET", f"{base_url}/stream") as stream_response:
                assert extract_peer_aic_from_httpx_response(stream_response) == PARTNER_AIC
                body = b"".join([chunk async for chunk in stream_response.aiter_bytes()])

    assert ok_response.status_code == 200
    assert fail_response.status_code == 500
    assert body == b"chunk-1\nchunk-2\n"


@pytest.mark.asyncio
async def test_extract_peer_aic_returns_none_for_asgi_transport() -> None:
    app = _build_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/json")

    assert extract_peer_aic_from_httpx_response(response) is None
