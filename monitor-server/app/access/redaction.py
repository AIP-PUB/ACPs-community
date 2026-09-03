"""app/access/redaction.py — Header 白名单脱敏（纯函数，C-ACCESS-WRITE-2）。

脱敏发生在写 CH 之前的唯一落点。request/response headers 各调一次，
结果分别写 request_headers / response_headers 列。

不修改传入字典，返回新 dict 与剔除计数（用于 amp_access_writer_redacted_headers_total 指标）。
"""

from __future__ import annotations

from typing import Final

# 永不入库的敏感头（无论是否在白名单，硬拒），小写比较。
SENSITIVE_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "x-api-key",
        "x-auth-token",
        "www-authenticate",
    }
)


def redact_headers(
    headers: dict[str, str] | None,
    allowlist: frozenset[str],
) -> tuple[dict[str, str], int]:
    """脱敏单组 headers，返回 (保留的白名单头, 被剔除条数)。

    规则（顺序优先）：
      1. headers 为空 → ({}, 0)
      2. key 小写化后在 SENSITIVE_HEADERS → 剔除（即使在 allowlist 中）
      3. key 小写化后不在 allowlist → 剔除
      4. 其余保留（保留原始大小写 key）

    剔除条数用于 amp_access_writer_redacted_headers_total 指标。
    """
    if not headers:
        return {}, 0

    kept: dict[str, str] = {}
    dropped = 0
    for key, value in headers.items():
        lower_key = key.lower()
        if lower_key in SENSITIVE_HEADERS or lower_key not in allowlist:
            dropped += 1
        else:
            kept[key] = value

    return kept, dropped


def parse_allowlist(raw: str) -> frozenset[str]:
    """将逗号分隔的白名单字符串解析为 frozenset（小写、去空白）。

    "content-type,x-request-id" → frozenset({"content-type", "x-request-id"})

    配置 access_redacted_header_allowlist 在 Writer 构造时解析一次，避免重复解析。
    """
    if not raw:
        return frozenset()
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())
