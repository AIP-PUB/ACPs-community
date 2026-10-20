/** 对应 Python tests/test_amp_heartbeat_emitter.py：HeartbeatEmitter。 */

import { existsSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it, vi } from "vitest";

import {
  HeartbeatEmitter,
  HeartbeatLogRecordSchema,
  LogRecordSchema,
} from "../../src/amp/index.js";
import {
  UUID_RE,
  readLines,
  readSingleRecord,
  sleep,
  useLogCapture,
  useTmpPath,
} from "./helpers.js";

describe("HeartbeatEmitter.emitSync", () => {
  const tmpPath = useTmpPath();
  const logs = useLogCapture();

  it("test_emit_sync_writes_valid_heartbeat_record", () => {
    const logFile = join(tmpPath(), "amp_heartbeat.jsonl");
    const emitter = new HeartbeatEmitter(logFile, "1.2.3.test");

    const logId = emitter.emitSync();

    expect(existsSync(logFile)).toBe(true);
    const lines = readLines(logFile);
    expect(lines).toHaveLength(1);
    const record = HeartbeatLogRecordSchema.parse(JSON.parse(lines[0]!));
    expect(record.log_id).toBe(logId);
    expect(record.log_type).toBe("heartbeat");
    expect(record.aic).toBe("1.2.3.test");
    expect(record.schema_version).toBe("1.0");
    expect(record.timestamp).toMatch(/\+00:00$/);
  });

  it("test_emit_sync_serialization_format", () => {
    const logFile = join(tmpPath(), "amp_heartbeat.jsonl");
    new HeartbeatEmitter(logFile, "test-aic").emitSync();

    const data = readSingleRecord(logFile);
    // 顶层 snake_case
    expect(data["log_type"]).toBe("heartbeat");
    for (const key of ["schema_version", "log_id", "timestamp", "aic"]) {
      expect(data).toHaveProperty(key);
    }
    // integrity 不写（心跳不签名）
    expect(data).not.toHaveProperty("integrity");

    // body camelCase
    const body = data["body"] as Record<string, unknown>;
    expect(typeof body["uptimeSeconds"]).toBe("number");
    expect(body["uptimeSeconds"] as number).toBeGreaterThanOrEqual(0);
    expect(body).not.toHaveProperty("uptime_seconds");
  });

  it("uptimeSeconds is written as a Python float (e.g. 0.0)", () => {
    const logFile = join(tmpPath(), "amp_heartbeat.jsonl");
    new HeartbeatEmitter(logFile, "aic").emitSync();
    expect(readLines(logFile)[0]).toMatch(/"body": \{"uptimeSeconds": \d+\.\d+(e[+-]\d+)?\}/);
  });

  it("test_emit_sync_also_accepted_by_log_record", () => {
    const logFile = join(tmpPath(), "amp_heartbeat.jsonl");
    new HeartbeatEmitter(logFile, "test-aic").emitSync();

    const record = LogRecordSchema.parse(readSingleRecord(logFile));
    expect(record.log_type).toBe("heartbeat");
    expect(record.aic).toBe("test-aic");
  });

  it("test_emit_sync_uptime_increases", async () => {
    const logFile = join(tmpPath(), "amp_heartbeat.jsonl");
    const emitter = new HeartbeatEmitter(logFile, "aic");

    emitter.emitSync();
    await sleep(50);
    emitter.emitSync();

    const lines = readLines(logFile);
    expect(lines).toHaveLength(2);
    const uptime = (line: string): number =>
      (JSON.parse(line) as { body: { uptimeSeconds: number } }).body.uptimeSeconds;
    expect(uptime(lines[1]!)).toBeGreaterThan(uptime(lines[0]!));
  });

  it("test_emit_sync_appends_multiple_lines", () => {
    const logFile = join(tmpPath(), "amp_heartbeat.jsonl");
    const emitter = new HeartbeatEmitter(logFile, "aic");
    emitter.emitSync();
    emitter.emitSync();
    emitter.emitSync();

    const lines = readLines(logFile);
    expect(lines).toHaveLength(3);
    const ids = lines.map((line) => (JSON.parse(line) as { log_id: string }).log_id);
    expect(new Set(ids).size).toBe(3); // 所有 log_id 唯一
  });

  it("test_emit_sync_creates_parent_dirs", () => {
    const logFile = join(tmpPath(), "nested", "deep", "heartbeat.jsonl");
    new HeartbeatEmitter(logFile, "aic").emitSync();
    expect(existsSync(logFile)).toBe(true);
  });

  it("test_emit_sync_failure_does_not_raise", () => {
    const emitter = new HeartbeatEmitter(tmpPath(), "aic"); // tmpPath 是目录，会失败
    expect(() => emitter.emitSync()).not.toThrow(); // 静默吞掉异常
    expect(logs.some((r) => r.message === "HeartbeatEmitter: 写入失败，跳过")).toBe(true);
  });

  it("test_emit_sync_log_id_is_valid_uuid", () => {
    const emitter = new HeartbeatEmitter(join(tmpPath(), "amp_heartbeat.jsonl"), "aic");
    expect(emitter.emitSync()).toMatch(UUID_RE);
  });

  it("trace / severity options are written as snake_case top-level fields", () => {
    const logFile = join(tmpPath(), "amp_heartbeat.jsonl");
    new HeartbeatEmitter(logFile, "aic").emitSync({ traceId: "t1", severityNumber: 9 });
    const data = readSingleRecord(logFile);
    expect(data["trace_id"]).toBe("t1");
    expect(data["severity_number"]).toBe(9);
  });
});

