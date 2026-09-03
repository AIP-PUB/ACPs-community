import base64
import hashlib
import hmac
import json
import logging
import os
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509 import ocsp

_DEFAULT_TIMEOUT = 10
_BAD_NONCE_RETRY_LIMIT = 1
_LOCAL_RUNTIME_HOSTS = {"localhost", "127.0.0.1", "host.docker.internal"}

logger = logging.getLogger(__name__)


def _response_error_detail(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return response.text


def is_container_runtime():
    flag = os.environ.get("ACPS_CONTAINER_MODE")
    if flag is not None:
        return flag.strip().lower() in {"1", "true", "yes", "on"}
    return os.path.exists("/.dockerenv")


def _rewrite_local_runtime_url(url, runtime_base_url):
    if not url or not runtime_base_url:
        return url

    advertised = urlsplit(url)
    runtime = urlsplit(runtime_base_url)
    if not advertised.scheme or not advertised.netloc or not runtime.scheme or not runtime.netloc:
        return url

    runtime_path_prefix = runtime.path.rstrip("/")
    if runtime_path_prefix and not advertised.path.startswith(f"{runtime_path_prefix}/"):
        return url

    if advertised.hostname not in _LOCAL_RUNTIME_HOSTS:
        return url

    if advertised.scheme == runtime.scheme and advertised.netloc == runtime.netloc:
        return url

    return urlunsplit(advertised._replace(scheme=runtime.scheme, netloc=runtime.netloc))


def normalize_runtime_url(url, runtime_base_url=None):
    if not url or is_container_runtime():
        return url

    parsed = urlsplit(url)
    if parsed.hostname != "host.docker.internal":
        return _rewrite_local_runtime_url(url, runtime_base_url)

    normalized_url = urlunsplit(parsed._replace(netloc=parsed.netloc.replace("host.docker.internal", "localhost", 1)))
    return _rewrite_local_runtime_url(normalized_url, runtime_base_url)


def normalize_directory_urls(directory, runtime_base_url=None):
    return {
        key: (normalize_runtime_url(value, runtime_base_url) if isinstance(value, str) else value)
        for key, value in directory.items()
    }


def normalize_acme_object(obj, runtime_base_url=None):
    """递归规范化 ACME 响应对象中的 host.docker.internal URL。"""
    if isinstance(obj, dict):
        return {k: normalize_acme_object(v, runtime_base_url) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_acme_object(item, runtime_base_url) for item in obj]
    if isinstance(obj, str):
        return normalize_runtime_url(obj, runtime_base_url)
    return obj


def base64url_encode(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def base64url_decode(data):
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + padding).encode("utf-8"))


