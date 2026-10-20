/** 对应 Python tests/amp/test_access_models.py：AccessRequest.route 字段序列化与缺省。 */

import { describe, expect, it } from "vitest";

import { AccessBodySchema, AccessRequestSchema } from "../../src/amp/models.js";

const dump = (value: unknown): Record<string, unknown> =>
  JSON.parse(JSON.stringify(value)) as Record<string, unknown>;

describe("TestAccessRequestRouteField", () => {
  it("test_route_present_in_model_dump_by_alias", () => {
    const data = dump(AccessRequestSchema.parse({ route: "/users/{id}" }));
    expect(data["route"]).toBe("/users/{id}");
  });

  it("test_route_present_in_model_dump", () => {
    const data = dump(AccessRequestSchema.parse({ route: "/users/{id}" }));
    expect(data).toEqual({ route: "/users/{id}" });
  });

  it("test_route_defaults_to_none", () => {
    expect(AccessRequestSchema.parse({ method: "GET" }).route).toBeUndefined();
  });

  it("test_route_and_url_coexist", () => {
    const req = AccessRequestSchema.parse({ url: "/users/123", route: "/users/{id}" });
    expect(req.url).toBe("/users/123");
    expect(req.route).toBe("/users/{id}");
  });

  it("test_route_field_position_after_url", () => {
    const fields = Object.keys(AccessRequestSchema.shape);
    expect(fields.indexOf("route")).toBeGreaterThan(fields.indexOf("url"));
    // 输出键顺序也遵循声明顺序（与 pydantic model_dump 一致）
    expect(
      Object.keys(AccessRequestSchema.parse({ route: "/r", url: "/u", method: "GET" })),
    ).toEqual(["method", "url", "route"]);
  });

  it("test_model_validate_with_route", () => {
    const req = AccessRequestSchema.parse({ method: "GET", url: "/items", route: "/items" });
    expect(req.route).toBe("/items");
  });

  it("test_model_validate_without_route", () => {
    expect(AccessRequestSchema.parse({ method: "GET" }).route).toBeUndefined();
  });

  it("test_access_body_with_route_in_request", () => {
    const body = AccessBodySchema.parse({
      durationMs: 12.5,
      request: { method: "POST", url: "/orders/456", route: "/orders/{id}" },
    });
    expect(body.request?.route).toBe("/orders/{id}");
  });

  it("test_access_body_without_route_in_request", () => {
    const body = AccessBodySchema.parse({
      durationMs: 5.0,
      request: { method: "GET", url: "/health" },
    });
    expect(body.request?.route).toBeUndefined();
  });

  it.each([
    "/users/{id}",
    "/api/v1/orders/{orderId}/items/{itemId}",
    "SomeRpcService/Method",
    null,
  ])("test_route_roundtrip(%s)", (route) => {
    const data = dump(AccessRequestSchema.parse({ route }));
    const restored = AccessRequestSchema.parse(data);
    expect(restored.route).toBe(route ?? undefined);
  });
});
