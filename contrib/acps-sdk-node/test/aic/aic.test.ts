/**
 * 对应 Python `tests/test_aic.py`，并用 Python 版生成的对照样例（scripts/fixtures/gen_aic.py）逐条比对。
 */

import { describe, expect, it } from "vitest";

import {
  type AICInfo,
  AICValidator,
  base36Decode,
  base36Encode,
  getAicSegment,
  getOntologyPrefixFromAic,
  isEntityAic,
  isOntologyAic,
  isValidAicFormat,
  parseAic,
  validateAicFormat,
} from "../../src/aic/index.js";
import { loadPythonFixture } from "../fixtures.js";

describe("test_aic", () => {
  it("test_validate_aic_format_accepts_v02_order", () => {
    const [valid, err] = validateAicFormat("1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4");
    expect(valid).toBe(true);
    expect(err).toBeNull();
  });

  it("test_validate_aic_format_rejects_wrong_segment_count", () => {
    const [valid, err] = validateAicFormat("1.2.156.3088.1.1.34C2.478BDF.3GF546");
    expect(valid).toBe(false);
    expect(err).not.toBeNull();
    expect(err).toContain("10 段");
  });

  it("test_validate_aic_format_rejects_multichar_version", () => {
    const [valid, err] = validateAicFormat("1.2.156.3088.11.1.34C2.478BDF.3GF546.0JU4");
    expect(valid).toBe(false);
    expect(err).not.toBeNull();
    expect(err).toContain("第 5 级");
  });

  it("test_parse_aic_maps_segments_correctly", () => {
    const info = parseAic("1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4");
    expect(info).not.toBeNull();
    expect(info?.prefix).toBe("1.2.156.3088");
    expect(info?.version).toBe("1");
    expect(info?.arsp).toBe("1");
    expect(info?.vendor).toBe("34C2");
    expect(info?.ontologySerial).toBe("478BDF");
    expect(info?.instanceSerial).toBe("3GF546");
    expect(info?.checksum).toBe("0JU4");
    expect(info?.body).toBe("1.2.156.3088.1.1.34C2.478BDF.3GF546");
  });

  it("test_ontology_entity_helpers", () => {
    const ontologyAic = "1.2.156.3088.1.1.34C2.478BDF.000000.0SV9";
    const entityAic = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4";
    expect(isOntologyAic(ontologyAic)).toBe(true);
    expect(isEntityAic(ontologyAic)).toBe(false);
    expect(isEntityAic(entityAic)).toBe(true);
    expect(isOntologyAic(entityAic)).toBe(false);
  });

  it("test_get_ontology_prefix_from_aic_uses_1_to_8_levels", () => {
    expect(getOntologyPrefixFromAic("1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4")).toBe(
      "1.2.156.3088.1.1.34C2.478BDF",
    );
  });

  it("test_get_aic_segment", () => {
    const aic = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4";
    expect(getAicSegment(aic, 5)).toBe("1");
    expect(getAicSegment(aic, 6)).toBe("1");
    expect(getAicSegment(aic, 10)).toBe("0JU4");
    expect(getAicSegment(aic, 11)).toBeNull();
  });

  it("test_validator_crc_with_salt_0x1234", () => {
    const body = "1.2.156.3088.1.1.34C2.478BDF.3GF546";
    const validator = new AICValidator("0x1234");
    expect(validator.calculateChecksum(body)).toBe("0JU4");
    expect(validator.validate(`${body}.0JU4`)).toBe(true);
    expect(validator.validate(`${body}.ZZZZ`)).toBe(false);
    expect(validator.lastError).not.toBeNull();
  });

  it("test_validator_without_salt_behavior", () => {
    const aic = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4";
    const validator = new AICValidator();
    expect(validator.validate(aic)).toBe(true);
    expect(validator.validate(aic, { requireCrc: true })).toBe(false);
  });

  it("test_is_valid_aic_format_bool_helper", () => {
    expect(isValidAicFormat("1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4")).toBe(true);
    expect(isValidAicFormat("1.2.156.3088.1.1.34C2.478BDF.3GF546")).toBe(false);
  });

  it("rejects non-Uint8Array/string salts (Python TypeError)", () => {
    expect(() => new AICValidator(42 as unknown as string)).toThrow(TypeError);
  });
});

// ---------------------------------------------------------------------------
// 跨语言对照样例
// ---------------------------------------------------------------------------

interface InfoFixture {
  raw: string;
  normalized: string;
  prefix: string;
  version: string;
  arsp: string;
  vendor: string;
  ontologySerial: string;
  instanceSerial: string;
  checksum: string;
  isOntology: boolean;
  isEntity: boolean;
  body: string;
  ontologyPrefix: string;
}

function infoToFixture(info: AICInfo | null): InfoFixture | null {
  if (!info) return null;
  return {
    raw: info.raw,
    normalized: info.normalized,
    prefix: info.prefix,
    version: info.version,
    arsp: info.arsp,
    vendor: info.vendor,
    ontologySerial: info.ontologySerial,
    instanceSerial: info.instanceSerial,
    checksum: info.checksum,
    isOntology: info.isOntology,
    isEntity: info.isEntity,
    body: info.body,
    ontologyPrefix: info.ontologyPrefix,
  };
}

