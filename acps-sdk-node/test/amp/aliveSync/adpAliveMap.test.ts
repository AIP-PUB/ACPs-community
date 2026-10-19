/**
 * 对应 Python tests/amp/alive_sync/test_adp_alive_map.py 中的 TestAmpAliveExports。
 *
 * 该文件的 TestDiscoveryResultAliveMap 测的是 adp 模块的 DiscoveryResult，属于 adp 移植范围，不在此处。
 */

import { describe, expect, it } from "vitest";

import * as amp from "../../../src/amp/index.js";

describe("TestAmpAliveExports", () => {
  it("test_alive_sync_engine_importable", () => {
    expect(amp.AliveSyncEngine).toBeDefined();
  });
  it("test_delta_decision_importable", () => {
    expect(amp.DeltaDecision.APPLY_UPSERT).toBeDefined();
  });
  it("test_alive_reader_importable (interface: compile-time only; AliveView companion is runtime)", () => {
    const reader: amp.AliveReader = { loadAliveViews: async () => ({}) };
    expect(reader).toBeDefined();
    expect(amp.AliveView.toOutputDict).toBeTypeOf("function");
  });
});
