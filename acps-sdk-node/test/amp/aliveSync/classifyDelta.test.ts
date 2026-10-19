/** 对应 Python tests/amp/alive_sync/test_classify_delta.py：passesSeqGate / isGap / passesVersion / classifyDelta。 */

import { describe, expect, it } from "vitest";

import {
  DeltaDecision,
  classifyDelta,
  isGap,
  passesSeqGate,
  passesVersion,
} from "../../../src/amp/aliveSync/index.js";
import {
  type AliveDeltaEnvelope,
  AliveDeltaEnvelopeSchema,
} from "../../../src/amp/heartbeatSync.js";

describe("TestSeqGate", () => {
  it("test_seq_greater_passes", () => expect(passesSeqGate(5, 4)).toBe(true));
  it("test_seq_equal_blocked", () => expect(passesSeqGate(4, 4)).toBe(false));
  it("test_seq_less_blocked", () => expect(passesSeqGate(3, 5)).toBe(false));
});

describe("TestIsGap", () => {
  it("test_gap_detected", () => expect(isGap(7, 4)).toBe(true)); // 7 > 4+1
  it("test_no_gap_consecutive", () => expect(isGap(5, 4)).toBe(false));
  // lastSeenSeq=0 (初始化默认值) + seq=1 = 连续
  it("test_no_gap_first_event", () => expect(isGap(1, 0)).toBe(false));
});

describe("TestPassesVersion", () => {
  it("test_no_local_version_passes", () => expect(passesVersion(1, null)).toBe(true));
  it("test_greater_version_passes", () => expect(passesVersion(5, 3)).toBe(true));
  it("test_equal_version_blocked", () => expect(passesVersion(3, 3)).toBe(false));
  it("test_older_version_blocked", () => expect(passesVersion(2, 5)).toBe(false));
});

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

describe("TestClassifyDelta", () => {
  it("test_skip_seq_gate_on_duplicate", () => {
    expect(classifyDelta(makeEnvelope(3), 5, null)).toBe(DeltaDecision.SKIP_SEQ_GATE);
  });
  it("test_gap_detected", () => {
    expect(classifyDelta(makeEnvelope(10), 4, null)).toBe(DeltaDecision.GAP);
  });
  it("test_skip_version_on_stale", () => {
    expect(classifyDelta(makeEnvelope(5), 4, 6)).toBe(DeltaDecision.SKIP_VERSION);
  });
  it("test_apply_upsert_on_fresh_enter", () => {
    expect(classifyDelta(makeEnvelope(5), 4, null)).toBe(DeltaDecision.APPLY_UPSERT);
  });
  it("test_apply_delete_on_leave", () => {
    const env = makeEnvelope(6, { op: "delete", kind: "leave_alive" });
    expect(classifyDelta(env, 5, 5)).toBe(DeltaDecision.APPLY_DELETE);
  });
  it("test_first_event_seq1", () => {
    expect(classifyDelta(makeEnvelope(1), 0, null)).toBe(DeltaDecision.APPLY_UPSERT);
  });
  it("test_refresh_alive_passes_version", () => {
    const env = makeEnvelope(7, { kind: "refresh_alive" });
    expect(classifyDelta(env, 6, 5)).toBe(DeltaDecision.APPLY_UPSERT);
  });
  it("seq comparison is numeric, never lexicographic", () => {
    expect(classifyDelta(makeEnvelope(1000), 999, null)).toBe(DeltaDecision.APPLY_UPSERT);
  });
});
