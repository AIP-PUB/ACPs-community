import { describe, expect, it } from "vitest";
import { z } from "zod";

import {
  DiscoveryAgentGroupSchema,
  DiscoveryAgentSkillSchema,
  DiscoveryContextSchema,
  DiscoveryFilterSchema,
  DiscoveryRequest,
  DiscoveryRequestSchema,
  DiscoveryResponseSchema,
  DiscoveryResultSchema,
  DiscoveryRouteSchema,
  ErrorDetailSchema,
  FilterConditionSchema,
  FilterOperator,
  FilterOperatorSchema,
} from "../../src/adp/index.js";
import { formatZodError } from "../../src/internal/schema.js";
import { loadPythonFixture } from "../fixtures.js";

interface PyError {
  loc: (string | number)[];
  type: string;
  msg: string;
}

interface ModelCase {
  model: string;
  name: string;
  input: unknown;
  ok: boolean;
  dump?: unknown;
  json?: string;
  compareLocs?: boolean;
  errors?: PyError[];
  kind?: "lax" | "populateByName";
}

const SCHEMAS: Record<string, z.ZodType> = {
  FilterCondition: FilterConditionSchema,
  DiscoveryFilter: DiscoveryFilterSchema,
  DiscoveryContext: DiscoveryContextSchema,
  DiscoveryRequest: DiscoveryRequestSchema,
  DiscoveryAgentSkill: DiscoveryAgentSkillSchema,
  DiscoveryAgentGroup: DiscoveryAgentGroupSchema,
  DiscoveryRoute: DiscoveryRouteSchema,
  DiscoveryResult: DiscoveryResultSchema,
  ErrorDetail: ErrorDetailSchema,
  DiscoveryResponse: DiscoveryResponseSchema,
};

function schemaFor(model: string): z.ZodType {
  const schema = SCHEMAS[model];
  if (!schema) {
    throw new Error(`no schema for ${model}`);
  }
  return schema;
}

/** pydantic 的 "Value error, xxx" 对应 Node 版 refine 的原始消息 xxx。 */
function stripValueErrorPrefix(message: string): string {
  return message.replace(/^Value error, /, "");
}

function expectMatchesPython(schema: z.ZodType, testCase: ModelCase): void {
  const parsed = schema.safeParse(testCase.input);
  if (testCase.ok) {
    expect(parsed.success, parsed.success ? "" : formatZodError(parsed.error)).toBe(true);
    // model_dump(by_alias=True, exclude_none=True, mode="json")
    expect(JSON.parse(JSON.stringify(parsed.data))).toEqual(testCase.dump);
    // model_dump_json(by_alias=True, exclude_none=True)：键顺序、转义都逐字一致
    expect(JSON.stringify(parsed.data)).toBe(testCase.json);
    return;
  }

  expect(parsed.success).toBe(false);
  if (parsed.success) {
    return;
  }
  const errors = testCase.errors ?? [];
  if (testCase.compareLocs) {
    const nodePaths = [...new Set(parsed.error.issues.map((issue) => JSON.stringify(issue.path)))];
    const pythonPaths = [...new Set(errors.map((error) => JSON.stringify(error.loc)))];
    expect(nodePaths.sort()).toEqual(pythonPaths.sort());
  }
  for (const error of errors.filter((item) => item.type === "value_error")) {
    expect(parsed.error.issues).toContainEqual(
      expect.objectContaining({ path: error.loc, message: stripValueErrorPrefix(error.msg) }),
    );
  }
}

describe("Python fixtures: adp models", () => {
  const cases = loadPythonFixture<ModelCase[]>("adp", "models");

  it("covers every model with valid and invalid inputs", () => {
    for (const model of Object.keys(SCHEMAS)) {
      expect(cases.some((item) => item.model === model && item.ok)).toBe(true);
      expect(cases.some((item) => item.model === model && !item.ok)).toBe(true);
    }
  });

  it.each(cases.map((item) => [`${item.model}: ${item.name}`, item] as const))("%s", (_, item) => {
    expectMatchesPython(schemaFor(item.model), item);
  });
});

