"""针对 utils/acs.py 和 utils/aic.py 的单元测试。

acs.py 覆盖：check_url_format（合法、无效、scheme 不匹配）、is_valid_json、
_collect_mutual_tls_scheme_names、_validate_endpoint_security（通过/拒绝）、
validate（None/空/有效/schema 失败/端点失败）。

aic.py 覆盖：_base36_encode/_base36_decode、_crc16_ccitt_false_with_salt、
calculate_aic_checksum、validate_aic（合法/各种非法输入）、
generate_aic/generate_ontology_aic、is_ontology_aic/is_entity_aic、
get_ontology_aic_from_entity、generate_entity_aic_from_ontology、
get_instance_serial、get_derived_entity_like_prefix。
"""

from __future__ import annotations

from typing import Any, cast

import pytest

import app.utils.acs as acs_mod
import app.utils.aic as aic_mod
from app.agent.exception import AgentError

pytestmark = pytest.mark.unit


# ===========================================================================
# 针对 utils/acs.py 的测试
# ===========================================================================


class TestCheckUrlFormat:
    def test_valid_http_url_no_transport(self) -> None:
        assert acs_mod.check_url_format("http://example.com/api") is True

    def test_valid_https_rest_transport(self) -> None:
        assert acs_mod.check_url_format("https://example.com/api", "REST") is True

    def test_invalid_url_no_scheme(self) -> None:
        assert acs_mod.check_url_format("example.com/api") is False

    def test_wrong_scheme_for_amqp(self) -> None:
        # AMQP 只允许 amqp/amqps，http 不应通过
        assert acs_mod.check_url_format("http://broker.com", "AMQP") is False

    def test_valid_amqp_scheme(self) -> None:
        assert acs_mod.check_url_format("amqps://broker.com", "AMQP") is True

    def test_unknown_transport_any_scheme_allowed(self) -> None:
        assert acs_mod.check_url_format("custom://service.com", "CUSTOM_TRANSPORT") is True

    def test_empty_url(self) -> None:
        assert acs_mod.check_url_format("") is False


class TestIsValidJson:
    def test_valid_json_object(self) -> None:
        assert acs_mod.is_valid_json('{"key": "value"}') is True

    def test_valid_json_array(self) -> None:
        assert acs_mod.is_valid_json("[1, 2, 3]") is True

    def test_invalid_json(self) -> None:
        assert acs_mod.is_valid_json("{not json}") is False

    def test_empty_string(self) -> None:
        assert acs_mod.is_valid_json("") is False


class TestCollectMutualTlsSchemeNames:
    def test_returns_mtls_scheme_names(self) -> None:
        instance = {
            "securitySchemes": {
                "agentMtls": {"type": "mutualTLS"},
                "apiKey": {"type": "apiKey"},
            }
        }
        result = acs_mod._collect_mutual_tls_scheme_names(instance)
        assert "agentMtls" in result
        assert "apiKey" not in result

    def test_empty_security_schemes(self) -> None:
        result = acs_mod._collect_mutual_tls_scheme_names({})
        assert result == set()

    def test_non_dict_security_schemes(self) -> None:
        result = acs_mod._collect_mutual_tls_scheme_names({"securitySchemes": "invalid"})
        assert result == set()


class TestValidateEndpointSecurity:
    def test_passes_with_mtls_security(self) -> None:
        endpoint = {"security": [{"agentMtls": []}], "url": "https://example.com"}
        # 不应抛出
        acs_mod._validate_endpoint_security(endpoint, {"agentMtls"}, "{}")

    def test_raises_without_mtls(self) -> None:
        endpoint = {"security": [{"apiKey": []}], "url": "https://example.com"}
        with pytest.raises(AgentError):
            acs_mod._validate_endpoint_security(endpoint, {"agentMtls"}, "{}")

    def test_raises_with_empty_security(self) -> None:
        endpoint = {"security": [], "url": "https://example.com"}
        with pytest.raises(AgentError):
            acs_mod._validate_endpoint_security(endpoint, {"agentMtls"}, "{}")


