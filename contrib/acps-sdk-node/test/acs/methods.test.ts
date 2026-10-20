import { fileURLToPath, pathToFileURL } from "node:url";

import { describe, expect, it } from "vitest";
import { ZodError } from "zod";

import { AgentCapabilitySpec, AgentCapabilitySpecSchema } from "../../src/acs/index.js";
import { loadPythonFixture } from "../fixtures.js";

interface PyError {
  loc: (string | number)[];
  type: string;
  msg: string;
}

interface MethodsFixture {
  serialization: {
    input: Record<string, unknown>;
    toDict: unknown;
    toJson: string;
    fromJsonOfToJson: unknown;
  };
  fromFile: {
    path: string;
    result?: unknown;
    error?:
      | { kind: "FileNotFoundError" | "ValueError"; message: string }
      | { kind: "ValidationError" | "InvalidJSON"; errors: PyError[] };
  }[];
}

const fixture = loadPythonFixture<MethodsFixture>("acs", "methods");

/** 生成脚本里的路径相对包根目录书写。 */
const PACKAGE_ROOT = fileURLToPath(new URL("../../", import.meta.url));

describe("Python fixtures: AgentCapabilitySpec to_dict / to_json / from_json", () => {
  const { serialization } = fixture;

  it("toDict / toJson match Python", () => {
    const spec = AgentCapabilitySpec.fromDict(serialization.input);
    expect(AgentCapabilitySpec.toDict(spec)).toEqual(serialization.toDict);
    expect(AgentCapabilitySpec.toJson(spec)).toBe(serialization.toJson);
  });

  it("fromJson(toJson()) round-trips", () => {
    const spec = AgentCapabilitySpec.fromDict(serialization.input);
    const again = AgentCapabilitySpec.fromJson(AgentCapabilitySpec.toJson(spec));
    expect(AgentCapabilitySpec.toDict(again)).toEqual(serialization.fromJsonOfToJson);
    expect(again).toEqual(spec);
  });

  it("toDict returns a deep copy", () => {
    const spec = AgentCapabilitySpecSchema.parse(serialization.input);
    const dict = AgentCapabilitySpec.toDict(spec) as { skills: { tags: string[] }[] };
    dict.skills[0]?.tags.push("新增");
    expect(spec.skills[0]?.tags).not.toContain("新增");
  });
});

describe("Python fixtures: AgentCapabilitySpec.from_file", () => {
  it.each(fixture.fromFile.map((item) => [item.path, item] as const))("%s", (relPath, item) => {
    const absPath = `${PACKAGE_ROOT}${relPath}`;
    const load = () => AgentCapabilitySpec.fromFile(absPath);
    const error = item.error;
    if (!error) {
      expect(AgentCapabilitySpec.toDict(load())).toEqual(item.result);
      return;
    }
    switch (error.kind) {
      case "FileNotFoundError":
        expect(load).toThrow(error.message.replace(relPath, absPath));
        expect(load).toThrow(expect.objectContaining({ code: "ENOENT" }));
        break;
      case "ValueError":
        expect(load).toThrow(error.message);
        break;
      case "ValidationError":
        expect(load).toThrow(ZodError);
        try {
          load();
        } catch (thrown) {
          const paths = (thrown as ZodError).issues.map((issue) => JSON.stringify(issue.path));
          expect([...new Set(paths)].sort()).toEqual(
            [...new Set(error.errors.map((e) => JSON.stringify(e.loc)))].sort(),
          );
        }
        break;
      case "InvalidJSON":
        // Python 的 model_validate_json 抛 ValidationError(json_invalid)；Node 版是 JSON.parse 的 SyntaxError
        expect(load).toThrow(SyntaxError);
        break;
    }
  });

  it("accepts file: URLs", () => {
    const url = pathToFileURL(`${PACKAGE_ROOT}test/fixtures/python/acs/files/agent.json`);
    expect(AgentCapabilitySpec.fromFile(url).name).toBe("北京城区旅游规划助手");
  });
});
