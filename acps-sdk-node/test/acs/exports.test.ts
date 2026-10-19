import { describe, expect, it } from "vitest";

import * as acs from "../../src/acs/index.js";
import { loadPythonFixture } from "../fixtures.js";

describe("Python fixtures: acps_sdk.acs exports", () => {
  const { all } = loadPythonFixture<{ all: string[] }>("acs", "exports");

  it.each(all)("%s", (name) => {
    // pydantic 模型 / Union 在 Node 版对应 `XxxSchema`（运行时）+ 同名类型；带方法的模型另有同名伴生对象。
    const exported = name in acs || `${name}Schema` in acs;
    expect(exported).toBe(true);
  });
});
