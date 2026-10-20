import { describe, expect, it } from "vitest";
import { ZodError } from "zod";

import {
  ADPError,
  type DiscoveryRequest as DiscoveryRequestType,
  DiscoveryRequest,
  DiscoveryRequestSchema,
  DiscoveryResponse,
  DiscoveryResult,
  DiscoveryResultSchema,
} from "../../src/adp/index.js";
import { loadPythonFixture } from "../fixtures.js";

interface MethodsFixture {
  request: {
    name: string;
    input: Record<string, unknown>;
    overrides: Record<string, unknown> | null;
    expected: Record<string, number>;
  }[];
  serialization: {
    name: string;
    input: Record<string, unknown>;
    toDict: unknown;
    toJson: string;
    fromJsonOfToJson: unknown;
  }[];
  iterAgentSkills: { name: string; input: Record<string, unknown>; expected: unknown[] }[];
  response: {
    success: {
      input: Record<string, unknown>;
      toDict: unknown;
      toJson: string;
      isSuccess: boolean;
      isError: boolean;
    };
    failures: {
      args: { code: number; message: string; data: unknown };
      toDict: unknown;
      toJson: string;
      isSuccess: boolean;
      isError: boolean;
      adpError: {
        code: number;
        message: string;
        data: unknown;
        httpStatus: number;
        isClientError: boolean;
        isForwardError: boolean;
      };
    }[];
    successWithNone: { error: { kind: string; errors: { msg: string }[] } };
    failureWithBadCode: { error: { kind: string } };
  };
}

const fixture = loadPythonFixture<MethodsFixture>("adp", "methods");

describe("Python fixtures: DiscoveryRequest.get_effective_*", () => {
  it.each(fixture.request.map((item) => [item.name, item] as const))("%s", (_, item) => {
    const request: Record<string, unknown> = { ...DiscoveryRequestSchema.parse(item.input) };
    for (const [key, value] of Object.entries(item.overrides ?? {})) {
      request[key] = value ?? undefined;
    }
    const typed = request as DiscoveryRequestType;
    expect({
      getEffectiveDepthLimit: DiscoveryRequest.getEffectiveDepthLimit(typed),
      getEffectiveFanoutLimit: DiscoveryRequest.getEffectiveFanoutLimit(typed),
      getEffectiveFanoutRemaining: DiscoveryRequest.getEffectiveFanoutRemaining(typed),
      getEffectiveEachTimeoutMs: DiscoveryRequest.getEffectiveEachTimeoutMs(typed),
      getEffectiveTotalTimeoutMs: DiscoveryRequest.getEffectiveTotalTimeoutMs(typed),
      getForwardChainLength: DiscoveryRequest.getForwardChainLength(typed),
    }).toEqual(item.expected);
  });
});

describe("Python fixtures: DiscoveryRequest to_dict / to_json / from_json", () => {
  it.each(fixture.serialization.map((item) => [item.name, item] as const))("%s", (_, item) => {
    const request = DiscoveryRequest.fromDict(item.input);
    expect(DiscoveryRequest.toDict(request)).toEqual(item.toDict);
    expect(DiscoveryRequest.toJson(request)).toBe(item.toJson);
    expect(
      DiscoveryRequest.toDict(DiscoveryRequest.fromJson(DiscoveryRequest.toJson(request))),
    ).toEqual(item.fromJsonOfToJson);
  });
});

describe("Python fixtures: DiscoveryResult.iter_agent_skills", () => {
  it.each(fixture.iterAgentSkills.map((item) => [item.name, item] as const))("%s", (_, item) => {
    const result = DiscoveryResultSchema.parse(item.input);
    const entries = [...DiscoveryResult.iterAgentSkills(result)].map(
      ([aic, acsData, skill, group]) => [aic, acsData, JSON.parse(JSON.stringify(skill)), group],
    );
    expect(entries).toEqual(item.expected);
  });

  it("returns the acsMap entry itself and a fresh empty object for missing AICs", () => {
    const result = DiscoveryResultSchema.parse({
      acsMap: { "AIC-001": { name: "n" } },
      agents: [
        {
          group: "g",
          agentSkills: [
            { aic: "AIC-001", skillId: "s", ranking: 1 },
            { aic: "constructor", skillId: "s", ranking: 2 },
          ],
        },
      ],
    });
    const [first, second] = [...DiscoveryResult.iterAgentSkills(result)];
    expect(first?.[1]).toBe(result.acsMap["AIC-001"]);
    expect(second?.[1]).toEqual({});
  });
});

describe("Python fixtures: DiscoveryResponse", () => {
  const { response } = fixture;

  it("success()", () => {
    const success = DiscoveryResponse.success(DiscoveryResultSchema.parse(response.success.input));
    expect(DiscoveryResponse.toDict(success)).toEqual(response.success.toDict);
    expect(DiscoveryResponse.toJson(success)).toBe(response.success.toJson);
    expect(DiscoveryResponse.isSuccess(success)).toBe(response.success.isSuccess);
    expect(DiscoveryResponse.isError(success)).toBe(response.success.isError);
    expect(DiscoveryResponse.getAdpError(success)).toBeNull();
  });

  it.each(response.failures.map((item) => [item.args.code, item] as const))(
    "failure(%s)",
    (_, item) => {
      const failure = DiscoveryResponse.failure(item.args.code, item.args.message, item.args.data);
      expect(DiscoveryResponse.toDict(failure)).toEqual(item.toDict);
      expect(DiscoveryResponse.toJson(failure)).toBe(item.toJson);
      expect(DiscoveryResponse.isSuccess(failure)).toBe(item.isSuccess);
      expect(DiscoveryResponse.isError(failure)).toBe(item.isError);
      const adpError = DiscoveryResponse.getAdpError(failure);
      expect(adpError).toBeInstanceOf(ADPError);
      expect({
        code: adpError?.code,
        message: adpError?.message,
        data: adpError?.data ?? null,
        httpStatus: adpError?.httpStatus,
        isClientError: adpError?.isClientError(),
        isForwardError: adpError?.isForwardError(),
      }).toEqual(item.adpError);
    },
  );

  it("success() without a result raises like Python", () => {
    expect(response.successWithNone.error.kind).toBe("ValidationError");
    const message = response.successWithNone.error.errors[0]?.msg.replace(/^Value error, /, "");
    expect(() => DiscoveryResponse.success(null as never)).toThrow(ZodError);
    expect(() => DiscoveryResponse.success(undefined as never)).toThrow(message);
  });

  it("failure() with a non-integer code raises like Python", () => {
    expect(response.failureWithBadCode.error.kind).toBe("ValidationError");
    expect(() => DiscoveryResponse.failure("x" as never, "m")).toThrow(ZodError);
  });

  it("fromJson / fromDict enforce result/error exclusivity", () => {
    expect(() => DiscoveryResponse.fromJson("{}")).toThrow("result 和 error 字段必须至少存在一个");
    expect(() =>
      DiscoveryResponse.fromDict({ result: {}, error: { code: 1, message: "m" } }),
    ).toThrow("result 和 error 字段互斥，不能同时存在");
  });
});
