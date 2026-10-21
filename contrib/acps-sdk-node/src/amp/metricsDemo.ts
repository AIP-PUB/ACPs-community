/**
 * AMP Metrics 合成采样器（demo/联调专用，对应 Python `acps_sdk.amp.metrics_demo`）。
 *
 * **警告**：本模块仅供 demo 与本地联调使用，请勿用于生产环境。
 * 生产环境应由 Agent 业务层实现 SampleProvider 协议，测量真实负载数据。
 *
 * DemoMetricsSampler 以 aic 为种子做随机游走（确保不同 aic 取值各异、同 aic 跨次调用值连续），
 * 合成以下字段：
 * - uptimeSeconds：单调递增
 * - loadMetrics：activeTasks / queuedTasks / cpuUsage / memoryUsage
 * - windowMetrics：多窗口（PT1M / PT5M / PT15M）× successRate / RPS / requestTotal / p50/p95/p99 latency
 *
 * 合成规则：
 * - p50 <= p95 <= p99（分位顺序不变）
 * - successRate 主要落 [90, 100]（偶有小毛刺）
 * - active/queued 为非负整数，不超过 max 上限
 *
 * 随机游走逐位复刻 Python 实现（`random.Random(<str>)` 的 MT19937 与字符串播种、`round()` 的银行家舍入、
 * `format(x, ".6f")`），同一 aic、同一调用序列下合成值与 Python 版完全相同（uptime 除外）。
 */

import { createHash } from "node:crypto";
import { performance } from "node:perf_hooks";

import { type MetricsBody, MetricsBodySchema, type WindowMetrics } from "./models.js";
import { pyFormatFixed, pyRound } from "../internal/pyCompat.js";
import type { SampleProvider } from "./metricsEmitter.js";

const WINDOWS = ["PT1M", "PT5M", "PT15M"] as const;

/** 各 aic 的状态缓存（游走基点），与 Python 版相同为模块级全局、跨实例共享。 */
const state = new Map<string, Map<string, number>>();

// ── Python random.Random（MT19937）────────────────────────────────────────────

const MT_N = 624;
const MT_M = 397;

/** CPython `random.Random` 的最小复刻：仅支持字符串种子（version 2）与 random() / uniform()。 */
class PythonRandom {
  private readonly mt = new Uint32Array(MT_N);
  private index = MT_N + 1;

  constructor(seed: string) {
    this.initByArray(PythonRandom.seedKey(seed));
  }

  /**
   * `random.seed(str)`（version 2）：`int.from_bytes(s + sha512(s).digest(), "big")`，
   * 再按 CPython `random_seed` 从低位起拆成 32 位字作为 init_by_array 的 key。
   */
  private static seedKey(seed: string): Uint32Array {
    const encoded = Buffer.from(seed, "utf-8");
    const digest = createHash("sha512").update(encoded).digest();
    let bytes: Uint8Array = Buffer.concat([encoded, digest]);
    let start = 0;
    while (start < bytes.length && bytes[start] === 0) {
      start += 1;
    }
    bytes = bytes.subarray(start);
    const words = Math.max(1, Math.ceil(bytes.length / 4));
    const key = new Uint32Array(words);
    for (let i = 0; i < words; i += 1) {
      const end = bytes.length - i * 4;
      let word = 0;
      for (let b = Math.max(0, end - 4); b < end; b += 1) {
        word = (word * 256 + (bytes[b] ?? 0)) >>> 0;
      }
      key[i] = word;
    }
    return key;
  }

  private initGenrand(seed: number): void {
    const mt = this.mt;
    mt[0] = seed >>> 0;
    for (let i = 1; i < MT_N; i += 1) {
      const prev = (mt[i - 1] ?? 0) ^ ((mt[i - 1] ?? 0) >>> 30);
      mt[i] = (Math.imul(1812433253, prev) + i) >>> 0;
    }
    this.index = MT_N;
  }

  private initByArray(key: Uint32Array): void {
    const mt = this.mt;
    this.initGenrand(19650218);
    let i = 1;
    let j = 0;
    for (let k = Math.max(MT_N, key.length); k > 0; k -= 1) {
      const prev = (mt[i - 1] ?? 0) ^ ((mt[i - 1] ?? 0) >>> 30);
      mt[i] = (((mt[i] ?? 0) ^ Math.imul(prev, 1664525)) + (key[j] ?? 0) + j) >>> 0;
      i += 1;
      j += 1;
      if (i >= MT_N) {
        mt[0] = mt[MT_N - 1] ?? 0;
        i = 1;
      }
      if (j >= key.length) {
        j = 0;
      }
    }
    for (let k = MT_N - 1; k > 0; k -= 1) {
      const prev = (mt[i - 1] ?? 0) ^ ((mt[i - 1] ?? 0) >>> 30);
      mt[i] = (((mt[i] ?? 0) ^ Math.imul(prev, 1566083941)) - i) >>> 0;
      i += 1;
      if (i >= MT_N) {
        mt[0] = mt[MT_N - 1] ?? 0;
        i = 1;
      }
    }
    mt[0] = 0x80000000;
  }

  private twist(): void {
    const mt = this.mt;
    for (let kk = 0; kk < MT_N; kk += 1) {
      const y = ((mt[kk] ?? 0) & 0x80000000) | ((mt[(kk + 1) % MT_N] ?? 0) & 0x7fffffff);
      mt[kk] = (mt[(kk + MT_M) % MT_N] ?? 0) ^ (y >>> 1) ^ (y & 1 ? 0x9908b0df : 0);
    }
    this.index = 0;
  }

