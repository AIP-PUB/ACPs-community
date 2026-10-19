/**
 * AIP v2 通知方式数据模型定义（对应 Python `acps_sdk.aip.aip_notification_model`）
 *
 * - 通知配置：NotificationConfig
 * - 四类 JSON-RPC 请求/响应：set / delete / get / start
 * - 常量：NOTIFICATION_TOKEN_HEADER
 */

import { z } from "zod";

import { optional } from "../internal/schema.js";
import { TaskCommandSchema, TaskStateSchema } from "./baseModel.js";
import { JSONRPCErrorSchema, JsonRpcIdSchema } from "./rpcModel.js";

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------

/** HTTP 请求头名称：通知回调时用于 token 校验。 */
export const NOTIFICATION_TOKEN_HEADER = "X-ACPs-AIP-Notification-Token";

const jsonRpcRequestBase = <M extends string>(method: M) => ({
  jsonrpc: z.literal("2.0").default("2.0"),
  method: z.literal(method).default(method),
  id: optional(JsonRpcIdSchema),
});

const jsonRpcResponseBase = {
  jsonrpc: z.literal("2.0").default("2.0"),
  id: optional(JsonRpcIdSchema),
};

// ---------------------------------------------------------------------------
// 通知配置
// ---------------------------------------------------------------------------

/**
 * 通知配置
 *
 * 由 Leader 通过 notification/set 注册，Partner 存储后用于推送回调。
 */
export const NotificationConfigSchema = z.object({
  /** Partner 侧生成；Leader 发送时可不填 */
  id: optional(z.string()),
  /** 回调 URL（Leader 端接收通知的端点） */
  url: z.string(),
  /** 用于签名/验证的共享 token */
  token: z.string(),
  /** 关联的任务 ID */
  taskId: z.string(),
});
export type NotificationConfig = z.output<typeof NotificationConfigSchema>;

// ---------------------------------------------------------------------------
// notification/set
// ---------------------------------------------------------------------------

/** AIP notification/set 请求 */
export const NotificationRequestSchema = z.object({
  ...jsonRpcRequestBase("notification/set"),
  params: NotificationConfigSchema,
});
export type NotificationRequest = z.output<typeof NotificationRequestSchema>;

/** AIP notification/set 响应（result 包含完整配置，含服务端生成的 id） */
export const NotificationResponseSchema = z.object({
  ...jsonRpcResponseBase,
  result: optional(NotificationConfigSchema),
  error: optional(JSONRPCErrorSchema),
});
export type NotificationResponse = z.output<typeof NotificationResponseSchema>;

// ---------------------------------------------------------------------------
// notification/delete & notification/get 公共参数
// ---------------------------------------------------------------------------

/** notification/delete 与 notification/get 共用的请求参数 */
export const NotificationIdParamsSchema = z.object({
  taskId: z.string(),
  /** 不填则操作该 task 全部配置 */
  notificationConfigId: optional(z.string()),
});
export type NotificationIdParams = z.output<typeof NotificationIdParamsSchema>;

// ---------------------------------------------------------------------------
// notification/delete
// ---------------------------------------------------------------------------

/** AIP notification/delete 请求 */
export const NotificationDeleteRequestSchema = z.object({
  ...jsonRpcRequestBase("notification/delete"),
  params: NotificationIdParamsSchema,
});
export type NotificationDeleteRequest = z.output<typeof NotificationDeleteRequestSchema>;

/** notification/delete 成功结果 */
export const NotificationDeleteResultSchema = z.object({
  success: z.literal(true).default(true),
});
export type NotificationDeleteResult = z.output<typeof NotificationDeleteResultSchema>;

/** AIP notification/delete 响应 */
export const NotificationDeleteResponseSchema = z.object({
  ...jsonRpcResponseBase,
  result: optional(NotificationDeleteResultSchema),
  error: optional(JSONRPCErrorSchema),
});
export type NotificationDeleteResponse = z.output<typeof NotificationDeleteResponseSchema>;

// ---------------------------------------------------------------------------
// notification/get
// ---------------------------------------------------------------------------

/** AIP notification/get 请求 */
export const NotificationGetRequestSchema = z.object({
  ...jsonRpcRequestBase("notification/get"),
  params: NotificationIdParamsSchema,
});
export type NotificationGetRequest = z.output<typeof NotificationGetRequestSchema>;

/** AIP notification/get 响应（result 为配置列表） */
export const NotificationGetResponseSchema = z.object({
  ...jsonRpcResponseBase,
  result: optional(z.array(NotificationConfigSchema)),
  error: optional(JSONRPCErrorSchema),
});
export type NotificationGetResponse = z.output<typeof NotificationGetResponseSchema>;

// ---------------------------------------------------------------------------
// notification/start
// ---------------------------------------------------------------------------

/** notification/start 命令参数（嵌入在 TaskCommand.commandParams 中） */
export const NotificationStartParamsSchema = z.object({
  notificationConfigId: z.string(),
  notifyOnStates: optional(z.array(TaskStateSchema)),
});
export type NotificationStartParams = z.output<typeof NotificationStartParamsSchema>;

/** notification/start 的 JSON-RPC params 字段 */
export const NotificationStartRequestParamsSchema = z.object({
  /** 使用 message 字段（与 stream 保持一致） */
  message: TaskCommandSchema,
});
export type NotificationStartRequestParams = z.output<typeof NotificationStartRequestParamsSchema>;

/** AIP notification/start 请求 */
export const NotificationStartRequestSchema = z.object({
  ...jsonRpcRequestBase("notification/start"),
  params: NotificationStartRequestParamsSchema,
});
export type NotificationStartRequest = z.output<typeof NotificationStartRequestSchema>;
