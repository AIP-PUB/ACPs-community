/**
 * AIP v2 流式传输数据模型定义（对应 Python `acps_sdk.aip.aip_stream_model`）
 *
 * - SSE 事件：TaskStatusUpdateEvent, ProductChunkEvent (均继承自 Message)
 * - 流式请求/响应：StreamRequest, StreamResponse
 * - 类型别名与 SSE 常量：StreamEventPayload, SSE_MEDIA_TYPE, SSE_HEADERS
 */

import { z } from "zod";

import { optional } from "../internal/schema.js";
import {
  ProductSchema,
  TaskCommandSchema,
  TaskResultSchema,
  TaskStatusSchema,
  messageShape,
} from "./baseModel.js";
import { JSONRPCErrorSchema, JsonRpcIdSchema } from "./rpcModel.js";

// ---------------------------------------------------------------------------
// SSE 常量
// ---------------------------------------------------------------------------

export const SSE_MEDIA_TYPE = "text/event-stream";
export const SSE_HEADERS: Readonly<Record<string, string>> = Object.freeze({
  "Cache-Control": "no-cache",
  Connection: "keep-alive",
});

// ---------------------------------------------------------------------------
// SSE 事件类型
// ---------------------------------------------------------------------------

/** AIP v2 任务状态更新事件，用于 SSE 流式传输中的任务状态更新通知。 */
export const TaskStatusUpdateEventSchema = z.object({
  type: z.literal("task-status-update").default("task-status-update"),
  ...messageShape,
  taskId: z.string(),
  status: TaskStatusSchema,
});
export type TaskStatusUpdateEvent = z.output<typeof TaskStatusUpdateEventSchema>;

/** AIP v2 产出物分块事件，用于 SSE 流式传输中的产出物分块传输。 */
export const ProductChunkEventSchema = z.object({
  type: z.literal("product-chunk").default("product-chunk"),
  ...messageShape,
  taskId: z.string(),
  product: ProductSchema,
  append: z.boolean(),
  lastChunk: z.boolean(),
});
export type ProductChunkEvent = z.output<typeof ProductChunkEventSchema>;

// ---------------------------------------------------------------------------
// 类型别名
// ---------------------------------------------------------------------------

/**
 * SSE 事件负载：eventData 字段可能包含的所有类型。
 *
 * 与 pydantic `Field(discriminator="type")` 一致：解析时 `type` 必须存在并据此选择具体类型。
 */
export const StreamEventPayloadSchema = z.discriminatedUnion("type", [
  TaskResultSchema.extend({ type: z.literal("task-result") }),
  TaskCommandSchema.extend({ type: z.literal("task-command") }),
  TaskStatusUpdateEventSchema.extend({ type: z.literal("task-status-update") }),
  ProductChunkEventSchema.extend({ type: z.literal("product-chunk") }),
]);
export type StreamEventPayload = z.output<typeof StreamEventPayloadSchema>;

// ---------------------------------------------------------------------------
// 流式请求 / 响应
// ---------------------------------------------------------------------------

/** 流式请求参数 */
export const StreamRequestParamsSchema = z.object({
  /** AIP v2: 使用 TaskCommand 类型 */
  message: TaskCommandSchema,
});
export type StreamRequestParams = z.output<typeof StreamRequestParamsSchema>;

/** AIP 流式请求 */
export const StreamRequestSchema = z.object({
  jsonrpc: z.literal("2.0").default("2.0"),
  method: z.literal("stream").default("stream"),
  id: optional(JsonRpcIdSchema),
  params: StreamRequestParamsSchema,
});
export type StreamRequest = z.output<typeof StreamRequestSchema>;

/** 流式事件数据（单条 SSE 事件的载体） */
export const StreamEventDataSchema = z.object({
  eventSeq: z.number().int(),
  eventData: StreamEventPayloadSchema,
});
export type StreamEventData = z.output<typeof StreamEventDataSchema>;

/**
 * AIP 流式响应
 *
 * result 与 error 互斥，二者至多其一存在。
 * - 正常事件帧：result 有值，error 为 undefined
 * - 错误帧（如 re-stream 不可续传）：error 有值，result 为 undefined
 */
export const StreamResponseSchema = z.object({
  jsonrpc: z.literal("2.0").default("2.0"),
  id: optional(JsonRpcIdSchema),
  result: optional(StreamEventDataSchema),
  error: optional(JSONRPCErrorSchema),
});
export type StreamResponse = z.output<typeof StreamResponseSchema>;

// ---------------------------------------------------------------------------
// 重连参数
// ---------------------------------------------------------------------------

/** 重连流式传输命令参数 */
export const ReStreamCommandParamsSchema = z.object({
  lastEventSeq: optional(z.number().int()),
});
export type ReStreamCommandParams = z.output<typeof ReStreamCommandParamsSchema>;
