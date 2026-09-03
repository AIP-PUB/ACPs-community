"""单元测试 — acme 模块（base64url、JWK、JWS、AcmeError）。"""

import base64
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID

from acps_cli.ca.acme import (
    AcmeClient,
    AcmeError,
    base64url_decode,
    base64url_encode,
    get_jwk,
    get_jwk_thumbprint,
    normalize_runtime_url,
    public_jwk_projection,
)
from acps_cli.ca.keys import generate_private_key


def _build_leaf_and_issuer_pem() -> tuple[bytes, bytes]:
    issuer_key = generate_private_key("ec")
    leaf_key = generate_private_key("ec")
    now = datetime.now(timezone.utc)

    issuer_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Issuer")])
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Leaf")])

    issuer_cert = (
        x509.CertificateBuilder()
        .subject_name(issuer_name)
        .issuer_name(issuer_name)
        .public_key(issuer_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key=issuer_key, algorithm=hashes.SHA256())
    )

    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(issuer_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=7))
        .sign(private_key=issuer_key, algorithm=hashes.SHA256())
    )

    return (
        leaf_cert.public_bytes(serialization.Encoding.PEM),
        issuer_cert.public_bytes(serialization.Encoding.PEM),
    )


# ---------------------------------------------------------------------------
# base64url_encode
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestBase64UrlEncode:
    def test_encode_bytes(self):
        result = base64url_encode(b"\x00\x01\x02")
        assert isinstance(result, str)
        # 不应包含 padding '='
        assert "=" not in result

    def test_encode_string(self):
        result = base64url_encode("hello")
        decoded = base64.urlsafe_b64decode(result + "==")
        assert decoded == b"hello"

    def test_empty_input(self):
        assert base64url_encode(b"") == ""

    def test_url_safe_characters(self):
        # 包含 +/ 的源数据在 base64url 中应被替换为 -_
        data = b"\xfb\xff\xfe"
        result = base64url_encode(data)
        assert "+" not in result
        assert "/" not in result


# ---------------------------------------------------------------------------
# get_jwk
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestGetJWK:
    def test_ec_key_jwk_fields(self):
        key = generate_private_key("ec")
        jwk = get_jwk(key)
        assert jwk["kty"] == "EC"
        assert jwk["crv"] == "P-256"
        assert "x" in jwk
        assert "y" in jwk

    def test_rsa_key_jwk_fields(self):
        key = generate_private_key("rsa")
        jwk = get_jwk(key)
        assert jwk["kty"] == "RSA"
        assert "n" in jwk
        assert "e" in jwk

    def test_ed25519_key_jwk_fields(self):
        key = generate_private_key("ed25519")
        jwk = get_jwk(key)
        assert jwk == public_jwk_projection(jwk)
        assert jwk["kty"] == "OKP"
        assert jwk["crv"] == "Ed25519"
        assert "x" in jwk

    def test_unsupported_key_type_raises(self):
        with pytest.raises(ValueError, match="Unsupported key type"):
            get_jwk("not-a-key")


# ---------------------------------------------------------------------------
# get_jwk_thumbprint
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestGetJWKThumbprint:
    def test_deterministic(self):
        key = generate_private_key("ec")
        jwk = get_jwk(key)
        t1 = get_jwk_thumbprint(jwk)
        t2 = get_jwk_thumbprint(jwk)
        assert t1 == t2

    def test_different_keys_different_thumbprints(self):
        k1 = generate_private_key("ec")
        k2 = generate_private_key("ec")
        t1 = get_jwk_thumbprint(get_jwk(k1))
        t2 = get_jwk_thumbprint(get_jwk(k2))
        assert t1 != t2

    def test_okp_thumbprint_supported(self):
        jwk = get_jwk(generate_private_key("ed25519"))
        assert len(get_jwk_thumbprint(jwk)) > 0


@pytest.mark.unit
class TestPublicJWKProjection:
    def test_rsa_private_fields_rejected(self):
        with pytest.raises(ValueError, match="private key material"):
            public_jwk_projection({"kty": "RSA", "n": "abc", "e": "AQAB", "d": "secret"})

    def test_ec_private_fields_rejected(self):
        with pytest.raises(ValueError, match="private key material"):
            public_jwk_projection({"kty": "EC", "crv": "P-256", "x": "abc", "y": "def", "d": "secret"})

    def test_okp_private_fields_rejected(self):
        with pytest.raises(ValueError, match="private key material"):
            public_jwk_projection({"kty": "OKP", "crv": "Ed25519", "x": "abc", "d": "secret"})