class TestValidateAcs:
    def test_none_raises(self) -> None:
        with pytest.raises(AgentError):
            acs_mod.validate(cast("Any", None))

    def test_empty_dict_raises(self) -> None:
        with pytest.raises(AgentError):
            acs_mod.validate({})

    def test_invalid_json_string_raises(self) -> None:
        with pytest.raises(AgentError):
            acs_mod.validate("not json")

    def test_valid_with_empty_endpoints_and_no_webapp_url(self) -> None:
        import json
        from unittest.mock import patch

        acs: dict[str, list[str]] = {"endPoints": []}
        with patch("app.utils.acs._validate_acs_schema"):
            acs_mod.validate(json.dumps(acs))

    def test_valid_with_webapp_url_only(self) -> None:
        import json
        from unittest.mock import patch

        acs = {"webAppUrl": "https://example.com/app"}
        # 通过 mock schema 校验，避免结构校验受到 JSON schema 细节影响
        with patch("app.utils.acs._validate_acs_schema"):
            acs_mod.validate(json.dumps(acs))

    def test_valid_with_endpoints_and_mtls(self) -> None:
        import json
        from unittest.mock import patch

        from tests.support.aic import generate_ontology_aic

        acs = {
            "aic": generate_ontology_aic(),
            "securitySchemes": {"agentMtls": {"type": "mutualTLS"}},
            "endPoints": [
                {
                    "url": "https://service.example.com/api",
                    "transport": "REST",
                    "security": [{"agentMtls": []}],
                }
            ],
        }
        with patch("app.utils.acs._validate_acs_schema"):
            acs_mod.validate(json.dumps(acs))


# ===========================================================================
# 针对 utils/aic.py 的测试
# ===========================================================================


class TestBase36Encode:
    def test_zero_encodes_to_zeros(self) -> None:
        assert aic_mod._base36_encode(0, 4) == "0000"

    def test_known_value(self) -> None:
        assert aic_mod._base36_encode(35, 1) == "Z"

    def test_value_exceeds_length_truncates(self) -> None:
        # 确保不超过指定长度
        result = aic_mod._base36_encode(9999999, 4)
        assert len(result) == 4

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            aic_mod._base36_encode(-1, 4)

    def test_zero_length_raises(self) -> None:
        with pytest.raises(ValueError):
            aic_mod._base36_encode(10, 0)


class TestBase36Decode:
    def test_empty_string_returns_zero(self) -> None:
        assert aic_mod._base36_decode("") == 0

    def test_single_digit(self) -> None:
        assert aic_mod._base36_decode("0") == 0
        assert aic_mod._base36_decode("Z") == 35

    def test_roundtrip(self) -> None:
        for val in [0, 1, 35, 1000, 65535]:
            encoded = aic_mod._base36_encode(val, 6)
            decoded = aic_mod._base36_decode(encoded)
            assert decoded == val

    def test_invalid_char_raises(self) -> None:
        with pytest.raises(ValueError):
            aic_mod._base36_decode("!")


class TestCrc16CcittFalse:
    def test_empty_data_with_empty_salt(self) -> None:
        result = aic_mod._crc16_ccitt_false_with_salt(b"", b"")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFF

    def test_different_data_different_crc(self) -> None:
        crc1 = aic_mod._crc16_ccitt_false_with_salt(b"hello", b"")
        crc2 = aic_mod._crc16_ccitt_false_with_salt(b"world", b"")
        assert crc1 != crc2

    def test_different_salt_different_crc(self) -> None:
        crc1 = aic_mod._crc16_ccitt_false_with_salt(b"data", b"\x00")
        crc2 = aic_mod._crc16_ccitt_false_with_salt(b"data", b"\xff")
        assert crc1 != crc2


class TestCalculateAicChecksum:
    def test_returns_4_char_base36(self) -> None:
        result = aic_mod.calculate_aic_checksum("1.2.156.3088.1.0001.00001.ABCDEF.123456")
        assert len(result) == 4
        assert result == result.upper()

    def test_deterministic(self) -> None:
        body = "1.2.156.3088.1.0001.00001.ABCDEF.123456"
        assert aic_mod.calculate_aic_checksum(body) == aic_mod.calculate_aic_checksum(body)


class TestGenerateAic:
    def test_generates_valid_aic(self) -> None:
        aic = aic_mod.generate_aic(manager_code="1", provider_code="00001")
        assert aic_mod.validate_aic(aic)

    def test_generated_aic_is_entity(self) -> None:
        aic = aic_mod.generate_aic(manager_code="1", provider_code="00001")
        assert aic_mod.is_entity_aic(aic)

    def test_generated_aic_not_ontology(self) -> None:
        aic = aic_mod.generate_aic(manager_code="1", provider_code="00001")
        assert not aic_mod.is_ontology_aic(aic)


class TestGenerateOntologyAic:
    def test_generates_valid_ontology_aic(self) -> None:
        aic = aic_mod.generate_ontology_aic(manager_code="1", provider_code="00001")
        assert aic_mod.validate_aic(aic)
        assert aic_mod.is_ontology_aic(aic)

    def test_ontology_aic_instance_serial_all_zeros(self) -> None:
        aic = aic_mod.generate_ontology_aic(manager_code="1", provider_code="00001")
        parts = aic_mod._split_aic(aic)
        assert set(parts[8]) == {"0"}
        assert parts[8] == "0"


