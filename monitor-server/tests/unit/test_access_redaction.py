"""tests/unit/test_access_redaction.py — Header 脱敏纯函数测试。

TDD B-2：先写测试（红）→ 实现 redaction.py（绿）。
"""

from __future__ import annotations


class TestRedactHeaders:
    """redact_headers 函数行为测试。"""

    def _redact(self, headers: dict | None, allowlist_str: str = "") -> tuple:
        from app.access.redaction import parse_allowlist, redact_headers

        al = parse_allowlist(allowlist_str)
        return redact_headers(headers, al)

    def test_none_headers_returns_empty(self) -> None:
        result, count = self._redact(None)
        assert result == {}
        assert count == 0

    def test_empty_headers_returns_empty(self) -> None:
        result, count = self._redact({})
        assert result == {}
        assert count == 0

    def test_sensitive_header_always_stripped(self) -> None:
        """Authorization 无论是否在白名单，都必须剔除。"""
        result, count = self._redact(
            {"Authorization": "Bearer tok", "Content-Type": "application/json"},
            "authorization,content-type",
        )
        assert "Authorization" not in result
        assert count >= 1

    def test_cookie_stripped(self) -> None:
        result, _ = self._redact({"cookie": "sid=abc"}, "cookie")
        assert "cookie" not in result

    def test_set_cookie_stripped(self) -> None:
        result, _ = self._redact({"Set-Cookie": "sid=abc; HttpOnly"}, "set-cookie")
        assert "Set-Cookie" not in result

    def test_x_api_key_stripped(self) -> None:
        result, _ = self._redact({"X-Api-Key": "secret"}, "x-api-key")
        assert "X-Api-Key" not in result

    def test_x_auth_token_stripped(self) -> None:
        result, _ = self._redact({"X-Auth-Token": "tok"}, "x-auth-token")
        assert "X-Auth-Token" not in result

    def test_proxy_authorization_stripped(self) -> None:
        result, _ = self._redact({"Proxy-Authorization": "Basic abc"}, "proxy-authorization")
        assert "Proxy-Authorization" not in result

    def test_www_authenticate_stripped(self) -> None:
        result, _ = self._redact({"WWW-Authenticate": "Basic"}, "www-authenticate")
        assert "WWW-Authenticate" not in result

    def test_not_in_allowlist_stripped(self) -> None:
        """非敏感但不在白名单中的头也要剔除。"""
        result, count = self._redact(
            {"Content-Type": "application/json", "X-Custom-Header": "foo"},
            "content-type",
        )
        assert "Content-Type" in result
        assert "X-Custom-Header" not in result
        assert count == 1

    def test_allowlist_header_preserved(self) -> None:
        """白名单内的非敏感头被保留（原始大小写 key 保留）。"""
        result, count = self._redact(
            {"Content-Type": "application/json", "X-Request-Id": "req-1"},
            "content-type,x-request-id",
        )
        assert "Content-Type" in result
        assert "X-Request-Id" in result
        assert count == 0

    def test_redacted_count_accurate(self) -> None:
        """剔除计数等于实际被移除的头数。"""
        headers = {
            "Authorization": "Bearer tok",
            "Content-Type": "application/json",
            "X-Custom": "foo",
            "X-Request-Id": "r1",
        }
        _, count = self._redact(headers, "content-type,x-request-id")
        # Authorization (sensitive) + X-Custom (not in allowlist) = 2
        assert count == 2

    def test_case_insensitive_comparison_for_sensitive(self) -> None:
        """敏感头名比较不区分大小写（AUTHORIZATION 也要剔除）。"""
        result, count = self._redact({"AUTHORIZATION": "Bearer tok"}, "authorization")
        assert "AUTHORIZATION" not in result
        assert count >= 1

    def test_original_case_preserved_in_output(self) -> None:
        """输出 key 保留原始大小写（不做 lowercase 转换）。"""
        result, _ = self._redact({"Content-Type": "application/json"}, "content-type")
        assert "Content-Type" in result

    def test_returns_new_dict_not_mutate(self) -> None:
        """redact_headers 不修改原始字典。"""
        headers = {"Content-Type": "application/json", "Authorization": "tok"}
        original_copy = dict(headers)
        from app.access.redaction import parse_allowlist, redact_headers

        redact_headers(headers, parse_allowlist("content-type"))
        assert headers == original_copy


class TestParseAllowlist:
    """parse_allowlist 辅助函数测试。"""

    def _parse(self, raw: str) -> frozenset:
        from app.access.redaction import parse_allowlist

        return parse_allowlist(raw)

    def test_empty_string_returns_empty(self) -> None:
        result = self._parse("")
        assert result == frozenset()

    def test_single_header(self) -> None:
        result = self._parse("content-type")
        assert "content-type" in result

    def test_multiple_headers(self) -> None:
        result = self._parse("content-type,x-request-id,accept")
        assert "content-type" in result
        assert "x-request-id" in result
        assert "accept" in result

    def test_whitespace_stripped(self) -> None:
        result = self._parse("content-type , x-request-id , accept ")
        assert "content-type" in result
        assert "x-request-id" in result

    def test_always_lowercase(self) -> None:
        result = self._parse("Content-Type,X-Request-Id")
        assert "content-type" in result
        assert "x-request-id" in result

    def test_returns_frozenset(self) -> None:
        result = self._parse("content-type")
        assert isinstance(result, frozenset)

    def test_deduplicates(self) -> None:
        result = self._parse("content-type,content-type")
        assert len(result) == 1