# ---------------------------------------------------------------------------
# AcmeClient._build_jws (class method, no network)
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestBuildJWS:
    def test_jws_structure_with_jwk(self):
        key = generate_private_key("ec")
        jwk = get_jwk(key)
        jws = AcmeClient._build_jws(
            key=key,
            payload={"test": "data"},
            url="https://example.com/acme/new-acct",
            nonce="fake-nonce",
            jwk=jwk,
        )
        assert "protected" in jws
        assert "payload" in jws
        assert "signature" in jws

        # 解码 protected header 验证内容
        protected_json = base64.urlsafe_b64decode(jws["protected"] + "==")
        protected = json.loads(protected_json)
        assert protected["alg"] == "ES256"
        assert protected["nonce"] == "fake-nonce"
        assert protected["url"] == "https://example.com/acme/new-acct"
        assert "jwk" in protected

    def test_jws_structure_with_kid(self):
        key = generate_private_key("ec")
        jws = AcmeClient._build_jws(
            key=key,
            payload=None,
            url="https://example.com/acme/orders/1",
            nonce="nonce-2",
            kid="https://example.com/acme/acct/123",
        )
        protected_json = base64.urlsafe_b64decode(jws["protected"] + "==")
        protected = json.loads(protected_json)
        assert protected["kid"] == "https://example.com/acme/acct/123"
        assert "jwk" not in protected
        # payload 应为空字符串 (POST-as-GET)
        assert jws["payload"] == ""

    def test_jws_rsa_key(self):
        key = generate_private_key("rsa")
        jwk = get_jwk(key)
        jws = AcmeClient._build_jws(
            key=key,
            payload={"foo": "bar"},
            nonce="n",
            jwk=jwk,
        )
        protected_json = base64.urlsafe_b64decode(jws["protected"] + "==")
        protected = json.loads(protected_json)
        assert protected["alg"] == "RS256"

    def test_jws_requires_kid_or_jwk(self):
        key = generate_private_key("ec")
        with pytest.raises(ValueError, match="Either kid or jwk"):
            AcmeClient._build_jws(key=key, payload={})

    def test_build_eab_jws_uses_hs256(self):
        key = generate_private_key("ec")
        jwk = get_jwk(key)
        jws = AcmeClient._build_eab_jws(
            key_id="kid-1",
            mac_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY",
            account_jwk=jwk,
            new_account_url="https://example.com/acme/new-account",
        )

        protected_json = json.loads(base64url_decode(jws["protected"]).decode("utf-8"))
        payload_json = json.loads(base64url_decode(jws["payload"]).decode("utf-8"))

        assert protected_json["alg"] == "HS256"
        assert protected_json["kid"] == "kid-1"
        assert protected_json["url"] == "https://example.com/acme/new-account"
        assert payload_json == public_jwk_projection(jwk)


# ---------------------------------------------------------------------------
# AcmeClient 初始化
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestAcmeClientInit:
    def test_init_with_key(self):
        key = generate_private_key("ec")
        client = AcmeClient("http://localhost:8003", key)
        assert client.ca_server_url == "http://localhost:8003"
        assert client.jwk is not None
        assert client.thumbprint is not None
        assert client.directory is None
        assert client.account_url is None

    def test_init_strips_trailing_slash(self):
        key = generate_private_key("ec")
        client = AcmeClient("http://localhost:8003/", key)
        assert client.ca_server_url == "http://localhost:8003"

    def test_init_without_key(self):
        client = AcmeClient("http://localhost:8003", None)
        assert client.jwk is None
        assert client.thumbprint is None

    def test_init_with_admin_token(self):
        client = AcmeClient("http://localhost:8003", None, admin_api_token=" admin-token ")
        assert client.admin_api_token == "admin-token"


