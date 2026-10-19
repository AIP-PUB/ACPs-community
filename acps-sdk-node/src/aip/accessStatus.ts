/**
 * AIP RPC 响应到 Access statusCode / ErrorInfo 的共享映射（caller/callee 同口径）。
 * 对应 Python `acps_sdk.aip.access_status`。
 */

import type { ErrorInfo } from "../amp/models.js";
import { TaskState } from "./baseModel.js";
import type { JSONRPCError, RpcResponse } from "./rpcModel.js";

const RPC_ERROR_STATUS = new Map<number, number>([
  [-32001, 404],
  [-32602, 400],
  [-32700, 400],
  [-32603, 500],
]);

/** JSON-RPC error → HTTP 风格 statusCode。 */
export function statusFromRpcError(error: JSONRPCError | null | undefined): number {
  if (!error) {
    return 200;
  }
  return RPC_ERROR_STATUS.get(error.code) ?? 500;
}

/** 从 RpcResponse 提取 ErrorInfo（含 Rejected 语义）。 */
export function errorInfoFromRpcResponse(resp: RpcResponse): ErrorInfo | null {
  if (resp.error) {
    return { code: statusFromRpcError(resp.error), message: resp.error.message };
  }
  const state = resp.result?.status.state;
  if (state === TaskState.Failed) {
    return { code: 500, message: "task failed" };
  }
  if (state === TaskState.Rejected) {
    return { code: "REJECTED", message: "task rejected" };
  }
  return null;
}

/** RpcResponse → statusCode（Rejected 映射为 200，靠 error.code 计错）。 */
export function statusFromRpcResponse(resp: RpcResponse): number {
  if (resp.error) {
    return statusFromRpcError(resp.error);
  }
  return resp.result?.status.state === TaskState.Failed ? 500 : 200;
}
