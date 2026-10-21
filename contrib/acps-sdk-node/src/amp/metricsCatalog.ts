/**
 * AMP Metrics 公共指标名目录（spec §5.3 字段名 / 设计 §4.1.1 对外列）。
 *
 * 对应 Python `acps_sdk.amp.metrics_catalog`。这是发射端、查询客户端与 Provider 三方共享的语义契约：
 * - 合法的公共 metric 取值（camelCase）
 * - 所属指标族（MetricFamily）
 * - 是否必须携带 window 参数
 * - 是否隐含固定 quantile
 *
 * 全部条目由 spec §5.3 LoadMetrics / WindowMetrics 字段 alias 与 MetricsBody 字段派生，
 * 入 SDK 防止三方漂移（C-METRIC-QUERY-6）。
 */

import { z } from "zod";

// ── 指标族（设计 §6.0.1.E；时间聚合兼容矩阵以此为键） ──────────────────────────

/** AMP 指标族。 */
export const MetricFamily = {
  /** 采样计数 Gauge（activeTasks / queuedTasks / max* 等瞬时计数）。 */
  SAMPLE_COUNT_GAUGE: "sample_count_gauge",
  /** 资源利用率 Gauge（cpu/memory/disk/network 使用率 0-100）。 */
  RESOURCE_USAGE_GAUGE: "resource_usage_gauge",
  /** 窗口 summary 速率 / 时延类（successRate / *LatencyMs / requestPerSecond 等）。 */
  WINDOW_RATE_LATENCY: "window_rate_latency",
  /** 窗口 summary 总量类（requestTotal）。 */
  WINDOW_TOTAL: "window_total",
  /** 单调递增运行时长 Gauge（uptimeSeconds）。 */
  MONOTONIC_UPTIME_GAUGE: "monotonic_uptime_gauge",
} as const;
export type MetricFamily = (typeof MetricFamily)[keyof typeof MetricFamily];
export const MetricFamilySchema = z.enum(
  Object.values(MetricFamily) as [MetricFamily, ...MetricFamily[]],
);

// ── 已知窗口与分位枚举（仅作校验/文档参考，取值由上报数据决定） ────────────────────

/** AMP spec §5.3 WindowMetrics 标准 window 枚举（ISO 8601 Duration）。 */
export const KNOWN_WINDOWS = ["PT1M", "PT5M", "PT15M", "PT1H"] as const;

/** AMP Metrics 标准分位名（字符串，与 TSDB 标签值一致）。 */
export const KNOWN_QUANTILES = ["p50", "p75", "p80", "p90", "p95", "p99"] as const;

// ── 单个公共 metric 的语义属性 ─────────────────────────────────────────────────

/** 单个公共 metric 的对外语义属性（目录条目）。字段名与 Python 模型相同（无 alias）。 */
export const PublicMetricMetaSchema = z.object({
  /** camelCase 业务名，如 "cpuUsage" / "p95LatencyMs"。 */
  public_name: z.string(),
  /** 指标族，决定合法聚合算子（avg / sum / max / last 等）。 */
  family: MetricFamilySchema,
  /** 是否必须由 filter.window / rankings.window 指定。窗口型指标为 true。 */
  needs_window: z.boolean(),
  /** p*LatencyMs 隐含固定 quantile 字符串（"p50"…"p99"），其余为 null。 */
  fixed_quantile: z.string().nullable(),
});
export type PublicMetricMeta = z.output<typeof PublicMetricMetaSchema>;

function meta(
  publicName: string,
  family: MetricFamily,
  needsWindow: boolean,
  fixedQuantile: string | null = null,
): PublicMetricMeta {
  return Object.freeze({
    public_name: publicName,
    family,
    needs_window: needsWindow,
    fixed_quantile: fixedQuantile,
  });
}

// ── §4.1.1 公共指标目录（22 条目，不含内部 amp_* series 名） ─────────────────────

