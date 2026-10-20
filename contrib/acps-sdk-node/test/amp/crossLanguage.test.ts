/**
 * 与 Python 版 acps_sdk.amp 的跨语言对照（样例由 scripts/fixtures/gen_amp.py 生成）。
 * 签名相关的对照见 crossLanguageSignatures.test.ts。
 */

import { describe, expect, it } from "vitest";

import { loadPythonFixture } from "../fixtures.js";
import { formatAccessLine } from "../../src/amp/accessEmitter.js";
import {
  AliveSyncEngine,
  type AliveRecord,
  type AliveSyncStore,
  AliveView,
  type ShardCheckpoint,
  nextLookbackSeconds,
  parseMetaLine,
  parseSnapshotRow,
  seekTimestampMs,
  snapshotRowToRecord,
} from "../../src/amp/aliveSync/index.js";
import { AliveSyncError } from "../../src/amp/aliveSync/errors.js";
import { formatAuditLine } from "../../src/amp/emitter.js";
import { formatHeartbeatLine } from "../../src/amp/heartbeatEmitter.js";
import {
  AliveDeltaEnvelopeSchema,
  AliveSnapshotMetaSchema,
  HeartbeatSyncInfoSchema,
  aicFromObjectId,
  aliveObjectId,
  seqFromStr,
  seqToStr,
  shardId,
  shardIndexFromId,
} from "../../src/amp/heartbeatSync.js";
import { canonicalize, canonicalizeToString } from "../../src/amp/jcs.js";
import { formatMessageLine } from "../../src/amp/messageEmitter.js";
import { buildReceiveBody, buildSendBody, buildSettlementBody } from "../../src/amp/messageTap.js";
import {
  KNOWN_QUANTILES,
  KNOWN_WINDOWS,
  MetricFamily,
  PUBLIC_METRIC_CATALOG,
  lookupPublicMetric,
} from "../../src/amp/metricsCatalog.js";
import { DemoMetricsSampler } from "../../src/amp/metricsDemo.js";
import { formatMetricsLine } from "../../src/amp/metricsEmitter.js";
import {
  AccessLogRecordSchema,
  AuditLogRecordSchema,
  HeartbeatLogRecordSchema,
  LogRecordSchema,
  MessageLogRecordSchema,
  MetricsLogRecordSchema,
  computeLogIdFallback,
} from "../../src/amp/models.js";
import {
  PyFloat,
  pyFloatRepr,
  pyFormatFixed,
  pyRound,
  pythonJsonDumps,
} from "../../src/internal/pyCompat.js";
import { formatSystemLine } from "../../src/amp/systemEmitter.js";
import { formatTraceparent, parseTraceparent } from "../../src/amp/traceContext.js";

type Json = unknown;
type PyResult<T = Json> = { ok: true; value: T } | { ok: false; type: string; message: string };

/** 调用 fn，转成与 Python `_error()` 相同的形状（Error ↔ ValueError）。 */
function capture<T>(fn: () => T): PyResult<T> {
  try {
    return { ok: true, value: fn() };
  } catch (error) {
    const err = error as Error;
    const type = err instanceof TypeError ? "TypeError" : "ValueError";
    return { ok: false, type, message: err.message };
  }
}

function roundTrip(value: unknown): unknown {
  return JSON.parse(JSON.stringify(value));
}

// ─── 1. NDJSON 行 ────────────────────────────────────────────────────────────

interface RecordCase {
  name: string;
  kind: "heartbeat" | "metrics" | "access" | "message" | "audit" | "system";
  input: Record<string, unknown>;
  line: string;
  resource?: Record<string, unknown>;
}

/** JSON 无法表达 NaN / ±Infinity：样例输入用占位字符串表示（与 gen_amp.py 的 NONFINITE_SENTINELS 对应）。 */
const NONFINITE_SENTINELS: Readonly<Record<string, number>> = {
  __NaN__: Number.NaN,
  __Infinity__: Infinity,
  "__-Infinity__": -Infinity,
};

function decodeNonfinite(value: unknown): unknown {
  if (typeof value === "string" && Object.hasOwn(NONFINITE_SENTINELS, value)) {
    return NONFINITE_SENTINELS[value];
  }
  if (Array.isArray(value)) {
    return value.map(decodeNonfinite);
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, decodeNonfinite(item)]),
    );
  }
  return value;
}

