/** 对应 Python tests/amp/alive_sync/test_alive_view_models.py：AliveView 与 toOutputDict()。 */

import { describe, expect, it } from "vitest";

import { AliveView } from "../../../src/amp/aliveSync/index.js";

describe("TestAliveView", () => {
  it("test_to_output_dict_alive", () => {
    const view: AliveView = { aic: "AIC-001", alive: true, lastSeenAt: "2026-06-13T01:20:00Z" };
    const d = AliveView.toOutputDict(view);
    expect(d.alive).toBe(true);
    expect(d.aliveLastSeenAt).toBe("2026-06-13T01:20:00Z");
  });

  it("test_to_output_dict_not_alive", () => {
    const d = AliveView.toOutputDict({
      aic: "AIC-001",
      alive: false,
      lastSeenAt: "2026-06-13T01:00:00Z",
    });
    expect(d.alive).toBe(false);
  });

  it("test_to_output_dict_last_seen_at_none_key_present", () => {
    const d = AliveView.toOutputDict({ aic: "AIC-001", alive: true, lastSeenAt: null });
    expect(d).toHaveProperty("aliveLastSeenAt");
    expect(d.aliveLastSeenAt).toBeNull();
    expect(JSON.stringify(d)).toBe('{"alive":true,"aliveLastSeenAt":null}');
  });

  // Python 的 test_frozen 验证 frozen dataclass 不可赋值；Node 版 AliveView 是 readonly 接口，
  // 只读性只在编译期保证：下面的函数从不执行，若字段不再是 readonly，@ts-expect-error 会让 tsc 报错。
  it("test_frozen (compile-time only: fields are readonly)", () => {
    const mutate = (view: AliveView): void => {
      // @ts-expect-error AliveView 字段只读
      view.alive = false;
    };
    expect(mutate).toBeTypeOf("function");
  });
});
