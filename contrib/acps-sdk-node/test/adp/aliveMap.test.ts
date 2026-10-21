/**
 * 移植自 Python tests/amp/alive_sync/test_adp_alive_map.py 中的 ADP 模型部分
 * （DiscoveryResult.aliveMap 字段 + _alive_enrichable 私有标记）。
 *
 * 同文件的 TestAmpAliveExports（AliveSyncEngine / DeltaDecision / AliveReader 的导出检查）属于 AMP 模块，
 * 不在本包 adp 测试范围内。
 */

import { describe, expect, it } from "vitest";

import {
  type DiscoveryResult as DiscoveryResultType,
  DiscoveryResponse,
  DiscoveryResult,
  DiscoveryResultSchema,
} from "../../src/adp/index.js";

/** Python `result.model_dump(by_alias=True)` 的对应物：解析后的对象就是线上形状。 */
function dumpByAlias(result: DiscoveryResultType): Record<string, unknown> {
  return JSON.parse(JSON.stringify(result)) as Record<string, unknown>;
}

describe("TestDiscoveryResultAliveMap", () => {
  it("test_alive_map_absent_by_default", () => {
    const result = DiscoveryResultSchema.parse({});
    expect(result.aliveMap).toBeUndefined();
  });

  it("test_alive_map_excluded_none_in_serialization", () => {
    // 未启用时 aliveMap 不出现在输出中（exclude_none=True），对既有调用方零破坏。
    const result = DiscoveryResultSchema.parse({});
    const d = dumpByAlias(result);
    expect("aliveMap" in d).toBe(false);
  });

  it("test_alive_map_present_when_set", () => {
    const result = DiscoveryResultSchema.parse({});
    result.aliveMap = { "AIC-001": { alive: true, aliveLastSeenAt: "2026-06-13T01:20:00Z" } };
    const d = dumpByAlias(result) as { aliveMap: Record<string, { alive: unknown }> };
    expect("aliveMap" in d).toBe(true);
    expect(d.aliveMap["AIC-001"]?.alive).toBe(true);
  });

  it("test_alive_last_seen_at_null_key_present", () => {
    // aliveLastSeenAt 为 null 时键仍存在（区别于键缺失=未知 AIC）。
    const result = DiscoveryResultSchema.parse({});
    result.aliveMap = { "AIC-001": { alive: true, aliveLastSeenAt: null } };
    const d = dumpByAlias(result) as { aliveMap: Record<string, Record<string, unknown>> };
    expect("aliveMap" in d).toBe(true);
    expect("aliveLastSeenAt" in (d.aliveMap["AIC-001"] ?? {})).toBe(true);
    expect(d.aliveMap["AIC-001"]?.aliveLastSeenAt).toBeNull();
  });

  it("test_alive_last_seen_at_null_key_present (survives parsing)", () => {
    const result = DiscoveryResultSchema.parse({
      aliveMap: { "AIC-001": { alive: true, aliveLastSeenAt: null } },
    });
    expect(result.aliveMap?.["AIC-001"]).toEqual({ alive: true, aliveLastSeenAt: null });
  });

  it("test_alive_enrichable_defaults_false", () => {
    const result = DiscoveryResultSchema.parse({});
    expect(DiscoveryResult.isAliveEnrichable(result)).toBe(false);
  });

  it("test_alive_enrichable_can_be_set", () => {
    const result = DiscoveryResultSchema.parse({});
    DiscoveryResult.setAliveEnrichable(result, true);
    expect(DiscoveryResult.isAliveEnrichable(result)).toBe(true);
    DiscoveryResult.setAliveEnrichable(result, false);
    expect(DiscoveryResult.isAliveEnrichable(result)).toBe(false);
  });

  it("test_alive_enrichable_not_in_serialization", () => {
    // _alive_enrichable 不进入 model_dump / JSON（线缆零影响）。
    const result = DiscoveryResultSchema.parse({});
    DiscoveryResult.setAliveEnrichable(result, true);
    const d = dumpByAlias(result);
    expect("_alive_enrichable" in d).toBe(false);
    expect("aliveEnrichable" in d).toBe(false);
    expect(Object.keys(result)).toEqual(["acsMap", "agents"]);
  });
});

describe("alive-enrichable flag follows object identity (discovery-server usage)", () => {
  it("is kept by DiscoveryResponse.success, like pydantic keeping the instance", () => {
    // discovery-server: result._alive_enrichable = True; return DiscoveryResponse.success(result=result)
    const result = DiscoveryResultSchema.parse({
      agents: [{ group: "g", agentSkills: [{ aic: "AIC-001", skillId: "s", ranking: 1 }] }],
    });
    DiscoveryResult.setAliveEnrichable(result, true);
    const response = DiscoveryResponse.success(result);
    expect(response.result).toBe(result);
    expect(DiscoveryResult.isAliveEnrichable(response.result as DiscoveryResultType)).toBe(true);

    // attach_alive_status 原地注入 aliveMap
    result.aliveMap = { "AIC-001": { alive: true, aliveLastSeenAt: null } };
    expect(DiscoveryResponse.toDict(response)).toEqual({
      result: {
        acsMap: {},
        agents: [{ group: "g", agentSkills: [{ aic: "AIC-001", skillId: "s", ranking: 1 }] }],
        aliveMap: { "AIC-001": { alive: true, aliveLastSeenAt: null } },
      },
    });
  });

  it("is not carried over by re-parsing (forwarded results stay non-enrichable)", () => {
    const result = DiscoveryResultSchema.parse({});
    DiscoveryResult.setAliveEnrichable(result, true);
    const forwarded = DiscoveryResponse.fromJson(DiscoveryResponse.toJson({ result }));
    expect(DiscoveryResult.isAliveEnrichable(forwarded.result as DiscoveryResultType)).toBe(false);
  });
});
