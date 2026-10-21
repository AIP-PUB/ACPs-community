/** src/internal/pyCompat.ts 的单元测试（float / json 的逐例对照见 test/amp/crossLanguage.test.ts / floats.json）。 */

import { describe, expect, it } from "vitest";

import {
  PyFloat,
  pyFloatRepr,
  pyFormatFixed,
  pyRepr,
  pyRound,
  pyStrip,
  pyStripChars,
  pyTruthy,
  pyTypeName,
  pythonJsonDumps,
  reprStr,
} from "../../src/internal/pyCompat.js";

describe("pythonJsonDumps (json.dumps(ensure_ascii=False))", () => {
  it("uses Python's default separators at every level", () => {
    expect(pythonJsonDumps({ a: [1, { b: null }], c: "é" })).toBe(
      '{"a": [1, {"b": null}], "c": "é"}',
    );
    expect(pythonJsonDumps({})).toBe("{}");
    expect(pythonJsonDumps([])).toBe("[]");
  });

  it("treats integer values below 1e21 as int and everything else as float", () => {
    expect(pythonJsonDumps([1, -0, 2.5, 1e21, 2 ** 53, 2 ** 60, 1e20, -(2 ** 60), 1e-7])).toBe(
      "[1, 0, 2.5, 1e+21, 9007199254740992, 1152921504606846976, 100000000000000000000, -1152921504606846976, 1e-07]",
    );
    expect(
      pythonJsonDumps([new PyFloat(12), new PyFloat(-0), new PyFloat(1e16), new PyFloat(2 ** 60)]),
    ).toBe("[12.0, -0.0, 1e+16, 1.152921504606847e+18]");
  });

  it("writes non-finite numbers like json.dumps(allow_nan=True)", () => {
    expect(pythonJsonDumps([Number.NaN, Infinity, -Infinity])).toBe("[NaN, Infinity, -Infinity]");
  });

  it("omits undefined object values (exclude_none) and keeps nulls", () => {
    expect(pythonJsonDumps({ a: undefined, b: null })).toBe('{"b": null}');
  });

  it("escapes strings like json.dumps", () => {
    expect(pythonJsonDumps('\u0000\u001f\b\f\n\r\t"\\\u007f /')).toBe(
      '"\\u0000\\u001f\\b\\f\\n\\r\\t\\"\\\\\u007f /"',
    );
  });

  it("rejects cycles and non-JSON values", () => {
    const cyclic: unknown[] = [];
    cyclic.push(cyclic);
    expect(() => pythonJsonDumps(cyclic)).toThrow("Circular reference detected");
    expect(() => pythonJsonDumps({ f: Symbol("x") })).toThrow(TypeError);
  });
});

describe("float helpers", () => {
  it("pyFloatRepr", () => {
    expect(
      [0, -0, 12, 0.1, 1e16, 1e15, 1e-4, 1e-5, 1.5e-7, Number.NaN, -Infinity].map(pyFloatRepr),
    ).toEqual([
      "0.0",
      "-0.0",
      "12.0",
      "0.1",
      "1e+16",
      "1000000000000000.0",
      "0.0001",
      "1e-05",
      "1.5e-07",
      "nan",
      "-inf",
    ]);
  });

  it("pyRound uses round-half-even on the exact binary value", () => {
    expect([pyRound(0.5), pyRound(1.5), pyRound(2.5), pyRound(-2.5)]).toEqual([0, 2, 2, -2]);
    expect(pyRound(0.125, 2)).toBe(0.12);
    expect(pyRound(2.675, 2)).toBe(2.67); // 2.675 的二进制值略小于 2.675
    expect(Object.is(pyRound(-0.04, 1), -0)).toBe(true);
  });

  it("pyFormatFixed", () => {
    expect(pyFormatFixed(0.0078125, 6)).toBe("0.007812"); // toFixed 会给出 0.007813
    expect(pyFormatFixed(10, 6)).toBe("10.000000");
    expect(pyFormatFixed(-0, 2)).toBe("-0.00");
    expect(pyFormatFixed(1.5, 0)).toBe("2");
  });
});

describe("repr / strip", () => {
  it("reprStr follows CPython quoting rules", () => {
    expect(reprStr("hb-000")).toBe("'hb-000'");
    expect(reprStr("it's")).toBe('"it\'s"');
    expect(reprStr("a\nb\x01中")).toBe("'a\\nb\\x01中'");
  });

  it("pyRepr covers JSON values", () => {
    expect(pyRepr({ a: [1, null, true, 1.5, "x"] })).toBe("{'a': [1, None, True, 1.5, 'x']}");
  });

  it("pyStrip uses Python's whitespace set", () => {
    expect(pyStrip("\x1c\u0085 x 　")).toBe("x");
    expect(pyStrip("﻿x")).toBe("﻿x");
  });
});

describe("strip(chars) / truthiness / type names", () => {
  it("pyStripChars strips the given code points only", () => {
    expect(pyStripChars("--a-b--", "-")).toBe("a-b");
    expect(pyStripChars("😀x😀", "😀")).toBe("x");
    expect(pyStripChars(" x ", "")).toBe(" x ");
  });

  it("pyStrip stays safe as an Array#map callback", () => {
    expect([" a ", "\tb\n"].map(pyStrip)).toEqual(["a", "b"]);
  });

  it("pyTruthy follows Python bool() for JSON values", () => {
    expect([null, undefined, false, 0, -0, "", [], {}].map(pyTruthy)).toEqual(Array(8).fill(false));
    expect([true, 1, Number.NaN, "x", [0], { a: null }].map(pyTruthy)).toEqual(Array(6).fill(true));
  });

  it("pyTypeName", () => {
    expect([null, true, 1, 1.5, 2 ** 60, 1e21, "s", [], {}].map(pyTypeName)).toEqual([
      "NoneType",
      "bool",
      "int",
      "float",
      "int",
      "float",
      "str",
      "list",
      "dict",
    ]);
  });
});
