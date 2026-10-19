/** 对应 Python tests/amp/test_trace_context.py：W3C traceparent 工具。 */

import { describe, expect, it } from "vitest";

import {
  type TraceContext,
  TRACEPARENT_HEADER,
  formatTraceparent,
  newSpanId,
  newTraceId,
  parseTraceparent,
} from "../../src/amp/traceContext.js";

describe("trace context", () => {
  it("test_new_trace_id_is_32_hex", () => {
    const traceId = newTraceId();
    expect(traceId).toHaveLength(32);
    expect(traceId).toMatch(/^[0-9a-f]{32}$/);
  });

  it("test_new_span_id_is_16_hex", () => {
    const spanId = newSpanId();
    expect(spanId).toHaveLength(16);
    expect(spanId).toMatch(/^[0-9a-f]{16}$/);
  });

  it("test_format_and_parse_roundtrip", () => {
    const ctx: TraceContext = { traceId: newTraceId(), spanId: newSpanId(), sampled: true };
    expect(parseTraceparent(formatTraceparent(ctx))).toEqual(ctx);
  });

  it("test_parse_invalid_or_missing_returns_none", () => {
    expect(parseTraceparent(null)).toBeNull();
    expect(parseTraceparent(undefined)).toBeNull();
    expect(parseTraceparent("")).toBeNull();
    expect(parseTraceparent("bad-header")).toBeNull();
    expect(parseTraceparent("00-abc-def-01")).toBeNull();
  });

  it("sampled defaults to true and unsampled flags round-trip", () => {
    const traceId = "4bf92f3577b34da6a3ce929d0e0e4736";
    const spanId = "00f067aa0ba902b7";
    expect(formatTraceparent({ traceId, spanId })).toBe(`00-${traceId}-${spanId}-01`);
    expect(parseTraceparent(`00-${traceId}-${spanId}-00`)).toEqual({
      traceId,
      spanId,
      sampled: false,
    });
    expect(TRACEPARENT_HEADER).toBe("traceparent");
  });
});