@pytest.mark.unit
class TestAcmePostRetries:
    def test_post_retries_once_on_bad_nonce(self, monkeypatch):
        key = generate_private_key("ec")
        client = AcmeClient("http://localhost:8003", key)
        client.account_url = "http://localhost:8003/acme/acct/1"
        client.nonce = "stale-nonce"

        first_response = Mock(status_code=400, headers={"Replay-Nonce": "fresh-nonce"})
        first_response.json.return_value = {"error_name": "BAD_NONCE"}
        second_response = Mock(status_code=200, headers={"Replay-Nonce": "final-nonce"})

        responses = [first_response, second_response]
        calls: list[dict[str, object]] = []

        def _fake_post(url, json, headers, timeout):
            calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
            return responses.pop(0)

        monkeypatch.setattr("acps_cli.ca.acme.httpx.post", _fake_post)

        result = client._post("http://localhost:8003/acme/finalize", {"csr": "abc"})

        assert result is second_response
        assert len(calls) == 2
        assert client.nonce == "final-nonce"

    def test_post_raises_when_bad_nonce_retry_is_exhausted(self, monkeypatch):
        key = generate_private_key("ec")
        client = AcmeClient("http://localhost:8003", key)
        client.account_url = "http://localhost:8003/acme/acct/1"
        client.nonce = "stale-nonce"

        response = Mock(status_code=400, headers={})
        monkeypatch.setattr("acps_cli.ca.acme.httpx.post", lambda *args, **kwargs: response)
        monkeypatch.setattr(client, "get_nonce", lambda: "refetched-nonce")

        with pytest.raises(AcmeError, match="ACME Request Failed"):
            client._post("http://localhost:8003/acme/finalize", {"csr": "abc"})


@pytest.mark.unit
class TestNewAccountWithEab:
    def test_new_account_requires_eab_for_creation(self):
        key = generate_private_key("ec")
        client = AcmeClient("http://localhost:8003", key)
        client.directory = {"newAccount": "https://example.com/acme/new-account"}

        with pytest.raises(ValueError, match="eab_credential is required"):
            client.new_account()

    def test_new_account_attaches_external_account_binding(self, monkeypatch):
        key = generate_private_key("ec")
        client = AcmeClient("http://localhost:8003", key)
        client.directory = {"newAccount": "https://example.com/acme/new-account"}
        captured = {}

        class DummyResponse:
            headers = {"Location": "https://example.com/acme/acct/1"}

            def json(self):
                return {"status": "valid"}

        monkeypatch.setattr(
            client,
            "_post",
            lambda url, payload: captured.update({"url": url, "payload": payload}) or DummyResponse(),
        )

        client.new_account(
            eab_credential={
                "keyId": "kid-1",
                "macKey": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY",
            }
        )

        assert captured["url"] == "https://example.com/acme/new-account"
        assert "externalAccountBinding" in captured["payload"]
        assert client.account_url == "https://example.com/acme/acct/1"


