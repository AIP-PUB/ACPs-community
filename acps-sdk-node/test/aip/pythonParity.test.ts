/**
 * 与 Python 版逐条比对的跨语言样例（scripts/fixtures/gen_aip_core.py 生成）：
 * 模型解析与序列化（含键顺序）、DataItem 联合解析、SSE 帧、身份工具、Access 状态映射。
 *
 * 序列化比对口径：`JSON.stringify(Schema.parse(input))` === Python
 * `json.dumps(Model.model_validate(input).model_dump(exclude_none=True, mode="json"), separators=(",", ":"), ensure_ascii=False)`。
 */

import { describe, expect, it } from "vitest";
import type { z } from "zod";

import {
  errorInfoFromRpcResponse,
  statusFromRpcError,
  statusFromRpcResponse,
} from "../../src/aip/accessStatus.js";
import {
  DataItemSchema,
  FileDataItemSchema,
  GetCommandParamsSchema,
  MessageSchema,
  ProductSchema,
  StartCommandParamsSchema,
  StructuredDataItemSchema,
  TaskCommandSchema,
  TaskResultSchema,
  TaskStatusSchema,
  TextDataItemSchema,
} from "../../src/aip/baseModel.js";
import {
  AipIdentityError,
  InvalidPeerCertificateError,
  PeerAicMissingError,
  type PeerCertificateLike,
  SenderIdentityMismatchError,
  assertAicMatchesExpected,
  assertSenderMatchesExpected,
  assertSenderMatchesPeer,
  extractAcpsUriSan,
  extractCommonName,
  extractPeerIdentity,
  identityErrorToJsonRpc,
  normalizeAic,
  normalizeAndValidateAic,
  normalizeAndValidateExpectedAic,
} from "../../src/aip/identity.js";
import {
  NotificationConfigSchema,
  NotificationDeleteRequestSchema,
  NotificationDeleteResponseSchema,
  NotificationDeleteResultSchema,
  NotificationGetRequestSchema,
  NotificationGetResponseSchema,
  NotificationIdParamsSchema,
  NotificationRequestSchema,
  NotificationResponseSchema,
  NotificationStartParamsSchema,
  NotificationStartRequestParamsSchema,
  NotificationStartRequestSchema,
} from "../../src/aip/notificationModel.js";
import {
  JSONRPCErrorSchema,
  JSONRPCRequestSchema,
  JSONRPCResponseSchema,
  RpcRequestParamsSchema,
  RpcRequestSchema,
  RpcResponseSchema,
} from "../../src/aip/rpcModel.js";
import {
  ProductChunkEventSchema,
  ReStreamCommandParamsSchema,
  StreamEventDataSchema,
  StreamRequestParamsSchema,
  StreamRequestSchema,
  StreamResponseSchema,
  TaskStatusUpdateEventSchema,
} from "../../src/aip/streamModel.js";
import {
  buildStreamErrorResponse,
  buildStreamResponse,
  formatSse,
  type StreamPayload,
} from "../../src/aip/streamServer.js";
import { loadPythonFixture } from "../fixtures.js";

const SCHEMAS: Record<string, z.ZodType> = {
  TextDataItem: TextDataItemSchema,
  FileDataItem: FileDataItemSchema,
  StructuredDataItem: StructuredDataItemSchema,
  DataItem: DataItemSchema,
  TaskStatus: TaskStatusSchema,
  Product: ProductSchema,
  GetCommandParams: GetCommandParamsSchema,
  StartCommandParams: StartCommandParamsSchema,
  Message: MessageSchema,
  TaskCommand: TaskCommandSchema,
  TaskResult: TaskResultSchema,
  JSONRPCRequest: JSONRPCRequestSchema,
  JSONRPCError: JSONRPCErrorSchema,
  JSONRPCResponse: JSONRPCResponseSchema,
  RpcRequestParams: RpcRequestParamsSchema,
  RpcRequest: RpcRequestSchema,
  RpcResponse: RpcResponseSchema,
  TaskStatusUpdateEvent: TaskStatusUpdateEventSchema,
  ProductChunkEvent: ProductChunkEventSchema,
  StreamRequestParams: StreamRequestParamsSchema,
  StreamRequest: StreamRequestSchema,
  StreamEventData: StreamEventDataSchema,
  StreamResponse: StreamResponseSchema,
  ReStreamCommandParams: ReStreamCommandParamsSchema,
  NotificationConfig: NotificationConfigSchema,
  NotificationRequest: NotificationRequestSchema,
  NotificationResponse: NotificationResponseSchema,
  NotificationIdParams: NotificationIdParamsSchema,
  NotificationDeleteRequest: NotificationDeleteRequestSchema,
  NotificationDeleteResult: NotificationDeleteResultSchema,
  NotificationDeleteResponse: NotificationDeleteResponseSchema,
  NotificationGetRequest: NotificationGetRequestSchema,
  NotificationGetResponse: NotificationGetResponseSchema,
  NotificationStartParams: NotificationStartParamsSchema,
  NotificationStartRequestParams: NotificationStartRequestParamsSchema,
  NotificationStartRequest: NotificationStartRequestSchema,
};

