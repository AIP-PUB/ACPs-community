/** 对应 Python tests/amp/test_metrics_catalog.py：公共 metric 名目录。 */

import { describe, expect, it } from "vitest";

import {
  MetricFamily,
  PUBLIC_METRIC_CATALOG,
  PublicMetricMetaSchema,
  lookupPublicMetric,
} from "../../src/amp/metricsCatalog.js";

// ── 1. 目录键完整性（与 spec §5.3 字段别名集合一致）────────────────────────────

const EXPECTED_NAMES = new Set([
  // LoadMetrics
  "activeTasks",
  "queuedTasks",
  "maxActiveTasks",
  "maxQueuedTasks",
  "cpuUsage",
  "memoryUsage",
  "diskUsage",
  "networkInUsage",
  "networkOutUsage",
  // WindowMetrics（无固定 quantile）
  "successRate",
  "requestTotal",
  "requestPerSecond",
  "avgThroughputMBps",
  "peakThroughputMBps",
  "avgLatencyMs",
  // WindowMetrics（固定 quantile）
  "p50LatencyMs",
  "p75LatencyMs",
  "p80LatencyMs",
  "p90LatencyMs",
  "p95LatencyMs",
  "p99LatencyMs",
  // MetricsBody
  "uptimeSeconds",
]);

const catalog = (name: string) => PUBLIC_METRIC_CATALOG[name]!;

describe("metrics catalog", () => {
  it("test_catalog_key_set_matches_spec", () => {
    expect(new Set(Object.keys(PUBLIC_METRIC_CATALOG))).toEqual(EXPECTED_NAMES);
  });

  it("test_catalog_has_22_entries", () => {
    expect(Object.keys(PUBLIC_METRIC_CATALOG)).toHaveLength(22);
  });

  const LATENCY_QUANTILE_MAP: [string, string][] = [
    ["p50LatencyMs", "p50"],
    ["p75LatencyMs", "p75"],
    ["p80LatencyMs", "p80"],
    ["p90LatencyMs", "p90"],
    ["p95LatencyMs", "p95"],
    ["p99LatencyMs", "p99"],
  ];

  it.each(LATENCY_QUANTILE_MAP)("test_latency_fixed_quantile_is_string(%s)", (name, quantile) => {
    expect(catalog(name).fixed_quantile).toBe(quantile);
    expect(typeof catalog(name).fixed_quantile).toBe("string");
  });

  it.each(LATENCY_QUANTILE_MAP)("test_latency_family_and_needs_window(%s)", (name) => {
    expect(catalog(name).family).toBe(MetricFamily.WINDOW_RATE_LATENCY);
    expect(catalog(name).needs_window).toBe(true);
  });

  it("test_request_total_family_is_window_total", () => {
    const meta = catalog("requestTotal");
    expect(meta.family).toBe(MetricFamily.WINDOW_TOTAL);
    expect(meta.needs_window).toBe(true);
    expect(meta.fixed_quantile).toBeNull();
  });

  it("test_uptime_seconds_meta", () => {
    const meta = catalog("uptimeSeconds");
    expect(meta.needs_window).toBe(false);
    expect(meta.family).toBe(MetricFamily.MONOTONIC_UPTIME_GAUGE);
    expect(meta.fixed_quantile).toBeNull();
  });

  const WINDOW_METRICS = [
    "successRate",
    "requestTotal",
    "requestPerSecond",
    "avgThroughputMBps",
    "peakThroughputMBps",
    "avgLatencyMs",
    "p50LatencyMs",
    "p75LatencyMs",
    "p80LatencyMs",
    "p90LatencyMs",
    "p95LatencyMs",
    "p99LatencyMs",
  ].sort();
  const NON_WINDOW_METRICS = [
    "activeTasks",
    "queuedTasks",
    "maxActiveTasks",
    "maxQueuedTasks",
    "cpuUsage",
    "memoryUsage",
    "diskUsage",
    "networkInUsage",
    "networkOutUsage",
    "uptimeSeconds",
  ].sort();

  it.each(WINDOW_METRICS)("test_window_metrics_need_window(%s)", (name) => {
    expect(catalog(name).needs_window).toBe(true);
  });

  it.each(NON_WINDOW_METRICS)("test_non_window_metrics_no_window(%s)", (name) => {
    expect(catalog(name).needs_window).toBe(false);
  });

  it("test_lookup_public_metric_hit", () => {
    const meta = lookupPublicMetric("cpuUsage");
    expect(meta).not.toBeNull();
    expect(PublicMetricMetaSchema.safeParse(meta).success).toBe(true);
    expect(meta?.public_name).toBe("cpuUsage");
    expect(meta?.family).toBe(MetricFamily.RESOURCE_USAGE_GAUGE);
  });

  it("test_lookup_public_metric_miss_internal_name", () => {
    expect(lookupPublicMetric("amp_load_cpu_usage")).toBeNull();
    expect(lookupPublicMetric("amp_window_success_rate")).toBeNull();
  });

  it("test_lookup_public_metric_miss_nonexistent", () => {
    expect(lookupPublicMetric("nonexistent")).toBeNull();
    expect(lookupPublicMetric("")).toBeNull();
    // 原型链上的名字不会误命中
    expect(lookupPublicMetric("toString")).toBeNull();
    expect(lookupPublicMetric("__proto__")).toBeNull();
  });

  it.each(["activeTasks", "queuedTasks", "maxActiveTasks", "maxQueuedTasks"])(
    "test_sample_count_gauge_no_fixed_quantile(%s)",
    (name) => {
      expect(catalog(name).family).toBe(MetricFamily.SAMPLE_COUNT_GAUGE);
      expect(catalog(name).fixed_quantile).toBeNull();
    },
  );

  it.each(["cpuUsage", "memoryUsage", "diskUsage", "networkInUsage", "networkOutUsage"])(
    "test_resource_usage_gauge_no_fixed_quantile(%s)",
    (name) => {
      expect(catalog(name).family).toBe(MetricFamily.RESOURCE_USAGE_GAUGE);
      expect(catalog(name).fixed_quantile).toBeNull();
    },
  );

  it("catalog entries are frozen", () => {
    expect(Object.isFrozen(PUBLIC_METRIC_CATALOG)).toBe(true);
    expect(Object.isFrozen(catalog("cpuUsage"))).toBe(true);
  });
});