class TestValidateAic:
    def _make_valid_entity_aic(self) -> str:
        return aic_mod.generate_aic(manager_code="1", provider_code="00001")

    def test_valid_entity_aic(self) -> None:
        assert aic_mod.validate_aic(self._make_valid_entity_aic())

    def test_wrong_number_of_parts(self) -> None:
        assert aic_mod.validate_aic("1.2.156.3088.1.0001.00001.ABCDEF") is False

    def test_empty_string(self) -> None:
        assert aic_mod.validate_aic("") is False

    def test_invalid_prefix(self) -> None:
        aic = aic_mod.generate_aic(manager_code="1", provider_code="00001")
        parts = aic_mod._split_aic(aic)
        parts[0] = "9"
        assert aic_mod.validate_aic(".".join(parts)) is False

    def test_wrong_crc(self) -> None:
        aic = aic_mod.generate_aic(manager_code="1", provider_code="00001")
        # 破坏 CRC（最后一段）
        corrupted = aic[:-1] + ("X" if aic[-1] != "X" else "Y")
        assert aic_mod.validate_aic(corrupted) is False

    def test_non_base36_segment(self) -> None:
        aic = aic_mod.generate_aic(manager_code="1", provider_code="00001")
        parts = aic.split(".")
        parts[4] = "!"  # 非法字符
        assert aic_mod.validate_aic(".".join(parts)) is False


class TestGetInstanceSerial:
    def test_extracts_instance_serial(self) -> None:
        aic = aic_mod.generate_aic(manager_code="1", provider_code="00001")
        serial = aic_mod.get_instance_serial(aic)
        assert serial is not None
        assert len(serial) > 0

    def test_invalid_aic_returns_none(self) -> None:
        assert aic_mod.get_instance_serial("not.an.aic") is None


class TestIsOntologyAndIsEntity:
    def test_entity_aic_is_entity(self) -> None:
        aic = aic_mod.generate_aic(manager_code="1", provider_code="00001")
        assert aic_mod.is_entity_aic(aic) is True
        assert aic_mod.is_ontology_aic(aic) is False

    def test_ontology_aic_is_ontology(self) -> None:
        aic = aic_mod.generate_ontology_aic(manager_code="1", provider_code="00001")
        assert aic_mod.is_ontology_aic(aic) is True
        assert aic_mod.is_entity_aic(aic) is False

    def test_invalid_aic_is_not_ontology(self) -> None:
        assert aic_mod.is_ontology_aic("invalid") is False


class TestGetOntologyAicFromEntity:
    def test_derived_ontology_is_valid(self) -> None:
        entity_aic = aic_mod.generate_aic(manager_code="1", provider_code="00001")
        ontology = aic_mod.get_ontology_aic_from_entity(entity_aic)
        assert ontology is not None
        assert aic_mod.is_ontology_aic(ontology)

    def test_invalid_aic_returns_none(self) -> None:
        assert aic_mod.get_ontology_aic_from_entity("invalid") is None

    def test_prefix_matches_entity(self) -> None:
        entity_aic = aic_mod.generate_aic(manager_code="1", provider_code="00001")
        ontology = aic_mod.get_ontology_aic_from_entity(entity_aic)
        assert ontology is not None
        # 前 8 段应与 entity AIC 相同
        e_parts = entity_aic.split(".")
        o_parts = ontology.split(".")
        assert e_parts[:8] == o_parts[:8]
        assert o_parts[8] == "0"


class TestGenerateEntityAicFromOntology:
    def test_generates_valid_entity(self) -> None:
        ontology = aic_mod.generate_ontology_aic(manager_code="1", provider_code="00001")
        entity = aic_mod.generate_entity_aic_from_ontology(ontology)
        assert entity is not None
        assert aic_mod.is_entity_aic(entity)
        assert aic_mod.validate_aic(entity)

    def test_non_ontology_returns_none(self) -> None:
        entity_aic = aic_mod.generate_aic(manager_code="1", provider_code="00001")
        assert aic_mod.generate_entity_aic_from_ontology(entity_aic) is None

    def test_invalid_aic_returns_none(self) -> None:
        assert aic_mod.generate_entity_aic_from_ontology("invalid") is None


class TestGetDerivedEntityLikePrefix:
    def test_valid_ontology_returns_prefix(self) -> None:
        ontology = aic_mod.generate_ontology_aic(manager_code="1", provider_code="00001")
        prefix = aic_mod.get_derived_entity_like_prefix(ontology)
        assert prefix is not None
        assert prefix.endswith(".")
        assert len(prefix.split(".")) == 9  # 8 segments + trailing dot = 9 parts

    def test_invalid_aic_returns_none(self) -> None:
        assert aic_mod.get_derived_entity_like_prefix("invalid") is None