const schemaFor = (name: string): z.ZodType => {
  const schema = SCHEMAS[name];
  if (!schema) throw new Error(`no schema for ${name}`);
  return schema;
};

interface ModelCase {
  model: string;
  input: unknown;
  dump?: string;
  error?: boolean;
}

/**
 * 响应模型保留 `"id": null`（JSON-RPC 要求响应带 id），而 Python 样例按 exclude_none 省略了它；
 * 这是有意差异（docs/porting-notes.md），比对时去掉值为 null 的 id。
 */
function dropNullResponseId(model: string, data: unknown): unknown {
  if (
    (model === "RpcResponse" || model === "JSONRPCResponse") &&
    data &&
    typeof data === "object"
  ) {
    const record = data as Record<string, unknown>;
    if (record["id"] === null) {
      const { id: _id, ...rest } = record;
      return rest;
    }
  }
  return data;
}

describe("python fixtures: model_dump(exclude_none=True)", () => {
  const cases = loadPythonFixture<ModelCase[]>("aip", "models");

  it("covers every exported AIP model", () => {
    expect(new Set(cases.map((c) => c.model))).toEqual(new Set(Object.keys(SCHEMAS)));
  });

  it.each(
    cases.map((c, i) => [`${i} ${c.model} ${JSON.stringify(c.input).slice(0, 80)}`, c] as const),
  )("%s", (_name, c) => {
    const parsed = schemaFor(c.model).safeParse(c.input);
    if (c.error) {
      expect(parsed.success).toBe(false);
    } else {
      expect(parsed.success, JSON.stringify(parsed.error?.issues)).toBe(true);
      expect(JSON.stringify(dropNullResponseId(c.model, parsed.data))).toBe(c.dump);
    }
  });

  it("serialization is stable: parse(dump) re-serializes identically", () => {
    for (const c of cases) {
      if (c.error || c.dump === undefined) continue;
      const again = schemaFor(c.model).parse(JSON.parse(c.dump));
      expect(JSON.stringify(again)).toBe(c.dump);
    }
  });
});

describe("python fixtures: DataItem smart-mode union", () => {
  const cases = loadPythonFixture<
    Array<{ input: unknown; type?: string; dump?: string; error?: boolean }>
  >("aip", "data_item_union");

  it.each(cases.map((c) => [JSON.stringify(c.input), c] as const))("%s", (_name, c) => {
    const parsed = DataItemSchema.safeParse(c.input);
    if (c.error) {
      expect(parsed.success).toBe(false);
      return;
    }
    expect(parsed.success).toBe(true);
    expect(parsed.data?.type).toBe(c.type);
    expect(JSON.stringify(parsed.data)).toBe(c.dump);
  });
});

interface SseFixture {
  frames: Array<{
    rpcId: string | number | null;
    eventSeq: number;
    model: string;
    payload: unknown;
    sse: string;
  }>;
  errors: Array<{
    rpcId: string | number | null;
    code: number;
    message: string;
    data: unknown;
    sse: string;
  }>;
}

describe("python fixtures: format_sse", () => {
  const fixture = loadPythonFixture<SseFixture>("aip", "sse");

  it.each(
    fixture.frames.map((f) => [`${f.model} seq=${f.eventSeq} id=${String(f.rpcId)}`, f] as const),
  )("event frame %s", (_name, f) => {
    const payload = schemaFor(f.model).parse(f.payload) as StreamPayload;
    const response = buildStreamResponse(f.rpcId, {
      eventSeq: f.eventSeq,
      payload,
      isTerminal: false,
    });
    expect(formatSse(response)).toBe(f.sse);
  });

  it.each(
    fixture.errors.map(
      (f, i) => [`${i} id=${String(f.rpcId)} data=${JSON.stringify(f.data)}`, f] as const,
    ),
  )("error frame %s", (_name, f) => {
    expect(formatSse(buildStreamErrorResponse(f.rpcId, f.code, f.message, f.data))).toBe(f.sse);
  });
});

// ---------------------------------------------------------------------------
// identity
// ---------------------------------------------------------------------------

type Outcome = { result?: unknown; error?: string; message?: string };

function outcome(fn: () => unknown): Outcome {
  try {
    return { result: fn() };
  } catch (error) {
    if (error instanceof AipIdentityError) {
      return { error: error.name, message: error.message };
    }
    if (error instanceof Error && error.name === "Error") {
      return { error: "Error", message: error.message };
    }
    throw error;
  }
}

interface CertSpec {
  cns?: string[];
  sans?: Array<[string, string]>;
  org?: string;
}

/** 把中立证书描述转成 Node `getPeerCertificate()` 的形状。 */
function nodeCertFromSpec(spec: CertSpec | null): PeerCertificateLike | null {
  if (spec === null) return null;
  const cert: PeerCertificateLike = {};
  if (spec.cns) {
    cert.subject = {
      ...(spec.org ? { O: spec.org } : {}),
      CN: spec.cns.length === 1 ? spec.cns[0] : spec.cns,
    };
  }
  if (spec.sans) {
    cert.subjectaltname = spec.sans.map(([kind, value]) => `${kind}:${value}`).join(", ");
  }
  return cert;
}

