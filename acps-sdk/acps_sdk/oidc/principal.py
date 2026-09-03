"""从 OIDC access token 派生出的真人 principal 抽象。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def canonical_json_bytes(value: Any) -> bytes:
    """把小型 JSON 值稳定序列化为字节串，便于做 hashing。"""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def build_principal_key(*, issuer: str, subject: str) -> str:
    """构造可读的复合身份键。"""

    return f"{issuer}#{subject}"


def build_principal_id(*, issuer: str, subject: str) -> str:
    """对 issuer/sub 组合做结构化 hashing，避免分隔符歧义。"""

    payload = canonical_json_bytes(["oidc-principal-v1", issuer, subject])
    return hashlib.sha256(payload).hexdigest()


class HumanPrincipal(BaseModel):
    """用于 API 授权与审计的规范化真人 principal。"""

    model_config = ConfigDict(extra="forbid")

    issuer: str
    subject: str = Field(repr=False, exclude=True)
    principal_key: str = Field(repr=False, exclude=True)
    principal_id: str
    audiences: tuple[str, ...]
    azp: str | None = None
    username: str | None = None
    name: str | None = None
    email: str | None = None
    email_verified: bool | None = None
    tenant_id: str | None = None
    allowed_aics: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    raw_claims: dict[str, Any] = Field(default_factory=dict, repr=False, exclude=True)

    def has_role(self, *required_roles: str) -> bool:
        role_set = set(self.roles)
        return all(role in role_set for role in required_roles)

    def has_scope(self, *required_scopes: str) -> bool:
        scope_set = set(self.scopes)
        return all(scope in scope_set for scope in required_scopes)
