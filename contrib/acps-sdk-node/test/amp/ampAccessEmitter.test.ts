/** 对应 Python tests/amp/test_amp_access_emitter.py：AccessEmitter。 */

import { join } from "node:path";

import { describe, expect, it } from "vitest";

import * as amp from "../../src/amp/index.js";
import { AccessEmitter } from "../../src/amp/accessEmitter.js";
import type { AccessBodyInput } from "../../src/amp/models.js";
import {
  readLines,
  readSingleRecord,
  unwritablePath,
  useLogCapture,
  useTmpPath,
} from "./helpers.js";

function makeBody(): AccessBodyInput {
  return {
    request: { method: "POST", route: "/rpc", url: "http://localhost/rpc" },
    response: { statusCode: 200 },
    caller: { aic: "caller-aic", serviceName: "demo-leader" },
    callee: { aic: "callee-aic", serviceName: "demo-partner" },
    durationMs: 12,
  };
}

describe("AccessEmitter", () => {
  const tmpPath = useTmpPath();
  const logs = useLogCapture();
  const tmpLog = (): string => join(tmpPath(), "logs", "amp_access.jsonl");

  it("test_emit_sync_writes_valid_ndjson", () => {
    const emitter = new AccessEmitter(tmpLog(), "aic-leader");
    const logId = emitter.emitSync(makeBody(), {
      traceId: "t".repeat(32),
      spanId: "s".repeat(16),
      parentSpanId: "",
      correlationId: "sess-1",
    });

    const record = readSingleRecord(tmpLog());
    expect(record["log_type"]).toBe("access");
    expect(record["aic"]).toBe("aic-leader");
    expect(record["log_id"]).toBe(logId);
    expect(record["trace_id"]).toBe("t".repeat(32));
    expect(record["span_id"]).toBe("s".repeat(16));
    expect(record["parent_span_id"]).toBe("");
    expect(record["correlation_id"]).toBe("sess-1");
    expect(record).not.toHaveProperty("integrity");
    expect(record["timestamp"]).toMatch(/Z$/);
  });

  it("test_emit_sync_body_uses_camel_case_aliases", () => {
    new AccessEmitter(tmpLog(), "aic-leader").emitSync(makeBody());
    const body = readSingleRecord(tmpLog())["body"] as Record<string, Record<string, unknown>>;
    expect(body["durationMs"]).toBe(12);
    expect(body["caller"]?.["serviceName"]).toBe("demo-leader");
    // pydantic float 字段：整数 12 写成 12.0
    expect(readLines(tmpLog())[0]).toContain('"durationMs": 12.0');
  });

  it("test_emit_sync_exclude_none", () => {
    new AccessEmitter(tmpLog(), "aic-leader").emitSync(makeBody());
    const record = readSingleRecord(tmpLog());
    expect(record).not.toHaveProperty("severity_text");
    expect(record).not.toHaveProperty("severityText");
  });

  it("test_emit_sync_write_failure_only_warns", () => {
    const emitter = new AccessEmitter(unwritablePath(tmpPath(), "amp_access.jsonl"), "aic-leader");
    expect(emitter.emitSync(makeBody())).toBeTruthy();
    expect(logs.some((r) => r.level === "warn" && r.message.includes("AccessEmitter"))).toBe(true);
  });

  it("test_emit_async_equivalent_to_emit_sync", async () => {
    const emitter = new AccessEmitter(tmpLog(), "aic-leader");
    const logId = await emitter.emit(makeBody(), { traceId: "trace-1", spanId: "span-1" });
    const record = readSingleRecord(tmpLog());
    expect(record["log_id"]).toBe(logId);
    expect(record["trace_id"]).toBe("trace-1");
  });

  it("async write failure only warns and still returns the log_id", async () => {
    const emitter = new AccessEmitter(unwritablePath(tmpPath(), "amp_access.jsonl"), "aic");
    await expect(emitter.emit(makeBody())).resolves.toMatch(/^[0-9a-f-]{36}$/);
    expect(logs.some((r) => r.message === "AccessEmitter: 写入失败，跳过")).toBe(true);
  });

  it("null optional fields are accepted like Python None", () => {
    new AccessEmitter(tmpLog(), "aic").emitSync(
      { durationMs: null, error: null, request: { method: "GET", headers: null } },
      { traceId: null },
    );
    const record = readSingleRecord(tmpLog());
    expect(record["body"]).toEqual({ request: { method: "GET" } });
    expect(record).not.toHaveProperty("trace_id");
  });

  it("NaN / ±Infinity in float fields are written like Python json.dumps", async () => {
    const emitter = new AccessEmitter(tmpLog(), "aic");
    await emitter.emit({ durationMs: Number.NaN });
    emitter.emitSync({ durationMs: -Infinity });
    const lines = readLines(tmpLog());
    expect(lines[0]).toContain('"body": {"durationMs": NaN}');
    expect(lines[1]).toContain('"body": {"durationMs": -Infinity}');
  });

  it("float fields still reject non-numbers", () => {
    const emitter = new AccessEmitter(tmpLog(), "aic");
    expect(() => emitter.emitSync({ durationMs: "12" as unknown as number })).toThrow(
      /expected number/,
    );
  });

  it("test_access_emitter_importable_from_amp_package", () => {
    expect(amp.AccessEmitter).toBe(AccessEmitter);
  });
});