export const PUBLIC_METRIC_CATALOG: Readonly<Record<string, PublicMetricMeta>> = Object.freeze({
  // ── LoadMetrics：采样计数 Gauge ──────────────────────────────────────────
  activeTasks: meta("activeTasks", MetricFamily.SAMPLE_COUNT_GAUGE, false),
  queuedTasks: meta("queuedTasks", MetricFamily.SAMPLE_COUNT_GAUGE, false),
  maxActiveTasks: meta("maxActiveTasks", MetricFamily.SAMPLE_COUNT_GAUGE, false),
  maxQueuedTasks: meta("maxQueuedTasks", MetricFamily.SAMPLE_COUNT_GAUGE, false),
  // ── LoadMetrics：资源利用率 Gauge ────────────────────────────────────────
  cpuUsage: meta("cpuUsage", MetricFamily.RESOURCE_USAGE_GAUGE, false),
  memoryUsage: meta("memoryUsage", MetricFamily.RESOURCE_USAGE_GAUGE, false),
  diskUsage: meta("diskUsage", MetricFamily.RESOURCE_USAGE_GAUGE, false),
  networkInUsage: meta("networkInUsage", MetricFamily.RESOURCE_USAGE_GAUGE, false),
  networkOutUsage: meta("networkOutUsage", MetricFamily.RESOURCE_USAGE_GAUGE, false),
  // ── WindowMetrics：速率 / 时延类（无固定 quantile）──────────────────────
  successRate: meta("successRate", MetricFamily.WINDOW_RATE_LATENCY, true),
  requestPerSecond: meta("requestPerSecond", MetricFamily.WINDOW_RATE_LATENCY, true),
  avgThroughputMBps: meta("avgThroughputMBps", MetricFamily.WINDOW_RATE_LATENCY, true),
  peakThroughputMBps: meta("peakThroughputMBps", MetricFamily.WINDOW_RATE_LATENCY, true),
  avgLatencyMs: meta("avgLatencyMs", MetricFamily.WINDOW_RATE_LATENCY, true),
  // ── WindowMetrics：总量类 ────────────────────────────────────────────────
  requestTotal: meta("requestTotal", MetricFamily.WINDOW_TOTAL, true),
  // ── WindowMetrics：带固定 quantile 的时延 ────────────────────────────────
  p50LatencyMs: meta("p50LatencyMs", MetricFamily.WINDOW_RATE_LATENCY, true, "p50"),
  p75LatencyMs: meta("p75LatencyMs", MetricFamily.WINDOW_RATE_LATENCY, true, "p75"),
  p80LatencyMs: meta("p80LatencyMs", MetricFamily.WINDOW_RATE_LATENCY, true, "p80"),
  p90LatencyMs: meta("p90LatencyMs", MetricFamily.WINDOW_RATE_LATENCY, true, "p90"),
  p95LatencyMs: meta("p95LatencyMs", MetricFamily.WINDOW_RATE_LATENCY, true, "p95"),
  p99LatencyMs: meta("p99LatencyMs", MetricFamily.WINDOW_RATE_LATENCY, true, "p99"),
  // ── MetricsBody：单调运行时长 Gauge ─────────────────────────────────────
  uptimeSeconds: meta("uptimeSeconds", MetricFamily.MONOTONIC_UPTIME_GAUGE, false),
});

/**
 * 按公共名查询 metric 元信息。
 *
 * @param name 公共 camelCase 业务名（如 "cpuUsage"、"p95LatencyMs"）
 * @returns 命中时返回 PublicMetricMeta；未命中（包括内部 amp_* 名，C-METRIC-QUERY-6）返回 null
 */
export function lookupPublicMetric(name: string): PublicMetricMeta | null {
  return Object.hasOwn(PUBLIC_METRIC_CATALOG, name) ? (PUBLIC_METRIC_CATALOG[name] ?? null) : null;
}