# ---------------------------------------------------------------------------
# Runtime URL normalization
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestRuntimeNormalization:
    def test_normalize_runtime_url_for_host_process(self, monkeypatch):
        monkeypatch.delenv("ACPS_CONTAINER_MODE", raising=False)
        monkeypatch.setattr("acps_cli.ca.acme.os.path.exists", lambda path: False)

        assert (
            normalize_runtime_url("http://host.docker.internal:9000/ca-server/acps-atr-v2/acme/new-nonce")
            == "http://localhost:9000/ca-server/acps-atr-v2/acme/new-nonce"
        )

    def test_normalize_runtime_url_keeps_container_address(self, monkeypatch):
        monkeypatch.setenv("ACPS_CONTAINER_MODE", "true")

        assert (
            normalize_runtime_url("http://host.docker.internal:9000/ca-server/acps-atr-v2/acme/new-nonce")
            == "http://host.docker.internal:9000/ca-server/acps-atr-v2/acme/new-nonce"
        )

    def test_get_directory_normalizes_advertised_urls_on_host(self, monkeypatch):
        monkeypatch.delenv("ACPS_CONTAINER_MODE", raising=False)
        monkeypatch.setattr("acps_cli.ca.acme.os.path.exists", lambda path: False)

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "newNonce": "http://host.docker.internal:9000/ca-server/acps-atr-v2/acme/new-nonce",
            "newAccount": "http://host.docker.internal:9000/ca-server/acps-atr-v2/acme/new-account",
        }
        monkeypatch.setattr("acps_cli.ca.acme.httpx.get", lambda url, **kwargs: response)

        client = AcmeClient("http://localhost:9000/ca-server/acps-atr-v2", None)
        directory = client.get_directory()

        assert directory["newNonce"] == "http://localhost:9000/ca-server/acps-atr-v2/acme/new-nonce"
        assert directory["newAccount"] == "http://localhost:9000/ca-server/acps-atr-v2/acme/new-account"

    def test_get_directory_rewrites_localhost_origin_to_runtime_base(self, monkeypatch):
        monkeypatch.delenv("ACPS_CONTAINER_MODE", raising=False)
        monkeypatch.setattr("acps_cli.ca.acme.os.path.exists", lambda path: False)

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "newNonce": "http://localhost:9003/acps-atr-v2/acme/new-nonce",
            "newAccount": "http://localhost:9003/acps-atr-v2/acme/new-account",
        }
        monkeypatch.setattr("acps_cli.ca.acme.httpx.get", lambda url, **kwargs: response)

        client = AcmeClient("http://127.0.0.1:19003/acps-atr-v2", None)
        directory = client.get_directory()

        assert directory["newNonce"] == "http://127.0.0.1:19003/acps-atr-v2/acme/new-nonce"
        assert directory["newAccount"] == "http://127.0.0.1:19003/acps-atr-v2/acme/new-account"

    def test_new_order_rewrites_localhost_urls_to_runtime_base(self, monkeypatch):
        key = generate_private_key("ec")
        client = AcmeClient("http://127.0.0.1:19003/acps-atr-v2", key)
        client.directory = {"newOrder": "http://127.0.0.1:19003/acps-atr-v2/acme/new-order"}
        client.account_url = "http://127.0.0.1:19003/acps-atr-v2/acme/acct/1"

        class DummyResponse:
            headers = {"Location": "http://localhost:9003/acps-atr-v2/acme/order/1"}

            def json(self):
                return {
                    "status": "ready",
                    "finalize": "http://localhost:9003/acps-atr-v2/acme/order/1/finalize",
                }

        monkeypatch.setattr(client, "_post", lambda url, payload: DummyResponse())

        order = client.new_order("test-aic")

        assert order["url"] == "http://127.0.0.1:19003/acps-atr-v2/acme/order/1"
        assert order["finalize"] == "http://127.0.0.1:19003/acps-atr-v2/acme/order/1/finalize"


# ---------------------------------------------------------------------------
# AcmeError
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestAcmeError:
    def test_basic_message(self):
        err = AcmeError("something failed")
        assert str(err) == "something failed"

    def test_with_detail(self):
        err = AcmeError("failed", status_code=400, detail={"type": "badCSR"})
        assert "Detail" in str(err)
        assert err.status_code == 400

    def test_without_detail(self):
        err = AcmeError("fail", status_code=500)
        assert "Detail" not in str(err)

    def test_no_status_code(self):
        err = AcmeError("timeout")
        assert err.status_code is None


# ---------------------------------------------------------------------------
# new_order usage 参数
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestNewOrderUsage:
    """new_order 的 usage 参数传递。"""

    def test_new_order_default_usage(self, monkeypatch):
        """不传 usage 时默认为 clientAuth。"""
        key = generate_private_key("ec")
        client = AcmeClient("http://localhost:8003", key)
        client.directory = {"newOrder": "http://localhost:8003/acme/new-order"}
        client.account_url = "http://localhost:8003/acme/acct/1"
        captured = {}

        class DummyResponse:
            headers = {"Location": "http://localhost:8003/acme/order/1"}

            def json(self):
                return {
                    "status": "ready",
                    "finalize": "http://localhost:8003/acme/order/1/finalize",
                }

        monkeypatch.setattr(
            client,
            "_post",
            lambda url, payload: captured.update({"url": url, "payload": payload}) or DummyResponse(),
        )

        client.new_order("test-aic")
        assert captured["payload"]["identifiers"][0]["usage"] == "clientAuth"
        assert captured["payload"]["identifiers"][0]["type"] == "agent"
        assert captured["payload"]["identifiers"][0]["value"] == "test-aic"

    def test_new_order_server_auth_usage(self, monkeypatch):
        """传入 serverAuth 应正确传递。"""
        key = generate_private_key("ec")
        client = AcmeClient("http://localhost:8003", key)
        client.directory = {"newOrder": "http://localhost:8003/acme/new-order"}
        client.account_url = "http://localhost:8003/acme/acct/1"
        captured = {}

        class DummyResponse:
            headers = {"Location": "http://localhost:8003/acme/order/1"}

            def json(self):
                return {
                    "status": "ready",
                    "finalize": "http://localhost:8003/acme/order/1/finalize",
                }

        monkeypatch.setattr(
            client,
            "_post",
            lambda url, payload: captured.update({"url": url, "payload": payload}) or DummyResponse(),
        )

        client.new_order("test-aic", usage="serverAuth")
        assert captured["payload"]["identifiers"][0]["usage"] == "serverAuth"


