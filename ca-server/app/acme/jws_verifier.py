"""
JWS (JSON Web Signature) 验证服务

实现 ACME 协议所需的 JWS 签名验证功能
"""

import json
from typing import Any

from .exception import AcmeError, AcmeException
from .jwk import (
    JWKPublicKey,
    base64url_decode,
    base64url_encode,
    compute_jwk_thumbprint,
    jwk_matches,
    jwk_to_public_key,
    verify_compact_jws_signature,
)


class JWSVerifier:
    """JWS 签名验证器"""

    @staticmethod
    def base64url_decode(data: str) -> bytes:
        """Base64URL 解码"""
        return base64url_decode(data)

    @staticmethod
    def base64url_encode(data: bytes) -> str:
        """Base64URL 编码"""
        return base64url_encode(data)

    def parse_jws(self, jws_data: str) -> tuple[dict[str, Any], dict[str, Any], str]:
        """解析 JWS 数据

        Returns:
            Tuple[protected_header, payload, signature]
        """
        try:
            # 分割 JWS 组件
            parts = jws_data.split(".")
            if len(parts) != 3:
                raise ValueError("JWS must have exactly 3 parts")

            protected_b64, payload_b64, signature_b64 = parts

            # 解码 protected header
            protected_bytes = self.base64url_decode(protected_b64)
            protected_header = json.loads(protected_bytes.decode("utf-8"))

            # 解码 payload
            if payload_b64 == "":
                payload = {}
            else:
                payload_bytes = self.base64url_decode(payload_b64)
                payload = json.loads(payload_bytes.decode("utf-8"))

            return protected_header, payload, signature_b64

        except json.JSONDecodeError as e:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED,
                error_msg=f"Invalid JSON in JWS: {e!s}",
            ) from e
        except Exception as e:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED,
                error_msg=f"Invalid JWS format: {e!s}",
            ) from e

    def verify_jws_signature(
        self,
        jws_data: str,
        public_key_jwk: dict[str, Any],
        expected_nonce: str | None = None,
        expected_url: str | None = None,
    ) -> dict[str, Any]:
        """验证 JWS 签名

        Args:
            jws_data: JWS 数据字符串
            public_key_jwk: 公钥 JWK 格式
            expected_nonce: 期望的 nonce 值
            expected_url: 期望的 URL

        Returns:
            验证后的 payload
        """
        # 解析 JWS
        protected_header, payload, signature_b64 = self.parse_jws(jws_data)

        # 验证 protected header
        self._verify_protected_header(protected_header, public_key_jwk, expected_nonce, expected_url)

        # 验证签名
        alg = protected_header["alg"]
        self._verify_signature(jws_data, public_key_jwk, signature_b64, alg)

        return payload

    def _verify_protected_header(
        self,
        protected_header: dict[str, Any],
        public_key_jwk: dict[str, Any],
        expected_nonce: str | None = None,
        expected_url: str | None = None,
    ) -> None:
        """验证 protected header"""
        # 检查必需字段
        if "alg" not in protected_header:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED,
                error_msg="Missing 'alg' in protected header",
            )

        # 检查算法
        alg = protected_header["alg"]
        if alg not in ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA"]:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.UNSUPPORTED_ALGORITHM,
                error_msg=f"Unsupported algorithm: {alg}",
            )

        # 验证 nonce
        if expected_nonce:
            if "nonce" not in protected_header:
                raise AcmeException(
                    status_code=400,
                    error_name=AcmeError.BAD_NONCE,
                    error_msg="Missing nonce in protected header",
                )

            if protected_header["nonce"] != expected_nonce:
                raise AcmeException(
                    status_code=400,
                    error_name=AcmeError.BAD_NONCE,
                    error_msg="Invalid nonce",
                )

        # 验证 URL
        if expected_url:
            if "url" not in protected_header:
                raise AcmeException(
                    status_code=400,
                    error_name=AcmeError.MALFORMED,
                    error_msg="Missing URL in protected header",
                )

            if protected_header["url"] != expected_url:
                raise AcmeException(
                    status_code=400,
                    error_name=AcmeError.MALFORMED,
                    error_msg=f"URL mismatch: expected {expected_url}, got {protected_header['url']}",
                )

        # 验证 JWK 或 kid
        if "jwk" in protected_header:
            if not isinstance(protected_header["jwk"], dict) or not jwk_matches(
                protected_header["jwk"], public_key_jwk
            ):
                raise AcmeException(
                    status_code=400,
                    error_name=AcmeError.MALFORMED,
                    error_msg="JWK in protected header does not match account key",
                )
        elif "kid" in protected_header:
            # 对于已有账户，应该使用 kid 而不是 jwk
            pass
        else:
            raise AcmeException(
                status_code=400,
                error_name=AcmeError.MALFORMED,
                error_msg="Protected header must contain either 'jwk' or 'kid'",
            )

    def _verify_signature(
        self,
        jws_data: str,
        public_key_jwk: dict[str, Any],
        signature_b64: str,
        alg: str,
    ) -> None:
        """验证 JWS 签名"""
        protected_b64, payload_b64, _ = jws_data.split(".")
        verify_compact_jws_signature(protected_b64, payload_b64, signature_b64, public_key_jwk, alg)

    def _jwk_to_public_key(self, jwk: dict[str, Any]) -> JWKPublicKey:
        """将 JWK 转换为公钥对象"""
        return jwk_to_public_key(jwk)

    def compute_jwk_thumbprint(self, jwk: dict[str, Any]) -> str:
        """计算 JWK 指纹"""
        return compute_jwk_thumbprint(jwk)


# 全局 JWS 验证器实例
_jws_verifier: JWSVerifier | None = None


def get_jws_verifier() -> JWSVerifier:
    """获取 JWS 验证器实例"""
    global _jws_verifier
    if _jws_verifier is None:
        _jws_verifier = JWSVerifier()
    return _jws_verifier