interface IdentityFixture {
  normalize: Array<{ input: string | null; result: string | null }>;
  normalizeAndValidate: Array<Outcome & { input: string | null; fieldName?: string }>;
  normalizeAndValidateExpected: Array<Outcome & { input: string | null; fieldName?: string }>;
  assertSenderMatchesPeer: Array<Outcome & { sender: unknown; peer: string | null }>;
  assertSenderMatchesExpected: Array<Outcome & { sender: unknown; expected: string | null }>;
  assertAicMatchesExpected: Array<
    Outcome & {
      actual: string | null;
      expected: string | null;
      actualLabel?: string;
      expectedLabel?: string;
    }
  >;
  certificates: Array<{
    spec: CertSpec | null;
    commonName: string | null;
    san: Outcome;
    identity: Outcome;
  }>;
  errorToJsonRpc: Array<{ error: string; message: string; jsonrpc: string }>;
}

type SenderArg = Parameters<typeof assertSenderMatchesPeer>[0];

describe("python fixtures: identity helpers", () => {
  const fixture = loadPythonFixture<IdentityFixture>("aip", "identity");

  it("normalize_aic", () => {
    for (const c of fixture.normalize) {
      expect(normalizeAic(c.input), JSON.stringify(c.input)).toBe(c.result);
    }
  });

  it("normalize_and_validate_aic", () => {
    for (const c of fixture.normalizeAndValidate) {
      const options = c.fieldName ? { fieldName: c.fieldName } : {};
      expect(
        outcome(() => normalizeAndValidateAic(c.input, options)),
        JSON.stringify(c),
      ).toEqual(expectedOutcome(c));
    }
  });

  it("normalize_and_validate_expected_aic", () => {
    for (const c of fixture.normalizeAndValidateExpected) {
      const options = c.fieldName ? { fieldName: c.fieldName } : {};
      expect(
        outcome(() => normalizeAndValidateExpectedAic(c.input, options)),
        JSON.stringify(c),
      ).toEqual(expectedOutcome(c));
    }
  });

  it("assert_sender_matches_peer", () => {
    for (const c of fixture.assertSenderMatchesPeer) {
      expect(
        outcome(() => assertSenderMatchesPeer(c.sender as SenderArg, c.peer)),
        JSON.stringify(c),
      ).toEqual(expectedOutcome(c));
    }
  });

  it("assert_sender_matches_expected", () => {
    for (const c of fixture.assertSenderMatchesExpected) {
      expect(
        outcome(() => assertSenderMatchesExpected(c.sender as SenderArg, c.expected)),
        JSON.stringify(c),
      ).toEqual(expectedOutcome(c));
    }
  });

  it("assert_aic_matches_expected", () => {
    for (const c of fixture.assertAicMatchesExpected) {
      const labels = c.actualLabel
        ? { actualLabel: c.actualLabel, expectedLabel: c.expectedLabel }
        : {};
      expect(
        outcome(() => assertAicMatchesExpected(c.actual, c.expected, labels)),
        JSON.stringify(c),
      ).toEqual(expectedOutcome(c));
    }
  });

  it.each(fixture.certificates.map((c) => [JSON.stringify(c.spec), c] as const))(
    "certificate %s",
    (_n, c) => {
      const cert = nodeCertFromSpec(c.spec);
      expect(extractCommonName(cert)).toBe(c.commonName);
      expect(outcome(() => extractAcpsUriSan(cert))).toEqual(expectedOutcome(c.san));
      expect(outcome(() => extractPeerIdentity(cert))).toEqual(expectedOutcome(c.identity));
    },
  );

  it("identity_error_to_jsonrpc", () => {
    const classes: Record<string, new (message: string) => AipIdentityError> = {
      AipIdentityError,
      PeerAicMissingError,
      SenderIdentityMismatchError,
      InvalidPeerCertificateError,
    };
    for (const c of fixture.errorToJsonRpc) {
      const ErrorClass = classes[c.error];
      if (!ErrorClass) throw new Error(c.error);
      expect(JSON.stringify(identityErrorToJsonRpc(new ErrorClass(c.message)))).toBe(c.jsonrpc);
    }
  });
});

function expectedOutcome(c: Outcome): Outcome {
  return c.error !== undefined ? { error: c.error, message: c.message } : { result: c.result };
}

describe("python fixtures: access_status", () => {
  const cases = loadPythonFixture<
    Array<{ response: unknown; status: number; errorStatus: number; errorInfo: unknown }>
  >("aip", "access_status");

  it.each(cases.map((c, i) => [`${i} ${JSON.stringify(c.response).slice(0, 60)}`, c] as const))(
    "%s",
    (_n, c) => {
      const response = RpcResponseSchema.parse(c.response);
      expect(statusFromRpcResponse(response)).toBe(c.status);
      expect(statusFromRpcError(response.error)).toBe(c.errorStatus);
      expect(errorInfoFromRpcResponse(response)).toEqual(c.errorInfo);
    },
  );
});
