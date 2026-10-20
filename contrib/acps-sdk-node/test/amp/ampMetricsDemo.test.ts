/** 对应 Python tests/amp/test_amp_metrics_demo.py：DemoMetricsSampler。 */

import { describe, expect, it } from "vitest";

import { DemoMetricsSampler } from "../../src/amp/metricsDemo.js";
import { MetricsBodySchema } from "../../src/amp/models.js";

const sampler = (): DemoMetricsSampler => new DemoMetricsSampler("test-aic-demo-001");

describe("DemoMetricsSampler", () => {
  it("test_sample_returns_metrics_body", () => {
    expect(MetricsBodySchema.safeParse(sampler().sample()).success).toBe(true);
  });

  it("test_sample_uptime_nonnegative", () => {
    const result = sampler().sample();
    expect(result.uptimeSeconds).toBeDefined();
    expect(result.uptimeSeconds!).toBeGreaterThanOrEqual(0.0);
  });

  it("test_sample_load_metrics_present", () => {
    expect(sampler().sample().loadMetrics).toBeDefined();
  });

  it("test_sample_window_metrics_present", () => {
    const result = sampler().sample();
    expect(result.windowMetrics).toBeDefined();
    expect(result.windowMetrics!.length).toBeGreaterThanOrEqual(1);
  });

  it("test_sample_quantile_order", () => {
    const s = sampler();
    for (let i = 0; i < 10; i += 1) {
      for (const w of s.sample().windowMetrics ?? []) {
        if (w.p50LatencyMs !== undefined && w.p95LatencyMs !== undefined) {
          expect(w.p50LatencyMs).toBeLessThanOrEqual(w.p95LatencyMs);
        }
        if (w.p95LatencyMs !== undefined && w.p99LatencyMs !== undefined) {
          expect(w.p95LatencyMs).toBeLessThanOrEqual(w.p99LatencyMs);
        }
      }
    }
  });

  it("test_sample_success_rate_in_range", () => {
    const s = sampler();
    for (let i = 0; i < 20; i += 1) {
      for (const w of s.sample().windowMetrics ?? []) {
        expect(w.successRate).toBeGreaterThanOrEqual(0.0);
        expect(w.successRate).toBeLessThanOrEqual(100.0);
      }
    }
  });

  it("test_sample_load_counts_nonnegative", () => {
    const s = sampler();
    for (let i = 0; i < 10; i += 1) {
      const load = s.sample().loadMetrics!;
      expect(load.activeTasks).toBeGreaterThanOrEqual(0);
      expect(load.queuedTasks).toBeGreaterThanOrEqual(0);
      expect(Number.isInteger(load.activeTasks)).toBe(true);
    }
  });

  it("test_sample_continuity_same_aic", () => {
    const s = sampler();
    for (let i = 0; i < 50; i += 1) {
      const cpu = s.sample().loadMetrics?.cpuUsage;
      if (cpu !== undefined) {
        expect(cpu).toBeGreaterThanOrEqual(0.0);
        expect(cpu).toBeLessThanOrEqual(100.0);
      }
    }
  });

  it("test_sample_different_aics_differ", () => {
    const results = new Map<string, number | undefined>();
    for (const aic of ["aic-A", "aic-B", "aic-C", "aic-D", "aic-E"]) {
      results.set(aic, new DemoMetricsSampler(aic).sample().loadMetrics?.cpuUsage);
    }
    // 至少 2 个 aic 取值不同
    const unique = new Set([...results.values()].filter((v) => v !== undefined));
    expect(unique.size).toBeGreaterThanOrEqual(2);
  });
});
