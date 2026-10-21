"""用 Python 版 acps_sdk.oidc 生成跨语言对照样例 → test/fixtures/python/oidc/*.json。

运行（在 contrib/acps-sdk-node 目录下）：
    uv run --project ../../acps-sdk --python 3.12 python -I scripts/fixtures/gen_oidc.py

确定性：
- 签名密钥由固定 seed 派生（Ed25519 签名本身是确定性的；ES256/PS256 带随机数，不放进样例）；
- iat/nbf 固定在过去（2023-11），exp 固定在 2100-01-01，过期/未生效样例用固定的过去/远未来时间戳。

签名用的 seed 只存在于本脚本（公开的一次性测试材料）；样例 JSON 里只有公钥 JWK 与签好的 token。
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import httpx  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402
from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from _common import write_fixture  # noqa: E402
from acps_sdk.oidc import (  # noqa: E402
    HumanPrincipal,
    KeycloakClaimMapping,
    OidcDeviceClientConfig,
    OidcProviderConfig,
    OidcTokenResponse,
    OidcTokenValidator,
    audit_actor_from_principal,
    build_principal_id,
    build_principal_key,
    canonical_json_bytes,
    claims_to_principal,
    normalize_audiences,
    optional_principal,
    parse_multi_value_claim,
    parse_scope_claim,
    require_principal,
    require_roles,
    require_scopes,
)

PKG = "oidc"

ISSUER = "https://issuer.example/realms/acps-fixture"
AUDIENCE = "fixture-api"
JWKS_URI = f"{ISSUER}/protocol/openid-connect/certs"


def _is_probable_prime(n: int, rng: random.Random) -> bool:
    """Miller-Rabin（40 轮）。见证数只影响判定，不影响选中哪个候选数，因此结果与 randrange 的实现无关。"""
    for small in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % small == 0:
            return n == small
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(40):
        x = pow(rng.randrange(2, n - 1), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def deterministic_rsa_key(seed: str, bits: int = 2048) -> rsa.RSAPrivateKey:
    """由固定 seed 派生 RSA 密钥（cryptography 不支持带种子生成）：random.Random(seed) 取候选数，e = 65537。"""
    rng = random.Random(seed)
    e = 65537

    def prime(nbits: int) -> int:
        while True:
            candidate = rng.getrandbits(nbits) | (0b11 << (nbits - 2)) | 1
            if candidate % e != 1 and _is_probable_prime(candidate, rng):
                return candidate

    p, q = prime(bits // 2), prime(bits // 2)
    p, q = max(p, q), min(p, q)
    d = pow(e, -1, (p - 1) * (q - 1))
    numbers = rsa.RSAPrivateNumbers(
        p=p,
        q=q,
        d=d,
        dmp1=rsa.rsa_crt_dmp1(d, p),
        dmq1=rsa.rsa_crt_dmq1(d, q),
        iqmp=rsa.rsa_crt_iqmp(p, q),
        public_numbers=rsa.RSAPublicNumbers(e, p * q),
    )
    return numbers.private_key()


# 一次性测试密钥（公开、确定性；样例 JSON 里只有公钥）
PRIMARY_KEY = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
OTHER_KEY = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))
EC_KEY = ec.derive_private_key(0x5EED, ec.SECP256R1())
ED448_KEY = ed448.Ed448PrivateKey.from_private_bytes(bytes(range(57)))
RSA_KEY = deterministic_rsa_key("acps-sdk-node/gen_oidc.py rsa-kid")

IAT = 1_700_000_000  # 2023-11-14
EXP = 4_102_444_800  # 2100-01-01
FUTURE = 4_000_000_000  # 2096-10
PAST_EXP = 1_700_000_100


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def raw_public(key: Any) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def rsa_jwk(key: rsa.RSAPrivateKey) -> dict[str, Any]:
    numbers = key.public_key().public_numbers()
    return {
        "n": b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }


def ec_jwk(key: ec.EllipticCurvePrivateKey) -> dict[str, Any]:
    numbers = key.public_key().public_numbers()
    return {
        "x": b64url(numbers.x.to_bytes(32, "big")),
        "y": b64url(numbers.y.to_bytes(32, "big")),
    }


JWKS: dict[str, Any] = {
    "keys": [
        {"kty": "OKP", "crv": "Ed25519", "kid": "fixture-kid", "alg": "EdDSA", "use": "sig", "x": b64url(raw_public(PRIMARY_KEY))},
        {"kty": "OKP", "crv": "Ed25519", "kid": "noalg-kid", "x": b64url(raw_public(PRIMARY_KEY))},
        {"kty": "OKP", "crv": "Ed25519", "kid": "enc-kid", "use": "enc", "x": b64url(raw_public(PRIMARY_KEY))},
        {"kty": "EC", "crv": "P-256", "kid": "ec-kid", "alg": "ES256", "use": "sig", **ec_jwk(EC_KEY)},
        {"kty": "OKP", "crv": "Ed448", "kid": "ed448-kid", "x": b64url(raw_public(ED448_KEY))},
        {"kty": "OKP", "crv": "Ed25519", "kid": "badx-kid", "alg": "EdDSA", "x": "AAAA"},
        {"kty": "RSA", "kid": "rsa-kid", "alg": "RS256", "use": "sig", **rsa_jwk(RSA_KEY)},
        {"kty": "RSA", "kid": "rsa-noalg-kid", **rsa_jwk(RSA_KEY)},
        "not-a-jwk-object",
    ]
}
DISCOVERY = {"issuer": ISSUER + "/", "jwks_uri": JWKS_URI}

CONFIG_INPUT: dict[str, Any] = {
    "issuer": ISSUER,
    "audience": AUDIENCE,
    "allowed_azp": ["fixture-web"],
    "algorithms": ["EdDSA", "RS256", "ES256", "ES384", "HS256", "none"],
    "claim_mapping": {"resource_client_id": AUDIENCE, "read_realm_roles": True},
}

BASE_HEADER: dict[str, Any] = {"alg": "EdDSA", "kid": "fixture-kid", "typ": "JWT"}

BASE_CLAIMS: dict[str, Any] = {
    "iss": ISSUER,
    "sub": "user-123",
    "aud": [AUDIENCE, "account"],
    "azp": "fixture-web",
    "exp": EXP,
    "iat": IAT,
    "nbf": IAT,
    "jti": "jti-1",
    "preferred_username": " alice ",
    "name": "Alice Liddell",
    "email": "alice@example.com",
    "email_verified": True,
    "tenant_id": " tenant-1 ",
    "allowed_aics": "aic-1, aic-2,,aic-1",
    "scope": "session:read  session:write\tsession:read",
    "groups": ["/ops", " /ops ", "/monitor"],
    "resource_access": {AUDIENCE: {"roles": ["USER", "ADMIN", "USER"]}, "other": {"roles": ["X"]}},
    "realm_access": {"roles": ["REALM_ADMIN", "USER"]},
}

_MISSING = object()


def claims(**overrides: Any) -> dict[str, Any]:
    result = dict(BASE_CLAIMS)
    for key, value in overrides.items():
        if value is _MISSING:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def header(**overrides: Any) -> dict[str, Any]:
    result = dict(BASE_HEADER)
    for key, value in overrides.items():
        if value is _MISSING:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def segment(value: Any) -> str:
    if isinstance(value, bytes):
        return b64url(value)
    return b64url(json.dumps(value, separators=(",", ":")).encode())


def sign(header_segment: str, payload_segment: str, key: Any = PRIMARY_KEY) -> str:
    data = f"{header_segment}.{payload_segment}".encode()
    if isinstance(key, rsa.RSAPrivateKey):
        return b64url(key.sign(data, padding.PKCS1v15(), hashes.SHA256()))  # RS256，本身是确定性的
    if isinstance(key, ec.EllipticCurvePrivateKey):
        # RFC 6979 确定性 ECDSA，保证重新生成样例时签名不变；JWS 使用 r || s 定长编码
        der = key.sign(data, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))
        r, s_value = decode_dss_signature(der)
        return b64url(r.to_bytes(32, "big") + s_value.to_bytes(32, "big"))
    return b64url(key.sign(data))


def token(hdr: Any = None, payload: Any = None, *, key: Any = PRIMARY_KEY) -> str:
    h = segment(BASE_HEADER if hdr is None else hdr)
    p = segment(BASE_CLAIMS if payload is None else payload)
    return f"{h}.{p}.{sign(h, p, key)}"


def build_token_cases() -> list[tuple[str, str]]:
    h = segment(BASE_HEADER)
    p = segment(BASE_CLAIMS)
    valid_signature = sign(h, p)
    tampered_payload = segment(claims(sub="mallory"))
    padded_header = h + "=" * ((4 - len(h) % 4) % 4 or 4)
    split_payload = f"{p[:8]}.{p[8:]}"
    empty_b64_header = segment(header(b64=False))
    empty_b64_crit_header = segment(header(b64=False, crit=["b64"]))
    # 补到“数据字符数 % 4 == 1”，binascii 必然报错
    bad_header = h + "A" * ((1 - len(h)) % 4)
    bad_signature = valid_signature + "A" * ((1 - len(valid_signature)) % 4)
    rs_header = segment(header(alg="RS256", kid="rsa-kid"))
    es_header = segment(header(alg="ES256", kid="ec-kid"))
    es_token = token(hdr=header(alg="ES256", kid="ec-kid"), key=EC_KEY)

    return [
        # ---- 成功 ----
        ("valid_full", token()),
        ("valid_aud_string", token(payload=claims(aud=AUDIENCE))),
        ("valid_minimal_claims", token(payload={"iss": ISSUER, "sub": "u-1", "aud": AUDIENCE, "azp": "fixture-web", "exp": EXP})),
        ("valid_iat_string", token(payload=claims(iat=str(IAT)))),
        ("valid_iat_nbf_float", token(payload=claims(iat=IAT + 0.9, nbf=IAT + 0.5))),
        ("valid_exp_string_whitespace", token(payload=claims(exp=f" {EXP} "))),
        ("valid_exp_string_underscores", token(payload=claims(exp="4_102_444_800"))),
        ("valid_iat_bool", token(payload=claims(iat=True))),
        ("valid_noalg_kid", token(hdr=header(kid="noalg-kid"))),
        ("valid_crit_b64_true", token(hdr=header(b64=True, crit=["b64"]))),
        ("valid_unicode_claims", token(payload=claims(name="张三 😀", email="张三@example.com"))),
        ("valid_email_verified_null", token(payload=claims(email_verified=None, name=None))),
        ("valid_proto_like_claim_names", token(payload=claims(constructor="x", toString="y", preferred_username=_MISSING))),
        ("valid_dict_resource_access_roles_string", token(payload=claims(resource_access={AUDIENCE: {"roles": "A, B ,A"}}))),
        ("valid_rs256", token(hdr=header(alg="RS256", kid="rsa-kid"), key=RSA_KEY)),
        ("valid_rs256_noalg_jwk", token(hdr=header(alg="RS256", kid="rsa-noalg-kid"), key=RSA_KEY)),
        ("valid_es256", token(hdr=header(alg="ES256", kid="ec-kid"), key=EC_KEY)),
        # ---- RS256 / ES256 失败 ----
        ("rs256_signed_by_other_key", token(hdr=header(alg="RS256", kid="rsa-kid"), key=deterministic_rsa_key("acps-sdk-node/gen_oidc.py other-rsa"))),
        ("rs256_tampered_payload", f"{rs_header}.{tampered_payload}.{sign(rs_header, p, RSA_KEY)}"),
        ("es256_truncated_signature", es_token[:-4]),
        ("es256_tampered_payload", f"{es_header}.{tampered_payload}.{es_token.rsplit('.', 1)[1]}"),
        ("rs256_with_okp_jwk", token(hdr=header(alg="RS256", kid="noalg-kid"), key=RSA_KEY)),
        ("es256_with_rsa_jwk", token(hdr=header(alg="ES256", kid="rsa-noalg-kid"), key=EC_KEY)),
        ("es256_jwk_alg_mismatch", token(hdr=header(alg="ES256", kid="rsa-kid"), key=EC_KEY)),
        # ---- PyJWT required / 时间 ----
        ("missing_exp", token(payload=claims(exp=_MISSING))),
        ("null_exp", token(payload=claims(exp=None))),
        ("missing_iss", token(payload=claims(iss=_MISSING))),
        ("missing_aud", token(payload=claims(aud=_MISSING))),
        ("expired", token(payload=claims(exp=PAST_EXP))),
        ("iat_in_future", token(payload=claims(iat=FUTURE))),
        ("nbf_in_future", token(payload=claims(nbf=FUTURE))),
        ("iat_not_integer", token(payload=claims(iat="abc"))),
        ("iat_decimal_string", token(payload=claims(iat="1.5"))),
        ("nbf_not_integer", token(payload=claims(nbf="abc"))),
        ("exp_not_integer", token(payload=claims(exp="abc"))),
        ("iat_null_raw_type_error", token(payload=claims(iat=None))),
        ("nbf_list_raw_type_error", token(payload=claims(nbf=[]))),
        ("exp_object_raw_type_error", token(payload=claims(exp={}))),
        # ---- iss / aud / sub / jti ----
        ("wrong_issuer", token(payload=claims(iss="https://other.example/realms/acps-fixture"))),
        ("issuer_not_string", token(payload=claims(iss=123))),
        ("audience_mismatch", token(payload=claims(aud="other-api"))),
        ("audience_id_token_style", token(payload=claims(aud="fixture-web"))),
        ("audience_empty_string", token(payload=claims(aud=""))),
        ("audience_empty_list", token(payload=claims(aud=[]))),
        ("audience_number", token(payload=claims(aud=5))),
        ("audience_list_with_number", token(payload=claims(aud=[AUDIENCE, 1]))),
        ("audience_object", token(payload=claims(aud={"a": 1}))),
        ("audience_empty_object", token(payload=claims(aud={}))),
        ("subject_not_string", token(payload=claims(sub=123))),
        ("jti_not_string", token(payload=claims(jti=5))),
        # ---- azp / Keycloak 映射 ----
        ("azp_missing", token(payload=claims(azp=_MISSING))),
        ("azp_not_allowed", token(payload=claims(azp="other-web"))),
        ("azp_not_string", token(payload=claims(azp=5))),
        ("subject_missing", token(payload=claims(sub=_MISSING))),
        ("subject_blank", token(payload=claims(sub="   "))),
        ("email_verified_not_bool", token(payload=claims(email_verified="yes"))),
        ("email_not_string", token(payload=claims(email=5))),
        ("tenant_not_string", token(payload=claims(tenant_id=5))),
        ("scope_not_string", token(payload=claims(scope=["a"]))),
        ("groups_number", token(payload=claims(groups=5))),
        ("groups_with_number", token(payload=claims(groups=["a", 1]))),
        ("client_roles_object", token(payload=claims(resource_access={AUDIENCE: {"roles": {"a": 1}}}))),
        ("allowed_aics_bool", token(payload=claims(allowed_aics=True))),
        # ---- payload 解码 ----
        ("payload_json_array", token(payload=[1, 2])),
        ("payload_not_json", token(payload=b"not json")),
        ("payload_invalid_utf8", token(payload=b'{"iss":"\xff"}')),
        # ---- header ----
        ("alg_missing", token(hdr=header(alg=_MISSING))),
        ("alg_empty", token(hdr=header(alg=""))),
        ("alg_number", token(hdr=header(alg=5))),
        ("alg_not_allowed", token(hdr=header(alg="PS256"))),
        ("alg_hs256_listed_but_rejected", token(hdr=header(alg="HS256"))),
        ("alg_none_listed_but_rejected", token(hdr=header(alg="none"))),
        ("alg_repr_with_quote", token(hdr=header(alg="it's"))),
        ("alg_unsupported_es384", token(hdr=header(alg="ES384", kid="noalg-kid"))),
        ("alg_es384_jwk_alg_mismatch", token(hdr=header(alg="ES384"))),
        ("kid_missing", token(hdr=header(kid=_MISSING))),
        ("kid_empty", token(hdr=header(kid=""))),
        ("kid_number", token(hdr=header(kid=5))),
        ("kid_null", token(hdr=header(kid=None))),
        ("kid_unknown", token(hdr=header(kid="unknown-kid"))),
        ("jwk_use_enc", token(hdr=header(kid="enc-kid"))),
        ("jwk_ec_alg_mismatch", token(hdr=header(kid="ec-kid"))),
        ("jwk_ed448_curve", token(hdr=header(kid="ed448-kid"))),
        ("jwk_bad_x", token(hdr=header(kid="badx-kid"))),
        ("crit_unsupported", token(hdr=header(crit=["exp"]))),
        ("crit_empty", token(hdr=header(crit=[]))),
        ("crit_not_list", token(hdr=header(crit="b64"))),
        ("crit_b64_missing_param", token(hdr=header(crit=["b64"]))),
        ("b64_false_with_payload", token(hdr=header(b64=False))),
        ("b64_false_without_crit", f"{empty_b64_header}..{sign(empty_b64_header, '')}"),
        ("b64_false_with_crit", f"{empty_b64_crit_header}..{sign(empty_b64_crit_header, '')}"),
        ("header_json_array", token(hdr=["EdDSA"])),
        ("header_not_json", token(hdr=b"not json")),
        ("header_invalid_utf8", token(hdr=b'{"alg":"\xff"}')),
        # ---- 结构 ----
        ("token_empty", ""),
        ("token_one_segment", "abc"),
        ("token_two_segments", "abc.def"),
        ("header_bad_padding", f"{bad_header}.{p}.{valid_signature}"),
        ("signature_bad_padding", f"{h}.{p}.{bad_signature}"),
        ("signature_other_key", token(key=OTHER_KEY)),
        ("signature_tampered_payload", f"{h}.{tampered_payload}.{valid_signature}"),
        ("signature_empty", f"{h}.{p}."),
        # ---- 已知差异：Python 的宽松 base64 解码接受这些非规范编码 ----
        ("lenient_padded_header", f"{padded_header}.{p}.{sign(padded_header, p)}"),
        ("lenient_dot_inside_payload", f"{h}.{split_payload}.{sign(h, split_payload)}"),
    ]


def describe_error(exc: BaseException) -> dict[str, Any]:
    return {"type": type(exc).__name__, "message": str(exc)}


def dump_principal(principal: HumanPrincipal) -> dict[str, Any]:
    return {
        "dump": principal.model_dump(mode="json"),
        "subject": principal.subject,
        "principal_key": principal.principal_key,
        "raw_claims": principal.raw_claims,
        "audit_actor": audit_actor_from_principal(principal).model_dump(
            mode="json", by_alias=True, exclude_none=True
        ),
    }


def provider_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=DISCOVERY)
        if str(request.url) == JWKS_URI:
            return httpx.Response(200, json=JWKS)
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)


def broken_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    return httpx.MockTransport(handler)


def make_validator(transport: httpx.BaseTransport | None = None) -> OidcTokenValidator:
    return OidcTokenValidator(
        config=OidcProviderConfig.model_validate(CONFIG_INPUT),
        http_client=httpx.AsyncClient(transport=transport or provider_transport()),
    )


async def validate(token_value: str) -> dict[str, Any]:
    validator = make_validator()
    try:
        return {"principal": dump_principal(await validator.validate_access_token(token_value))}
    except Exception as exc:  # noqa: BLE001 - 样例需要记录所有异常类型
        return {"error": describe_error(exc)}
    finally:
        await validator.close()


def gen_validator() -> None:
    cases = []
    for name, token_value in build_token_cases():
        cases.append({"name": name, "token": token_value, "expected": asyncio.run(validate(token_value))})
    write_fixture(
        PKG,
        "validator",
        {
            "config": CONFIG_INPUT,
            "discovery": DISCOVERY,
            "jwks_uri": JWKS_URI,
            "jwks": JWKS,
            "cases": cases,
        },
    )


# =============================================================================
# FastAPI 依赖层
# =============================================================================

REQUIRED_ROLES = ["ADMIN", "", "USER", "ADMIN"]
REQUIRED_SCOPES = ["session:write", "session:read", "session:admin"]


def gen_http() -> None:
    user_token = token(payload=claims(resource_access={AUDIENCE: {"roles": ["USER"]}}, realm_access=_MISSING))
    admin_token = token()
    expired_token = token(payload=claims(exp=PAST_EXP))
    authorizations: list[tuple[str, str | None]] = [
        ("no_header", None),
        ("empty_header", ""),
        ("bearer_admin", f"Bearer {admin_token}"),
        ("bearer_lowercase", f"bearer {admin_token}"),
        ("bearer_uppercase_extra_spaces", f"BEARER   {admin_token}  "),
        ("bearer_user", f"Bearer {user_token}"),
        ("bearer_expired", f"Bearer {expired_token}"),
        ("bearer_invalid", "Bearer invalid-token"),
        ("bearer_only", "Bearer"),
        ("bearer_trailing_space_only", "Bearer "),
        ("bearer_tab_separator", f"Bearer\t{admin_token}"),
        ("basic_scheme", "Basic YWxpY2U6c2VjcmV0"),
        ("token_scheme", f"Token {admin_token}"),
    ]

    def app_for(validator: OidcTokenValidator) -> FastAPI:
        app = FastAPI()
        principal_dep = require_principal(validator)
        optional_dep = optional_principal(validator)
        roles_dep = require_roles(validator, REQUIRED_ROLES)
        scopes_dep = require_scopes(validator, REQUIRED_SCOPES)

        @app.get("/principal")
        async def principal_endpoint(principal=Depends(principal_dep)) -> dict[str, Any]:
            return {"principalId": principal.principal_id, "roles": list(principal.roles)}

        @app.get("/optional")
        async def optional_endpoint(principal=Depends(optional_dep)) -> dict[str, Any]:
            return {"authenticated": principal is not None}

        @app.get("/roles")
        async def roles_endpoint(principal=Depends(roles_dep)) -> dict[str, Any]:
            return {"principalId": principal.principal_id}

        @app.get("/scopes")
        async def scopes_endpoint(principal=Depends(scopes_dep)) -> dict[str, Any]:
            return {"principalId": principal.principal_id}

        return app

    def run(provider: str, endpoint: str, authorization: str | None) -> dict[str, Any]:
        transport = provider_transport() if provider == "ok" else broken_transport()
        client = TestClient(app_for(make_validator(transport)), raise_server_exceptions=False)
        headers = {} if authorization is None else {"Authorization": authorization}
        response = client.get(f"/{endpoint}", headers=headers)
        return {
            "status": response.status_code,
            "body": response.json(),
            "www_authenticate": response.headers.get("www-authenticate"),
        }

    cases = []
    for endpoint in ("principal", "optional", "roles", "scopes"):
        for name, authorization in authorizations:
            cases.append(
                {
                    "name": f"{endpoint}:{name}",
                    "provider": "ok",
                    "endpoint": endpoint,
                    "authorization": authorization,
                    "expected": run("ok", endpoint, authorization),
                }
            )
        for name, authorization in [("bearer_admin", f"Bearer {admin_token}"), ("no_header", None)]:
            cases.append(
                {
                    "name": f"{endpoint}:provider_down:{name}",
                    "provider": "broken",
                    "endpoint": endpoint,
                    "authorization": authorization,
                    "expected": run("broken", endpoint, authorization),
                }
            )
    write_fixture(
        PKG,
        "http",
        {
            "required_roles": REQUIRED_ROLES,
            "required_scopes": REQUIRED_SCOPES,
            "cases": cases,
        },
    )


# =============================================================================
# principal / canonical JSON / audit actor
# =============================================================================


def gen_principal() -> None:
    identities = [
        ("https://issuer.example/realms/acps-leader", "user-123"),
        ("issuer#a", "subject"),
        ("issuer", "a#subject"),
        ("https://身份.example/realms/测试", "用户-😀"),
        ("", ""),
        ('quote"and\\slash', "control\u0001\n"),
    ]
    principal_ids = [
        {
            "issuer": issuer,
            "subject": subject,
            "principal_key": build_principal_key(issuer=issuer, subject=subject),
            "principal_id": build_principal_id(issuer=issuer, subject=subject),
        }
        for issuer, subject in identities
    ]

    canonical_values: list[Any] = [
        {"b": 2, "a": 1},
        {"a": 1, "b": 2},
        ["oidc-principal-v1", "https://issuer.example", "sub"],
        {"10": 1, "2": 2, "a": 3, "B": 4, "é": 5, "￿": 6, "😀": 7, "": 8},
        {"nested": {"z": [3, {"y": None, "x": True}], "a": False}},
        [0, -1, 9007199254740991, -9007199254740991],
        [0.1, 1.5e-05, 1e-07, 123.456, -2.5e-10, 0.0001, 0.00011, 9.99e-05, 1e-300, 2.5e15 + 0.5],
        ["\u0000\u001f\"\\\n\t\b\f\r", "\u007f\u0080  ", "中文", "😀"],
        None,
        "plain string",
        [],
        {},
    ]
    canonical = [
        {"value": value, "hex": canonical_json_bytes(value).hex()} for value in canonical_values
    ]

    principal_inputs: list[dict[str, Any]] = [
        {
            "issuer": "https://issuer.example/realms/acps-leader",
            "subject": "raw-subject",
            "principal_key": "https://issuer.example/realms/acps-leader#raw-subject",
            "principal_id": "principal-id",
            "audiences": ["leader-api"],
            "username": "alice",
            "roles": ["USER", "ADMIN"],
            "raw_claims": {"sub": "raw-subject"},
        },
        {
            "issuer": "i",
            "subject": "s",
            "principal_key": "i#s",
            "principal_id": "p",
            "audiences": [],
            "name": "",
            "username": "",
            "email": "e@example.com",
        },
        {
            "issuer": "i",
            "subject": "s",
            "principal_key": "i#s",
            "principal_id": "p",
            "audiences": ["a"],
            "name": None,
            "username": None,
            "email": "",
            "roles": ["b", "A", "a", "Ä"],
        },
        {
            "issuer": "i",
            "subject": "s",
            "principal_key": "i#s",
            "principal_id": "p",
            "audiences": ["a"],
            "name": "Name",
            "username": "user",
            "email_verified": False,
            "tenant_id": "t",
            "allowed_aics": ["aic"],
            "scopes": ["x"],
            "groups": ["/g"],
        },
    ]
    principals = []
    for data in principal_inputs:
        principal = HumanPrincipal(**data)
        principals.append(
            {
                "input": data,
                "dump": principal.model_dump(mode="json"),
                "audit_actor": audit_actor_from_principal(principal).model_dump(
                    mode="json", by_alias=True, exclude_none=True
                ),
            }
        )

    write_fixture(
        PKG,
        "principal",
        {"principal_ids": principal_ids, "canonical_json": canonical, "principals": principals},
    )


# =============================================================================
# Keycloak claim 映射
# =============================================================================


def capture(fn: Any) -> dict[str, Any]:
    try:
        return {"value": fn()}
    except Exception as exc:  # noqa: BLE001
        return {"error": describe_error(exc)}


def gen_keycloak() -> None:
    function_cases: list[dict[str, Any]] = []
    for fn_name, fn, values in [
        ("normalizeAudiences", normalize_audiences, ["a", " a ", ["a", "b", "a", " ", "c "], [], None, 5, {"a": 1}, ["a", 1], ""]),
        ("parseScopeClaim", parse_scope_claim, ["a b  a\tc\n", None, "", ["a"], 5, "　x　y\u001c"]),
        (
            "parseMultiValueClaim",
            parse_multi_value_claim,
            ["a", " a ,b,, a", ["a", " b ", "a", ""], None, "", 5, True, {"a": 1}, ["a", 2], "a b"],
        ),
    ]:
        for value in values:
            result = capture(lambda fn=fn, value=value: list(fn(value)))
            function_cases.append({"fn": fn_name, "input": value, "expected": result})

    base = {
        "iss": "https://issuer.example/realms/acps-monitor",
        "sub": "user-123",
        "aud": ["monitor-api", "account"],
        "azp": "monitor-web",
        "preferred_username": "alice",
        "name": "Alice",
        "email": "alice@example.com",
        "email_verified": True,
        "tenant_id": "tenant-1",
        "allowed_aics": ["aic-1", "aic-2"],
        "scope": "metrics:read system:query",
        "groups": ["/ops", "/monitor"],
        "resource_access": {"monitor-api": {"roles": ["AUDITOR", "SYSTEM_READER"]}},
        "realm_access": {"roles": ["REALM_ADMIN"]},
    }

    def variant(**overrides: Any) -> dict[str, Any]:
        result = dict(base)
        for key, value in overrides.items():
            if value is _MISSING:
                result.pop(key, None)
            else:
                result[key] = value
        return result

    mapping_cases: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        ("default_client_roles", variant(), {"resource_client_id": "monitor-api"}),
        ("no_resource_client", variant(), {}),
        ("realm_roles_enabled", variant(), {"resource_client_id": "monitor-api", "read_realm_roles": True}),
        ("client_roles_disabled", variant(), {"resource_client_id": "monitor-api", "read_client_roles": False, "read_realm_roles": True}),
        ("groups_disabled", variant(), {"resource_client_id": "monitor-api", "read_groups": False}),
        ("custom_claim_names", variant(nick="  nick  ", display="Disp", mail="m@example.com", verified=False, org="org-1", aics="x,y"), {
            "username_claims": ["missing", "nick"],
            "name_claims": "display",
            "email_claim": "mail",
            "email_verified_claim": "verified",
            "tenant_claim": "org",
            "allowed_aics_claim": "aics",
        }),
        ("username_fallback", variant(preferred_username="   ", username="bob"), {}),
        ("name_falls_back_to_username", variant(name=_MISSING), {}),
        ("blank_optional_strings", variant(azp="  ", email=" ", tenant_id=""), {}),
        ("duplicate_roles_across_sources", variant(resource_access={"monitor-api": {"roles": "A,B"}}, realm_access={"roles": ["B", "C"]}), {"resource_client_id": "monitor-api", "read_realm_roles": True}),
        ("resource_access_not_object", variant(resource_access=["x"]), {"resource_client_id": "monitor-api"}),
        ("realm_access_not_object", variant(realm_access="x"), {"read_realm_roles": True}),
        ("prototype_claim_names", variant(preferred_username=_MISSING, name=_MISSING), {"username_claims": ["constructor", "toString"], "name_claims": ["__proto__", "hasOwnProperty"]}),
        ("missing_iss", variant(iss=_MISSING), {}),
        ("blank_sub", variant(sub=" "), {}),
        ("aud_missing", variant(aud=_MISSING), {}),
        ("aud_bad_item", variant(aud=["a", None]), {}),
        ("azp_number", variant(azp=1), {}),
        ("email_verified_string", variant(email_verified="true"), {}),
        ("scope_list", variant(scope=["a"]), {}),
        ("allowed_aics_number", variant(allowed_aics=1), {}),
        ("client_roles_bad_item", variant(resource_access={"monitor-api": {"roles": ["A", 1]}}), {"resource_client_id": "monitor-api"}),
        ("non_ascii_values", variant(iss="https://身份.example/realms/测试", sub="用户", name="张三"), {}),
    ]
    principal_cases = []
    for name, claim_values, mapping_input in mapping_cases:
        mapping = KeycloakClaimMapping.model_validate(mapping_input)
        try:
            expected: dict[str, Any] = {
                "principal": dump_principal(claims_to_principal(claims=claim_values, claim_mapping=mapping))
            }
        except Exception as exc:  # noqa: BLE001
            expected = {"error": describe_error(exc)}
        principal_cases.append({"name": name, "claims": claim_values, "claim_mapping": mapping_input, "expected": expected})

    write_fixture(PKG, "keycloak", {"functions": function_cases, "principals": principal_cases})


# =============================================================================
# 配置模型与 token 响应
# =============================================================================


def describe_model_error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ValidationError):
        issues = []
        for error in exc.errors():
            message = error["msg"]
            if error["type"] == "value_error":
                message = message.removeprefix("Value error, ")
            issues.append({"loc": list(error["loc"]), "type": error["type"], "msg": message})
        return {"type": "ValidationError", "issues": issues}
    return describe_error(exc)


def dump_model(model_cls: Any, data: Any) -> dict[str, Any]:
    try:
        return {"value": model_cls.model_validate(data).model_dump(mode="json")}
    except Exception as exc:  # noqa: BLE001
        return {"error": describe_model_error(exc)}


def gen_config() -> None:
    cases: list[tuple[str, Any, dict[str, Any]]] = [
        ("OidcProviderConfig", OidcProviderConfig, {"issuer": " https://idp.example/realms/a// ", "audience": " api "}),
        ("OidcProviderConfig", OidcProviderConfig, {"issuer": "x", "audience": "a", "allowed_azp": "web", "algorithms": [" ES256 ", "ES256", "", "EdDSA"]}),
        ("OidcProviderConfig", OidcProviderConfig, {"issuer": "x", "audience": "a", "allowed_azp": None, "algorithms": None}),
        ("OidcProviderConfig", OidcProviderConfig, {"issuer": "x", "audience": "a", "algorithms": [1, True, None, 1.5, 2.5e-05]}),
        ("OidcProviderConfig", OidcProviderConfig, {"issuer": "x", "audience": "a", "allowed_azp": {"web": 1, "cli": 2}}),
        ("OidcProviderConfig", OidcProviderConfig, {"issuer": "x", "audience": "a", "algorithms": 5}),
        ("OidcProviderConfig", OidcProviderConfig, {"issuer": " / ", "audience": "a"}),
        ("OidcProviderConfig", OidcProviderConfig, {"issuer": "x", "audience": "　"}),
        ("OidcProviderConfig", OidcProviderConfig, {"issuer": "x", "audience": "a", "leeway_seconds": -1, "http_timeout_seconds": 0}),
        ("OidcProviderConfig", OidcProviderConfig, {"issuer": "x", "audience": "a", "jwks_cache_ttl_seconds": 0, "discovery_cache_ttl_seconds": 0, "require_https": False, "client_id": "rs"}),
        ("OidcProviderConfig", OidcProviderConfig, {"issuer": "x", "audience": "a", "unknown_field": 1}),
        ("OidcProviderConfig", OidcProviderConfig, {"issuer": "x", "audience": "a", "claim_mapping": {"resource_client_id": "api", "username_claims": "nick"}}),
        ("OidcProviderConfig", OidcProviderConfig, {"issuer": "x", "audience": "a", "claim_mapping": {"bogus": True}}),
        ("KeycloakClaimMapping", KeycloakClaimMapping, {}),
        ("KeycloakClaimMapping", KeycloakClaimMapping, {"username_claims": None, "name_claims": [" a ", "a", "b"]}),
        ("KeycloakClaimMapping", KeycloakClaimMapping, {"username_claims": 5}),
        ("OidcDeviceClientConfig", OidcDeviceClientConfig, {"issuer": " https://idp.example/realms/a/ ", "client_id": " cli "}),
        ("OidcDeviceClientConfig", OidcDeviceClientConfig, {"issuer": "x", "client_id": "c", "scopes": "openid  profile openid\temail"}),
        ("OidcDeviceClientConfig", OidcDeviceClientConfig, {"issuer": "x", "client_id": "c", "scopes": ["openid", " profile ", "", "openid", 7]}),
        ("OidcDeviceClientConfig", OidcDeviceClientConfig, {"issuer": "x", "client_id": "c", "scopes": None}),
        ("OidcDeviceClientConfig", OidcDeviceClientConfig, {"issuer": "x", "client_id": "c", "scopes": 123}),
        ("OidcDeviceClientConfig", OidcDeviceClientConfig, {"issuer": "x", "client_id": " "}),
        ("OidcDeviceClientConfig", OidcDeviceClientConfig, {"issuer": "x", "client_id": "c", "http_timeout_seconds": -1}),
    ]
    out = [
        {"model": model_name, "input": data, "expected": dump_model(model_cls, data)}
        for model_name, model_cls, data in cases
    ]

    token_inputs: list[Any] = [
        {"access_token": "a"},
        {"access_token": "a", "token_type": None},
        {"access_token": "a", "token_type": ""},
        {"access_token": "a", "token_type": " BEARER "},
        {"access_token": "a", "token_type": "mac"},
        {"access_token": "a", "token_type": 5},
        {"access_token": "a", "expires_in": 300, "refresh_token": "r", "refresh_expires_in": 0, "scope": "openid", "id_token": "i", "session_state": "s", "not-before-policy": 0},
        {"access_token": "a", "expires_in": -1},
        {"access_token": "", "refresh_token": ""},
        {"token_type": "Bearer"},
        {"access_token": 5},
    ]
    token_cases = []
    for data in token_inputs:
        try:
            response = OidcTokenResponse.model_validate(data)
            expected: dict[str, Any] = {
                "value": response.model_dump(mode="json"),
                "access_token_value": response.access_token_value,
                "refresh_token_value": response.refresh_token_value,
                "id_token_value": response.id_token_value,
            }
        except Exception as exc:  # noqa: BLE001
            expected = {"error": describe_model_error(exc)}
        token_cases.append({"input": data, "expected": expected})

    write_fixture(PKG, "models", {"configs": out, "token_responses": token_cases})


def main() -> None:
    gen_principal()
    gen_keycloak()
    gen_config()
    gen_validator()
    gen_http()


if __name__ == "__main__":
    main()