describe("records.json: 各日志类型的 NDJSON 行与 Python 逐字节一致", () => {
  const { cases, nonfinite_sentinels } = loadPythonFixture<{
    cases: RecordCase[];
    nonfinite_sentinels: string[];
  }>("amp", "records");

  it("非有限数占位符与生成脚本一致", () => {
    expect(Object.keys(NONFINITE_SENTINELS)).toEqual(nonfinite_sentinels);
  });

  it.each(cases.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    const input = decodeNonfinite(c.input);
    const resource = (decodeNonfinite(c.resource) as Record<string, unknown> | undefined) ?? null;
    let line: string;
    switch (c.kind) {
      case "heartbeat":
        line = formatHeartbeatLine(HeartbeatLogRecordSchema.parse(input));
        break;
      case "metrics":
        line = formatMetricsLine(MetricsLogRecordSchema.parse(input));
        break;
      case "access":
        line = formatAccessLine(AccessLogRecordSchema.parse(input), resource);
        break;
      case "message":
        line = formatMessageLine(MessageLogRecordSchema.parse(input), resource);
        break;
      case "audit":
        line = formatAuditLine(AuditLogRecordSchema.parse(input), null);
        break;
      case "system":
        line = formatSystemLine(LogRecordSchema.parse(input));
        break;
    }
    expect(line).toBe(c.line);
  });
});

// ─── 2. JCS ─────────────────────────────────────────────────────────────────

describe("jcs.json: RFC 8785 规范化与 log_id 兜底", () => {
  const fixture = loadPythonFixture<{
    cases: { input: Json; canonical: string }[];
    log_id_fallback: { raw: Record<string, unknown>; log_id: string }[];
  }>("amp", "jcs");

  it.each(fixture.cases.map((c) => [c.canonical, c] as const))("canonicalize → %s", (_c, c) => {
    expect(canonicalizeToString(c.input)).toBe(c.canonical);
    expect(canonicalize(c.input).toString("utf-8")).toBe(c.canonical);
  });

  it("computeLogIdFallback 与 Python compute_log_id_fallback 一致", () => {
    for (const c of fixture.log_id_fallback) {
      expect(computeLogIdFallback(c.raw)).toBe(c.log_id);
    }
  });
});

// ─── 3. float repr / round / format ─────────────────────────────────────────

describe("floats.json: Python float repr / round / format", () => {
  const { cases } = loadPythonFixture<{
    cases: {
      bits: string;
      repr: string;
      json: string;
      format6: string | null;
      round?: string[];
      round_int?: string;
    }[];
  }>("amp", "floats");
  const fromBits = (hex: string): number => Buffer.from(hex, "hex").readDoubleBE(0);

  it(`${cases.length} 个 double 的 repr 与 json.dumps 一致`, () => {
    for (const c of cases) {
      const value = fromBits(c.bits);
      expect(pyFloatRepr(value), c.repr).toBe(c.repr);
      expect(pythonJsonDumps(new PyFloat(value)), c.repr).toBe(c.json);
    }
  });

  it("round(x, n) 与 format(x, '.6f') 一致（含恰好居中的取偶）", () => {
    for (const c of cases) {
      const value = fromBits(c.bits);
      if (c.format6 !== null) {
        expect(pyFormatFixed(value, 6), c.repr).toBe(c.format6);
      }
      if (c.round && c.round_int !== undefined) {
        c.round.forEach((bits, ndigits) => {
          expect(Object.is(pyRound(value, ndigits), fromBits(bits)), `${c.repr} ${ndigits}`).toBe(
            true,
          );
        });
        expect(BigInt(pyRound(value)).toString(), c.repr).toBe(c.round_int);
      }
    }
  });
});

// ─── 5. traceparent ─────────────────────────────────────────────────────────

