import { describe, expect, it } from "vitest";
import { ZodError } from "zod";

import {
  ADPError,
  type DiscoveryRequest as DiscoveryRequestType,
  DiscoveryRequest,
  DiscoveryRequestSchema,
  allocateFanoutBudget,
  buildForwardedRequest,
  shouldContinueForwarding,
  validateDiscoveryRequest,
  validateFanoutBudget,
  validateForwardChain,
  validateTrustedTarget,
} from "../../src/adp/index.js";
import { loadPythonFixture } from "../fixtures.js";

interface PyError {
  loc: (string | number)[];
  type: string;
  msg: string;
}

type Outcome =
  | { result: unknown; error?: undefined }
  | {
      result?: undefined;
      error:
        | {
            kind: "ADPError";
            code: number;
            message: string;
            data: unknown;
            httpStatus: number;
            body: unknown;
          }
        | { kind: "ValidationError"; errors: PyError[] }
        | { kind: "ValueError" | "ZeroDivisionError"; message: string };
    };

type RequestCase = Outcome & {
  name: string;
  input: Record<string, unknown>;
  overrides: Record<string, unknown> | null;
};

interface ValidatorsFixture {
  validateDiscoveryRequest: RequestCase[];
  validateForwardChain: (RequestCase & { currentServerAic: string; senderAic: string | null })[];
  validateFanoutBudget: (RequestCase & { requiredBranches: number })[];
  validateTrustedTarget: (RequestCase & { targetAic: string })[];
  shouldContinueForwarding: RequestCase[];
  buildForwardedRequest: (RequestCase & {
    args: {
      currentServerAic: string;
      fanoutRemainingForBranch: number;
      elapsedMs?: number;
      signature?: string | null;
      trustedServers?: string[] | null;
    };
    originalUnchanged: boolean;
  })[];
  allocateFanoutBudget: (Outcome & {
    args: {
      totalRemaining: number;
      branchCount: number;
      strategy?: string;
      weights?: number[] | null;
    };
  })[];
}

const fixture = loadPythonFixture<ValidatorsFixture>("adp", "validators");

/**
 * 与生成脚本的 make_request 一致：先按线上格式解析，再直接赋值覆盖字段（不经校验）。
 * Python 的 None 对应 Node 解析结果里的 undefined。
 */
function makeRequest(item: RequestCase): DiscoveryRequestType {
  const request: Record<string, unknown> = { ...DiscoveryRequestSchema.parse(item.input) };
  for (const [key, value] of Object.entries(item.overrides ?? {})) {
    request[key] = value ?? undefined;
  }
  return request as DiscoveryRequestType;
}

function expectOutcome(run: () => unknown, outcome: Outcome): void {
  if (outcome.error === undefined) {
    expect(run()).toEqual(outcome.result ?? undefined);
    return;
  }
  let thrown: unknown;
  try {
    run();
  } catch (error) {
    thrown = error;
  }
  expect(thrown, "expected an exception").toBeDefined();
  const expected = outcome.error;
  switch (expected.kind) {
    case "ADPError": {
      expect(thrown).toBeInstanceOf(ADPError);
      const error = thrown as ADPError;
      expect(error.code).toBe(expected.code);
      expect(error.message).toBe(expected.message);
      expect(error.data ?? null).toEqual(expected.data);
      expect(error.httpStatus).toBe(expected.httpStatus);
      expect(error.toErrorBody()).toEqual(expected.body);
      break;
    }
    case "ValidationError": {
      expect(thrown).toBeInstanceOf(ZodError);
      const issues = (thrown as ZodError).issues;
      expect([...new Set(issues.map((issue) => JSON.stringify(issue.path)))].sort()).toEqual(
        [...new Set(expected.errors.map((error) => JSON.stringify(error.loc)))].sort(),
      );
      for (const error of expected.errors.filter((item) => item.type === "value_error")) {
        expect(issues).toContainEqual(
          expect.objectContaining({ message: error.msg.replace(/^Value error, /, "") }),
        );
      }
      break;
    }
    case "ValueError":
      expect(thrown).toBeInstanceOf(Error);
      expect(thrown).not.toBeInstanceOf(ADPError);
      expect((thrown as Error).message).toBe(expected.message);
      break;
    case "ZeroDivisionError":
      expect(thrown).toBeInstanceOf(RangeError);
      expect((thrown as Error).message).toBe(expected.message);
      break;
  }
}

describe("Python fixtures: validate_discovery_request", () => {
  it.each(fixture.validateDiscoveryRequest.map((item) => [item.name, item] as const))(
    "%s",
    (_, item) => {
      const request = makeRequest(item);
      expectOutcome(() => validateDiscoveryRequest(request), item);
    },
  );
});

