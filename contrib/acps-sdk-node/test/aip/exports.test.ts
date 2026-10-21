/**
 * 对应 Python `tests/test_aip_exports.py`：`acps-sdk/aip` 公共导出完整性（不含群组模式）。
 *
 * 命名规则见 docs/porting-notes.md：pydantic 模型导出为 `XxxSchema` + 同名类型；函数改 camelCase；
 * FastAPI 专属的路由注册函数对应 Node 的 `createXxxRequestListener`；dataclass 形式的纯数据类型
 * （PeerIdentity、StreamHandlers 等）在 Node 中是 interface，只能做类型层面的检查（见文件末尾）。
 */

import { describe, expect, it } from "vitest";

import * as aip from "../../src/aip/index.js";
import type {
  AipIdentityBindingConfig,
  BufferedStreamEvent,
  CommandHandlers,
  DataItem,
  FileDataItem,
  GetCommandParams,
  Message,
  NotificationConfig,
  NotificationDeleteRequest,
  NotificationDeleteResponse,
  NotificationDeleteResult,
  NotificationGetRequest,
  NotificationGetResponse,
  NotificationHandlers,
  NotificationIdParams,
  NotificationRequest,
  NotificationResponse,
  NotificationStartParams,
  NotificationStartRequest,
  NotificationStartRequestParams,
  NotificationSubscription,
  PeerCertificateLike,
  PeerIdentity,
  Product,
  ProductChunkEvent,
  ReStreamCommandParams,
  StartCommandParams,
  StreamEventData,
  StreamEventPayload,
  StreamHandlers,
  StreamRequest,
  StreamRequestParams,
  StreamResponse,
  StructuredDataItem,
  TaskCommand,
  TaskResult,
  TaskStatus,
  TaskStatusUpdateEvent,
  TextDataItem,
} from "../../src/aip/index.js";

/** Python 名 → Node 运行时导出名（非群组部分）。 */
const RUNTIME_EXPORTS: Record<string, string> = {
  // 基础类型
  TaskState: "TaskState",
  TaskCommandType: "TaskCommandType",
  DataItem: "DataItemSchema",
  TextDataItem: "TextDataItemSchema",
  FileDataItem: "FileDataItemSchema",
  StructuredDataItem: "StructuredDataItemSchema",
  Message: "MessageSchema",
  TaskCommand: "TaskCommandSchema",
  TaskResult: "TaskResultSchema",
  TaskStatus: "TaskStatusSchema",
  Product: "ProductSchema",
  GetCommandParams: "GetCommandParamsSchema",
  StartCommandParams: "StartCommandParamsSchema",
  // 身份
  AUTHENTICATION_REQUIRED_CODE: "AUTHENTICATION_REQUIRED_CODE",
  AUTHORIZATION_FAILED_CODE: "AUTHORIZATION_FAILED_CODE",
  AipIdentityError: "AipIdentityError",
  InvalidPeerCertificateError: "InvalidPeerCertificateError",
  PeerAicMissingError: "PeerAicMissingError",
  SenderIdentityMismatchError: "SenderIdentityMismatchError",
  assert_aic_matches_expected: "assertAicMatchesExpected",
  assert_sender_matches_expected: "assertSenderMatchesExpected",
  assert_sender_matches_peer: "assertSenderMatchesPeer",
  extract_acps_uri_san: "extractAcpsUriSan",
  extract_common_name: "extractCommonName",
  extract_peer_aic_from_httpx_response: "extractPeerAicFromResponse",
  extract_peer_identity: "extractPeerIdentity",
  identity_error_to_jsonrpc: "identityErrorToJsonRpc",
  normalize_aic: "normalizeAic",
  normalize_and_validate_aic: "normalizeAndValidateAic",
  normalize_and_validate_expected_aic: "normalizeAndValidateExpectedAic",
  // 对端证书（uvicorn H11Protocol + Starlette 中间件 → 直接读 req.socket）
  AipPeerCertH11Protocol: "peerCertificateRegistry",
  AipPeerCertificateMiddleware: "getRequestPeerCertificateInfo",
  PeerCertificateInfo: "PeerCertificateInfo",
  PeerCertificateRegistry: "PeerCertificateRegistry",
  get_request_peer_aic: "getRequestPeerAic",
  // RPC
  AipRpcClient: "AipRpcClient",
  // 流式模型
  SSE_HEADERS: "SSE_HEADERS",
  SSE_MEDIA_TYPE: "SSE_MEDIA_TYPE",
  StreamEventData: "StreamEventDataSchema",
  StreamEventPayload: "StreamEventPayloadSchema",
  StreamRequest: "StreamRequestSchema",
  StreamRequestParams: "StreamRequestParamsSchema",
  StreamResponse: "StreamResponseSchema",
  TaskStatusUpdateEvent: "TaskStatusUpdateEventSchema",
  ProductChunkEvent: "ProductChunkEventSchema",
  ReStreamCommandParams: "ReStreamCommandParamsSchema",
  // 流式服务端 / 客户端
  StreamHub: "StreamHub",
  TaskStreamChannel: "TaskStreamChannel",
  add_aip_stream_router: "createAipStreamRequestListener",
  build_stream_error_response: "buildStreamErrorResponse",
  build_stream_response: "buildStreamResponse",
  format_sse: "formatSse",
  handle_stream_request: "handleStreamRequest",
  AipStreamClient: "AipStreamClient",
  StreamProtocolError: "StreamProtocolError",
  // 通知模型
  NOTIFICATION_TOKEN_HEADER: "NOTIFICATION_TOKEN_HEADER",
  NotificationConfig: "NotificationConfigSchema",
  NotificationDeleteRequest: "NotificationDeleteRequestSchema",
  NotificationDeleteResponse: "NotificationDeleteResponseSchema",
  NotificationDeleteResult: "NotificationDeleteResultSchema",
  NotificationGetRequest: "NotificationGetRequestSchema",
  NotificationGetResponse: "NotificationGetResponseSchema",
  NotificationIdParams: "NotificationIdParamsSchema",
  NotificationRequest: "NotificationRequestSchema",
  NotificationResponse: "NotificationResponseSchema",
  NotificationStartParams: "NotificationStartParamsSchema",
  NotificationStartRequest: "NotificationStartRequestSchema",
  NotificationStartRequestParams: "NotificationStartRequestParamsSchema",
  // 通知服务端 / 客户端
  NotificationConfigStore: "NotificationConfigStore",
  NotificationDispatcher: "NotificationDispatcher",
  NotificationRegistry: "NotificationRegistry",
  NotificationService: "NotificationService",
  add_aip_notification_router: "createAipNotificationRequestListener",
  AipNotificationClient: "AipNotificationClient",
  NotificationReceiver: "NotificationReceiver",
  add_aip_notification_receiver_router: "createAipNotificationReceiverListener",
};