describe("trace_context.json", () => {
  const fx = loadPythonFixture<{
    format: { trace_id: string; span_id: string; sampled: boolean; header: string }[];
    parse: { header: string | null; parsed: Record<string, unknown> | null }[];
    python_lenient_parse: { header: string; python_parsed: Record<string, unknown> | null }[];
  }>("amp", "trace_context");

  it("formatTraceparent", () => {
    for (const c of fx.format) {
      expect(
        formatTraceparent({ traceId: c.trace_id, spanId: c.span_id, sampled: c.sampled }),
      ).toBe(c.header);
    }
  });

  it("parseTraceparent", () => {
    for (const c of fx.parse) {
      const ctx = parseTraceparent(c.header);
      const asPython =
        ctx === null ? null : { trace_id: ctx.traceId, span_id: ctx.spanId, sampled: ctx.sampled };
      expect(asPython, JSON.stringify(c.header)).toEqual(c.parsed);
    }
  });

  it("已知差异：Python int(x, 16) 接受的 0x/+/_/空白写法在 Node 版视为非法", () => {
    for (const c of fx.python_lenient_parse) {
      expect(c.python_parsed).not.toBeNull();
      expect(parseTraceparent(c.header)).toBeNull();
    }
  });
});

// ─── 6. heartbeat_sync ──────────────────────────────────────────────────────

describe("heartbeat_sync.json", () => {
  const fx = loadPythonFixture<{
    alive_object_id: { aic: string; object_id: string }[];
    aic_from_object_id: { input: string; result: PyResult }[];
    shard_id: { index: number; count: number; result: string }[];
    shard_index_from_id: { input: string; result: PyResult }[];
    seq_from_str: { input: string; result: PyResult }[];
    seq_to_str: { input: number; result: string }[];
    envelopes: { input: unknown; dump: unknown }[];
    sync_info: { input: unknown; dump: unknown };
    snapshot_meta: { input: unknown; dump: unknown };
  }>("amp", "heartbeat_sync");

  it("aliveObjectId / aicFromObjectId", () => {
    for (const c of fx.alive_object_id) {
      expect(aliveObjectId(c.aic)).toBe(c.object_id);
    }
    for (const c of fx.aic_from_object_id) {
      expect(capture(() => aicFromObjectId(c.input))).toEqual(c.result);
    }
  });

  it("shardId / shardIndexFromId", () => {
    for (const c of fx.shard_id) {
      expect(shardId(c.index, c.count), `${c.index}/${c.count}`).toBe(c.result);
    }
    for (const c of fx.shard_index_from_id) {
      expect(
        capture(() => shardIndexFromId(c.input)),
        c.input,
      ).toEqual(c.result);
    }
  });

  it("seqFromStr / seqToStr", () => {
    for (const c of fx.seq_from_str) {
      const result = capture(() => seqFromStr(c.input));
      expect(result, JSON.stringify(c.input)).toEqual(c.result);
      if (result.ok) {
        expect(Object.is(result.value, -0)).toBe(false);
      }
    }
    for (const c of fx.seq_to_str) {
      expect(seqToStr(c.input)).toBe(c.result);
    }
  });

  it("线缆模型解析结果与 Python model_dump(by_alias, exclude_none) 一致", () => {
    for (const c of fx.envelopes) {
      expect(roundTrip(AliveDeltaEnvelopeSchema.parse(c.input))).toEqual(c.dump);
    }
    expect(roundTrip(HeartbeatSyncInfoSchema.parse(fx.sync_info.input))).toEqual(fx.sync_info.dump);
    expect(roundTrip(AliveSnapshotMetaSchema.parse(fx.snapshot_meta.input))).toEqual(
      fx.snapshot_meta.dump,
    );
  });
});

// ─── 7. metrics catalog / demo ──────────────────────────────────────────────

describe("metrics_catalog.json", () => {
  const fx = loadPythonFixture<{
    families: Record<string, string>;
    known_windows: string[];
    known_quantiles: string[];
    catalog: [string, unknown][];
    lookups: { name: string; result: unknown }[];
  }>("amp", "metrics_catalog");

  it("枚举、窗口、分位与目录条目（含顺序）一致", () => {
    expect({ ...MetricFamily }).toEqual(fx.families);
    expect([...KNOWN_WINDOWS]).toEqual(fx.known_windows);
    expect([...KNOWN_QUANTILES]).toEqual(fx.known_quantiles);
    expect(Object.entries(PUBLIC_METRIC_CATALOG)).toEqual(fx.catalog);
  });

  it("lookupPublicMetric", () => {
    for (const c of fx.lookups) {
      expect(lookupPublicMetric(c.name), c.name).toEqual(c.result);
    }
  });
});