  private nextUint32(): number {
    if (this.index >= MT_N) {
      this.twist();
    }
    let y = this.mt[this.index] ?? 0;
    this.index += 1;
    y ^= y >>> 11;
    y ^= (y << 7) & 0x9d2c5680;
    y ^= (y << 15) & 0xefc60000;
    y ^= y >>> 18;
    return y >>> 0;
  }

  /** `random.random()`：53 位精度的 [0, 1) 浮点数。 */
  random(): number {
    const a = this.nextUint32() >>> 5;
    const b = this.nextUint32() >>> 6;
    return (a * 67108864 + b) * (1 / 9007199254740992);
  }

  /** `random.uniform(a, b)`。 */
  uniform(a: number, b: number): number {
    return a + (b - a) * this.random();
  }
}

// ── 游走 ─────────────────────────────────────────────────────────────────────

/** 用 aic + key 生成 [0, 1) 的确定性伪随机数。 */
function seedFloat(aic: string, key: string): number {
  const digest = createHash("sha256").update(`${aic}:${key}`, "utf-8").digest("hex");
  return parseInt(digest.slice(0, 8), 16) / 0xffffffff;
}

/**
 * 在 [lo, hi] 内对 walkState[key] 做随机游走，step 为最大单次变化量。
 * 首次调用以 key 的确定性伪随机数初始化。
 */
function walk(
  walkState: Map<string, number>,
  key: string,
  lo: number,
  hi: number,
  step: number,
): number {
  let current = walkState.get(key);
  if (current === undefined) {
    current = lo + seedFloat("init", key) * (hi - lo);
  }
  const rng = new PythonRandom(`${pyFormatFixed(current, 6)}:${key}`);
  const delta = rng.uniform(-step, step);
  const next = Math.max(lo, Math.min(hi, current + delta));
  walkState.set(key, next);
  return next;
}

/** 将 ISO 8601 Duration 转换为秒（仅支持 PTxM/PTxH/PTxS 简单格式）。 */
function windowSeconds(isoDuration: string): number {
  if (isoDuration.startsWith("PT") && isoDuration.endsWith("M")) {
    return Number(isoDuration.slice(2, -1)) * 60.0;
  }
  if (isoDuration.startsWith("PT") && isoDuration.endsWith("H")) {
    return Number(isoDuration.slice(2, -1)) * 3600.0;
  }
  if (isoDuration.startsWith("PT") && isoDuration.endsWith("S")) {
    return Number(isoDuration.slice(2, -1));
  }
  return 300.0; // fallback: 5 min
}

/**
 * 指标合成采样器（demo/联调专用）。
 */
export class DemoMetricsSampler implements SampleProvider {
  private readonly aic: string;
  private readonly maxActive: number;
  private readonly maxQueued: number;
  private readonly startTime: number;
  private callCount = 0;

  /**
   * @param aic 发射方 Agent Instance Context 标识符（用于派生随机种子）
   * @param maxActive 最大 activeTasks 上限（默认 10）
   * @param maxQueued 最大 queuedTasks 上限（默认 20）
   */
  constructor(aic: string, maxActive = 10, maxQueued = 20) {
    this.aic = aic;
    this.maxActive = maxActive;
    this.maxQueued = maxQueued;
    this.startTime = performance.now();
  }

  /** 生成一份合成 MetricsBody。 */
  sample(): MetricsBody {
    this.callCount += 1;
    let walkState = state.get(this.aic);
    if (walkState === undefined) {
      walkState = new Map();
      state.set(this.aic, walkState);
    }
    const aic = this.aic;

    const uptime = pyRound((performance.now() - this.startTime) / 1000, 3);

    // LoadMetrics
    const active = pyRound(walk(walkState, `${aic}:active`, 0, this.maxActive, 2));
    const queued = pyRound(walk(walkState, `${aic}:queued`, 0, this.maxQueued, 3));
    const cpu = pyRound(walk(walkState, `${aic}:cpu`, 0.0, 100.0, 5.0), 1);
    const mem = pyRound(walk(walkState, `${aic}:mem`, 10.0, 90.0, 3.0), 1);

    // WindowMetrics
    const windows: WindowMetrics[] = [];
    for (const win of WINDOWS) {
      const sr = pyRound(walk(walkState, `${aic}:${win}:sr`, 85.0, 100.0, 2.0), 2);
      const rps = pyRound(walk(walkState, `${aic}:${win}:rps`, 0.1, 50.0, 3.0), 2);
      const total = pyRound(rps * windowSeconds(win));
      const p50 = pyRound(walk(walkState, `${aic}:${win}:p50`, 5.0, 100.0, 5.0), 1);
      // Ensure p50 <= p95 <= p99 by building on each previous
      const p95 = pyRound(
        Math.max(p50, walk(walkState, `${aic}:${win}:p95`, p50, p50 * 3.0, 10.0)),
        1,
      );
      const p99 = pyRound(
        Math.max(p95, walk(walkState, `${aic}:${win}:p99`, p95, p95 * 2.0, 15.0)),
        1,
      );
      windows.push({
        window: win,
        successRate: sr,
        requestTotal: total,
        requestPerSecond: rps,
        p50LatencyMs: p50,
        p95LatencyMs: p95,
        p99LatencyMs: p99,
      });
    }

    return MetricsBodySchema.parse({
      uptimeSeconds: uptime,
      loadMetrics: {
        activeTasks: active,
        queuedTasks: queued,
        maxActiveTasks: this.maxActive,
        maxQueuedTasks: this.maxQueued,
        cpuUsage: cpu,
        memoryUsage: mem,
      },
      windowMetrics: windows,
    });
  }
}
