"""crl/api.py 单元测试 - mock CRLService。"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.common import CRL, CRLStatus, RevokedCertificateEntry
from app.common.crl_service import CRLService
from app.core.base_exception import register_exception_handlers
from app.core.config import get_settings
from app.crl.api import get_crl_service, router
from app.crl.exception import CRLGenerationFailedError, CRLNotFoundError


def _make_crl(
    version: str = "2025061012",
    status: CRLStatus = CRLStatus.CURRENT,
    revoked_count: int = 0,
) -> CRL:
    now = datetime.now(tz=UTC)
    return CRL(
        id=uuid4(),
        version=version,
        crl_number=1,
        issuer="CN=Test CA",
        this_update=now,
        next_update=now,
        status=status,
        revoked_certificates_count=revoked_count,
        crl_der=b"\x30\x82\x01\x01",
        crl_pem="-----BEGIN X509 CRL-----\nfake\n-----END X509 CRL-----",
        crl_size=100,
        distribution_points=["http://example.com/crl"],
        signature_algorithm="sha256WithRSAEncryption",
        signature_key_id="AABB",
        created_at=now,
    )


@pytest.fixture()
def mock_service() -> CRLService:
    svc = MagicMock(spec=CRLService)
    for attr in dir(svc):
        if not attr.startswith("_") and callable(getattr(svc, attr)):
            setattr(svc, attr, AsyncMock())
    return svc


@pytest.fixture()
def app_with_mock(mock_service: CRLService) -> Any:
    test_app = FastAPI()
    register_exception_handlers(test_app)
    test_app.include_router(router, prefix="/acps-atr-v2/crl")
    test_app.dependency_overrides[get_crl_service] = lambda: mock_service
    return test_app


@pytest.fixture()
def client(app_with_mock: Any) -> TestClient:
    return TestClient(
        app_with_mock,
        raise_server_exceptions=False,
        headers={"Authorization": f"Bearer {os.environ['CA_SERVER_ADMIN_API_TOKEN']}"},
    )


class TestDownloadCRL:
    def test_default_der_format(self, client: TestClient, mock_service: MagicMock) -> None:
        crl = _make_crl()
        mock_service.get_current_crl.return_value = crl
        resp = client.get("/acps-atr-v2/crl")
        assert resp.status_code == 200
        assert resp.content == crl.crl_der
        assert resp.headers["cache-control"] == "max-age=3600"
        assert resp.headers["expires"] == crl.next_update.strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert resp.headers["etag"] == f'"{crl.version}"'
        assert resp.headers["last-modified"] == crl.this_update.strftime("%a, %d %b %Y %H:%M:%S GMT")

    def test_pem_format(self, client: TestClient, mock_service: MagicMock) -> None:
        crl = _make_crl()
        mock_service.get_current_crl.return_value = crl
        resp = client.get("/acps-atr-v2/crl?format=pem")
        assert resp.status_code == 200
        assert "BEGIN X509 CRL" in resp.text
        assert resp.headers["expires"] == crl.next_update.strftime("%a, %d %b %Y %H:%M:%S GMT")


class TestCRLAdminAuth:
    def test_list_requires_auth(self, app_with_mock: Any) -> None:
        client = TestClient(app_with_mock, raise_server_exceptions=False)

        resp = client.get("/acps-atr-v2/crl/list")

        assert resp.status_code == 401

    def test_list_rejects_internal_service_token(self, app_with_mock: Any) -> None:
        client = TestClient(
            app_with_mock,
            raise_server_exceptions=False,
            headers={"Authorization": f"Bearer {os.environ['CA_SERVER_INTERNAL_API_TOKEN']}"},
        )

        resp = client.get("/acps-atr-v2/crl/list")

        assert resp.status_code == 401

    def test_no_crl_triggers_generation(self, client: TestClient, mock_service: MagicMock) -> None:
        crl = _make_crl()
        mock_service.get_current_crl.return_value = None
        mock_service.generate_new_crl.return_value = crl
        resp = client.get("/acps-atr-v2/crl")
        assert resp.status_code == 200
        mock_service.generate_new_crl.assert_called_once()

    def test_generation_failure_returns_500(self, client: TestClient, mock_service: MagicMock) -> None:
        mock_service.get_current_crl.return_value = None
        mock_service.generate_new_crl.side_effect = RuntimeError("CA unavailable")
        resp = client.get("/acps-atr-v2/crl")
        assert resp.status_code == 500

    def test_crl_app_error_reraises(self, client: TestClient, mock_service: MagicMock) -> None:
        mock_service.get_current_crl.return_value = None
        mock_service.generate_new_crl.side_effect = CRLGenerationFailedError()
        resp = client.get("/acps-atr-v2/crl")
        assert resp.status_code == 500


class TestGetCurrentCRL:
    def test_success(self, client: TestClient, mock_service: MagicMock) -> None:
        crl = _make_crl()
        mock_service.get_current_crl.return_value = crl
        resp = client.get("/acps-atr-v2/crl/current")
        assert resp.status_code == 200
        assert resp.content == crl.crl_der

    def test_not_found(self, client: TestClient, mock_service: MagicMock) -> None:
        mock_service.get_current_crl.return_value = None
        resp = client.get("/acps-atr-v2/crl/current")
        assert resp.status_code == 404


class TestGetCurrentCRLPEM:
    def test_success(self, client: TestClient, mock_service: MagicMock) -> None:
        crl = _make_crl()
        mock_service.get_current_crl.return_value = crl
        resp = client.get("/acps-atr-v2/crl/current/pem")
        assert resp.status_code == 200
        assert "BEGIN X509 CRL" in resp.text

    def test_not_found(self, client: TestClient, mock_service: MagicMock) -> None:
        mock_service.get_current_crl.return_value = None
        resp = client.get("/acps-atr-v2/crl/current/pem")
        assert resp.status_code == 404


class TestGetCRLInfo:
    def test_success_with_distribution_point(self, client: TestClient, mock_service: MagicMock) -> None:
        crl = _make_crl()
        mock_service.get_current_crl.return_value = crl
        resp = client.get("/acps-atr-v2/crl/info")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "2025061012"
        assert data["issuer"] == "CN=Test CA"

    def test_success_without_distribution_points(self, client: TestClient, mock_service: MagicMock) -> None:
        crl = _make_crl()
        crl.distribution_points = []
        mock_service.get_current_crl.return_value = crl
        with MagicMock() as _:
            # get_settings() 会被调用来获取 crl_distribution_point_url
            resp = client.get("/acps-atr-v2/crl/info")
        assert resp.status_code == 200

    def test_not_found(self, client: TestClient, mock_service: MagicMock) -> None:
        mock_service.get_current_crl.return_value = None
        resp = client.get("/acps-atr-v2/crl/info")
        assert resp.status_code == 404


class TestGetCRLByVersion:
    def test_success(self, client: TestClient, mock_service: MagicMock) -> None:
        crl = _make_crl(version="2025010100")
        mock_service.get_crl_by_version.return_value = crl
        resp = client.get("/acps-atr-v2/crl/version/2025010100")
        assert resp.status_code == 200
        mock_service.get_crl_by_version.assert_called_once_with("2025010100")

    def test_not_found(self, client: TestClient, mock_service: MagicMock) -> None:
        mock_service.get_crl_by_version.return_value = None
        resp = client.get("/acps-atr-v2/crl/version/9999999999")
        assert resp.status_code == 404


class TestGetCRLDistributionPoints:
    def test_success(self, client: TestClient, mock_service: MagicMock) -> None:
        mock_service.get_crl_distribution_points.return_value = {
            "primary": "http://example.com/crl",
            "mirrors": [],
            "update_interval": "PT24H",
            "max_age": "PT1H",
        }
        resp = client.get("/acps-atr-v2/crl/distribution-points")
        assert resp.status_code == 200


class TestGetCRLList:
    def test_basic_list(self, client: TestClient, mock_service: MagicMock) -> None:
        crls = [_make_crl()]
        mock_service.get_crl_list.return_value = (crls, 1)
        resp = client.get("/acps-atr-v2/crl/list")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1

    def test_empty_list(self, client: TestClient, mock_service: MagicMock) -> None:
        mock_service.get_crl_list.return_value = ([], 0)
        resp = client.get("/acps-atr-v2/crl/list")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_pagination_params(self, client: TestClient, mock_service: MagicMock) -> None:
        mock_service.get_crl_list.return_value = ([], 0)
        resp = client.get("/acps-atr-v2/crl/list?page=2&page_size=5")
        assert resp.status_code == 200
        mock_service.get_crl_list.assert_called_once_with(status=None, page=2, page_size=5)


class TestRefreshCRL:
    def test_success(self, client: TestClient, mock_service: MagicMock) -> None:
        new_crl = _make_crl(version="2025061013")
        mock_service.generate_new_crl.return_value = new_crl
        resp = client.post("/acps-atr-v2/crl/refresh")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "2025061013"

    def test_success_empty_distribution_points(self, client: TestClient, mock_service: MagicMock) -> None:
        new_crl = _make_crl()
        new_crl.distribution_points = []
        mock_service.generate_new_crl.return_value = new_crl
        resp = client.post("/acps-atr-v2/crl/refresh")
        assert resp.status_code == 200
        # 兜底分发点必须取自配置：写死的占位地址会被签进证书，事后变更要全平面重签。
        assert resp.json()["distribution_point"] == get_settings().crl_distribution_point_url

    def test_runtime_error_wrapped(self, client: TestClient, mock_service: MagicMock) -> None:
        mock_service.generate_new_crl.side_effect = RuntimeError("signing error")
        resp = client.post("/acps-atr-v2/crl/refresh")
        assert resp.status_code == 500

    def test_app_error_reraises(self, client: TestClient, mock_service: MagicMock) -> None:
        mock_service.generate_new_crl.side_effect = CRLNotFoundError()
        resp = client.post("/acps-atr-v2/crl/refresh")
        assert resp.status_code == 404


class TestGetCRLDetail:
    def test_success(self, client: TestClient, mock_service: MagicMock) -> None:
        crl = _make_crl(revoked_count=2)
        now = datetime.now(tz=UTC)
        entries = [
            RevokedCertificateEntry(
                id=uuid4(),
                crl_id=crl.id,
                serial_number="AABB",
                revocation_date=now,
                revocation_reason="unspecified",
            ),
        ]
        mock_service.get_current_crl.return_value = crl
        mock_service.get_revoked_entries_for_crl.return_value = entries
        resp = client.get("/acps-atr-v2/crl/detail")
        assert resp.status_code == 200
        data = resp.json()
        assert data["revokedCertificatesCount"] == 1

    def test_no_current_crl_returns_404(self, client: TestClient, mock_service: MagicMock) -> None:
        mock_service.get_current_crl.return_value = None
        resp = client.get("/acps-atr-v2/crl/detail")
        assert resp.status_code == 404

    def test_app_error_reraises(self, client: TestClient, mock_service: MagicMock) -> None:
        crl = _make_crl()
        mock_service.get_current_crl.return_value = crl
        mock_service.get_revoked_entries_for_crl.side_effect = CRLNotFoundError()
        resp = client.get("/acps-atr-v2/crl/detail")
        assert resp.status_code == 404

    def test_runtime_error_wrapped(self, client: TestClient, mock_service: MagicMock) -> None:
        crl = _make_crl()
        mock_service.get_current_crl.return_value = crl
        mock_service.get_revoked_entries_for_crl.side_effect = RuntimeError("db error")
        resp = client.get("/acps-atr-v2/crl/detail")
        assert resp.status_code == 500
