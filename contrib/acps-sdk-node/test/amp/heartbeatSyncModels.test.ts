/** 对应 Python tests/amp/test_heartbeat_sync_models.py：Heartbeat Sync Profile 线缆模型与编码工具。 */

import { describe, expect, it } from "vitest";

import {
  ALIVE_DELTA_TYPE,
  ALIVE_OBJECT_ID_PREFIX,
  type AliveDeltaEnvelope,
  AliveDeltaEnvelopeSchema,
  AliveSetEntrySchema,
  AliveSnapshotMetaSchema,
  HeartbeatSyncInfoSchema,
  aicFromObjectId,
  aliveObjectId,
  seqFromStr,
  seqToStr,
  shardId,
  shardIndexFromId,
} from "../../src/amp/heartbeatSync.js";

const dump = (value: unknown): Record<string, unknown> =>
  JSON.parse(JSON.stringify(value)) as Record<string, unknown>;

describe("TestAliveObjectId", () => {
  it("test_encode_roundtrip", () => {
    expect(aicFromObjectId(aliveObjectId("agent-001"))).toBe("agent-001");
  });
  it("test_encode_format", () => {
    expect(aliveObjectId("foo")).toBe(`${ALIVE_OBJECT_ID_PREFIX}foo`);
  });
  it("test_decode_valid", () => {
    expect(aicFromObjectId("urn:amp:alive:foo")).toBe("foo");
  });
  it("test_decode_invalid_prefix_raises", () => {
    expect(() => aicFromObjectId("urn:other:foo")).toThrow(/前缀不匹配/);
  });
  it("test_decode_empty_aic", () => {
    expect(aicFromObjectId("urn:amp:alive:")).toBe("");
  });
});

describe("TestShardId", () => {
  it("test_zero_index_three_digits", () => {
    expect(shardId(0, 1)).toBe("hb-000");
  });
  it("test_padding_three_digits", () => {
    expect(shardId(7, 10)).toBe("hb-007");
  });
  it("test_exactly_1000_shards_still_3_digits", () => {
    expect(shardId(0, 1000)).toBe("hb-000");
    expect(shardId(999, 1000)).toBe("hb-999");
  });
  it("test_shard_count_1001_widens_to_4", () => {
    expect(shardId(0, 1001)).toBe("hb-0000");
    expect(shardId(1000, 1001)).toBe("hb-1000");
  });
  it("test_shard_count_10000_widens_to_4", () => {
    expect(shardId(0, 10000)).toBe("hb-0000");
  });
  it("test_roundtrip", () => {
    for (const idx of [0, 5, 42, 999]) {
      expect(shardIndexFromId(shardId(idx, 1000))).toBe(idx);
    }
  });
  it("test_decode_valid", () => {
    expect(shardIndexFromId("hb-007")).toBe(7);
    expect(shardIndexFromId("hb-000")).toBe(0);
  });
  it("test_decode_no_prefix_raises", () => {
    expect(() => shardIndexFromId("007")).toThrow();
  });
  it("test_decode_non_numeric_raises", () => {
    expect(() => shardIndexFromId("hb-abc")).toThrow();
  });
  it("test_decode_empty_numeric_raises", () => {
    expect(() => shardIndexFromId("hb-")).toThrow();
  });
  it("non-integer arguments are rejected (Python 'd' format requires int)", () => {
    expect(() => shardId(1.5, 10)).toThrow(TypeError);
  });
});

describe("TestSeq", () => {
  it("test_roundtrip", () => {
    expect(seqFromStr(seqToStr(42))).toBe(42);
    expect(seqFromStr(seqToStr(0))).toBe(0);
  });
  it("test_numeric_comparison_not_lexicographic", () => {
    expect(seqFromStr("1000")).toBeGreaterThan(seqFromStr("999"));
  });
  it("test_large_value", () => {
    const large = 10 ** 15;
    expect(seqFromStr(seqToStr(large))).toBe(large);
  });
  it("test_zero_is_valid", () => {
    expect(seqFromStr("0")).toBe(0);
    expect(seqFromStr("-0")).toBe(0); // 不是 -0
  });
  it("test_empty_string_raises", () => {
    expect(() => seqFromStr("")).toThrow(/不能为空/);
  });
  it("test_non_numeric_raises", () => {
    expect(() => seqFromStr("abc")).toThrow();
  });
  it("test_negative_raises", () => {
    expect(() => seqFromStr("-1")).toThrow(/不能为负数/);
  });
  it("test_float_string_raises", () => {
    expect(() => seqFromStr("1.5")).toThrow();
  });
  it("values beyond Number.MAX_SAFE_INTEGER raise RangeError instead of losing precision", () => {
    expect(() => seqFromStr("9007199254740993")).toThrow(RangeError);
  });
});