describe("Python fixtures: validate_forward_chain", () => {
  it.each(fixture.validateForwardChain.map((item) => [item.name, item] as const))(
    "%s",
    (_, item) => {
      const request = makeRequest(item);
      expectOutcome(
        () => validateForwardChain(request, item.currentServerAic, item.senderAic),
        item,
      );
    },
  );

  it("keeps the request's own chain array in error data", () => {
    const request = DiscoveryRequestSchema.parse({ query: "q", forwardChain: ["A"] });
    try {
      validateForwardChain(request, "A");
      expect.unreachable();
    } catch (error) {
      expect((error as ADPError).data).toEqual({ forwardChain: ["A"], currentServerAic: "A" });
      expect(((error as ADPError).data as { forwardChain: unknown }).forwardChain).toBe(
        request.forwardChain,
      );
    }
  });
});

describe("Python fixtures: validate_fanout_budget", () => {
  it.each(fixture.validateFanoutBudget.map((item) => [item.name, item] as const))(
    "%s",
    (_, item) => {
      const request = makeRequest(item);
      expectOutcome(() => validateFanoutBudget(request, item.requiredBranches), item);
    },
  );
});

describe("Python fixtures: validate_trusted_target", () => {
  it.each(fixture.validateTrustedTarget.map((item) => [item.name, item] as const))(
    "%s",
    (_, item) => {
      const request = makeRequest(item);
      expectOutcome(() => validateTrustedTarget(request, item.targetAic), item);
    },
  );
});

describe("Python fixtures: should_continue_forwarding", () => {
  it.each(fixture.shouldContinueForwarding.map((item) => [item.name, item] as const))(
    "%s",
    (_, item) => {
      const request = makeRequest(item);
      expectOutcome(() => shouldContinueForwarding(request), item);
    },
  );
});

describe("Python fixtures: build_forwarded_request", () => {
  it.each(fixture.buildForwardedRequest.map((item) => [item.name, item] as const))(
    "%s",
    (_, item) => {
      const original = makeRequest(item);
      const before = JSON.stringify(original);
      const { args } = item;
      expectOutcome(() => {
        const forwarded = buildForwardedRequest(
          original,
          args.currentServerAic,
          args.fanoutRemainingForBranch,
          args.elapsedMs,
          args.signature,
          args.trustedServers,
        );
        return {
          toDict: DiscoveryRequest.toDict(forwarded),
          toJson: DiscoveryRequest.toJson(forwarded),
        };
      }, item);
      expect(JSON.stringify(original) === before).toBe(item.originalUnchanged);
    },
  );

  it("returns a deep copy that does not alias the original", () => {
    const original = DiscoveryRequestSchema.parse({
      query: "q",
      forwardChain: ["A"],
      context: { userProfile: { city: "北京" } },
    });
    const forwarded = buildForwardedRequest(original, "B", 0);
    expect(forwarded.forwardChain).toEqual(["A", "B"]);
    expect(original.forwardChain).toEqual(["A"]);
    expect(forwarded.context).not.toBe(original.context);
  });
});

describe("Python fixtures: allocate_fanout_budget", () => {
  it.each(fixture.allocateFanoutBudget.map((item) => [JSON.stringify(item.args), item] as const))(
    "%s",
    (_, item) => {
      const { args } = item;
      expectOutcome(
        () =>
          allocateFanoutBudget(
            args.totalRemaining,
            args.branchCount,
            args.strategy ?? "equal",
            args.weights,
          ),
        item,
      );
    },
  );

  it("matches the README example", () => {
    expect(allocateFanoutBudget(4, 3)).toEqual([0, 0, 1]);
  });
});

describe("README example: forward chain validation and build", () => {
  it("validates, allocates and builds the forwarded request", () => {
    const request = DiscoveryRequest.fromDict({
      type: "explicit",
      query: "翻译智能体",
      forwardDepthLimit: 3,
      forwardFanoutLimit: 4,
      forwardChain: ["AIC-DS-A"],
    });
    validateForwardChain(request, "AIC-DS-B", "AIC-DS-A");
    validateFanoutBudget(request, 3);
    const budgets = allocateFanoutBudget(4, 3);
    const forwarded = buildForwardedRequest(request, "AIC-DS-B", budgets[0] ?? 0, 200, undefined, [
      "AIC-DS-B",
      "AIC-DS-C",
      "AIC-DS-D",
    ]);
    expect(forwarded.forwardChain).toEqual(["AIC-DS-A", "AIC-DS-B"]);
    expect(forwarded.forwardFanoutRemaining).toBe(0);
  });
});
