/** 对应 Python tests/amp/test_amp_metrics_emitter.py：MetricsEmitter。 */

import { existsSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { MetricsEmitter, type SampleProvider } from "../../src/amp/metricsEmitter.js";
import type { MetricsBody } from "../../src/amp/models.js";
import {
  readLines,
  readSingleRecord,
  sleep,
  unwritablePath,
  useLogCapture,
  useTmpPath,
} from "./helpers.js";

/** 构造一个返回固定 MetricsBody 的合成采样器。 */
function makeSampler(uptime = 100.0, body?: MetricsBody): SampleProvider {
  return { sample: () => body ?? { uptimeSeconds: uptime } };
}

describe("MetricsEmitter.emitSync", () => {
  const tmpPath = useTmpPath();
  const logs = useLogCapture();
  const tmpLog = (): string => join(tmpPath(), "logs", "amp_metrics.jsonl");

  it("test_emit_sync_writes_valid_ndjson", () => {
    const resource = { "service.name": "svc-test", "deployment.environment.name": "dev" };
    const emitter = new MetricsEmitter(tmpLog(), "aic-001", makeSampler(123.0), { resource });

    const logId = emitter.emitSync();

    expect(existsSync(tmpLog())).toBe(true);
    const lines = readLines(tmpLog());
    expect(lines).toHaveLength(1);
    const record = JSON.parse(lines[0]!) as Record<string, unknown>;
    expect(record["log_type"]).toBe("metrics");
    expect(record["aic"]).toBe("aic-001");
    expect(record["log_id"]).toBe(logId);
    expect(record).toHaveProperty("timestamp");
    expect(record).toHaveProperty("body");
    expect(record["resource"]).toEqual(resource); // D-6 resource 字段存在
    expect(lines[0]).toContain('"uptimeSeconds": 123.0');
  });

  it("test_emit_sync_body_uses_camel_case_aliases", () => {
    const sampler = makeSampler(0, {
      uptimeSeconds: 50.0,
      loadMetrics: { activeTasks: 2, queuedTasks: 3 },
    });
    new MetricsEmitter(tmpLog(), "aic-001", sampler).emitSync();

    const body = readSingleRecord(tmpLog())["body"] as Record<string, Record<string, unknown>>;
    expect(body).toHaveProperty("uptimeSeconds");
    expect(body).toHaveProperty("loadMetrics");
    expect(body["loadMetrics"]).toHaveProperty("activeTasks");
  });

  it("test_emit_sync_exclude_none", () => {
    new MetricsEmitter(tmpLog(), "aic-001", makeSampler()).emitSync();
    expect(readSingleRecord(tmpLog())).not.toHaveProperty("resource"); // resource 为 null 时不出现
  });

  it("test_emit_sync_write_failure_only_warns", () => {
    const badPath = unwritablePath(tmpPath(), "amp_metrics.jsonl");
    const emitter = new MetricsEmitter(badPath, "aic-001", makeSampler());
    expect(() => emitter.emitSync()).not.toThrow(); // 不应抛出
    expect(
      logs.some((r) => r.message.includes("写入失败") || r.message.includes("MetricsEmitter")),
    ).toBe(true);
  });

  it("sampler errors propagate (only write failures are swallowed)", () => {
    const failing: SampleProvider = {
      sample: () => {
        throw new Error("sampler broke");
      },
    };
    expect(() => new MetricsEmitter(tmpLog(), "aic", failing).emitSync()).toThrow("sampler broke");
  });
});

describe("MetricsEmitter.emit / runPeriodic", () => {
  const tmpPath = useTmpPath();
  const tmpLog = (): string => join(tmpPath(), "logs", "amp_metrics.jsonl");

  it("test_emit_async_writes_line", async () => {
    await new MetricsEmitter(tmpLog(), "aic-002", makeSampler()).emit();
    expect(existsSync(tmpLog())).toBe(true);
    expect(readLines(tmpLog())).toHaveLength(1);
  });

  it("test_run_periodic_emits_immediately_then_interval", async () => {
    const emitter = new MetricsEmitter(tmpLog(), "aic-003", makeSampler());
    const task = emitter.runPeriodic(0.05);
    await sleep(120); // 够发 2-3 条
    emitter.stop();
    await task; // 正常退出

    expect(readLines(tmpLog()).length).toBeGreaterThanOrEqual(2); // 至少 2 条
  });

  it("non-finite float values are accepted and written like Python (NaN / Infinity)", async () => {
    const sampler: SampleProvider = {
      sample: () => ({
        uptimeSeconds: Infinity,
        loadMetrics: { activeTasks: 0, queuedTasks: 0, cpuUsage: 0 / 0 },
      }),
    };
    const emitter = new MetricsEmitter(tmpLog(), "aic", sampler);
    const task = emitter.runPeriodic(0.01);
    await sleep(40);
    emitter.stop();
    await task; // 不会因 NaN 而以 ZodError 结束
    const lines = readLines(tmpLog());
    expect(lines.length).toBeGreaterThanOrEqual(2);
    expect(lines[0]).toContain(
      '"body": {"uptimeSeconds": Infinity, "loadMetrics": {"activeTasks": 0, "queuedTasks": 0, "cpuUsage": NaN}}',
    );
  });

  it("runPeriodic rejects when the sampler throws", async () => {
    const failing: SampleProvider = {
      sample: () => {
        throw new Error("sampler broke");
      },
    };
    await expect(new MetricsEmitter(tmpLog(), "aic", failing).runPeriodic(0.01)).rejects.toThrow(
      "sampler broke",
    );
  });
});