describe("HeartbeatEmitter.emit (async)", () => {
  const tmpPath = useTmpPath();

  it("test_emit_async_equivalent_to_emit_sync", async () => {
    const logFile = join(tmpPath(), "amp_heartbeat.jsonl");
    const emitter = new HeartbeatEmitter(logFile, "aic");

    const logId = await emitter.emit();

    expect(logId).toMatch(UUID_RE);
    const lines = readLines(logFile);
    expect(lines).toHaveLength(1);
    const data = JSON.parse(lines[0]!) as Record<string, unknown>;
    expect(data["log_id"]).toBe(logId);
    expect(data["log_type"]).toBe("heartbeat");
  });

  it("concurrent emits to the same file are written in call order", async () => {
    const logFile = join(tmpPath(), "amp_heartbeat.jsonl");
    const emitter = new HeartbeatEmitter(logFile, "aic");
    const ids = await Promise.all(Array.from({ length: 25 }, () => emitter.emit()));
    const written = readLines(logFile).map(
      (line) => (JSON.parse(line) as { log_id: string }).log_id,
    );
    expect(written).toEqual(ids);
  });
});

describe("HeartbeatEmitter.runPeriodic", () => {
  const tmpPath = useTmpPath();

  it("test_run_periodic_produces_multiple_records_then_cancels", async () => {
    const logFile = join(tmpPath(), "amp_heartbeat.jsonl");
    const emitter = new HeartbeatEmitter(logFile, "aic");
    const task = emitter.runPeriodic(0.05);
    await sleep(200);
    emitter.stop();
    await task; // stop() 后正常 resolve

    const lines = readLines(logFile);
    expect(lines.length).toBeGreaterThanOrEqual(2);
    for (const line of lines) {
      expect((JSON.parse(line) as { log_type: string }).log_type).toBe("heartbeat");
    }
  });

  it("test_run_periodic_first_record_emitted_immediately", async () => {
    const logFile = join(tmpPath(), "amp_heartbeat.jsonl");
    const emitter = new HeartbeatEmitter(logFile, "aic");
    const controller = new AbortController();
    const task = emitter.runPeriodic(60.0, { signal: controller.signal }); // 60s 间隔
    await sleep(50); // 只等 50ms
    controller.abort();
    await task;

    expect(existsSync(logFile)).toBe(true);
    expect(readLines(logFile).length).toBeGreaterThanOrEqual(1);
  });

  it("the interval timer keeps the process alive (not unref'd), like Python's run_periodic", async () => {
    const logFile = join(tmpPath(), "amp_heartbeat.jsonl");
    const emitter = new HeartbeatEmitter(logFile, "aic");
    const created: { ms: number | undefined; timer: NodeJS.Timeout }[] = [];
    const realSetTimeout = globalThis.setTimeout;
    const spy = vi.spyOn(globalThis, "setTimeout").mockImplementation(((
      handler: () => void,
      ms?: number,
    ) => {
      const timer = realSetTimeout(handler, ms);
      created.push({ ms, timer });
      return timer;
    }) as typeof setTimeout);
    try {
      const task = emitter.runPeriodic(42);
      await vi.waitFor(() => expect(created.some((t) => t.ms === 42_000)).toBe(true));
      const interval = created.find((t) => t.ms === 42_000)!;
      expect(interval.timer.hasRef()).toBe(true);
      emitter.stop();
      await task;
    } finally {
      spy.mockRestore();
    }
  });

  it("an already-aborted signal emits nothing", async () => {
    const logFile = join(tmpPath(), "amp_heartbeat.jsonl");
    await new HeartbeatEmitter(logFile, "aic").runPeriodic(0.01, { signal: AbortSignal.abort() });
    expect(existsSync(logFile)).toBe(false);
  });
});
