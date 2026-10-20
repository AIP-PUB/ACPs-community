/**
 * W3C Trace Context（traceparent）最小工具：生成 trace_id/span_id 与 header 编解码。
 *
 * 对应 Python `acps_sdk.amp.trace_context`。
 */

import { randomBytes } from "node:crypto";

import { pyStrip } from "../internal/pyCompat.js";

export const TRACEPARENT_HEADER = "traceparent";

/** 生成 32 位十六进制 trace_id（16 字节）。 */
export function newTraceId(): string {
  return randomBytes(16).toString("hex");
}

/** 生成 16 位十六进制 span_id（8 字节）。 */
export function newSpanId(): string {
  return randomBytes(8).toString("hex");
}

/** 当前 trace 上下文（W3C traceparent 语义）。`sampled` 缺省视为 true。 */
export interface TraceContext {
  readonly traceId: string;
  readonly spanId: string;
  readonly sampled?: boolean;
}

/** 编码为 W3C traceparent header 值。 */
export function formatTraceparent(ctx: TraceContext): string {
  const flags = (ctx.sampled ?? true) ? "01" : "00";
  return `00-${ctx.traceId}-${ctx.spanId}-${flags}`;
}

const HEX = /^[0-9a-fA-F]+$/;

/**
 * 解析 traceparent header；格式非法或缺失时返回 null。
 *
 * 十六进制校验比 Python 版的 `int(x, 16)` 严格：Python 还会接受 `0x` 前缀、`+` 号、
 * 下划线分隔与段内空白（这些都不是合法的 traceparent），Node 版一律视为非法。
 */
export function parseTraceparent(header: string | null | undefined): TraceContext | null {
  if (!header) {
    return null;
  }
  const parts = pyStrip(header).split("-");
  if (parts.length !== 4 || parts[0] !== "00") {
    return null;
  }
  const [, traceId = "", spanId = "", flags = ""] = parts;
  if (traceId.length !== 32 || spanId.length !== 16 || flags.length !== 2) {
    return null;
  }
  if (!HEX.test(traceId) || !HEX.test(spanId) || !HEX.test(flags)) {
    return null;
  }
  return { traceId, spanId, sampled: flags !== "00" };
}
