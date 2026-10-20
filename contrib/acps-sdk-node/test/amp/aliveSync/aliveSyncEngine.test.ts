/** 对应 Python tests/amp/alive_sync/test_alive_sync_engine.py：hydrate / applySnapshot / applyDelta。 */

import { describe, expect, it } from "vitest";

import {
  AliveSyncEngine,
  DeltaDecision,
  GapDetectedError,
  ResyncRequired,
} from "../../../src/amp/aliveSync/index.js";
import {
  type AliveDeltaEnvelope,
  AliveDeltaEnvelopeSchema,
  AliveSnapshotMetaSchema,
} from "../../../src/amp/heartbeatSync.js";
import { FakeAliveSyncStore } from "./fakeStore.js";

// ── 工具函数 ─────────────────────────────────────────────────────────────────

function makeEnvelope(
  seq: number,
  { op = "upsert", kind = "enter_alive", aic = "AIC-001", shard = "hb-000" } = {},
): AliveDeltaEnvelope {
  return AliveDeltaEnvelopeSchema.parse({
    shard,
    seq: String(seq),
    type: "amp-alive-delta",
    id: `urn:amp:alive:${aic}`,
    version: String(seq),
    op,
    kind,
    payload: { aic, lastSeenAt: "2026-06-13T01:20:00Z" },
  });
}

function makeSnapshotEnv(seq: number, aic = "AIC-001", shard = "hb-000"): AliveDeltaEnvelope {
  return AliveDeltaEnvelopeSchema.parse({
    shard,
    seq: String(seq),
    type: "amp-alive-delta",
    id: `urn:amp:alive:${aic}`,
    version: String(seq),
    op: "upsert",
    kind: "snapshot",
    payload: { aic, lastSeenAt: "2026-06-13T01:00:00Z" },
  });
}

async function* agen(...items: AliveDeltaEnvelope[]): AsyncGenerator<AliveDeltaEnvelope> {
  yield* items;
}

/** Python 测试直接读写 engine._last_seen_seq / _local_version，这里经类型断言访问私有字段。 */
function internals(engine: AliveSyncEngine): {
  lastSeenSeq: Map<string, number>;
  localVersion: Map<string, number>;
} {
  return engine as unknown as {
    lastSeenSeq: Map<string, number>;
    localVersion: Map<string, number>;
  };
}

// ── hydrate ──────────────────────────────────────────────────────────────────

describe("TestHydrate", () => {
  it("test_no_checkpoints_raises_resync", async () => {
    const engine = new AliveSyncEngine(new FakeAliveSyncStore());
    await expect(engine.hydrate()).rejects.toBeInstanceOf(ResyncRequired);
    await expect(engine.hydrate()).rejects.toMatchObject({ reason: "no_checkpoints" });
  });

  it("test_hydrate_restores_memory", async () => {
    const store = new FakeAliveSyncStore();
    store.checkpoints.set("hb-000", {
      shard: "hb-000",
      lastSeenSeq: 10,
      cutoverSeq: 5,
      kafkaNextOffset: 100,
      snapshotGeneratedAt: "2026-06-13T00:00:00Z",
    });
    store.rows.set("AIC-001", {
      aic: "AIC-001",
      alive: true,
      lastSeenAt: "2026-06-13T00:00:00Z",
      version: 10,
      shard: "hb-000",
    });
    const engine = new AliveSyncEngine(store);
    await engine.hydrate();
    expect(Object.fromEntries(internals(engine).lastSeenSeq)).toEqual({ "hb-000": 10 });
    expect(Object.fromEntries(internals(engine).localVersion)).toEqual({ "AIC-001": 10 });
    expect(engine.cutoverSeqByShard()).toEqual({ "hb-000": 5 });
  });
});

// ── applySnapshot ────────────────────────────────────────────────────────────

describe("TestApplySnapshot", () => {
  it("test_snapshot_replaces_store_and_updates_memory", async () => {
    const store = new FakeAliveSyncStore();
    const meta = AliveSnapshotMetaSchema.parse({
      recordType: "snapshot-meta",
      type: "amp-alive-delta",
      cutoverSeqByShard: { "hb-000": "20" },
      generatedAt: "2026-06-13T01:00:00Z",
    });
    const engine = new AliveSyncEngine(store);
    await engine.applySnapshot(meta, agen(makeSnapshotEnv(20, "AIC-001")));

    // store 中应有 AIC-001
    expect(store.rows.get("AIC-001")?.alive).toBe(true);
    expect(store.rows.get("AIC-001")?.version).toBe(20);
    // checkpoint 应更新
    expect(store.checkpoints.get("hb-000")?.lastSeenSeq).toBe(20);
    expect(store.checkpoints.get("hb-000")?.cutoverSeq).toBe(20);
    expect(store.checkpoints.get("hb-000")?.snapshotGeneratedAt).toBe("2026-06-13T01:00:00Z");
    // 内存水位
    expect(internals(engine).lastSeenSeq.get("hb-000")).toBe(20);
    expect(internals(engine).localVersion.get("AIC-001")).toBe(20);
  });

  it("test_snapshot_memory_not_updated_on_store_error", async () => {
    const store = new FakeAliveSyncStore();
    store.replaceAliveSet = async (records) => {
      // 先消耗 records（模拟开始流式写）再抛异常
      for await (const _record of records) {
        // 消耗
      }
      throw new Error("db error");
    };
    const meta = AliveSnapshotMetaSchema.parse({
      recordType: "snapshot-meta",
      type: "amp-alive-delta",
      cutoverSeqByShard: { "hb-000": "30" },
      generatedAt: "2026-06-13T02:00:00Z",
    });
    const engine = new AliveSyncEngine(store);
    await expect(engine.applySnapshot(meta, agen(makeSnapshotEnv(30, "AIC-002")))).rejects.toThrow(
      "db error",
    );
    // 内存水位不应被推进
    expect(internals(engine).lastSeenSeq.size).toBe(0);
    expect(internals(engine).localVersion.size).toBe(0);
  });
});

