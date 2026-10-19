"""AIC 跨语言对照样例：格式校验 / 解析 / 本体实体判断 / Base36 / CRC 校验。

输出：test/fixtures/python/aic/*.json
运行：npm run fixtures:python（或单独 `uv run --project ../acps-sdk --python 3.12 python -I scripts/fixtures/gen_aic.py`）
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from _common import write_fixture  # noqa: E402

from acps_sdk.aic import (  # noqa: E402
    AICInfo,
    AICValidator,
    base36_decode,
    base36_encode,
    get_aic_segment,
    get_ontology_prefix_from_aic,
    is_entity_aic,
    is_ontology_aic,
    is_valid_aic_format,
    parse_aic,
    validate_aic_format,
)

VALID = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
ONTOLOGY = "1.2.156.3088.1.1.34C2.478BDF.000000.0SV9"
BODY = "1.2.156.3088.1.1.34C2.478BDF.3GF546"
ONTOLOGY_BODY = "1.2.156.3088.1.1.34C2.478BDF.000000"

# Python `\s` / str.isspace() 认的空白里，JS `\s` 不认 \x1c-\x1f、\x85；JS 认而 Python 不认的是 ﻿。
PY_ONLY_WHITESPACE = ["\x1c", "\x1d", "\x1e", "\x1f", "\x85"]
SHARED_WHITESPACE = ["\t", "\n", "\x0b", "\x0c", "\r", " ", "\xa0", " ", " ", " ", " ", " ", " ", " ", "　"]
JS_ONLY_WHITESPACE = ["﻿"]


def _info(info: AICInfo | None) -> dict[str, Any] | None:
    if info is None:
        return None
    return {
        "raw": info.raw,
        "normalized": info.normalized,
        "prefix": info.prefix,
        "version": info.version,
        "arsp": info.arsp,
        "vendor": info.vendor,
        "ontologySerial": info.ontology_serial,
        "instanceSerial": info.instance_serial,
        "checksum": info.checksum,
        "isOntology": info.is_ontology,
        "isEntity": info.is_entity,
        "body": info.body,
        "ontologyPrefix": info.ontology_prefix,
    }


def _format_inputs() -> list[str | None]:
    inputs: list[str | None] = [
        VALID,
        ONTOLOGY,
        VALID.lower(),
        f"  {VALID}  ",
        f"{VALID[:10]} {VALID[10:]}",
        f"\t{VALID}\n",
        None,
        "",
        "   ",
        BODY,  # 9 段
        f"{VALID}.X",  # 11 段
        "1.2..156.3088.1.1.34C2.478BDF.3GF546.0JU4",  # 空段
        f"{VALID}.",
        f".{VALID}",
        "1.2.156.3089.1.1.34C2.478BDF.3GF546.0JU4",  # 前缀不匹配
        "01.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4",
        "1.2.156.3088.11.1.34C2.478BDF.3GF546.0JU4",  # 版本多位
        "1.2.156.3088.Z.1.34C2.478BDF.3GF546.0JU4",
        "1.2.156.3088.1.1234567.34C2.478BDF.3GF546.0JU4",  # ARSP 7 位
        "1.2.156.3088.1.1.1234567.478BDF.3GF546.0JU4",  # Vendor 7 位
        "1.2.156.3088.1.1.34C2.1234567890.3GF546.0JU4",  # Ontology 10 位
        "1.2.156.3088.1.1.34C2.478BDF.1234567890.0JU4",  # Instance 10 位
        "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU",  # CRC 3 位
        "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU45",  # CRC 5 位
        "1.2.156.3088.1.1.34C_2.478BDF.3GF546.0JU4",
        "1.2.156.3088.1.1.34-2.478BDF.3GF546.0JU4",
        "1.2.156.3088.1.1.34É2.478BDF.3GF546.0JU4",
        "1.2.156.3088.1.1.34ß.478BDF.3GF546.0JU4",  # upper() → SS
        "1.2.156.3088.1.1.34ſ2.478BDF.3GF546.0JU4",  # upper() → S
        "1.2.156.3088.1.1.34ı2.478BDF.3GF546.0JU4",  # upper() → I
        "1.2.156.3088.1.1.34C2.478BDF.3GF546.０JU4",  # 全角数字
        "1.2.156.3088.1.1.34C2.478BDF.0.0SV9",
        "1.2.156.3088.1.1.34C2.478BDF.000000000.0SV9",
        "1.2.156.3088.1.1.34C2.478BDF.00A.0SV9",
        "9.9.9.9.1.1.A.B.C.ABCD",
        "A.B.C.D.1.1.A.B.C.ABCD",
        "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4\n",
    ]
    for ch in PY_ONLY_WHITESPACE + SHARED_WHITESPACE + JS_ONLY_WHITESPACE:
        inputs.append(f"{VALID[:12]}{ch}{VALID[12:]}")
    return inputs


PREFIX_VARIANTS = ["", "1.2.156", "1.2.156.3088.1", "9.9.9.9", "A.B.C.D", "1.2.156.3088"]


def format_cases() -> list[dict[str, Any]]:
    cases = []
    for aic in _format_inputs():
        valid, error = validate_aic_format(aic)  # type: ignore[arg-type]
        cases.append(
            {
                "aic": aic,
                "validate": [valid, error],
                "isValid": is_valid_aic_format(aic),  # type: ignore[arg-type]
                "byPrefix": [
                    {"expectedPrefix": prefix, "validate": list(validate_aic_format(aic, expected_prefix=prefix))}  # type: ignore[arg-type]
                    for prefix in PREFIX_VARIANTS
                ],
                "parse": _info(parse_aic(aic)),  # type: ignore[arg-type]
                "segments": [get_aic_segment(aic, level) for level in range(-1, 12)],  # type: ignore[arg-type]
                "isOntology": is_ontology_aic(aic),  # type: ignore[arg-type]
                "isEntity": is_entity_aic(aic),  # type: ignore[arg-type]
                "ontologyPrefix": get_ontology_prefix_from_aic(aic),  # type: ignore[arg-type]
            }
        )
    return cases


def base36_cases() -> dict[str, Any]:
    encode = []
    for num in [0, 1, 35, 36, 1295, 1296, 46655, 65535, 2**31, 2**53 - 1]:
        for length in [0, 1, 4, 10]:
            encode.append({"num": num, "length": length, "result": base36_encode(num, length)})
    encode.append({"num": -1, "length": 0, "error": "ValueError"})

    decode = []
    for text in ["", "0", "z", "Z", "10", "zz", "ZZZZ", " 1A ", "\t1a\n", "\x1c1A", "1A\x85", "1A﻿", "2GOSA7PA2GV", "!", "1.2", "É", "1 A"]:
        try:
            value = base36_decode(text)
        except ValueError as exc:
            decode.append({"text": text, "error": type(exc).__name__})
            continue
        entry: dict[str, Any] = {"text": text, "result": value}
        if value > 2**53 - 1:
            # Python 为任意精度整数；Node 超出安全整数范围时抛 RangeError（有意差异）
            entry = {"text": text, "pythonResult": str(value), "nodeError": "RangeError"}
        decode.append(entry)
    for text in ["ZZZZZZZZZZZ", "1Y2P0IJ32E8E8"]:
        decode.append({"text": text, "pythonResult": str(base36_decode(text)), "nodeError": "RangeError"})
    return {"encode": encode, "decode": decode}


def crc_cases() -> list[dict[str, Any]]:
    cases = []
    for data in [b"", b"123456789", b"A", BODY.encode("ascii") + b"\x12\x34", bytes(range(256))]:
        cases.append({"hex": data.hex(), "crc": AICValidator._crc16_ccitt_false(data)})
    return cases


SALTS: list[dict[str, Any]] = [
    {"none": True},
    {"bytes": "1234"},
    {"bytes": ""},
    {"bytes": "00"},
    {"bytes": "ff" * 16},
    {"bytes": "deadbeef"},
    {"str": "0x1234"},
    {"str": "0X1234"},
    {"str": "1234"},
    {"str": "abc"},
    {"str": " 1234 "},
    {"str": "12  34"},
    {"str": "12\t\t34"},
    {"str": "　1234　"},
    {"str": "\x1c1234"},
    {"str": ""},
    {"str": "0x"},
    {"str": "12 34"},
    {"str": "12 345"},
    {"str": "1 2"},
    {"str": "zz"},
    {"str": "0xg1"},
    {"str": "ÿÿ"},
]

CHECKSUM_BODIES = [BODY, ONTOLOGY_BODY, BODY.lower(), f" {BODY} ", "", "SS", "ß", "1.2.É", "1.2.\x1c3"]


def _make_validator(spec: dict[str, Any]) -> AICValidator:
    if spec.get("none"):
        return AICValidator()
    if "bytes" in spec:
        return AICValidator(salt=bytes.fromhex(spec["bytes"]))
    return AICValidator(salt=spec["str"])


def validator_cases() -> list[dict[str, Any]]:
    cases = []
    for spec in SALTS:
        try:
            validator = _make_validator(spec)
        except ValueError as exc:
            cases.append({"salt": spec, "constructError": type(exc).__name__})
            continue
        checksums = []
        for body in CHECKSUM_BODIES:
            try:
                checksums.append({"body": body, "result": validator.calculate_checksum(body)})
            except ValueError as exc:  # UnicodeEncodeError 是 ValueError 子类
                checksums.append({"body": body, "error": type(exc).__name__})

        aics = [VALID, ONTOLOGY, f"{BODY}.ZZZZ", VALID.lower(), BODY, "", None]
        own = validator.calculate_checksum(BODY)
        if own is not None:
            aics.append(f"{BODY}.{own}")
            aics.append(f"{BODY.lower()}.{own.lower()}")
        validations = []
        for aic in aics:
            entry: dict[str, Any] = {"aic": aic}
            for require_crc in (False, True):
                result = validator.validate(aic, require_crc=require_crc)  # type: ignore[arg-type]
                entry["requireCrc" if require_crc else "default"] = [result, validator.last_error]
            entry["noPrefix"] = [validator.validate(aic, expected_prefix=""), validator.last_error]  # type: ignore[arg-type]
            valid, error, info = validator.validate_with_detail(aic)  # type: ignore[arg-type]
            entry["withDetail"] = [valid, error, _info(info)]
            validations.append(entry)
        cases.append(
            {
                "salt": spec,
                "saltHex": validator._salt.hex() if validator._salt is not None else None,
                "hasSalt": validator.has_salt,
                "checksums": checksums,
                "validations": validations,
            }
        )
    return cases


def main() -> None:
    write_fixture("aic", "format", format_cases())
    write_fixture("aic", "base36", base36_cases())
    write_fixture("aic", "crc16", crc_cases())
    write_fixture("aic", "validator", validator_cases())


if __name__ == "__main__":
    main()