@pytest.mark.unit
class TestOcspAndCrlClientExtensions:
    def test_list_crls_uses_admin_authorization_header(self, monkeypatch):
        client = AcmeClient("http://localhost:8003", None, admin_api_token="token-1")
        captured = {}

        monkeypatch.setattr(
            client,
            "_request_json_with_headers",
            lambda url, **kwargs: captured.update({"url": url, "kwargs": kwargs}) or {"items": []},
        )

        result = client.list_crls()

        assert result == {"items": []}
        assert captured["url"] == "http://localhost:8003/crl/list"
        assert captured["kwargs"]["headers"] == {"Authorization": "Bearer token-1"}

    def test_refresh_crl_uses_admin_authorization_header(self, monkeypatch):
        client = AcmeClient("http://localhost:8003", None, admin_api_token="token-2")
        captured = {}

        monkeypatch.setattr(
            client,
            "_post_json",
            lambda url, payload, **kwargs: (
                captured.update({"url": url, "payload": payload, "kwargs": kwargs}) or {"version": "2026010101"}
            ),
        )

        result = client.refresh_crl()

        assert result == {"version": "2026010101"}
        assert captured["url"] == "http://localhost:8003/crl/refresh"
        assert captured["payload"] == {}
        assert captured["kwargs"]["headers"] == {"Authorization": "Bearer token-2"}

    def test_download_crl_uses_version_endpoint(self, monkeypatch):
        client = AcmeClient("http://localhost:8003", None)
        captured = {}

        response = Mock()
        response.content = b"historical-crl"

        monkeypatch.setattr(
            client,
            "_request",
            lambda method, url, **kwargs: captured.update({"method": method, "url": url, "kwargs": kwargs}) or response,
        )

        result = client.download_crl(version="2026010101")

        assert result == b"historical-crl"
        assert captured["method"] == "GET"
        assert captured["url"] == "http://localhost:8003/crl/version/2026010101"

    def test_get_crl_info_uses_json_endpoint(self, monkeypatch):
        client = AcmeClient("http://localhost:8003", None)
        monkeypatch.setattr(
            client,
            "_get_json",
            lambda url, **kwargs: {"url": url, "version": "2026010101"},
        )

        result = client.get_crl_info()

        assert result["version"] == "2026010101"
        assert result["url"] == "http://localhost:8003/crl/info"

    def test_get_certificate_status_uses_serial_endpoint(self, monkeypatch):
        client = AcmeClient("http://localhost:8003", None)
        monkeypatch.setattr(
            client,
            "_get_json",
            lambda url, **kwargs: {"serialNumber": "ABCD", "url": url},
        )

        result = client.get_certificate_status("ABCD")

        assert result["serialNumber"] == "ABCD"
        assert result["url"] == "http://localhost:8003/ocsp/certificate/ABCD"

    def test_check_ocsp_get_uses_encoded_request_path(self, monkeypatch):
        client = AcmeClient("http://localhost:8003", None)
        cert_pem, issuer_pem = _build_leaf_and_issuer_pem()
        captured = {}

        response = Mock()
        response.content = b"parsed-ocsp-response"

        monkeypatch.setattr(
            client,
            "_request",
            lambda method, url, **kwargs: captured.update({"method": method, "url": url, "kwargs": kwargs}) or response,
        )
        monkeypatch.setattr(
            "acps_cli.ca.acme.ocsp.load_der_ocsp_response",
            lambda content: {"content": content},
        )

        result = client.check_ocsp(cert_pem, issuer_pem, method="get")

        assert captured["method"] == "GET"
        assert captured["url"].startswith("http://localhost:8003/ocsp/")
        assert captured["url"] != "http://localhost:8003/ocsp/"
        assert result == {"content": b"parsed-ocsp-response"}

    def test_check_ocsp_batch_posts_certificates(self, monkeypatch):
        client = AcmeClient("http://localhost:8003", None)
        captured = {}

        monkeypatch.setattr(
            client,
            "_post_json",
            lambda url, payload: captured.update({"url": url, "payload": payload}) or {"responses": []},
        )

        result = client.check_ocsp_batch([{"serial_number": "A1", "issuer_key_hash": "hash-1"}])

        assert result == {"responses": []}
        assert captured["url"] == "http://localhost:8003/ocsp/batch"
        assert captured["payload"] == {"certificates": [{"serial_number": "A1", "issuer_key_hash": "hash-1"}]}