describe("Python fixtures: intentional divergences", () => {
  const cases = loadPythonFixture<ModelCase[]>("adp", "divergences");

  it.each(cases.filter((item) => item.kind === "lax").map((item) => [item.name, item] as const))(
    "%s: Python coerces, Node rejects",
    (_, item) => {
      expect(item.ok).toBe(true);
      expect(schemaFor(item.model).safeParse(item.input).success).toBe(false);
    },
  );

  it.each(
    cases
      .filter((item) => item.kind === "populateByName")
      .map((item) => [item.name, item] as const),
  )("%s: Python accepts snake_case keys, Node only reads wire names", (_, item) => {
    expect(item.ok).toBe(true);
    const parsed = schemaFor(item.model).safeParse(item.input);
    expect(parsed.success && JSON.stringify(parsed.data) === item.json).toBe(false);
  });
});

describe("FilterOperator", () => {
  it("matches the Python FilterOperator enum (names, values and order)", () => {
    const python = loadPythonFixture<Array<[string, string]>>("adp", "filter_operators");
    expect(Object.entries(FilterOperator)).toEqual(python);
    expect(FilterOperatorSchema.options).toEqual(Object.values(FilterOperator));
  });
});

describe("deeply nested filters", () => {
  it("fromDict reports excessive nesting as a validation error, not a stack overflow", () => {
    let filter: Record<string, unknown> = {
      conditions: [{ field: "active", op: "eq", value: true }],
    };
    for (let depth = 0; depth < 5000; depth += 1) {
      filter = { groups: [filter] };
    }
    expect(() => DiscoveryRequest.fromDict({ query: "q", filter })).toThrow(z.ZodError);
  });
});

describe("DiscoveryRequest", () => {
  it("applies the explicit default and keeps it in output", () => {
    const request = DiscoveryRequestSchema.parse({ query: "q" });
    expect(request.type).toBe("explicit");
    expect(DiscoveryRequest.toJson(request)).toBe('{"type":"explicit","query":"q"}');
  });

  it("drops an explicit null type, matching exclude_none", () => {
    const request = DiscoveryRequestSchema.parse({ type: null });
    expect(request.type).toBeUndefined();
    expect(DiscoveryRequest.toDict(request)).toEqual({});
  });

  it("reports the field validator message on the type path", () => {
    const parsed = DiscoveryRequestSchema.safeParse({ type: "foo", query: "q" });
    expect(parsed.success).toBe(false);
    expect(parsed.error?.issues).toEqual([
      expect.objectContaining({
        path: ["type"],
        message:
          "无效的查询类型: 'foo'，允许的类型: ('explicit', 'exploratory', 'trending', 'filtered')",
      }),
    ]);
  });

  it("fromJson / fromDict round-trip", () => {
    const request = DiscoveryRequest.fromDict({ query: "q", forwardChain: ["A"] });
    expect(DiscoveryRequest.fromJson(DiscoveryRequest.toJson(request))).toEqual(request);
    expect(() => DiscoveryRequest.fromJson("{")).toThrow(SyntaxError);
    expect(() => DiscoveryRequest.fromDict({ type: "explicit" })).toThrow(
      "type='explicit' 时 query 字段必填且不能为空字符串",
    );
  });

  it("toDict returns a deep copy", () => {
    const request = DiscoveryRequestSchema.parse({
      query: "q",
      context: { userProfile: { city: "北京" } },
    });
    const dict = DiscoveryRequest.toDict(request) as { context: { userProfile: { city: string } } };
    dict.context.userProfile.city = "上海";
    expect(request.context?.userProfile).toEqual({ city: "北京" });
  });

  it("does not share default arrays between parses", () => {
    const first = DiscoveryAgentGroupSchema.parse({ group: "g" });
    first.agentSkills.push({ aic: "a", skillId: "s", ranking: 1 });
    expect(DiscoveryAgentGroupSchema.parse({ group: "g" }).agentSkills).toEqual([]);
  });
});
