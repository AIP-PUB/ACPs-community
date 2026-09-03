from typing import Any, cast
from unittest.mock import patch

import pytest

from app.utils import aic


@pytest.mark.unit
class TestAICSpecV02:
    def test_crc16_example_from_spec(self) -> None:
        body_1_9 = "1.2.156.1234.1.1.34C2.478BDF.3GF546"
        with patch("app.utils.aic.AIC_CRC_SALT", ""):
            assert aic.calculate_aic_checksum(body_1_9) == "0H9T"

    def test_validate_example_from_spec(self) -> None:
        full = "1.2.156.1234.1.1.34C2.478BDF.3GF546.0H9T"
        with patch("app.utils.aic.AIC_CRC_SALT", ""):
            assert aic.validate_aic(full, expected_prefix="1.2.156.1234") is True
            # 大小写/空白容忍
            assert aic.validate_aic("  " + full.lower() + "\n", expected_prefix="1.2.156.1234") is True

    def test_generate_entity_aic(self) -> None:
        code = aic.generate_aic(manager_code="1", provider_code="00001")
        assert aic.validate_aic(code) is True
        assert aic.is_entity_aic(code) is True
        assert aic.is_ontology_aic(code) is False
        assert aic.get_instance_serial(code) is not None

    def test_generate_uses_explicit_protocol_version(self) -> None:
        code = aic.generate_aic("A", manager_code="1", provider_code="00001")
        assert code.split(".")[4] == "A"

    def test_generate_ontology_aic(self) -> None:
        code = aic.generate_ontology_aic(manager_code="1", provider_code="00001")
        assert aic.validate_aic(code) is True
        assert aic.is_ontology_aic(code) is True
        assert aic.is_entity_aic(code) is False
        instance = aic.get_instance_serial(code)
        assert instance == "0"

    def test_generate_aic_uses_explicit_level_codes(self) -> None:
        code = aic.generate_aic(manager_code="9Z", provider_code="00001")
        parts = code.split(".")
        assert parts[5] == "9Z"
        assert parts[6] == "00001"

    def test_generate_aic_requires_level_codes(self) -> None:
        generate_aic = cast("Any", aic.generate_aic)
        generate_ontology_aic = cast("Any", aic.generate_ontology_aic)
        with pytest.raises(TypeError):
            generate_aic()
        with pytest.raises(TypeError):
            generate_ontology_aic()

    def test_get_ontology_from_entity(self) -> None:
        entity = aic.generate_aic(manager_code="1", provider_code="00001")
        onto = aic.get_ontology_aic_from_entity(entity)
        assert onto is not None
        assert aic.validate_aic(onto) is True
        assert aic.is_ontology_aic(onto) is True
        # 1~8 级保持一致
        e_parts = entity.split(".")
        o_parts = onto.split(".")
        assert e_parts[:8] == o_parts[:8]
        assert o_parts[8] == "0"

    def test_generate_entity_from_ontology(self) -> None:
        onto = aic.generate_ontology_aic(manager_code="1", provider_code="00001")
        entity = aic.generate_entity_aic_from_ontology(onto)
        assert entity is not None
        assert aic.validate_aic(entity) is True
        assert aic.is_entity_aic(entity) is True
        # 1~8 级保持一致，不读取 Settings
        o_parts = onto.split(".")
        e_parts = entity.split(".")
        assert o_parts[:8] == e_parts[:8]
        assert e_parts[5] == "1"
        assert e_parts[6] == "00001"

    def test_derived_entity_like_prefix(self) -> None:
        onto = aic.generate_ontology_aic(manager_code="1", provider_code="00001")
        prefix = aic.get_derived_entity_like_prefix(onto)
        assert prefix is not None
        entity = aic.generate_entity_aic_from_ontology(onto)
        assert entity is not None
        assert entity.startswith(prefix)


@pytest.mark.unit
class TestAICInvalidInputs:
    def test_invalid_prefix_or_segments(self) -> None:
        assert aic.validate_aic("") is False
        assert aic.validate_aic("1.2.156.1234") is False
        # 错误的 checksum
        assert aic.validate_aic("1.2.156.1234.1.1.AAAAAA.1.1.1.0000") is False

    def test_generate_invalid_custom_codes(self) -> None:
        with pytest.raises(ValueError):
            aic.generate_aic(protocol_version="", manager_code="1", provider_code="00001")
        with pytest.raises(ValueError):
            aic.generate_aic(protocol_version="12", manager_code="1", provider_code="00001")
        with pytest.raises(ValueError):
            aic.generate_aic(protocol_version="0", manager_code="1", provider_code="00001")
        with pytest.raises(ValueError):
            aic.generate_aic(manager_code="00*1", provider_code="00001")
        with pytest.raises(ValueError):
            aic.generate_aic(manager_code="1", provider_code="")


@pytest.mark.unit
class TestNormalizeAicLevelCode:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1", "1"),
            ("A", "A"),
            ("34C2", "34C2"),
            ("0001", "0001"),
            ("0ABC", "0ABC"),
            ("ZZZZZZ", "ZZZZZZ"),
            ("34c2", "34C2"),
            ("  0abc  ", "0ABC"),
        ],
    )
    def test_accepts_spec_legal_codes(self, raw: str, expected: str) -> None:
        assert aic.normalize_aic_level_code(raw) == expected

    def test_does_not_pad_or_strip_leading_zeros(self) -> None:
        assert aic.normalize_aic_level_code("1") != aic.normalize_aic_level_code("0001")
        assert aic.normalize_aic_level_code("0001") == "0001"

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "0", "00", "000000", "1234567", "34-C2", "34_C2", "1.2"],
    )
    def test_rejects_illegal_codes(self, raw: str) -> None:
        with pytest.raises(ValueError):
            aic.normalize_aic_level_code(raw)


