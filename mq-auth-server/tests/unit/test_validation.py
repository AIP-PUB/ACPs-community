"""validate_aic_format / validate_group_id_format / extract_common_name 的单元测试。

这些辅助函数是授权判断的基础，需要完整覆盖边界条件。
"""

from __future__ import annotations

from typing import Any

from app.core.validation import (
    extract_common_name,
    validate_aic_format,
    validate_group_id_format,
)

# 合法的 AIC 样本（来自 tests/conftest.py）
VALID_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"


class TestValidateAicFormat:
    """validate_aic_format — 正常路径与各类边界异常。"""

    def test_valid_aic_returns_true(self) -> None:
        assert validate_aic_format(VALID_AIC) is True

    def test_empty_string_returns_false(self) -> None:
        assert validate_aic_format("") is False

    def test_nine_parts_returns_false(self) -> None:
        # 少一段（只有 9 段）
        assert validate_aic_format("1.2.156.3088.1.1.34C2.478BDF.3GF546") is False

    def test_eleven_parts_returns_false(self) -> None:
        # 多一段（11 段）
        assert validate_aic_format("1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4.EXTRA") is False

    def test_wrong_prefix_second_part_returns_false(self) -> None:
        # 第二段应为 "2"，改为 "3"
        assert validate_aic_format("1.3.156.3088.1.1.34C2.478BDF.3GF546.0JU4") is False

    def test_wrong_prefix_third_part_returns_false(self) -> None:
        # 第三段应为 "156"，改为 "157"
        assert validate_aic_format("1.2.157.3088.1.1.34C2.478BDF.3GF546.0JU4") is False

    def test_wrong_prefix_fourth_part_returns_false(self) -> None:
        # 第四段应为 "3088"
        assert validate_aic_format("1.2.156.3089.1.1.34C2.478BDF.3GF546.0JU4") is False

    def test_non_digit_in_prefix_segment_returns_false(self) -> None:
        # 前四段必须为纯数字
        assert validate_aic_format("1.2.15a.3088.1.1.34C2.478BDF.3GF546.0JU4") is False

    def test_lowercase_in_base36_segment_returns_false(self) -> None:
        # BASE36 段只允许大写字母 + 数字
        assert validate_aic_format("1.2.156.3088.1.1.34c2.478BDF.3GF546.0JU4") is False

    def test_segment5_too_long_returns_false(self) -> None:
        # 段 5（index=4）约束长度为 (1,1)，"12" 超长
        assert validate_aic_format("1.2.156.3088.12.1.34C2.478BDF.3GF546.0JU4") is False

    def test_segment6_too_long_returns_false(self) -> None:
        # 段 6（index=5）约束 (1,6)，7 字符超长
        assert validate_aic_format("1.2.156.3088.1.1234567.34C2.478BDF.3GF546.0JU4") is False

    def test_segment10_too_short_returns_false(self) -> None:
        # 段 10（index=9）约束 (4,4)，"0JU" 只有 3 个字符
        assert validate_aic_format("1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU") is False

    def test_segment10_too_long_returns_false(self) -> None:
        # 段 10 必须恰好 4 字符，"0JU45" 过长
        assert validate_aic_format("1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU45") is False

    def test_custom_prefix_matching_returns_true(self) -> None:
        # 自定义只检查前两段
        assert validate_aic_format(VALID_AIC, expected_prefix=("1", "2")) is True

    def test_custom_prefix_mismatch_returns_false(self) -> None:
        assert validate_aic_format(VALID_AIC, expected_prefix=("1", "3")) is False

    def test_empty_expected_prefix_skips_prefix_check(self) -> None:
        # 空元组跳过前缀校验
        assert validate_aic_format(VALID_AIC, expected_prefix=()) is True

    def test_empty_prefix_still_checks_segment_format(self) -> None:
        # 即使跳过前缀，小写字母仍然不合法
        assert (
            validate_aic_format(
                "1.2.156.3088.1.1.34c2.478BDF.3GF546.0JU4",
                expected_prefix=(),
            )
            is False
        )

    def test_multiple_valid_aics(self) -> None:
        # 验证 conftest 中的所有 AIC 样本均合法
        valid_aics = [
            "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4",
            "1.2.156.3088.1.1.89AB.123456.7LMNOP.1ABC",
            "1.2.156.3088.1.1.CDEF.654321.ZYXWVU.2DEF",
        ]
        for aic in valid_aics:
            assert validate_aic_format(aic) is True, f"应合法：{aic}"