interface FormatCase {
  aic: string | null;
  validate: [boolean, string | null];
  isValid: boolean;
  byPrefix: Array<{ expectedPrefix: string; validate: [boolean, string | null] }>;
  parse: InfoFixture | null;
  segments: Array<string | null>;
  isOntology: boolean;
  isEntity: boolean;
  ontologyPrefix: string | null;
}

const label = (value: unknown): string => JSON.stringify(value);

describe("python fixtures: AIC format / parse", () => {
  const cases = loadPythonFixture<FormatCase[]>("aic", "format");

  it.each(cases.map((c) => [label(c.aic), c] as const))("%s", (_name, c) => {
    expect(validateAicFormat(c.aic)).toEqual(c.validate);
    expect(isValidAicFormat(c.aic)).toBe(c.isValid);
    for (const variant of c.byPrefix) {
      expect(validateAicFormat(c.aic, { expectedPrefix: variant.expectedPrefix })).toEqual(
        variant.validate,
      );
    }
    expect(infoToFixture(parseAic(c.aic))).toEqual(c.parse);
    const levels = Array.from({ length: 13 }, (_, i) => i - 1);
    expect(levels.map((level) => getAicSegment(c.aic, level))).toEqual(c.segments);
    expect(isOntologyAic(c.aic)).toBe(c.isOntology);
    expect(isEntityAic(c.aic)).toBe(c.isEntity);
    expect(getOntologyPrefixFromAic(c.aic)).toBe(c.ontologyPrefix);
  });
});

interface Base36Fixture {
  encode: Array<{ num: number; length: number; result?: string; error?: string }>;
  decode: Array<{
    text: string;
    result?: number;
    error?: string;
    pythonResult?: string;
    nodeError?: string;
  }>;
}

describe("python fixtures: base36", () => {
  const fixture = loadPythonFixture<Base36Fixture>("aic", "base36");

  it.each(fixture.encode.map((c) => [`${c.num}/${c.length}`, c] as const))("encode %s", (_n, c) => {
    if (c.error) {
      expect(() => base36Encode(c.num, c.length)).toThrow();
    } else {
      expect(base36Encode(c.num, c.length)).toBe(c.result);
    }
  });

  it.each(fixture.decode.map((c) => [label(c.text), c] as const))("decode %s", (_n, c) => {
    if (c.error) {
      expect(() => base36Decode(c.text)).toThrow(Error);
    } else if (c.nodeError) {
      // Python 为任意精度整数，Node 超出 Number.MAX_SAFE_INTEGER 时显式报错（有意差异）
      expect(() => base36Decode(c.text)).toThrow(RangeError);
    } else {
      expect(base36Decode(c.text)).toBe(c.result);
    }
  });
});

describe("python fixtures: crc16", () => {
  const cases = loadPythonFixture<Array<{ hex: string; crc: number }>>("aic", "crc16");
  it.each(cases.map((c) => [c.hex.slice(0, 16) || "<empty>", c] as const))("%s", (_n, c) => {
    expect(AICValidator.crc16CcittFalse(Uint8Array.from(Buffer.from(c.hex, "hex")))).toBe(c.crc);
  });
});

type SaltSpec = { none?: boolean; bytes?: string; str?: string };

interface ValidatorCase {
  salt: SaltSpec;
  constructError?: string;
  saltHex?: string | null;
  hasSalt?: boolean;
  checksums?: Array<{ body: string; result?: string | null; error?: string }>;
  validations?: Array<{
    aic: string | null;
    default: [boolean, string | null];
    requireCrc: [boolean, string | null];
    noPrefix: [boolean, string | null];
    withDetail: [boolean, string | null, InfoFixture | null];
  }>;
}

function makeValidator(spec: SaltSpec): AICValidator {
  if (spec.none) return new AICValidator();
  if (spec.bytes !== undefined)
    return new AICValidator(Uint8Array.from(Buffer.from(spec.bytes, "hex")));
  return new AICValidator(spec.str as string);
}

describe("python fixtures: AICValidator", () => {
  const cases = loadPythonFixture<ValidatorCase[]>("aic", "validator");

  it.each(cases.map((c) => [label(c.salt), c] as const))("salt %s", (_n, c) => {
    if (c.constructError) {
      expect(() => makeValidator(c.salt)).toThrow(Error);
      return;
    }
    const validator = makeValidator(c.salt);
    expect(validator.hasSalt).toBe(c.hasSalt);
    for (const checksum of c.checksums ?? []) {
      if (checksum.error) {
        expect(() => validator.calculateChecksum(checksum.body), label(checksum.body)).toThrow();
      } else {
        expect(validator.calculateChecksum(checksum.body), label(checksum.body)).toBe(
          checksum.result,
        );
      }
    }
    for (const v of c.validations ?? []) {
      expect([validator.validate(v.aic), validator.lastError]).toEqual(v.default);
      expect([validator.validate(v.aic, { requireCrc: true }), validator.lastError]).toEqual(
        v.requireCrc,
      );
      expect([validator.validate(v.aic, { expectedPrefix: "" }), validator.lastError]).toEqual(
        v.noPrefix,
      );
      const [valid, error, info] = validator.validateWithDetail(v.aic);
      expect([valid, error, infoToFixture(info)]).toEqual(v.withDetail);
    }
  });
});