describe("metrics_demo.json: DemoMetricsSampler 与 Python 逐值一致（uptime 除外）", () => {
  const fx = loadPythonFixture<{
    series: { label: string; body: Record<string, unknown> }[];
    line: { input: { body: Record<string, unknown> }; line: string };
  }>("amp", "metrics_demo");

  it("同一调用序列下的合成值一致（含同 aic 跨实例共享游走状态）", () => {
    const samplers: Record<string, DemoMetricsSampler> = {
      A1: new DemoMetricsSampler("fixture-aic-A"),
      A2: new DemoMetricsSampler("fixture-aic-A"),
      B1: new DemoMetricsSampler("fixture-aic-B", 3, 5),
      C1: new DemoMetricsSampler("中文-aic-😀"),
    };
    for (const entry of fx.series) {
      const body = roundTrip(samplers[entry.label]!.sample()) as Record<string, unknown>;
      delete body["uptimeSeconds"];
      expect(body, entry.label).toEqual(entry.body);
    }
  });

  it("demo 指标记录的 NDJSON 行一致", () => {
    const body = roundTrip(new DemoMetricsSampler("fixture-aic-line").sample()) as Record<
      string,
      unknown
    >;
    delete body["uptimeSeconds"];
    const { uptimeSeconds: _uptime, ...expected } = fx.line.input.body;
    expect(body).toEqual(expected);
    expect(formatMetricsLine(MetricsLogRecordSchema.parse(fx.line.input))).toBe(fx.line.line);
  });
});

// ─── 8. message tap ─────────────────────────────────────────────────────────

describe("message_tap.json", () => {
  const fx = loadPythonFixture<{
    cases: {
      fn: "send" | "receive" | "settlement";
      event_type?: string;
      kwargs: Record<string, unknown>;
      dump: unknown;
      line: string;
    }[];
    errors: { event_type: string; result: PyResult }[];
  }>("amp", "message_tap");

  const camel = (kwargs: Record<string, unknown>): Record<string, unknown> =>
    Object.fromEntries(
      Object.entries(kwargs).map(([key, value]) => [
        key.replace(/_([a-z])/g, (_m, ch: string) => ch.toUpperCase()),
        value,
      ]),
    );

  it.each(fx.cases.map((c, i) => [`${i}:${c.fn}:${c.event_type ?? ""}`, c] as const))(
    "%s",
    (_label, c) => {
      const options = camel(c.kwargs) as never;
      const body =
        c.fn === "send"
          ? buildSendBody(options)
          : c.fn === "receive"
            ? buildReceiveBody(options)
            : buildSettlementBody(c.event_type!, options);
      expect(roundTrip(body)).toEqual(c.dump);
      const record = MessageLogRecordSchema.parse({
        log_id: "0192f3a4-5b6c-7d8e-9f01-23456789abcd",
        timestamp: "2026-10-19T08:01:12.160000Z",
        aic: "aic-tap",
        body,
      });
      expect(formatMessageLine(record, null)).toBe(c.line);
    },
  );

  it("非法结算事件类型的报错一致", () => {
    for (const c of fx.errors) {
      const result = capture(() =>
        buildSettlementBody(c.event_type, {
          system: "rabbitmq",
          exchange: "group.abc",
          virtualHost: "acps",
          queueName: "q",
          messageId: "m",
          deliveryAttempt: 1,
          latencyMs: 1.0,
        }),
      );
      expect(result.ok).toBe(false);
      expect(result).toMatchObject({
        ok: false,
        message: (c.result as { message: string }).message,
      });
    }
  });
});

// ─── 9. alive sync ──────────────────────────────────────────────────────────

class RecordingFakeStore implements AliveSyncStore {
  rows = new Map<string, AliveRecord>();
  checkpoints = new Map<string, ShardCheckpoint>();
  calls: unknown[] = [];