def get_jwk(private_key):
    if isinstance(private_key, rsa.RSAPrivateKey):
        pn = private_key.private_numbers()
        return {
            "e": base64url_encode(pn.public_numbers.e.to_bytes((pn.public_numbers.e.bit_length() + 7) // 8, "big")),
            "kty": "RSA",
            "n": base64url_encode(pn.public_numbers.n.to_bytes((pn.public_numbers.n.bit_length() + 7) // 8, "big")),
        }
    if isinstance(private_key, ec.EllipticCurvePrivateKey):
        pn = private_key.private_numbers()
        coordinate_length = (private_key.curve.key_size + 7) // 8
        if isinstance(private_key.curve, ec.SECP256R1):
            curve_name = "P-256"
        elif isinstance(private_key.curve, ec.SECP384R1):
            curve_name = "P-384"
        elif isinstance(private_key.curve, ec.SECP521R1):
            curve_name = "P-521"
        else:
            raise ValueError(f"Unsupported EC curve: {private_key.curve.name}")
        return {
            "crv": curve_name,
            "kty": "EC",
            "x": base64url_encode(pn.public_numbers.x.to_bytes(coordinate_length, "big")),
            "y": base64url_encode(pn.public_numbers.y.to_bytes(coordinate_length, "big")),
        }
    if isinstance(private_key, Ed25519PrivateKey):
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return {
            "crv": "Ed25519",
            "kty": "OKP",
            "x": base64url_encode(public_bytes),
        }
    raise ValueError("Unsupported key type")


def public_jwk_projection(jwk):
    kty = jwk.get("kty")
    if kty == "RSA":
        if any(field in jwk for field in ("d", "p", "q", "dp", "dq", "qi", "oth")):
            raise ValueError("ACME public JWK must not contain private key material")
        return {
            "e": jwk["e"],
            "kty": "RSA",
            "n": jwk["n"],
        }
    if kty == "EC":
        if "d" in jwk:
            raise ValueError("ACME public JWK must not contain private key material")
        return {
            "crv": jwk["crv"],
            "kty": "EC",
            "x": jwk["x"],
            "y": jwk["y"],
        }
    if kty == "OKP":
        if "d" in jwk:
            raise ValueError("ACME public JWK must not contain private key material")
        return {
            "crv": jwk["crv"],
            "kty": "OKP",
            "x": jwk["x"],
        }
    raise ValueError(f"Unsupported key type: {kty}")


def get_jwk_thumbprint(jwk):
    # Sort keys and remove whitespace for canonical JSON
    canonical_json = json.dumps(public_jwk_projection(jwk), sort_keys=True, separators=(",", ":"))
    return base64url_encode(hashlib.sha256(canonical_json.encode("utf-8")).digest())


def _is_bad_nonce_error(detail: Any) -> bool:
    if not isinstance(detail, dict):
        return False

    error_name = str(detail.get("error_name") or detail.get("code") or "").upper()
    if error_name == "BAD_NONCE":
        return True

    error_type = str(detail.get("type") or "").lower().replace("-", "_")
    return error_type.endswith("bad_nonce")


class AcmeError(Exception):
    def __init__(self, message, status_code=None, detail=None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail

    def __str__(self):
        if self.detail:
            return f"{super().__str__()} - Detail: {self.detail}"
        return super().__str__()


class AcmeClient:
    def __init__(self, ca_server_url, account_key, admin_api_token: str | None = None):
        self.ca_server_url = ca_server_url.rstrip("/")
        self.account_key = account_key
        self.admin_api_token = admin_api_token.strip() if admin_api_token else None
        if account_key:
            self.jwk = get_jwk(account_key)
            self.thumbprint = get_jwk_thumbprint(self.jwk)
        else:
            self.jwk = None
            self.thumbprint = None
        self.directory = None
        self.account_url = None
        self.nonce = None

    def _build_admin_headers(self) -> dict[str, str] | None:
        if not self.admin_api_token:
            return None
        return {"Authorization": f"Bearer {self.admin_api_token}"}

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        logger.debug(f"{method} {url}")
        try:
            response = httpx.request(
                method,
                url,
                params=params,
                headers=headers,
                json=json_body,
                content=content,
                timeout=_DEFAULT_TIMEOUT,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AcmeError(
                f"HTTP request failed: {method} {url}",
                exc.response.status_code,
                _response_error_detail(exc.response),
            ) from exc
        except httpx.RequestError as exc:
            raise AcmeError(f"HTTP request failed: {method} {url}", detail=str(exc)) from exc
        logger.debug(f"Response: {response.status_code}")
        return response

    def _get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request("GET", url, params=params)
        return normalize_acme_object(response.json(), self.ca_server_url)

    def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self._request("POST", url, json_body=payload, headers=headers)
        return normalize_acme_object(response.json(), self.ca_server_url)

    def get_directory(self):
        if not self.directory:
            url = f"{self.ca_server_url}/acme/directory"
            logger.debug(f"Fetching ACME directory from {url}")
            try:
                resp = httpx.get(url, timeout=_DEFAULT_TIMEOUT)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise AcmeError(
                    f"ACME Request Failed: {exc.response.status_code} {url}",
                    exc.response.status_code,
                    _response_error_detail(exc.response),
                ) from exc
            except httpx.RequestError as exc:
                raise AcmeError(f"HTTP request failed: GET {url}", detail=str(exc)) from exc
            self.directory = normalize_directory_urls(resp.json(), self.ca_server_url)
            logger.debug(f"Directory endpoints: {list(self.directory.keys())}")
        return self.directory

    def get_nonce(self):
        directory = self.get_directory()
        logger.debug(f"Fetching new nonce from {directory['newNonce']}")
        try:
            resp = httpx.head(directory["newNonce"], timeout=_DEFAULT_TIMEOUT)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AcmeError(
                f"ACME Request Failed: {exc.response.status_code} {directory['newNonce']}",
                exc.response.status_code,
                _response_error_detail(exc.response),
            ) from exc
        except httpx.RequestError as exc:
            raise AcmeError(
                f"HTTP request failed: HEAD {directory['newNonce']}",
                detail=str(exc),
            ) from exc
        self.nonce = resp.headers["Replay-Nonce"]
        logger.debug(f"Got nonce: {self.nonce}")
        return self.nonce

    def _sign_request(self, url, payload):
        if not self.account_key:
            raise ValueError("Account key is required for signed ACME requests")

        if not self.nonce:
            self.get_nonce()

        kid = self.account_url if self.account_url else None
        jwk = None if kid else self.jwk
        return self._build_jws(
            key=self.account_key,
            url=url,
            payload=payload,
            nonce=self.nonce,
            kid=kid,
            jwk=jwk,
        )

    @staticmethod
    def _algorithm_for_key(key):
        if isinstance(key, rsa.RSAPrivateKey):
            return "RS256"
        if isinstance(key, ec.EllipticCurvePrivateKey):
            return "ES256"
        if isinstance(key, Ed25519PrivateKey):
            return "EdDSA"
        raise ValueError("Unsupported key type for signing")

    @staticmethod
    def _sign_bytes(key, data):
        if isinstance(key, rsa.RSAPrivateKey):
            return key.sign(data, padding.PKCS1v15(), hashes.SHA256())
        if isinstance(key, ec.EllipticCurvePrivateKey):
            signature = key.sign(data, ec.ECDSA(hashes.SHA256()))
            r, s = decode_dss_signature(signature)
            curve_size = (key.curve.key_size + 7) // 8
            return r.to_bytes(curve_size, "big") + s.to_bytes(curve_size, "big")
        if isinstance(key, Ed25519PrivateKey):
            return key.sign(data)
        raise ValueError("Unsupported key type for signing")

    @classmethod
    def _build_jws(cls, key, payload, url=None, nonce=None, kid=None, jwk=None):
        if not kid and not jwk:
            raise ValueError("Either kid or jwk must be provided for a JWS header")

        protected = {
            "alg": cls._algorithm_for_key(key),
        }

        if nonce is not None:
            protected["nonce"] = nonce
        if url is not None:
            protected["url"] = url

        if kid:
            protected["kid"] = kid
        if jwk:
            protected["jwk"] = jwk

        payload_b64 = "" if payload is None else base64url_encode(json.dumps(payload).encode("utf-8"))

        protected_b64 = base64url_encode(json.dumps(protected).encode("utf-8"))
        signing_input = f"{protected_b64}.{payload_b64}".encode()
        signature = cls._sign_bytes(key, signing_input)

        return {
            "protected": protected_b64,
            "payload": payload_b64,
            "signature": base64url_encode(signature),
        }

    @classmethod
    def _build_eab_jws(cls, key_id, mac_key, account_jwk, new_account_url):
        projected_account_jwk = public_jwk_projection(account_jwk)
        protected = {
            "alg": "HS256",
            "kid": key_id,
            "url": new_account_url,
        }
        protected_b64 = base64url_encode(json.dumps(protected, separators=(",", ":")).encode("utf-8"))
        payload_b64 = base64url_encode(json.dumps(projected_account_jwk, separators=(",", ":")).encode("utf-8"))
        signature = hmac.new(
            base64url_decode(mac_key),
            f"{protected_b64}.{payload_b64}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        return {
            "protected": protected_b64,
            "payload": payload_b64,
            "signature": base64url_encode(signature),
        }

    def _post(self, url, payload):
        headers = {"Content-Type": "application/jose+json"}

        for attempt in range(_BAD_NONCE_RETRY_LIMIT + 1):
            data = self._sign_request(url, payload)
            logger.debug(f"POST {url}")
            try:
                resp = httpx.post(url, json=data, headers=headers, timeout=_DEFAULT_TIMEOUT)
            except httpx.RequestError as exc:
                raise AcmeError(f"HTTP request failed: POST {url}", detail=str(exc)) from exc
            logger.debug(f"Response: {resp.status_code}")

            if "Replay-Nonce" in resp.headers:
                self.nonce = resp.headers["Replay-Nonce"]
            else:
                self.nonce = None

            if resp.status_code in [200, 201]:
                return resp

            error_detail = _response_error_detail(resp)
            logger.debug(f"Error response body: {error_detail}")
            if attempt < _BAD_NONCE_RETRY_LIMIT and resp.status_code == 400 and _is_bad_nonce_error(error_detail):
                logger.warning("ACME bad nonce received, retrying request once")
                if self.nonce is None:
                    self.get_nonce()
                continue

            raise AcmeError(
                f"ACME Request Failed: {resp.status_code} {url}",
                resp.status_code,
                error_detail,
            )

        raise AcmeError(f"ACME Request Failed: exhausted retries for {url}")

    def new_account(
        self,
        contact=None,
        terms_of_service_agreed=True,
        only_return_existing=False,
        eab_credential=None,
    ):
        directory = self.get_directory()
        payload = {
            "termsOfServiceAgreed": terms_of_service_agreed,
            "onlyReturnExisting": only_return_existing,
        }
        if contact:
            payload["contact"] = contact
        if not only_return_existing:
            if not isinstance(eab_credential, dict):
                raise ValueError("eab_credential is required when creating a new ACME account")
            key_id = eab_credential.get("keyId")
            mac_key = eab_credential.get("macKey")
            if not isinstance(key_id, str) or not isinstance(mac_key, str):
                raise ValueError("eab_credential must contain keyId and macKey")
            payload["externalAccountBinding"] = self._build_eab_jws(
                key_id,
                mac_key,
                self.jwk,
                directory["newAccount"],
            )

        logger.debug(f"Account request to {directory['newAccount']} (onlyReturnExisting={only_return_existing})")
        resp = self._post(directory["newAccount"], payload)
        self.account_url = normalize_runtime_url(resp.headers.get("Location", self.account_url), self.ca_server_url)
        logger.debug(f"Account URL: {self.account_url}")
        return resp.json()

    def new_order(self, aic, usage="clientAuth"):
        directory = self.get_directory()
        payload = {"identifiers": [{"type": "agent", "value": aic, "usage": usage}]}
        logger.debug(f"Creating order at {directory['newOrder']} for identifier: agent={aic}, usage={usage}")
        resp = self._post(directory["newOrder"], payload)
        order = normalize_acme_object(resp.json(), self.ca_server_url)
        order["url"] = normalize_runtime_url(resp.headers["Location"], self.ca_server_url)
        logger.debug(f"Order URL: {order['url']}")
        logger.debug(f"Order status: {order.get('status')}, authorizations: {order.get('authorizations')}")
        return order

    def finalize_order(self, finalize_url, csr_pem):
        logger.debug(f"Finalizing order at {finalize_url}")
        # CSR needs to be DER encoded and then base64url encoded
        # csr_pem is bytes
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        csr = x509.load_pem_x509_csr(csr_pem)
        csr_der = csr.public_bytes(serialization.Encoding.DER)

        payload = {"csr": base64url_encode(csr_der)}
        resp = self._post(finalize_url, payload)
        order = normalize_acme_object(resp.json(), self.ca_server_url)
        if "url" not in order:
            order["url"] = normalize_runtime_url(finalize_url, self.ca_server_url)
        return order

    def get_certificate(self, cert_url):
        # POST-as-GET
        logger.debug(f"Downloading certificate from {cert_url}")
        resp = self._post(cert_url, None)
        return resp.content  # PEM content

    def revoke_cert(self, cert_pem, reason=0):
        directory = self.get_directory()
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        cert = x509.load_pem_x509_certificate(cert_pem)
        cert_der = cert.public_bytes(serialization.Encoding.DER)

        payload = {"certificate": base64url_encode(cert_der), "reason": reason}
        logger.debug(f"Revoking certificate at {directory['revokeCert']} (reason={reason})")
        self._post(directory["revokeCert"], payload)

    def key_change(self, new_key):
        if not self.account_url:
            raise AcmeError("Account URL is unknown. Call new_account(only_return_existing=True) before key rollover.")

        directory = self.get_directory()
        key_change_url = directory["keyChange"]
        logger.debug(f"Requesting key change at {key_change_url}")
        logger.debug(f"Account URL: {self.account_url}")

        inner_payload = {
            "account": self.account_url,
            "oldKey": public_jwk_projection(self.jwk),
        }

        new_jwk = public_jwk_projection(get_jwk(new_key))
        # RFC 8555 §7.3.5: 内层 JWS 的 url 字段必须与外层 JWS 的 url 相同，
        # 且不得包含 nonce。v2.1.0 恢复了 protected.url 完整性校验。
        inner_jws = self._build_jws(
            key=new_key,
            payload=inner_payload,
            url=key_change_url,
            jwk=new_jwk,
        )

        outer_nonce = self.get_nonce()
        outer_jws = self._build_jws(
            key=self.account_key,
            payload=inner_jws,
            url=key_change_url,
            nonce=outer_nonce,
            kid=self.account_url,
        )

        headers = {"Content-Type": "application/jose+json"}
        logger.debug(f"POST {key_change_url}")
        try:
            resp = httpx.post(
                key_change_url,
                json=outer_jws,
                headers=headers,
                timeout=_DEFAULT_TIMEOUT,
            )
        except httpx.RequestError as exc:
            raise AcmeError(f"HTTP request failed: POST {key_change_url}", detail=str(exc)) from exc
        logger.debug(f"Response: {resp.status_code}")

        if "Replay-Nonce" in resp.headers:
            self.nonce = resp.headers["Replay-Nonce"]
        else:
            self.nonce = None

        if resp.status_code not in [200, 201]:
            error_detail = _response_error_detail(resp)
            raise AcmeError(
                f"ACME Key Rollover Failed: {resp.status_code} {key_change_url}",
                resp.status_code,
                error_detail,
            )

        self.account_key = new_key
        self.jwk = new_jwk
        self.thumbprint = get_jwk_thumbprint(new_jwk)
        logger.debug("Key change completed, local client state updated")

        return resp.json()

    def download_crl(self, output_format: str = "der", version: str | None = None) -> bytes:
        if version:
            url = f"{self.ca_server_url}/crl/version/{quote(version, safe='')}"
            logger.debug(f"Downloading historical CRL from {url}")
            response = self._request("GET", url)
        else:
            url = f"{self.ca_server_url}/crl"
            logger.debug(f"Downloading CRL from {url} (format={output_format})")
            response = self._request("GET", url, params={"format": output_format})
        logger.debug(f"CRL downloaded: {len(response.content)} bytes")
        return response.content

    def get_crl_info(self) -> dict[str, Any]:
        return self._get_json(f"{self.ca_server_url}/crl/info")

    def get_crl_distribution_points(self) -> dict[str, Any]:
        return self._get_json(f"{self.ca_server_url}/crl/distribution-points")

    def get_crl_detail(self) -> dict[str, Any]:
        return self._get_json(f"{self.ca_server_url}/crl/detail")

    def list_crls(self, *, status: str | None = None, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if status:
            params["status"] = status
        return self._request_json_with_headers(
            f"{self.ca_server_url}/crl/list",
            params=params,
            headers=self._build_admin_headers(),
        )

    def refresh_crl(self) -> dict[str, Any]:
        return self._post_json(
            f"{self.ca_server_url}/crl/refresh",
            {},
            headers=self._build_admin_headers(),
        )

    def _request_json_with_headers(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self._request("GET", url, params=params, headers=headers)
        return normalize_acme_object(response.json())

    def get_ocsp_responder_info(self) -> dict[str, Any]:
        return self._get_json(f"{self.ca_server_url}/ocsp/responder/info")

    def get_ocsp_statistics(self) -> dict[str, Any]:
        return self._get_json(f"{self.ca_server_url}/ocsp/stats")

    def get_certificate_status(self, serial_number: str) -> dict[str, Any]:
        serial = quote(serial_number, safe="")
        return self._get_json(f"{self.ca_server_url}/ocsp/certificate/{serial}")

    def check_ocsp_batch(self, certificates: list[dict[str, str]]) -> dict[str, Any]:
        return self._post_json(
            f"{self.ca_server_url}/ocsp/batch",
            {"certificates": certificates},
        )

    def check_ocsp(self, cert_pem, issuer_pem, method: str = "post"):
        cert = x509.load_pem_x509_certificate(cert_pem)
        issuer = x509.load_pem_x509_certificate(issuer_pem)

        builder = ocsp.OCSPRequestBuilder()
        builder = builder.add_certificate(cert, issuer, hashes.SHA1())  # noqa: S303
        req = builder.build()

        req_der = req.public_bytes(serialization.Encoding.DER)

        request_method = method.lower()
        if request_method == "get":
            ocsp_request = quote(base64url_encode(req_der), safe="")
            url = f"{self.ca_server_url}/ocsp/{ocsp_request}"
            response = self._request("GET", url)
        elif request_method == "post":
            url = f"{self.ca_server_url}/ocsp"
            headers = {"Content-Type": "application/ocsp-request"}
            response = self._request("POST", url, headers=headers, content=req_der)
        else:
            raise ValueError("method must be either 'get' or 'post'")

        return ocsp.load_der_ocsp_response(response.content)
