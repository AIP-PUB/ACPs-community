/** 对应 Python tests/amp/test_amp_system_emitter.py：SystemEmitter。 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import * as amp from "../../src/amp/index.js";
import { SystemEmitter } from "../../src/amp/systemEmitter.js";
import { newLogId } from "../../src/amp/models.js";
import { readSingleRecord, unwritablePath, useLogCapture, useTmpPath } from "./helpers.js";

describe("SystemEmitter", () => {
  const tmpPath = useTmpPath();
  const logs = useLogCapture();
  const tmpLog = (): string => join(tmpPath(), "logs", "amp_system.jsonl");

  it("test_emit_sync_writes_valid_ndjson", () => {
    const resource = { "service.name": "demo-partner-test", "service.namespace": "acps-demo" };
    const emitter = new SystemEmitter(tmpLog(), "aic-partner", { resource });
    const logId = emitter.emitSync(
      { message: "hi", category: "llm" },
      { severityNumber: 9, severityText: "INFO", correlationId: "tid-1" },
    );

    const record = readSingleRecord(tmpLog());
    expect(record["log_type"]).toBe("system");
    expect(record["schema_version"]).toBe("1.0");
    expect(record["aic"]).toBe("aic-partner");
    expect(record["log_id"]).toBe(logId);
    expect(record["severity_number"]).toBe(9);
    expect(record["severity_text"]).toBe("INFO");
    expect(record["correlation_id"]).toBe("tid-1");
    expect(record["resource"]).toEqual(resource);
    expect((record["body"] as Record<string, unknown>)["message"]).toBe("hi");
    expect(record).not.toHaveProperty("integrity");
    expect(record["timestamp"]).toMatch(/Z$/);
  });

  it("test_emit_sync_exclude_none_trace_id", () => {
    new SystemEmitter(tmpLog(), "aic-partner").emitSync(
      { message: "hi" },
      { severityNumber: 9, severityText: "INFO" },
    );
    expect(readSingleRecord(tmpLog())).not.toHaveProperty("trace_id");
  });

  it("test_emit_sync_body_none", () => {
    const logId = new SystemEmitter(tmpLog(), "aic-partner").emitSync(null, {
      severityNumber: 9,
      severityText: "INFO",
    });
    expect(logId).toMatch(/^[0-9a-f-]{36}$/);
    const record = readSingleRecord(tmpLog());
    expect(record["log_id"]).toBe(logId);
    expect(record).not.toHaveProperty("body");
  });

  it("test_log_id_is_valid_uuid", () => {
    const logId = new SystemEmitter(tmpLog(), "aic-partner").emitSync(
      { message: "x" },
      { severityNumber: 9, severityText: "INFO" },
    );
    expect(logId).toMatch(/^[0-9a-f-]{36}$/);
  });

  // Python 版通过 patch LogRecord.__init__ 断言 resource 传入了模型；Node 版直接断言写出的记录。
  it("test_resource_passed_to_log_record_constructor (asserted on the written record)", () => {
    const resource = { "service.name": "svc-a" };
    new SystemEmitter(tmpLog(), "aic-partner", { resource }).emitSync({ message: "hi" });
    expect(readSingleRecord(tmpLog())["resource"]).toEqual(resource);
  });

  it("empty resource is still written (it goes through the model, unlike Access/Message)", () => {
    new SystemEmitter(tmpLog(), "aic-partner", { resource: {} }).emitSync({ message: "hi" });
    expect(readSingleRecord(tmpLog())["resource"]).toEqual({});
  });

  // Python 的 test_new_log_id_fallback_uuid4 验证 uuid7 缺失时回退 uuid4；Node 版固定使用 UUID v4。
  it("test_new_log_id_fallback_uuid4 (Node always generates UUID v4)", () => {
    expect(newLogId()).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });

  it("test_emit_sync_write_failure_only_warns", () => {
    const emitter = new SystemEmitter(unwritablePath(tmpPath(), "amp_system.jsonl"), "aic-partner");
    const logId = emitter.emitSync({ message: "hi" }, { severityNumber: 9, severityText: "INFO" });
    expect(logId).toBeTruthy();
    expect(logs.some((r) => r.level === "warn" && r.message.includes("SystemEmitter"))).toBe(true);
  });

  it("test_emit_async_equivalent_to_emit_sync", async () => {
    const emitter = new SystemEmitter(tmpLog(), "aic-partner");
    const logId = await emitter.emit(
      { message: "async" },
      { severityNumber: 9, severityText: "INFO", correlationId: "corr-1" },
    );
    const record = readSingleRecord(tmpLog());
    expect(record["log_id"]).toBe(logId);
    expect(record["correlation_id"]).toBe("corr-1");
    expect(record["severity_number"]).toBe(9);
  });

  it("a >1 MiB async line is written in one piece even while emitSync runs concurrently", async () => {
    const file = tmpLog();
    const emitter = new SystemEmitter(file, "aic");
    const blob = "x".repeat(3 * 1024 * 1024);
    let small = 0;
    const ticker = setInterval(() => {
      small += 1;
      emitter.emitSync({ small });
    }, 0);
    try {
      await Promise.all([
        emitter.emit({ blob, n: 1 }),
        emitter.emit({ blob, n: 2 }),
        emitter.emit({ blob, n: 3 }),
      ]);
      // 让定时器在大行写完之后再跑几轮
      await new Promise((resolve) => setTimeout(resolve, 20));
    } finally {
      clearInterval(ticker);
    }
    emitter.emitSync({ small: "last" });

    const lines = readFileSync(file, "utf-8").split("\n");
    expect(lines.pop()).toBe(""); // 以换行结尾
    const records = lines.map((line) => JSON.parse(line) as { body: Record<string, unknown> });
    const big = records.filter((r) => typeof r.body["blob"] === "string");
    expect(big.map((r) => r.body["n"])).toEqual([1, 2, 3]);
    expect(big.every((r) => r.body["blob"] === blob)).toBe(true);
    expect(records.length).toBe(3 + small + 1);
  });

  it("test_system_emitter_importable_from_amp_package", () => {
    expect(amp.SystemEmitter).toBe(SystemEmitter);
  });
});