  async loadAliveViews(aics: readonly string[]): Promise<Record<string, AliveView>> {
    const result: Record<string, AliveView> = {};
    for (const aic of aics) {
      const row = this.rows.get(aic);
      if (row) {
        result[aic] = { aic: row.aic, alive: row.alive, lastSeenAt: row.lastSeenAt };
      }
    }
    return result;
  }

  async replaceAliveSet(
    records: AsyncIterable<AliveRecord>,
    checkpoints: ShardCheckpoint[],
  ): Promise<void> {
    this.calls.push(["replace_alive_set", checkpoints.map((cp) => ({ ...cp }))]);
    this.rows.clear();
    this.checkpoints.clear();
    for await (const record of records) {
      this.rows.set(record.aic, record);
    }
    for (const cp of checkpoints) {
      this.checkpoints.set(cp.shard, cp);
    }
  }

  async applyUpsert(
    record: AliveRecord,
    shard: string,
    lastSeenSeq: number,
    kafkaNextOffset: number | null,
  ): Promise<void> {
    this.calls.push(["apply_upsert", { ...record }, shard, lastSeenSeq, kafkaNextOffset]);
    this.rows.set(record.aic, record);
    const old = this.checkpoints.get(shard);
    this.checkpoints.set(shard, {
      shard,
      lastSeenSeq,
      cutoverSeq: old ? old.cutoverSeq : 0,
      kafkaNextOffset,
      snapshotGeneratedAt: old ? old.snapshotGeneratedAt : null,
    });
  }

  async applyDelete(
    aic: string,
    shard: string,
    lastSeenSeq: number,
    kafkaNextOffset: number | null,
    version: number,
  ): Promise<void> {
    this.calls.push(["apply_delete", aic, shard, lastSeenSeq, kafkaNextOffset, version]);
    const oldRow = this.rows.get(aic);
    if (oldRow) {
      this.rows.set(aic, { ...oldRow, alive: false, version });
    }
    const old = this.checkpoints.get(shard);
    this.checkpoints.set(shard, {
      shard,
      lastSeenSeq,
      cutoverSeq: old ? old.cutoverSeq : 0,
      kafkaNextOffset,
      snapshotGeneratedAt: old ? old.snapshotGeneratedAt : null,
    });
  }

  async loadCheckpoints(): Promise<ShardCheckpoint[]> {
    return [...this.checkpoints.values()];
  }

  async loadLocalVersions(): Promise<Record<string, number>> {
    return Object.fromEntries([...this.rows].map(([aic, row]) => [aic, row.version]));
  }

  async reset(): Promise<void> {
    this.rows.clear();
    this.checkpoints.clear();
  }
}

type ScenarioStep =
  | { op: "hydrate" }
  | { op: "snapshot"; meta: unknown; rows: unknown[] }
  | { op: "delta"; env: unknown; kafka_next_offset?: number | null }
  | { op: "seed_checkpoint"; cp: ShardCheckpoint }
  | { op: "seed_row"; row: AliveRecord };

interface EngineInternals {
  lastSeenSeq: Map<string, number>;
  localVersion: Map<string, number>;
}

