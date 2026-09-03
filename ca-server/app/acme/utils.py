"""
ACME 工具函数

提供 ACME 协议相关的工具函数，包括 JWS 验证、密钥处理等。
"""

import json
import secrets
from typing import Any

from cryptography import x509
from fastapi.responses import JSONResponse

from .exception import AcmeError, AcmeException
from .jwk import (
    base64url_decode as _base64url_decode,
)
from .jwk import (
    base64url_encode as _base64url_encode,
)
from .jwk import (
    compute_jwk_thumbprint as _compute_jwk_thumbprint,
)
from .jwk import (
    jwk_to_public_key as _jwk_to_public_key,
)
from .jwk import (
    verify_compact_jws_signature,
)


def base64url_decode(data: str) -> bytes:
    """Base64URL 解码"""
    return _base64url_decode(data)


def base64url_encode(data: bytes) -> str:
    """Base64URL 编码"""
    return _base64url_encode(data)


def jwk_to_public_key(jwk: dict[str, Any]) -> Any:
    """将 JWK 转换为 cryptography 公钥对象"""
    return _jwk_to_public_key(jwk)


def verify_jws_signature(protected: str, payload: str, signature: str, jwk: dict[str, Any]) -> bool:
    """验证 JWS 签名"""
    try:
        protected_header = parse_protected_header(protected)
        verify_compact_jws_signature(
            protected,
            payload,
            signature,
            jwk,
            protected_header.get("alg", ""),
        )
        return True
    except Exception:
        return False


def compute_jwk_thumbprint(jwk: dict[str, Any]) -> str:
    """计算 JWK 指纹"""
    return _compute_jwk_thumbprint(jwk)


def create_key_authorization(token: str, jwk: dict[str, Any]) -> str:
    """创建密钥授权字符串"""
    thumbprint = compute_jwk_thumbprint(jwk)
    return f"{token}.{thumbprint}"


def parse_protected_header(protected_b64: str) -> dict[str, Any]:
    """解析 JWS protected header"""
    try:
        protected_data = base64url_decode(protected_b64)
        protected_header = json.loads(protected_data.decode("utf-8"))
        if not isinstance(protected_header, dict):
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="Protected header must be a JSON object",
            )
        return protected_header
    except AcmeException:
        raise
    except (ValueError, UnicodeDecodeError, KeyError) as e:
        raise AcmeException(
            status_code=400,
            error_name=AcmeError.MALFORMED_REQUEST,
            error_msg=f"Invalid protected header: {e!s}",
        ) from e


def parse_payload(payload_b64: str) -> dict[str, Any]:
    """解析 JWS payload"""
    try:
        if not payload_b64:
            return {}

        payload_data = base64url_decode(payload_b64)
        payload = json.loads(payload_data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED_REQUEST,
                error_msg="Payload must be a JSON object",
            )
        return payload
    except AcmeException:
        raise
    except (ValueError, UnicodeDecodeError, KeyError) as e:
        raise AcmeException(
            status_code=400,
            error_name=AcmeError.MALFORMED_REQUEST,
            error_msg=f"Invalid payload: {e!s}",
        ) from e


def validate_contact_list(contact: list[Any]) -> bool:
    """验证联系人列表格式"""
    if not isinstance(contact, list):
        return False

    for contact_info in contact:
        if not isinstance(contact_info, str):
            return False

        # 验证邮箱格式
        if contact_info.startswith("mailto:"):
            email = contact_info[7:]  # 移除 'mailto:' 前缀
            if "@" not in email or "." not in email.split("@")[1]:
                return False
        else:
            # 其他类型的联系方式可以在这里添加验证
            return False

    return True


def format_acme_error(error_type: str, detail: str, instance: str | None = None) -> dict[str, Any]:
    """格式化 ACME 错误响应"""
    error = {"type": f"urn:ietf:params:acme:error:{error_type}", "detail": detail}

    if instance:
        error["instance"] = instance

    return error


def generate_token() -> str:
    """生成随机令牌"""
    return base64url_encode(secrets.token_bytes(32))


def is_valid_identifier(identifier: dict[str, str]) -> bool:
    """验证标识符格式"""
    if not isinstance(identifier, dict):
        return False

    if "type" not in identifier or "value" not in identifier:
        return False

    # Agent CA 只支持 'agent' 类型的标识符
    if identifier["type"] != "agent":
        return False

    # 验证 Agent ID 格式（可以根据实际需求调整）
    agent_id = identifier["value"]
    return isinstance(agent_id, str) and len(agent_id) > 0


def extract_account_url_id(account_url: str) -> int | None:
    """从账户 URL 中提取账户 ID"""
    try:
        # 期望格式: /api/v1/acme/acct/{account_id}
        parts = account_url.split("/")
        if len(parts) >= 2 and parts[-2] == "acct":
            return int(parts[-1])
        return None
    except ValueError, IndexError:
        return None


def build_acme_url(base_url: str, endpoint: str, resource_id: str | None = None) -> str:
    """构建 ACME URL"""
    url = f"{base_url}/{endpoint}"
    if resource_id:
        url = f"{url}/{resource_id}"
    return url


def validate_csr_format(csr_b64: str) -> bool:
    """验证 CSR 格式"""
    try:
        csr_der = base64url_decode(csr_b64)
        x509.load_der_x509_csr(csr_der)
        return True
    except Exception:  # 保留广捕获：x509 CSR 格式检查可能抛出多种异常，验证执丑简化为突简 True/False
        return False


class ACMEResponse:
    """ACME 响应构建器"""

    def __init__(self, data: dict[str, Any], status_code: int = 200):
        self.data = data
        self.status_code = status_code
        self.headers = {"Cache-Control": "no-store", "Content-Type": "application/json"}

    def add_nonce(self, nonce: str) -> ACMEResponse:
        """添加 Replay-Nonce 头"""
        self.headers["Replay-Nonce"] = nonce
        return self

    def add_location(self, location: str) -> ACMEResponse:
        """添加 Location 头"""
        self.headers["Location"] = location
        return self

    def add_link(self, link: str) -> ACMEResponse:
        """添加 Link 头"""
        self.headers["Link"] = link
        return self

    def to_json_response(self) -> JSONResponse:
        """转换为 JSONResponse"""
        return JSONResponse(content=self.data, status_code=self.status_code, headers=self.headers)
