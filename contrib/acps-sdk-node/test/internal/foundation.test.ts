/**
 * 公共基础设施的回归测试（来自全量审查）。
 */

import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";
import { z } from "zod";

import { base36Encode } from "../../src/aic/index.js";
import * as aip from "../../src/aip/index.js";
import { VERSION } from "../../src/index.js";
import { HttpStatusError } from "../../src/internal/http.js";
import { type Logger, getLogger, setLoggerFactory } from "../../src/internal/logger.js";
import { optional } from "../../src/internal/schema.js";

describe("base36Encode", () => {
  it("rejects integers beyond Number.MAX_SAFE_INTEGER instead of returning wrong digits", () => {
    expect(base36Encode(Number.MAX_SAFE_INTEGER)).toBe("2GOSA7PA2GV");
    expect(() => base36Encode(2 ** 53)).toThrow(RangeError);
    expect(() => base36Encode(1e20)).toThrow(RangeError);
  });

  it("rejects non-integers and negatives", () => {
    expect(() => base36Encode(1.5)).toThrow(RangeError);
    expect(() => base36Encode(-1)).toThrow(RangeError);
  });
});

describe("optional()", () => {
  it("is a real ZodOptional at runtime", () => {
    const schema = z.object({ name: optional(z.string()) });
    expect(() => schema.shape.name.unwrap()).not.toThrow();
    expect(schema.parse({ name: null })).toEqual({});
    expect(schema.parse({})).toEqual({});
    expect(schema.parse({ name: "x" })).toEqual({ name: "x" });
    expect(schema.safeParse({ name: 5 }).success).toBe(false);
  });
});

describe("logger", () => {
  it("creates one logger per name and recreates them when the factory changes", () => {
    const created: string[] = [];
    const lines: string[] = [];
    const factory = (name: string): Logger => {
      created.push(name);
      const write = (message: string): void => {
        lines.push(`${name}:${message}`);
      };
      return { debug: write, info: write, warn: write, error: write };
    };
    try {
      setLoggerFactory(factory);
      const logger = getLogger("acps.test");
      logger.info("a");
      logger.warn("b");
      expect(created).toEqual(["acps.test"]);
      setLoggerFactory(factory);
      logger.error("c");
      expect(created).toEqual(["acps.test", "acps.test"]);
      expect(lines).toEqual(["acps.test:a", "acps.test:b", "acps.test:c"]);
    } finally {
      setLoggerFactory(undefined);
    }
  });
});

describe("package metadata", () => {
  it("VERSION matches package.json", () => {
    const pkg = JSON.parse(
      readFileSync(new URL("../../package.json", import.meta.url), "utf-8"),
    ) as { version: string };
    expect(VERSION).toBe(pkg.version);
  });

  it("HttpStatusError (thrown by the clients) is exported from acps-sdk/aip", () => {
    expect(aip.HttpStatusError).toBe(HttpStatusError);
  });
});
