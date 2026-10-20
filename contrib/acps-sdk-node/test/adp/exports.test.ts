import { describe, expect, it } from "vitest";

import * as adp from "../../src/adp/index.js";
import { loadPythonFixture } from "../fixtures.js";

/** Python 导出名 → Node 导出名：snake_case 函数改 camelCase，类名与常量不变。 */
function toNodeName(name: string): string {
  if (/^[a-z][a-z0-9_]*$/.test(name)) {
    return name.replace(/_([a-z0-9])/g, (_, ch: string) => ch.toUpperCase());
  }
  return name;
}

describe("Python fixtures: acps_sdk.adp exports", () => {
  const { all } = loadPythonFixture<{ all: string[] }>("adp", "exports");

  it.each(all.map((name) => [name, toNodeName(name)] as const))(
    "%s is exported as %s",
    (_, nodeName) => {
      // pydantic 模型在 Node 版对应 `XxxSchema`（运行时）+ 同名类型；带方法的模型另有同名伴生对象。
      const exported = nodeName in adp || `${nodeName}Schema` in adp;
      expect(exported).toBe(true);
    },
  );
});

describe("Python fixtures: acps_sdk.adp constants", () => {
  const constants = loadPythonFixture<Record<string, unknown>>("adp", "constants");

  it.each(Object.entries(constants))("%s", (name, value) => {
    expect((adp as Record<string, unknown>)[name]).toEqual(value);
  });
});