# ---------------------------------------------------------------------------
# key_change — 内层 JWS url 字段（RFC 8555 §7.3.5）
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestKeyChangeInnerJWSUrl:
    """验证 key_change 内层 JWS 包含正确的 url（v2.1.0 breaking change）。"""

    def test_inner_jws_has_key_change_url(self, monkeypatch):
        """内层 JWS 的 protected.url 应等于 keyChange 端点地址。"""
        old_key = generate_private_key("ec")
        new_key = generate_private_key("ec")
        key_change_url = "http://localhost:8003/acme/key-change"

        client = AcmeClient("http://localhost:8003", old_key)
        client.directory = {
            "keyChange": key_change_url,
            "newNonce": "http://localhost:8003/acme/new-nonce",
        }
        client.account_url = "http://localhost:8003/acme/acct/1"
        # 直接设置 nonce，并跳过 get_nonce 的网络请求
        monkeypatch.setattr(client, "get_nonce", lambda: "test-nonce")

        captured: dict[str, object] = {}

        class DummyResponse:
            headers = {"Replay-Nonce": "new-nonce"}
            status_code = 200

            def json(self):
                return {"status": "valid"}

        def _fake_post(url, json, headers, timeout):
            # 外层 JWS 的 payload 即为内层 JWS（已 base64url 编码后嵌入）
            captured["outer_json"] = json
            return DummyResponse()

        monkeypatch.setattr("acps_cli.ca.acme.httpx.post", _fake_post)

        client.key_change(new_key)

        # 解码外层 payload 以获取内层 JWS
        outer_payload_b64 = captured["outer_json"]["payload"]
        inner_jws = json.loads(base64url_decode(outer_payload_b64).decode("utf-8"))
        inner_protected = json.loads(base64url_decode(inner_jws["protected"]).decode("utf-8"))

        # RFC 8555 §7.3.5：内层 JWS url 必须等于 keyChange 端点地址
        assert inner_protected.get("url") == key_change_url
        # 内层 JWS 不得包含 nonce
        assert "nonce" not in inner_protected

    def test_inner_jws_no_nonce(self, monkeypatch):
        """内层 JWS 不应携带 nonce（RFC 8555 §7.3.5 要求）。"""
        old_key = generate_private_key("ec")
        new_key = generate_private_key("ec")
        key_change_url = "http://localhost:8003/acme/key-change"

        client = AcmeClient("http://localhost:8003", old_key)
        client.directory = {
            "keyChange": key_change_url,
            "newNonce": "http://localhost:8003/acme/new-nonce",
        }
        client.account_url = "http://localhost:8003/acme/acct/1"
        monkeypatch.setattr(client, "get_nonce", lambda: "test-nonce")

        captured: dict[str, object] = {}

        class DummyResponse:
            headers = {"Replay-Nonce": "new-nonce"}
            status_code = 200

            def json(self):
                return {"status": "valid"}

        monkeypatch.setattr(
            "acps_cli.ca.acme.httpx.post",
            lambda url, json, headers, timeout: captured.update({"outer_json": json}) or DummyResponse(),
        )

        client.key_change(new_key)

        outer_payload_b64 = captured["outer_json"]["payload"]
        inner_jws = json.loads(base64url_decode(outer_payload_b64).decode("utf-8"))
        inner_protected = json.loads(base64url_decode(inner_jws["protected"]).decode("utf-8"))

        assert "nonce" not in inner_protected