async function runScenario(steps: ScenarioStep[]): Promise<Record<string, unknown>> {
  const store = new RecordingFakeStore();
  const engine = new AliveSyncEngine(store);
  const results: unknown[] = [];
  for (const step of steps) {
    try {
      if (step.op === "hydrate") {
        await engine.hydrate();
        results.push({ ok: true });
      } else if (step.op === "snapshot") {
        const meta = AliveSnapshotMetaSchema.parse(step.meta);
        const rows = step.rows.map((row) => AliveDeltaEnvelopeSchema.parse(row));
        await engine.applySnapshot(
          meta,
          (async function* () {
            yield* rows;
          })(),
        );
        results.push({ ok: true });
      } else if (step.op === "delta") {
        const decision = await engine.applyDelta(AliveDeltaEnvelopeSchema.parse(step.env), {
          kafkaNextOffset: step.kafka_next_offset ?? null,
        });
        results.push({ ok: true, decision });
      } else if (step.op === "seed_checkpoint") {
        store.checkpoints.set(step.cp.shard, step.cp);
        results.push({ ok: true });
      } else {
        store.rows.set(step.row.aic, step.row);
        results.push({ ok: true });
      }
    } catch (error) {
      if (error instanceof AliveSyncError) {
        const info: Record<string, unknown> = {
          ok: false,
          type: error.name,
          message: error.message,
        };
        const fields = error as unknown as Record<string, unknown>;
        for (const [py, ts] of [
          ["shard", "shard"],
          ["expected_seq", "expectedSeq"],
          ["got_seq", "gotSeq"],
          ["reason", "reason"],
        ] as const) {
          if (ts in fields) {
            info[py] = fields[ts];
          }
        }
        results.push(info);
      } else {
        results.push({ ok: false, type: "ValueError", message: (error as Error).message });
      }
    }
  }
  const internals = engine as unknown as EngineInternals;
  const views = await store.loadAliveViews([...[...store.rows.keys()].sort(), "UNKNOWN"]);
  return {
    results,
    calls: roundTrip(store.calls),
    rows: Object.fromEntries(store.rows),
    checkpoints: Object.fromEntries(store.checkpoints),
    engine: {
      last_seen_seq: Object.fromEntries(internals.lastSeenSeq),
      local_version: Object.fromEntries(internals.localVersion),
      cutover_seq_by_shard: engine.cutoverSeqByShard(),
    },
    views: Object.fromEntries(
      Object.entries(views).map(([aic, view]) => [aic, AliveView.toOutputDict(view)]),
    ),
  };
}

describe("alive_sync.json", () => {
  const fx = loadPythonFixture<{
    scenarios: Record<string, { steps: ScenarioStep[] } & Record<string, unknown>>;
    seek_timestamp_ms: { generated_at: string; lookback_seconds: number; result: number }[];
    seek_timestamp_ms_errors: { generated_at: string; result: PyResult }[];
    next_lookback_seconds: {
      current: number;
      factor: number;
      max_seconds: number;
      result: number;
    }[];
    parse_meta_line: { line: string; result: PyResult }[];
    parse_snapshot_row: { line: string; result: PyResult }[];
    snapshot_row_to_record: unknown;
  }>("amp", "alive_sync");

  it.each(Object.entries(fx.scenarios))(
    "引擎场景 %s：决策、store 调用与水位一致",
    async (_name, s) => {
      const { steps, ...expected } = s;
      expect(await runScenario(steps)).toEqual(expected);
    },
  );

  it("seekTimestampMs", () => {
    for (const c of fx.seek_timestamp_ms) {
      expect(seekTimestampMs(c.generated_at, c.lookback_seconds), c.generated_at).toBe(c.result);
    }
    for (const c of fx.seek_timestamp_ms_errors) {
      expect(
        capture(() => seekTimestampMs(c.generated_at, 0)),
        c.generated_at,
      ).toEqual(c.result);
    }
  });

  it("nextLookbackSeconds", () => {
    for (const c of fx.next_lookback_seconds) {
      expect(nextLookbackSeconds(c.current, { factor: c.factor, maxSeconds: c.max_seconds })).toBe(
        c.result,
      );
    }
  });

  const protocol = (fn: () => unknown): PyResult => {
    try {
      return { ok: true, value: roundTrip(fn()) };
    } catch (error) {
      const err = error as Error;
      const message = err.message.includes("校验失败") ? err.message.split(":")[0]! : err.message;
      return { ok: false, type: err.name, message };
    }
  };

  it("parseMetaLine / parseSnapshotRow 的结果与报错一致", () => {
    for (const c of fx.parse_meta_line) {
      expect(
        protocol(() => parseMetaLine(c.line)),
        c.line,
      ).toEqual(c.result);
      expect(
        protocol(() => parseMetaLine(Buffer.from(c.line))),
        c.line,
      ).toEqual(c.result);
    }
    for (const c of fx.parse_snapshot_row) {
      expect(
        protocol(() => parseSnapshotRow(c.line)),
        c.line,
      ).toEqual(c.result);
    }
  });

  it("snapshotRowToRecord", () => {
    const first = fx.parse_snapshot_row[0]!;
    expect(snapshotRowToRecord(parseSnapshotRow(first.line))).toEqual(fx.snapshot_row_to_record);
  });
});
