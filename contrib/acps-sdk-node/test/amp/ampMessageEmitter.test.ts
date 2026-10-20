/** 对应 Python tests/amp/test_amp_message_emitter.py：MessageEmitter。 */

import { join } from "node:path";

import { describe, expect, it } from "vitest";

import * as amp from "../../src/amp/index.js";
import { MessageEmitter } from "../../src/amp/messageEmitter.js";
import type { MessageBodyInput } from "../../src/amp/models.js";
import { readSingleRecord, unwritablePath, useLogCapture, useTmpPath } from "./helpers.js";

function makeBody(): MessageBodyInput {
  return {
    eventType: "send",
    system: "rabbitmq",
    destination: { name: "group.test", kind: "exchange" },
    messageId: "msg-001",
    payloadSizeBytes: 128,
  };
}

describe("MessageEmitter", () => {
  const tmpPath = useTmpPath();
  const logs = useLogCapture();
  const tmpLog = (): string => join(tmpPath(), "logs", "amp_message.jsonl");

  it("test_emit_sync_writes_valid_ndjson", () => {
    const emitter = new MessageEmitter(tmpLog(), "aic-leader");
    const logId = emitter.emitSync(makeBody(), {
      traceId: "t".repeat(32),
      spanId: "s".repeat(16),
      parentSpanId: "",
      correlationId: "sess-1",
    });

    const record = readSingleRecord(tmpLog());
    expect(record["log_type"]).toBe("message");
    expect(record["aic"]).toBe("aic-leader");
    expect(record["log_id"]).toBe(logId);
    expect(record["trace_id"]).toBe("t".repeat(32));
    expect(record["span_id"]).toBe("s".repeat(16));
    expect(record["parent_span_id"]).toBe("");
    expect(record["correlation_id"]).toBe("sess-1");
    expect(record).not.toHaveProperty("integrity");
  });

  it("test_emit_sync_body_uses_camel_case_aliases", () => {
    new MessageEmitter(tmpLog(), "aic-leader").emitSync(makeBody());
    const body = readSingleRecord(tmpLog())["body"] as Record<string, unknown>;
    expect(body["eventType"]).toBe("send");
    expect(body["messageId"]).toBe("msg-001");
    expect(body["payloadSizeBytes"]).toBe(128);
  });

  it("test_emit_sync_exclude_none", () => {
    new MessageEmitter(tmpLog(), "aic-leader").emitSync(makeBody());
    const record = readSingleRecord(tmpLog());
    expect(record).not.toHaveProperty("severity_text");
    expect(record).not.toHaveProperty("severityText");
  });

  it("resource is appended last; an empty resource is not written", () => {
    new MessageEmitter(tmpLog(), "aic", { resource: { "service.name": "x" } }).emitSync(
      makeBody(),
      {
        severityNumber: 9,
      },
    );
    const record = readSingleRecord(tmpLog());
    expect(Object.keys(record).at(-1)).toBe("resource");

    const other = join(tmpPath(), "other.jsonl");
    new MessageEmitter(other, "aic", { resource: {} }).emitSync(makeBody());
    expect(readSingleRecord(other)).not.toHaveProperty("resource");
  });

  it("test_emit_sync_write_failure_only_warns", () => {
    const emitter = new MessageEmitter(
      unwritablePath(tmpPath(), "amp_message.jsonl"),
      "aic-leader",
    );
    expect(emitter.emitSync(makeBody())).toBeTruthy();
    expect(logs.some((r) => r.level === "warn" && r.message.includes("MessageEmitter"))).toBe(true);
  });

  it("test_emit_async_equivalent_to_emit_sync", async () => {
    const emitter = new MessageEmitter(tmpLog(), "aic-leader");
    const logId = await emitter.emit(makeBody(), { traceId: "trace-1", spanId: "span-1" });
    const record = readSingleRecord(tmpLog());
    expect(record["log_id"]).toBe(logId);
    expect(record["trace_id"]).toBe("trace-1");
  });

  it("test_message_emitter_importable_from_amp_package", () => {
    expect(amp.MessageEmitter).toBe(MessageEmitter);
  });
});
