/** 对应 Python tests/amp/alive_sync/test_alive_sync_errors.py：异常层级与字段。 */

import { describe, expect, it } from "vitest";

import {
  AliveSyncError,
  GapDetectedError,
  ResyncRequired,
  SnapshotProtocolError,
} from "../../../src/amp/aliveSync/index.js";

describe("TestExceptionHierarchy", () => {
  it("test_gap_detected_is_alive_sync_error", () => {
    expect(new GapDetectedError("hb-000", 5, 7)).toBeInstanceOf(AliveSyncError);
  });
  it("test_resync_required_is_alive_sync_error", () => {
    expect(new ResyncRequired("checkpoint_lost")).toBeInstanceOf(AliveSyncError);
  });
  it("test_snapshot_protocol_error_is_alive_sync_error", () => {
    const err = new SnapshotProtocolError("bad meta");
    expect(err).toBeInstanceOf(AliveSyncError);
    expect(err).toBeInstanceOf(Error);
  });
  it("error names are set for logging", () => {
    expect(new AliveSyncError("x").name).toBe("AliveSyncError");
    expect(new SnapshotProtocolError("x").name).toBe("SnapshotProtocolError");
    expect(new GapDetectedError("s", 1, 3).name).toBe("GapDetectedError");
    expect(new ResyncRequired("r").name).toBe("ResyncRequired");
  });
});

describe("TestGapDetectedError", () => {
  it("test_fields", () => {
    const err = new GapDetectedError("hb-000", 5, 7);
    expect(err.shard).toBe("hb-000");
    expect(err.expectedSeq).toBe(5);
    expect(err.gotSeq).toBe(7);
  });
  it("test_str_contains_info", () => {
    expect(new GapDetectedError("hb-001", 3, 5).message).toBe(
      "Gap on shard 'hb-001': expected seq 3, got 5",
    );
  });
});

describe("TestResyncRequired", () => {
  it("test_reason_field", () => {
    expect(new ResyncRequired("offset_out_of_range").reason).toBe("offset_out_of_range");
  });
  it("test_raise_and_catch", () => {
    try {
      throw new ResyncRequired("checkpoint_lost");
    } catch (error) {
      expect(error).toBeInstanceOf(ResyncRequired);
      expect((error as ResyncRequired).reason).toBe("checkpoint_lost");
      expect((error as ResyncRequired).message).toBe("Resync required: checkpoint_lost");
    }
  });
});
