/** Node 版 RFC 8785 实现的补充测试（与 Python 的逐例对照见 crossLanguage.test.ts / jcs.json）。 */

import { describe, expect, it } from "vitest";

import { canonicalize, canonicalizeToString } from "../../src/amp/jcs.js";
import { PyFloat } from "../../src/internal/pyCompat.js";

describe("canonicalize", () => {
  it("sorts keys by UTF-16 code units and uses compact separators", () => {
    expect(canonicalizeToString({ b: [1, { d: 1, c: 2 }], a: "x", "\u{1F600}": 1, "￿": 2 })).toBe(
      '{"a":"x","b":[1,{"c":2,"d":1}],"😀":1,"￿":2}',
    );
  });

  it("formats numbers per ECMAScript Number::toString", () => {
    expect(canonicalizeToString([-0, 1e21, 1e-7, 0.1, 100, 1.5e300])).toBe(
      "[0,1e+21,1e-7,0.1,100,1.5e+300]",
    );
    expect(canonicalizeToString(new PyFloat(12))).toBe("12");
  });

  it("returns UTF-8 bytes", () => {
    expect(canonicalize({ k: "é" })).toEqual(Buffer.from('{"k":"é"}', "utf-8"));
  });

  it("skips undefined object values and writes undefined array items as null", () => {
    expect(canonicalizeToString({ a: undefined, b: [undefined] })).toBe('{"b":[null]}');
  });

  it("rejects non-finite numbers, lone surrogates, functions and cycles", () => {
    expect(() => canonicalizeToString(Number.NaN)).toThrow("Invalid JSON number: nan");
    expect(() => canonicalizeToString(Infinity)).toThrow("Invalid JSON number: inf");
    expect(() => canonicalizeToString("\ud800")).toThrow(/lone surrogate/);
    expect(() => canonicalizeToString({ f: () => 1 })).toThrow(TypeError);
    const cyclic: Record<string, unknown> = {};
    cyclic["self"] = cyclic;
    expect(() => canonicalizeToString(cyclic)).toThrow("Circular reference detected");
  });
});
