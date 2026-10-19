/** 对应 Python tests/amp/alive_sync/test_alive_sync_bootstrap.py：seekTimestampMs / nextLookbackSeconds。 */

import { describe, expect, it } from "vitest";

import { nextLookbackSeconds, seekTimestampMs } from "../../../src/amp/aliveSync/index.js";

describe("TestSeekTimestampMs", () => {
  it("test_basic_subtraction", () => {
    const ts1 = seekTimestampMs("2026-06-13T01:20:00Z", 0);
    const ts2 = seekTimestampMs("2026-06-13T01:20:00Z", 300);
    expect(ts1 - ts2).toBe(300_000);
    expect(ts1).toBe(Date.UTC(2026, 5, 13, 1, 20, 0));
  });

  it("test_result_non_negative", () => {
    expect(seekTimestampMs("2026-06-13T01:20:00Z", 999_999_999)).toBeGreaterThanOrEqual(0);
  });

  it("test_z_suffix_parsed", () => {
    expect(seekTimestampMs("2026-06-13T01:20:00Z", 60)).toBeGreaterThan(0);
  });

  it("test_invalid_format_raises", () => {
    expect(() => seekTimestampMs("not-a-date", 60)).toThrow(/格式无效/);
  });

  it("naive timestamps are interpreted in local time (like Python's astimezone())", () => {
    expect(seekTimestampMs("2026-06-13T01:20:00", 0)).toBe(
      new Date(2026, 5, 13, 1, 20, 0).getTime(),
    );
    expect(seekTimestampMs("2026-06-13", 0)).toBe(new Date(2026, 5, 13).getTime());
  });

  it("ISO week dates are not supported (Python 3.11+ accepts them)", () => {
    expect(() => seekTimestampMs("2026-W24-6T01:20:00Z", 0)).toThrow(/格式无效/);
  });
});

describe("TestNextLookbackSeconds", () => {
  it("test_doubles_by_default", () => {
    expect(nextLookbackSeconds(300, { maxSeconds: 86400 })).toBe(600);
  });
  it("test_clamps_at_max", () => {
    expect(nextLookbackSeconds(50000, { maxSeconds: 86400 })).toBe(86400);
  });
  it("test_already_at_max_stays", () => {
    expect(nextLookbackSeconds(86400, { maxSeconds: 86400 })).toBe(86400);
  });
  it("test_custom_factor", () => {
    expect(nextLookbackSeconds(100, { factor: 3, maxSeconds: 10000 })).toBe(300);
  });
});