@pytest.mark.unit
class TestNormalizeAicProtocolVersion:
    @pytest.mark.parametrize(("raw", "expected"), [("1", "1"), ("A", "A"), ("z", "Z"), ("  b  ", "B")])
    def test_accepts_spec_legal_versions(self, raw: str, expected: str) -> None:
        assert aic.normalize_aic_protocol_version(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "0", "12", "10", "-", "1.2"])
    def test_rejects_illegal_versions(self, raw: str) -> None:
        with pytest.raises(ValueError):
            aic.normalize_aic_protocol_version(raw)

    def test_generate_reads_settings_protocol_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = type(
            "SettingsStub",
            (),
            {
                "aic_protocol_version": "Z",
                "aic_ontology_serial_len": 6,
                "aic_instance_serial_len": 6,
                "aic_crc_salt": "0x1234",
            },
        )()
        monkeypatch.setattr("app.core.config.settings", stub)
        code = aic.generate_aic(manager_code="1", provider_code="00001")
        assert code.split(".")[4] == "Z"
        assert aic.validate_aic(code) is True


@pytest.mark.unit
class TestGenerateAicProviderCode:
    def test_generates_six_char_nonzero_base36(self) -> None:
        codes = {aic.generate_aic_provider_code() for _ in range(20)}
        assert len(codes) >= 2
        for code in codes:
            assert aic.normalize_aic_level_code(code) == code
            assert len(code) == 6
            assert code[0] != "0"
            assert set(code) != {"0"}

    def test_first_char_skips_zero_even_when_choice_picks_alphabet_head(self) -> None:
        with patch("app.utils.aic.secrets.choice", lambda seq: seq[0]):
            code = aic.generate_aic_provider_code()
        assert code == "1" + "0" * 5
        assert code[0] != "0"


@pytest.mark.unit
class TestAicSerialLen:
    def test_validate_accepts_one_to_nine(self) -> None:
        assert aic.validate_aic_serial_len(1) == 1
        assert aic.validate_aic_serial_len(9) == 9

    @pytest.mark.parametrize("raw", [0, 10, -1, True, "6", 6.0, None])
    def test_validate_rejects_illegal(self, raw: object) -> None:
        with pytest.raises(ValueError):
            aic.validate_aic_serial_len(raw)

    def test_generate_entity_honors_explicit_serial_len(self) -> None:
        code = aic.generate_aic(
            manager_code="1",
            provider_code="00001",
            ontology_serial_len=9,
            instance_serial_len=1,
        )
        parts = code.split(".")
        assert len(parts[7]) == 9
        assert len(parts[8]) == 1
        assert aic.validate_aic(code) is True
        assert aic.is_entity_aic(code) is True

    def test_generate_ontology_uses_single_zero_regardless_of_serial_len(self) -> None:
        code = aic.generate_ontology_aic(
            manager_code="1",
            provider_code="00001",
            ontology_serial_len=8,
            instance_serial_len=9,
        )
        parts = code.split(".")
        assert len(parts[7]) == 8
        assert parts[8] == "0"
        assert aic.is_ontology_aic(code) is True

    def test_derived_entity_uses_configured_instance_len(self) -> None:
        onto = aic.generate_ontology_aic(
            manager_code="1",
            provider_code="00001",
            ontology_serial_len=9,
        )
        entity = aic.generate_entity_aic_from_ontology(onto, instance_serial_len=9)
        assert entity is not None
        assert entity.split(".")[:8] == onto.split(".")[:8]
        assert len(entity.split(".")[7]) == 9
        assert len(entity.split(".")[8]) == 9
        assert aic.is_entity_aic(entity) is True

    def test_is_ontology_still_accepts_legacy_padded_zeros(self) -> None:
        onto = aic.generate_ontology_aic(manager_code="1", provider_code="00001")
        parts = onto.split(".")
        parts[8] = "000000"
        parts[9] = aic.calculate_aic_checksum(".".join(parts[:9]))
        padded = ".".join(parts)
        assert aic.is_ontology_aic(padded) is True
        assert aic.get_instance_serial(onto) == "0"

    def test_generate_rejects_illegal_serial_len(self) -> None:
        with pytest.raises(ValueError):
            aic.generate_aic(manager_code="1", provider_code="00001", ontology_serial_len=10)
        with pytest.raises(ValueError):
            aic.generate_ontology_aic(manager_code="1", provider_code="00001", instance_serial_len=0)

    def test_generate_reads_settings_serial_len(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = type(
            "SettingsStub",
            (),
            {
                "aic_protocol_version": "1",
                "aic_ontology_serial_len": 9,
                "aic_instance_serial_len": 4,
                "aic_crc_salt": "0x1234",
            },
        )()
        monkeypatch.setattr("app.core.config.settings", stub)
        code = aic.generate_aic(manager_code="1", provider_code="00001")
        parts = code.split(".")
        assert len(parts[7]) == 9
        assert len(parts[8]) == 4
        assert aic.validate_aic(code) is True
