"""用于调用 registry-server API 的 HTTP 客户端。"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.x509.oid import NameOID

from acps_cli.shared.auth_session import AuthSessionError, OidcAuthSessionManager

from .config import Config
from .exceptions import RegistryClientError
from .storage import TokenStore

LOGGER = logging.getLogger(__name__)

# 派生实体 ATR 请求体中允许从 --payload-file 透传的字段。
ENTITY_REGISTRATION_PAYLOAD_FIELDS = ("endPoints", "entityUserId", "entityMeta", "certificate")


class RegistryApiClient:
    """封装用户与审核侧所需的 Registry API 调用。"""

    ATR_OK_STATUS = "ok"
    USER_NOT_FOUND_ERROR = "USER_NOT_FOUND"

    def __init__(
        self,
        config: Config,
        *,
        auth_mode: str = "local",
        oidc_session: OidcAuthSessionManager | None = None,
    ):
        self.config = config
        self.auth_mode = auth_mode
        self.oidc_session = oidc_session
        self.token_store = TokenStore(config.token_file)

    @property
    def base_api_url(self) -> str:
        return self.config.server_base_url

    @property
    def base_atr_url(self) -> str:
        return self.config.atr_base_url

    @property
    def base_mtls_url(self) -> str:
        return self.config.mtls_base_url

    @property
    def atr_path_prefix(self) -> str:
        return urlparse(self.base_atr_url).path.rstrip("/")

    def _save_token_response(self, token_response: dict[str, Any]) -> None:
        token_data = {
            "access_token": str(token_response["access_token"]),
            "token_type": str(token_response.get("token_type", "bearer")),
        }
        if token_response.get("refresh_token"):
            token_data["refresh_token"] = str(token_response["refresh_token"])
        self.token_store.save(token_data)

    def _get_auth_header(self) -> dict[str, str]:
        if self.auth_mode == "oidc":
            if self.oidc_session is None:
                raise RegistryClientError("OIDC auth session is not configured")
            try:
                access_token = self.oidc_session.get_access_token()
            except AuthSessionError as exc:
                raise RegistryClientError(str(exc)) from exc
            return {"Authorization": f"Bearer {access_token}"}
        token_data = self.token_store.load()
        if token_data is None or "access_token" not in token_data:
            raise RegistryClientError("No access token found, run login first")
        return {"Authorization": f"Bearer {token_data['access_token']}"}

    def _refresh_auth_header_after_unauthorized(self) -> dict[str, str]:
        if self.auth_mode == "oidc":
            if self.oidc_session is None:
                raise RegistryClientError("OIDC auth session is not configured")
            try:
                access_token = self.oidc_session.handle_unauthorized()
            except AuthSessionError as exc:
                raise RegistryClientError(f"{exc}. Verify OIDC configuration and log in again.") from exc
            return {"Authorization": f"Bearer {access_token}"}
        refreshed = self.refresh_local_token()
        refreshed_access_token = refreshed.get("access_token")
        if not isinstance(refreshed_access_token, str) or not refreshed_access_token:
            raise RegistryClientError("Refresh response missing access_token", payload=refreshed)
        return {"Authorization": f"Bearer {refreshed_access_token}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth_required: bool,
        base_url: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        data: dict[str, Any] | None = None,
        cert: tuple[str, str] | None = None,
        verify: str | bool | None = None,
        retry_on_unauthorized: bool = True,
    ) -> Any:
        target_base_url = base_url or self.base_api_url
        url = f"{target_base_url}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if auth_required:
            headers.update(self._get_auth_header())
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        resolved_verify = True if verify is None else verify

        response = self._send_request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json_body=json_body,
            data=data,
            cert=cert,
            verify=resolved_verify,
        )

        if response.status_code == 401 and auth_required and retry_on_unauthorized:
            retry_headers = dict(headers)
            retry_headers.update(self._refresh_auth_header_after_unauthorized())
            response = self._send_request(
                method=method,
                url=url,
                headers=retry_headers,
                params=params,
                json_body=json_body,
                data=data,
                cert=cert,
                verify=resolved_verify,
            )

        LOGGER.debug(f"Registry response {method} {path} -> {response.status_code}")

        if response.status_code >= 400:
            payload: Any
            try:
                payload = response.json()
            except ValueError:
                payload = response.text
            LOGGER.debug(f"Registry error payload: {payload}")
            raise RegistryClientError(
                message=self._build_error_message(
                    payload,
                    default_message=self._build_default_error_message(
                        method=method, path=path, status_code=response.status_code
                    ),
                ),
                status_code=response.status_code,
                payload=payload,
            )

        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise RegistryClientError("Invalid JSON response from server") from exc

    def _send_request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any] | None,
        json_body: Any | None,
        data: dict[str, Any] | None,
        cert: tuple[str, str] | None,
        verify: str | bool,
    ) -> httpx.Response:
        LOGGER.debug(f"Registry request {method} {url}")
        try:
            if cert is None:
                return httpx.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    data=data,
                    timeout=self.config.timeout_seconds,
                    verify=verify,
                )
            ssl_context = self._build_client_ssl_context(
                cert=cert,
                verify=verify,
            )
            with httpx.Client(
                verify=ssl_context,
                timeout=self.config.timeout_seconds,
            ) as client:
                return client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    data=data,
                    timeout=self.config.timeout_seconds,
                )
        except httpx.RequestError as exc:
            raise RegistryClientError(f"Request failed: {exc}") from exc

    def _build_client_ssl_context(
        self,
        *,
        cert: tuple[str, str],
        verify: str | bool | ssl.SSLContext,
    ) -> ssl.SSLContext:
        if isinstance(verify, ssl.SSLContext):
            ssl_context = verify
        elif verify is False:
            raise RegistryClientError("mTLS requests require server certificate verification")
        elif isinstance(verify, str):
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
            ssl_context.check_hostname = True
            ssl_context.verify_mode = ssl.CERT_REQUIRED
            ssl_context.load_verify_locations(cafile=verify)
        else:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
            ssl_context.check_hostname = True
            ssl_context.verify_mode = ssl.CERT_REQUIRED
            ssl_context.load_default_certs()

        ssl_context.load_cert_chain(certfile=cert[0], keyfile=cert[1])
        return ssl_context

    def _build_error_message(self, payload: Any, *, default_message: str) -> str:
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()

            for field_name in ("detail", "title", "message"):
                field_value = payload.get(field_name)
                if isinstance(field_value, str) and field_value.strip():
                    return field_value.strip()

        return default_message

    def login(self, username: str, password: str) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/auth/login",
            auth_required=False,
            data={"username": username, "password": password},
        )
        if not isinstance(result, dict) or "access_token" not in result:
            raise RegistryClientError("Login response missing access_token", payload=result)
        self._save_token_response(result)
        return result

    def refresh_local_token(self) -> dict[str, Any]:
        token_data = self.token_store.load()
        refresh_token = token_data.get("refresh_token") if token_data else None
        if not refresh_token:
            raise RegistryClientError("Current session does not include a refresh token, run login again")
        result = self._request(
            "POST",
            "/auth/refresh-token",
            auth_required=False,
            json_body={"refresh_token": refresh_token},
            retry_on_unauthorized=False,
        )
        if not isinstance(result, dict) or "access_token" not in result:
            raise RegistryClientError("Refresh response missing access_token", payload=result)
        self._save_token_response(result)
        return result

    def register_user(
        self,
        username: str,
        password: str,
        name: str | None = None,
        org_name: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "username": username,
            "password": password,
        }
        if name:
            payload["name"] = name
        if org_name:
            payload["org_name"] = org_name
        result = self._request(
            "POST",
            "/auth/register",
            auth_required=False,
            json_body=payload,
        )
        if not isinstance(result, dict) or "access_token" not in result:
            raise RegistryClientError("Register response missing access_token", payload=result)
        self._save_token_response(result)
        return result

    def login_or_register_user(
        self,
        username: str,
        password: str,
        name: str | None = None,
        org_name: str | None = None,
    ) -> dict[str, Any]:
        try:
            token_response = self.login(username=username, password=password)
            return {
                "status": "logged-in",
                "username": username,
                "token": token_response,
            }
        except RegistryClientError as login_error:
            if login_error.error_name != self.USER_NOT_FOUND_ERROR:
                raise
            token_response = self.register_user(
                username=username,
                password=password,
                name=name,
                org_name=org_name,
            )
            return {
                "status": "registered",
                "username": username,
                "token": token_response,
            }

    def clear_token(self) -> None:
        self.token_store.clear()

    def logout(self) -> dict[str, Any]:
        token_data = self.token_store.load()
        auth_required = isinstance(token_data, dict) and "access_token" in token_data
        result = self._request(
            "POST",
            "/auth/logout",
            auth_required=auth_required,
        )
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for /auth/logout", payload=result)
        return result

    def _build_default_error_message(self, *, method: str, path: str, status_code: int) -> str:
        if status_code == 410 and self.auth_mode == "local":
            return (
                "Registry local auth endpoint is disabled. "
                "The server may have switched to OIDC; update [registry.auth].mode."
            )
        if status_code == 401 and self.auth_mode == "oidc":
            return "OIDC authentication failed after retry. Verify issuer/client configuration and log in again."
        return f"API request failed: {method} {path}"

    def whoami(self) -> dict[str, Any]:
        result = self._request("GET", "/account/me", auth_required=True)
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for /account/me", payload=result)
        return result

    def update_current_user_password(self, old_password: str, new_password: str) -> dict[str, Any]:
        result = self._request(
            "PUT",
            "/account/me/password",
            auth_required=True,
            json_body={
                "old_password": old_password,
                "new_password": new_password,
            },
        )
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for /account/me/password", payload=result)
        return result

    def reset_user_password(self, user_id: str, new_password: str) -> dict[str, Any]:
        result = self._request(
            "PUT",
            f"/account/user/{user_id}/password",
            auth_required=True,
            json_body={"new_password": new_password},
        )
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for admin reset user password", payload=result)
        return result

    def list_my_agents(
        self,
        page_num: int,
        page_size: int,
        statuses: list[str],
        *,
        name: str | None = None,
        version: str | None = None,
        aic: str | None = None,
        is_deleted: bool | None = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page_num": page_num, "page_size": page_size}
        if statuses:
            params["statuses"] = statuses
        if name:
            params["name"] = name
        if version:
            params["version"] = version
        if aic:
            params["aic"] = aic
        if is_deleted is not None:
            params["is_deleted"] = str(is_deleted).lower()
        result = self._request("GET", "/agent/client", auth_required=True, params=params)
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for list_my_agents", payload=result)
        return result

    def create_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", "/agent/client", auth_required=True, json_body=payload)
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for create_agent", payload=result)
        return result

    def update_agent(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("PUT", f"/agent/client/{agent_id}", auth_required=True, json_body=payload)
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for update_agent", payload=result)
        return result

    def submit_agent(self, agent_id: str) -> dict[str, Any]:
        result = self._request("POST", f"/agent/client/{agent_id}/submit", auth_required=True)
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for submit_agent", payload=result)
        return result

    def _build_entity_registration_body(
        self,
        ontology_aic: str,
        entity_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"ontologyAic": ontology_aic}
        if not entity_payload:
            return body

        for field_name in ENTITY_REGISTRATION_PAYLOAD_FIELDS:
            if field_name in entity_payload:
                body[field_name] = entity_payload[field_name]
        return body

    def _resolve_entity_registration_materials(
        self,
        ontology_aic: str,
        *,
        mtls_cert_file: str | None,
        mtls_key_file: str | None,
        mtls_server_ca_file: str | None,
    ) -> tuple[Path, Path, Path | None]:
        cert_file = self._resolve_required_path(
            mtls_cert_file,
            default_path=self.config.resolve_ontology_mtls_cert_file(ontology_aic),
            label="mTLS certificate",
        )
        key_file = self._resolve_required_path(
            mtls_key_file,
            default_path=self.config.resolve_ontology_mtls_key_file(ontology_aic),
            label="mTLS private key",
        )
        ca_file = self._resolve_optional_path(
            mtls_server_ca_file,
            default_path=self.config.mtls_server_ca_file,
            label="mTLS server CA file",
        )
        self._ensure_certificate_matches_ontology(cert_file, ontology_aic)
        return cert_file, key_file, ca_file

    def _parse_entity_registration_result(self, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for ATR entity registration", payload=result)
        if result.get("status") == self.ATR_OK_STATUS and isinstance(result.get("result"), dict):
            return result["result"]

        error = result.get("error") if isinstance(result.get("error"), dict) else None
        message = "ATR entity registration failed"
        if error and isinstance(error.get("message"), str) and error["message"]:
            message = error["message"]
        raise RegistryClientError(message, payload=result)

    def register_entity_via_atr(
        self,
        ontology_aic: str,
        entity_payload: dict[str, Any] | None = None,
        *,
        mtls_cert_file: str | None = None,
        mtls_key_file: str | None = None,
        mtls_server_ca_file: str | None = None,
    ) -> dict[str, Any]:
        normalized_ontology_aic = ontology_aic.strip().upper()
        body = self._build_entity_registration_body(normalized_ontology_aic, entity_payload)
        cert_file, key_file, ca_file = self._resolve_entity_registration_materials(
            normalized_ontology_aic,
            mtls_cert_file=mtls_cert_file,
            mtls_key_file=mtls_key_file,
            mtls_server_ca_file=mtls_server_ca_file,
        )

        result = self._request(
            "POST",
            f"{self.atr_path_prefix}/entity",
            auth_required=True,
            base_url=self.base_mtls_url,
            json_body=body,
            cert=(str(cert_file), str(key_file)),
            verify=str(ca_file) if ca_file is not None else None,
        )
        return self._parse_entity_registration_result(result)

    def _resolve_required_path(self, provided_path: str | None, *, default_path: Path, label: str) -> Path:
        path = Path(provided_path).expanduser() if provided_path else default_path
        if not path.exists():
            raise RegistryClientError(f"{label} not found: {path}")
        if not path.is_file():
            raise RegistryClientError(f"{label} is not a file: {path}")
        return path

    def _resolve_optional_path(
        self,
        provided_path: str | None,
        *,
        default_path: Path | None,
        label: str,
    ) -> Path | None:
        if provided_path is None and default_path is None:
            return None
        return self._resolve_required_path(provided_path, default_path=default_path or Path(), label=label)

    def _ensure_certificate_matches_ontology(self, cert_file: Path, ontology_aic: str) -> None:
        try:
            cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
        except ValueError as exc:
            raise RegistryClientError(f"Invalid mTLS certificate PEM: {cert_file}") from exc

        common_names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        raw_common_name = common_names[0].value if common_names else ""
        if isinstance(raw_common_name, bytes):
            common_name = raw_common_name.decode("utf-8", errors="replace").strip().upper()
        else:
            common_name = raw_common_name.strip().upper()
        if common_name != ontology_aic:
            raise RegistryClientError(
                "mTLS certificate common name does not match ontology AIC: "
                f"expected {ontology_aic}, got {common_name or '<empty>'}"
            )

    def get_eab_credential(self, aic: str) -> dict[str, Any]:
        result = self._request(
            "POST",
            f"/eab/{aic.strip().upper()}",
            auth_required=True,
            base_url=self.base_atr_url,
        )
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for get_eab_credential", payload=result)
        return result

    def get_my_agent(self, agent_id: str) -> dict[str, Any]:
        result = self._request(
            "GET",
            f"/agent/client/{agent_id}",
            auth_required=True,
        )
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for get_my_agent", payload=result)
        return result

    def find_my_agent_by_aic(self, aic: str) -> dict[str, Any] | None:
        result = self.list_my_agents(
            page_num=1,
            page_size=100,
            statuses=[],
            aic=aic.strip().upper(),
            is_deleted=False,
        )
        items = result.get("items", []) if isinstance(result, dict) else []
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict):
                return item
        return None

    def find_my_agent_by_name_version(self, name: str, version: str) -> dict[str, Any] | None:
        result = self.list_my_agents(
            page_num=1,
            page_size=100,
            statuses=[],
            name=name,
            version=version,
            is_deleted=False,
        )
        items = result.get("items", []) if isinstance(result, dict) else []
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict) and item.get("name") == name and item.get("version") == version:
                return item
        return None

    def delete_agent(self, agent_id: str) -> dict[str, Any]:
        result = self._request(
            "DELETE",
            f"/agent/client/{agent_id}",
            auth_required=True,
            json_body="setup-agents cleanup",
        )
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for delete_agent", payload=result)
        return result

    def list_review_agents(self, page_num: int, page_size: int, statuses: list[str]) -> dict[str, Any]:
        params: dict[str, Any] = {"page_num": page_num, "page_size": page_size}
        if statuses:
            params["statuses"] = statuses
        result = self._request("GET", "/agent/staff", auth_required=True, params=params)
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for list_review_agents", payload=result)
        return result

    def process_review(self, agent_id: str, approve: bool, comments: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"approve": approve}
        if comments:
            body["comments"] = comments
        result = self._request(
            "POST",
            f"/agent/staff/{agent_id}/process",
            auth_required=True,
            json_body=body,
        )
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for process_review", payload=result)
        return result

    def disable_agent(self, agent_id: str, reason: str) -> dict[str, Any]:
        result = self._request(
            "POST",
            f"/agent/staff/{agent_id}/disable",
            auth_required=True,
            json_body=reason,
        )
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for disable_agent", payload=result)
        return result

    def enable_agent(self, agent_id: str) -> dict[str, Any]:
        result = self._request(
            "POST",
            f"/agent/staff/{agent_id}/enable",
            auth_required=True,
        )
        if not isinstance(result, dict):
            raise RegistryClientError("Invalid response for enable_agent", payload=result)
        return result
