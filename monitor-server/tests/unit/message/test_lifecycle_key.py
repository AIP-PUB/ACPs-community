"""单元测试：B-2 lifecycle_key.py — 键推导（纯函数）。"""

from __future__ import annotations

from app.message.lifecycle_key import (
    CID_PREFIX,
    MID_PREFIX,
    compute_lifecycle_key,
    is_synthetic,
    message_id_to_lifecycle_key,
)


class TestComputeLifecycleKey:
    """compute_lifecycle_key 纯函数，相同输入恒同输出（C-MESSAGE-MODEL-2）。"""

    def test_message_id_takes_priority(self) -> None:
        key = compute_lifecycle_key(
            message_id="m1",
            correlation_id="c1",
            correlation_id_stable_unique=True,
        )
        assert key == "mid:m1"

    def test_message_id_prefix(self) -> None:
        key = compute_lifecycle_key(
            message_id="abc",
            correlation_id=None,
            correlation_id_stable_unique=False,
        )
        assert key.startswith(MID_PREFIX)
        assert key == "mid:abc"

    def test_correlation_id_only_when_stable_unique_true(self) -> None:
        key = compute_lifecycle_key(
            message_id=None,
            correlation_id="c1",
            correlation_id_stable_unique=True,
        )
        assert key == "cid:c1"

    def test_correlation_id_not_used_when_stable_unique_false(self) -> None:
        key = compute_lifecycle_key(
            message_id=None,
            correlation_id="c1",
            correlation_id_stable_unique=False,
        )
        assert key == ""

    def test_both_none_returns_empty(self) -> None:
        key = compute_lifecycle_key(
            message_id=None,
            correlation_id=None,
            correlation_id_stable_unique=True,
        )
        assert key == ""

    def test_empty_message_id_falls_back_to_cid(self) -> None:
        key = compute_lifecycle_key(
            message_id="",
            correlation_id="c1",
            correlation_id_stable_unique=True,
        )
        assert key == "cid:c1"

    def test_empty_correlation_id_returns_empty_even_if_stable(self) -> None:
        key = compute_lifecycle_key(
            message_id=None,
            correlation_id="",
            correlation_id_stable_unique=True,
        )
        assert key == ""

    def test_same_input_same_output(self) -> None:
        key1 = compute_lifecycle_key(message_id="x", correlation_id=None, correlation_id_stable_unique=False)
        key2 = compute_lifecycle_key(message_id="x", correlation_id=None, correlation_id_stable_unique=False)
        assert key1 == key2

    def test_message_id_whitespace_not_treated_as_empty(self) -> None:
        key = compute_lifecycle_key(
            message_id="  ",
            correlation_id="c1",
            correlation_id_stable_unique=True,
        )
        assert key == "mid:  "


class TestIsSynthetic:
    def test_empty_key_not_synthetic(self) -> None:
        # 空串不计 synthetic 指标（直接不进 lifecycle 表，由 compactor SQL 过滤）
        assert is_synthetic("") is False

    def test_mid_prefix_not_synthetic(self) -> None:
        assert is_synthetic("mid:abc") is False

    def test_cid_prefix_is_synthetic(self) -> None:
        # cid: 前缀 = 合成键（基于 correlation_id，设计 §6.2）
        assert is_synthetic("cid:abc") is True

    def test_cid_prefix_full(self) -> None:
        assert is_synthetic("cid:correlation-123") is True

    def test_random_string_not_synthetic(self) -> None:
        assert is_synthetic("foo") is False


class TestMessageIdToLifecycleKey:
    def test_adds_mid_prefix(self) -> None:
        assert message_id_to_lifecycle_key("msg123") == "mid:msg123"

    def test_cid_prefix_constant(self) -> None:
        assert CID_PREFIX == "cid:"