// ── applyDelta ───────────────────────────────────────────────────────────────

describe("TestApplyDelta", () => {
  it("test_apply_upsert_enter_alive", async () => {
    const store = new FakeAliveSyncStore();
    const engine = new AliveSyncEngine(store);
    internals(engine).lastSeenSeq.set("hb-000", 0);

    const decision = await engine.applyDelta(makeEnvelope(1), { kafkaNextOffset: 101 });

    expect(decision).toBe(DeltaDecision.APPLY_UPSERT);
    expect(internals(engine).lastSeenSeq.get("hb-000")).toBe(1);
    expect(internals(engine).localVersion.get("AIC-001")).toBe(1);
    expect(store.rows.get("AIC-001")?.alive).toBe(true);
    expect(store.checkpoints.get("hb-000")?.kafkaNextOffset).toBe(101);
  });

  it("test_apply_delete_leave_alive", async () => {
    const store = new FakeAliveSyncStore();
    const engine = new AliveSyncEngine(store);
    internals(engine).lastSeenSeq.set("hb-000", 5);
    internals(engine).localVersion.set("AIC-001", 5);
    // 预置存活行
    store.rows.set("AIC-001", {
      aic: "AIC-001",
      alive: true,
      lastSeenAt: "2026-06-13T01:00:00Z",
      version: 5,
      shard: "hb-000",
    });

    const decision = await engine.applyDelta(
      makeEnvelope(6, { op: "delete", kind: "leave_alive" }),
    );

    expect(decision).toBe(DeltaDecision.APPLY_DELETE);
    expect(store.rows.get("AIC-001")?.alive).toBe(false);
    expect(internals(engine).localVersion.get("AIC-001")).toBe(6); // 保留+更新 version
  });

  it("test_gap_raises_gap_detected", async () => {
    const engine = new AliveSyncEngine(new FakeAliveSyncStore());
    internals(engine).lastSeenSeq.set("hb-000", 4);

    const error = await engine.applyDelta(makeEnvelope(10)).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(GapDetectedError);
    expect((error as GapDetectedError).expectedSeq).toBe(5);
    expect((error as GapDetectedError).gotSeq).toBe(10);
  });

  it("test_skip_seq_gate_duplicate", async () => {
    const store = new FakeAliveSyncStore();
    const engine = new AliveSyncEngine(store);
    internals(engine).lastSeenSeq.set("hb-000", 10);

    expect(await engine.applyDelta(makeEnvelope(5))).toBe(DeltaDecision.SKIP_SEQ_GATE);
    // 重复事件不写库
    expect(store.rows.has("AIC-001")).toBe(false);
  });

  it("test_skip_version_stale", async () => {
    const store = new FakeAliveSyncStore();
    const engine = new AliveSyncEngine(store);
    internals(engine).lastSeenSeq.set("hb-000", 4);
    internals(engine).localVersion.set("AIC-001", 10);

    expect(await engine.applyDelta(makeEnvelope(5))).toBe(DeltaDecision.SKIP_VERSION);
    expect(store.rows.has("AIC-001")).toBe(false);
    // SKIP_* 只推进内存 seq
    expect(internals(engine).lastSeenSeq.get("hb-000")).toBe(5);
  });

  it("test_leave_alive_then_enter_alive_reaccepts", async () => {
    const store = new FakeAliveSyncStore();
    const engine = new AliveSyncEngine(store);
    internals(engine).lastSeenSeq.set("hb-000", 5);
    internals(engine).localVersion.set("AIC-001", 5);
    store.rows.set("AIC-001", {
      aic: "AIC-001",
      alive: true,
      lastSeenAt: "2026-06-13T01:00:00Z",
      version: 5,
      shard: "hb-000",
    });

    // leave_alive seq=6
    await engine.applyDelta(makeEnvelope(6, { op: "delete", kind: "leave_alive" }));
    expect(store.rows.get("AIC-001")?.alive).toBe(false);

    // 旧重复 seq=4（≤ lastSeenSeq=6）被 gate 拦截
    expect(await engine.applyDelta(makeEnvelope(4))).toBe(DeltaDecision.SKIP_SEQ_GATE);

    // 新 enter_alive seq=7 → 正常接受
    expect(await engine.applyDelta(makeEnvelope(7))).toBe(DeltaDecision.APPLY_UPSERT);
    expect(store.rows.get("AIC-001")?.alive).toBe(true);
  });
});
