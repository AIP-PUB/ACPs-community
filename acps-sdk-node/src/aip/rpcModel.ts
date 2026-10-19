/**
 * AIP v2 RPC 数据模型定义（对应 Python `acps_sdk.aip.aip_rpc_model`）
 *
 * - JSON-RPC 基础类型：JSONRPCRequest, JSONRPCResponse, JSONRPCError
 * - RPC 请求/响应：RpcRequest, RpcResponse
 */

import { z } from "zod";

import { AnyDict, optional } from "../internal/schema.js";
import { TaskCommandSchema, TaskResultSchema } from "./baseModel.js";

export const JsonRpcIdSchema = z.union([z.string(), z.number().int()]);
export type JsonRpcId = z.output<typeof JsonRpcIdSchema>;

/** JSON-RPC 2.0 请求 */
export const JSONRPCRequestSchema = z.object({
  jsonrpc: z.literal("2.0").default("2.0"),
  method: z.string(),
  id: optional(JsonRpcIdSchema),
  params: optional(z.union([z.array(z.unknown()), AnyDict])),
});
export type JSONRPCRequest = z.output<typeof JSONRPCRequestSchema>;

/** JSON-RPC 2.0 错误 */
export const JSONRPCErrorSchema = z.object({
  code: z.number().int(),
  message: z.string(),
  data: optional(z.unknown()),
});
export type JSONRPCError = z.output<typeof JSONRPCErrorSchema>;

/**
 * 响应里的 id：按 JSON-RPC 2.0 与 AIP §5.1 必须出现，无法确定请求 id 时为 null。
 * 与其他空字段不同，这里保留 null（Python 的 FastAPI response_model 同样输出 `"id": null`）。
 */
export const JsonRpcResponseIdSchema = JsonRpcIdSchema.nullable().optional();

/** JSON-RPC 2.0 响应 */
export const JSONRPCResponseSchema = z.object({
  jsonrpc: z.literal("2.0").default("2.0"),
  id: JsonRpcResponseIdSchema,
  result: optional(z.unknown()),
  error: optional(JSONRPCErrorSchema),
});
export type JSONRPCResponse = z.output<typeof JSONRPCResponseSchema>;

/** RPC 请求参数 */
export const RpcRequestParamsSchema = z.object({
  /** AIP v2: 使用 TaskCommand 类型 */
  command: TaskCommandSchema,
});
export type RpcRequestParams = z.output<typeof RpcRequestParamsSchema>;

/** AIP RPC 请求 */
export const RpcRequestSchema = z.object({
  jsonrpc: z.literal("2.0").default("2.0"),
  method: z.literal("rpc").default("rpc"),
  id: optional(JsonRpcIdSchema),
  params: RpcRequestParamsSchema,
});
export type RpcRequest = z.output<typeof RpcRequestSchema>;

/** AIP RPC 响应 */
export const RpcResponseSchema = z.object({
  jsonrpc: z.literal("2.0").default("2.0"),
  id: JsonRpcResponseIdSchema,
  /** AIP v2: 使用 TaskResult 类型 */
  result: optional(TaskResultSchema),
  error: optional(JSONRPCErrorSchema),
});
export type RpcResponse = z.output<typeof RpcResponseSchema>;
