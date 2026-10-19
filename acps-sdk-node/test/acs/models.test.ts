import { describe, expect, it } from "vitest";
import type { z } from "zod";

import {
  APIKeySecuritySchemeSchema,
  AgentCapabilitiesSchema,
  AgentCapabilitySpecSchema,
  AgentEndPointSchema,
  AgentProviderSchema,
  AgentSkillSchema,
  HTTPAuthSecuritySchemeSchema,
  MQProtocolVersion,
  MQProtocolVersionSchema,
  MutualTLSSecuritySchemeSchema,
  OAuth2FlowSchema,
  OAuth2FlowsSchema,
  OAuth2SecuritySchemeSchema,
  OpenIdConnectSecuritySchemeSchema,
  SecuritySchemeSchema,
} from "../../src/acs/index.js";
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
  AgentProvider: AgentProviderSchema,
  AgentCapabilities: AgentCapabilitiesSchema,
  MutualTLSSecurityScheme: MutualTLSSecuritySchemeSchema,
  OpenIdConnectSecurityScheme: OpenIdConnectSecuritySchemeSchema,
  APIKeySecurityScheme: APIKeySecuritySchemeSchema,
  HTTPAuthSecurityScheme: HTTPAuthSecuritySchemeSchema,
  OAuth2Flow: OAuth2FlowSchema,
  OAuth2Flows: OAuth2FlowsSchema,
  OAuth2SecurityScheme: OAuth2SecuritySchemeSchema,
  SecurityScheme: SecuritySchemeSchema,
  AgentEndPoint: AgentEndPointSchema,
  AgentSkill: AgentSkillSchema,
  AgentCapabilitySpec: AgentCapabilitySpecSchema,
};

function schemaFor(model: string): z.ZodType {
  const schema = SCHEMAS[model];
  if (!schema) {
    throw new Error(`no schema for ${model}`);
  }
  return schema;
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
  if (parsed.success || !testCase.compareLocs) {
    // compareLocs=false：pydantic 的错误位置里带 Union 成员标签，只比对「是否抛错」
    return;
  }
  const nodePaths = [...new Set(parsed.error.issues.map((issue) => JSON.stringify(issue.path)))];
  const pythonPaths = [
    ...new Set((testCase.errors ?? []).map((error) => JSON.stringify(error.loc))),
  ];
  expect(nodePaths.sort()).toEqual(pythonPaths.sort());
}

describe("test_acs_models.py", () => {
  it("test_mutual_tls_security_scheme_allows_missing_legacy_challenge_url", () => {
    // 主链路测试：EAB 模式下 MutualTLSSecurityScheme 不再要求 legacy challenge URL。
    const scheme = MutualTLSSecuritySchemeSchema.parse({ type: "mutualTLS" });

    expect(scheme.type).toBe("mutualTLS");
    expect(scheme["x-caChallengeBaseUrl"]).toBeUndefined();
  });
});

describe("Python fixtures: acs models", () => {
  const cases = loadPythonFixture<ModelCase[]>("acs", "models");

  it("covers every model with valid inputs", () => {
    for (const model of Object.keys(SCHEMAS)) {
      expect(cases.some((item) => item.model === model && item.ok)).toBe(true);
    }
  });

  it.each(cases.map((item) => [`${item.model}: ${item.name}`, item] as const))("%s", (_, item) => {
    expectMatchesPython(schemaFor(item.model), item);
  });
});

describe("Python fixtures: intentional divergences", () => {
  const cases = loadPythonFixture<ModelCase[]>("acs", "divergences");

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

describe("MQProtocolVersion", () => {
  it("matches the Python enum members", () => {
    const { mqProtocolVersion } = loadPythonFixture<{ mqProtocolVersion: Record<string, string> }>(
      "acs",
      "exports",
    );
    expect({ ...MQProtocolVersion }).toEqual(mqProtocolVersion);
    expect(MQProtocolVersionSchema.options).toEqual(Object.values(mqProtocolVersion));
  });
});

describe("AgentProvider.countryCode", () => {
  it("defaults to CN when missing and is dropped when null", () => {
    expect(AgentProviderSchema.parse({}).countryCode).toBe("CN");
    expect(AgentProviderSchema.parse({ countryCode: null }).countryCode).toBeUndefined();
    expect(JSON.stringify(AgentProviderSchema.parse({ countryCode: null }))).toBe("{}");
  });
});

describe("SecurityScheme", () => {
  it("narrows on type", () => {
    const scheme = SecuritySchemeSchema.parse({ type: "apiKey", name: "k", in: "header" });
    expect(scheme.type === "apiKey" ? scheme.in : undefined).toBe("header");
  });
});