describe("TestAliveSetEntry", () => {
  it("test_camel_case_serialization", () => {
    const data = dump(AliveSetEntrySchema.parse({ aic: "a1", lastSeenAt: "2026-01-01T00:00:00Z" }));
    expect(data).toHaveProperty("lastSeenAt");
    expect(data).not.toHaveProperty("last_seen_at");
    expect(data).toHaveProperty("aic");
  });
  it("test_source_timestamp_excluded_when_none", () => {
    const data = dump(AliveSetEntrySchema.parse({ aic: "a1", lastSeenAt: "2026-01-01T00:00:00Z" }));
    expect(data).not.toHaveProperty("sourceTimestamp");
  });
  it("test_source_timestamp_included_when_set", () => {
    const data = dump(
      AliveSetEntrySchema.parse({
        aic: "a1",
        lastSeenAt: "2026-01-01T00:00:00Z",
        sourceTimestamp: "2026-01-01T00:00:00Z",
      }),
    );
    expect(data).toHaveProperty("sourceTimestamp");
  });
  it("test_deserialization_via_camel_case", () => {
    const entry = AliveSetEntrySchema.parse({ aic: "a1", lastSeenAt: "2026-01-01T00:00:00Z" });
    expect(entry.aic).toBe("a1");
    expect(entry.lastSeenAt).toBe("2026-01-01T00:00:00Z");
  });
});

describe("TestAliveDeltaEnvelope", () => {
  const makeUpsert = (): AliveDeltaEnvelope =>
    AliveDeltaEnvelopeSchema.parse({
      shard: "hb-000",
      seq: "1",
      type: ALIVE_DELTA_TYPE,
      id: "urn:amp:alive:agent-001",
      version: "1",
      op: "upsert",
      kind: "enter_alive",
      payload: { aic: "agent-001", lastSeenAt: "2026-01-01T00:00:00Z" },
    });
  const makeDelete = (): AliveDeltaEnvelope =>
    AliveDeltaEnvelopeSchema.parse({
      shard: "hb-000",
      seq: "2",
      type: ALIVE_DELTA_TYPE,
      id: "urn:amp:alive:agent-001",
      version: "2",
      op: "delete",
      kind: "leave_alive",
      payload: null,
    });

  it("test_upsert_serialization", () => {
    const data = dump(makeUpsert());
    expect(data["op"]).toBe("upsert");
    expect(data["kind"]).toBe("enter_alive");
    expect(data).toHaveProperty("payload");
  });
  it("test_delete_no_payload_in_output", () => {
    const data = dump(makeDelete());
    expect(data["op"]).toBe("delete");
    expect(data).not.toHaveProperty("payload");
  });
  it("test_roundtrip_deserialization", () => {
    const original = makeUpsert();
    const restored = AliveDeltaEnvelopeSchema.parse(dump(original));
    expect(restored.shard).toBe(original.shard);
    expect(restored.seq).toBe(original.seq);
    expect(restored.kind).toBe(original.kind);
  });
  it("test_type_field_is_literal", () => {
    expect(makeUpsert().type).toBe(ALIVE_DELTA_TYPE);
    expect(
      AliveDeltaEnvelopeSchema.safeParse({ ...dump(makeUpsert()), type: "other" }).success,
    ).toBe(false);
  });
});

describe("TestHeartbeatSyncInfo", () => {
  const input = (seqByShard: Record<string, string>) => ({
    type: ALIVE_DELTA_TYPE,
    schemaVersion: "1",
    snapshotContentType: "application/x-ndjson",
    kafkaTopic: "amp.heartbeat.alive-delta",
    shardCount: 2,
    refreshEmitIntervalSeconds: 30,
    deltaRetentionHours: 168,
    currentPublishedSeqByShard: seqByShard,
  });

  it("test_camel_case_serialization", () => {
    const data = dump(HeartbeatSyncInfoSchema.parse(input({ "hb-000": "10", "hb-001": "20" })));
    for (const key of [
      "schemaVersion",
      "snapshotContentType",
      "kafkaTopic",
      "shardCount",
      "currentPublishedSeqByShard",
    ]) {
      expect(data).toHaveProperty(key);
    }
  });
  it("test_seq_by_shard_values_are_strings", () => {
    const info = HeartbeatSyncInfoSchema.parse(input({ "hb-000": "42" }));
    expect(typeof info.currentPublishedSeqByShard["hb-000"]).toBe("string");
    expect(HeartbeatSyncInfoSchema.safeParse(input({ "hb-000": 42 as never })).success).toBe(false);
  });
});

describe("TestAliveSnapshotMeta", () => {
  const meta = () =>
    AliveSnapshotMetaSchema.parse({
      recordType: "snapshot-meta",
      type: ALIVE_DELTA_TYPE,
      cutoverSeqByShard: { "hb-000": "5" },
      generatedAt: "2026-01-01T00:00:00Z",
    });

  it("test_camel_case_serialization", () => {
    const data = dump(meta());
    expect(data).toHaveProperty("recordType");
    expect(data).toHaveProperty("cutoverSeqByShard");
    expect(data).toHaveProperty("generatedAt");
    expect(data["recordType"]).toBe("snapshot-meta");
  });
  it("test_roundtrip_deserialization", () => {
    const restored = AliveSnapshotMetaSchema.parse(dump(meta()));
    expect(restored.cutoverSeqByShard).toEqual({ "hb-000": "5" });
    expect(restored.generatedAt).toBe("2026-01-01T00:00:00Z");
  });
});