/** Python 是 dataclass / 类型别名，Node 是 interface（仅类型）：由文件末尾的类型检查覆盖。 */
const TYPE_ONLY = [
  "AipIdentityBindingConfig",
  "PeerIdentity",
  "BufferedStreamEvent",
  "StreamHandlers",
  "NotificationHandlers",
  "NotificationSubscription",
];

const exported = aip as unknown as Record<string, unknown>;
const isClass = (value: unknown): boolean =>
  typeof value === "function" && /^class\s/.test(String(value));

describe("test_aip_exports", () => {
  it.each(Object.entries(RUNTIME_EXPORTS))("%s → %s", (_python, node) => {
    expect(exported[node], node).toBeDefined();
  });

  it("test_base_model_exports", () => {
    for (const name of [
      "PeerCertificateRegistry",
      "PeerCertificateInfo",
      "AipIdentityError",
      "PeerAicMissingError",
      "SenderIdentityMismatchError",
      "InvalidPeerCertificateError",
    ]) {
      expect(isClass(exported[name]), name).toBe(true);
    }
    expect(Object.values(aip.TaskState)).toContain("awaiting-input");
    expect(Object.values(aip.TaskCommandType)).toContain("re-stream");
    expect(typeof aip.TaskCommandSchema.parse).toBe("function");
    expect(new aip.PeerAicMissingError("x")).toBeInstanceOf(aip.AipIdentityError);
  });

  it("test_identity_helper_exports", () => {
    expect(typeof aip.AUTHENTICATION_REQUIRED_CODE).toBe("number");
    expect(typeof aip.AUTHORIZATION_FAILED_CODE).toBe("number");
    for (const name of [
      "normalizeAic",
      "normalizeAndValidateAic",
      "normalizeAndValidateExpectedAic",
      "extractCommonName",
      "extractAcpsUriSan",
      "extractPeerIdentity",
      "extractPeerAicFromResponse",
      "assertSenderMatchesPeer",
      "assertSenderMatchesExpected",
      "assertAicMatchesExpected",
      "identityErrorToJsonRpc",
      "getRequestPeerAic",
    ]) {
      expect(typeof exported[name], name).toBe("function");
    }
  });

  it("test_rpc_client_export", () => {
    expect(isClass(aip.AipRpcClient)).toBe(true);
  });

  it("test_stream_model_exports", () => {
    expect(typeof aip.SSE_MEDIA_TYPE).toBe("string");
    expect(typeof aip.SSE_HEADERS).toBe("object");
    expect(typeof aip.StreamResponseSchema.parse).toBe("function");
    expect(typeof aip.StreamEventDataSchema.parse).toBe("function");
  });

  it("test_stream_server_exports", () => {
    expect(isClass(aip.StreamHub)).toBe(true);
    expect(isClass(aip.TaskStreamChannel)).toBe(true);
    expect(typeof aip.createAipStreamRequestListener).toBe("function");
    expect(typeof aip.formatSse).toBe("function");
  });

  it("test_stream_client_exports", () => {
    expect(isClass(aip.AipStreamClient)).toBe(true);
    expect(isClass(aip.StreamProtocolError)).toBe(true);
    expect(new aip.StreamProtocolError(1, "m")).toBeInstanceOf(Error);
  });

  it("test_stream_protocol_error_attrs", () => {
    const err = new aip.StreamProtocolError(-32000, "buffer expired", { seq: 5 });
    expect(err.code).toBe(-32000);
    // Python 把 message 属性覆盖为原始消息；Node 的 Error#message 保持完整文本，原始消息在 protocolMessage
    expect(err.protocolMessage).toBe("buffer expired");
    expect(err.message).toBe("StreamProtocolError -32000: buffer expired");
    expect(err.data).toEqual({ seq: 5 });
  });

  it("test_notification_model_exports", () => {
    expect(typeof aip.NOTIFICATION_TOKEN_HEADER).toBe("string");
    expect(typeof aip.NotificationConfigSchema.parse).toBe("function");
  });

  it("test_notification_server_exports", () => {
    for (const name of [
      "NotificationConfigStore",
      "NotificationDispatcher",
      "NotificationService",
      "NotificationRegistry",
    ]) {
      expect(isClass(exported[name]), name).toBe(true);
    }
    expect(typeof aip.createAipNotificationRequestListener).toBe("function");
  });

  it("test_notification_handlers_optional_fields", () => {
    const handlers: NotificationHandlers = {};
    expect(handlers.onNotificationStart).toBeUndefined();
    expect(new aip.NotificationService({ identityBindingEnabled: false }).handlers).toBeNull();
  });

  it("test_notification_client_exports", () => {
    expect(isClass(aip.AipNotificationClient)).toBe(true);
    expect(isClass(aip.NotificationReceiver)).toBe(true);
  });

  it("type-only exports are documented", () => {
    // 这些名字只存在于类型层面；下方的 TypeExports 让 tsc 校验它们确实被导出
    expect(TYPE_ONLY.every((name) => !(name in exported))).toBe(true);
  });
});

/** 类型层面的导出检查（`npx tsc --noEmit` 时生效）。 */
export type TypeExports = [
  AipIdentityBindingConfig,
  PeerIdentity,
  PeerCertificateLike,
  BufferedStreamEvent,
  StreamHandlers,
  NotificationHandlers,
  NotificationSubscription,
  CommandHandlers,
  DataItem,
  TextDataItem,
  FileDataItem,
  StructuredDataItem,
  Message,
  TaskCommand,
  TaskResult,
  TaskStatus,
  Product,
  GetCommandParams,
  StartCommandParams,
  StreamEventData,
  StreamEventPayload,
  StreamRequest,
  StreamRequestParams,
  StreamResponse,
  TaskStatusUpdateEvent,
  ProductChunkEvent,
  ReStreamCommandParams,
  NotificationConfig,
  NotificationDeleteRequest,
  NotificationDeleteResponse,
  NotificationDeleteResult,
  NotificationGetRequest,
  NotificationGetResponse,
  NotificationIdParams,
  NotificationRequest,
  NotificationResponse,
  NotificationStartParams,
  NotificationStartRequest,
  NotificationStartRequestParams,
];