class TestValidateGroupIdFormat:
    """validate_group_id_format — 正常路径与各类边界异常。"""

    def test_valid_group_id(self) -> None:
        assert validate_group_id_format("group-20260414-abc123") is True

    def test_alphanumeric_with_hyphens(self) -> None:
        assert validate_group_id_format("abc-123-XYZ") is True

    def test_single_character(self) -> None:
        assert validate_group_id_format("x") is True

    def test_empty_string_returns_false(self) -> None:
        assert validate_group_id_format("") is False

    def test_underscore_not_allowed(self) -> None:
        # 只允许 [a-zA-Z0-9-]，下划线不合法
        assert validate_group_id_format("group_id") is False

    def test_exclamation_not_allowed(self) -> None:
        assert validate_group_id_format("bad!group") is False

    def test_space_not_allowed(self) -> None:
        assert validate_group_id_format("bad group") is False

    def test_dot_not_allowed(self) -> None:
        assert validate_group_id_format("group.id") is False

    def test_exactly_128_bytes_returns_true(self) -> None:
        assert validate_group_id_format("a" * 128) is True

    def test_129_bytes_returns_false(self) -> None:
        assert validate_group_id_format("a" * 129) is False

    def test_pure_digits_valid(self) -> None:
        assert validate_group_id_format("12345") is True

    def test_pure_letters_valid(self) -> None:
        assert validate_group_id_format("groupABC") is True

    def test_leading_hyphen_valid(self) -> None:
        # 规范只约束字符集和长度，不禁止前导连字符
        assert validate_group_id_format("-leading") is True


class TestExtractCommonName:
    """extract_common_name — 从 ssl.getpeercert() 输出中提取 CN。"""

    def test_none_returns_none(self) -> None:
        assert extract_common_name(None) is None

    def test_empty_dict_returns_none(self) -> None:
        assert extract_common_name({}) is None

    def test_subject_not_tuple_returns_none(self) -> None:
        assert extract_common_name({"subject": "not-a-tuple"}) is None

    def test_rdns_not_tuple_returns_none(self) -> None:
        assert extract_common_name({"subject": ("not-a-tuple",)}) is None

    def test_valid_cert_returns_common_name(self) -> None:
        cert: dict[str, Any] = {"subject": ((("commonName", VALID_AIC),),)}
        assert extract_common_name(cert) == VALID_AIC

    def test_no_common_name_attribute_returns_none(self) -> None:
        cert: dict[str, Any] = {"subject": ((("organizationName", "ACPs"),),)}
        assert extract_common_name(cert) is None

    def test_multiple_attributes_finds_common_name(self) -> None:
        cert: dict[str, Any] = {
            "subject": (
                (("countryName", "CN"),),
                (("organizationName", "ACPs"),),
                (("commonName", VALID_AIC),),
            )
        }
        assert extract_common_name(cert) == VALID_AIC

    def test_common_name_value_not_string_returns_none(self) -> None:
        cert: dict[str, Any] = {"subject": ((("commonName", 12345),),)}
        assert extract_common_name(cert) is None

    def test_attribute_tuple_wrong_length_returns_none(self) -> None:
        # 属性元组长度不是 2
        cert: dict[str, Any] = {"subject": ((("commonName",),),)}
        assert extract_common_name(cert) is None
