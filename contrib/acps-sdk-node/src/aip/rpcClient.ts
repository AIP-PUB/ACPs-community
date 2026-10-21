/**
 * AIP Direct RPC 客户端（Leader 侧，对应 Python `acps_sdk.aip.aip_rpc_client`）
 *
 * 支持 HTTP 与 HTTPS + mTLS。启用身份绑定时：
 * - 发送前校验命令的 senderId 等于本端 leaderId；
 * - 校验 TLS 服务端证书中的 AIC 等于 expectedPartnerAic；
 * - 校验返回的 TaskResult.senderId 等于 TLS 服务端 AIC。
 *
 * 可选注入 AccessEmitter，在每次 RPC 前后发射 caller 侧 Access 日志（AMP）。
 */

import { randomUUID } from "node:crypto";

import type { AccessEmitter } from "../amp/accessEmitter.js";
import type { AccessBody, ErrorInfo } from "../amp/models.js";
import {
  type TraceContext,
  TRACEPARENT_HEADER,
  formatTraceparent,
  newSpanId,
  newTraceId,
} from "../amp/traceContext.js";
import {
  type HttpTransport,
  type TlsClientOptions,
  HttpStatusError,
  createNodeTransport,
} from "../internal/http.js";
import { getLogger } from "../internal/logger.js";
import { utcNowIso } from "../internal/time.js";
import { errorInfoFromRpcResponse, statusFromRpcResponse } from "./accessStatus.js";
import { type DataItem, type TaskCommand, type TaskResult, TaskCommandType } from "./baseModel.js";
import {
  assertAicMatchesExpected,
  assertSenderMatchesExpected,
  assertSenderMatchesPeer,
  extractPeerIdentity,
} from "./identity.js";
import { type RpcRequest, type RpcResponse, RpcResponseSchema } from "./rpcModel.js";

const logger = getLogger("acps_sdk.aip.aip_rpc_client");

export interface AipRpcClientOptions {
  /** HTTPS/mTLS 选项（对应 Python 的 ssl_context） */
  tls?: TlsClientOptions | null;
  /** 注入传输层（测试用，对应 Python 的 transport） */
  transport?: HttpTransport | null;
  /** 可选 Access 发射器（opt-in caller 埋点） */
  accessEmitter?: AccessEmitter | null;
  /** 被调方 AIC（AccessBody.callee） */
  calleeAic?: string;
  /** 主调服务名 */
  callerService?: string;
  /** 被调服务名 */
  calleeService?: string;
  /** 返回当前 trace 上下文；无则每次 RPC 新生成 trace_id */
  traceContextProvider?: (() => TraceContext | null | undefined) | null;
  /** 期望的 Partner AIC；启用身份绑定时必填 */
  expectedPartnerAic?: string | null;
  identityBindingEnabled?: boolean;
  /** 单次请求超时（毫秒），默认 30000 */
  timeoutMs?: number;
}

/**
 * A client for interacting with an AIP-compliant Partner over RPC.
 */
export class AipRpcClient {
  readonly partnerUrl: string;
  readonly leaderId: string;
  private readonly transport: HttpTransport;
  private readonly accessEmitter: AccessEmitter | null;
  private readonly calleeAic: string;
  private readonly callerService: string;
  private readonly calleeService: string;
  private readonly traceContextProvider: (() => TraceContext | null | undefined) | null;
  private readonly expectedPartnerAic: string | null;
  private readonly identityBindingEnabled: boolean;
  private readonly timeoutMs: number;

  constructor(partnerUrl: string, leaderId: string, options: AipRpcClientOptions = {}) {
    this.partnerUrl = partnerUrl;
    this.leaderId = leaderId;
    this.accessEmitter = options.accessEmitter ?? null;
    this.calleeAic = options.calleeAic ?? "";
    this.callerService = options.callerService ?? "";
    this.calleeService = options.calleeService ?? "";
    this.traceContextProvider = options.traceContextProvider ?? null;
    this.expectedPartnerAic = options.expectedPartnerAic ?? null;
    this.identityBindingEnabled = options.identityBindingEnabled ?? true;
    this.timeoutMs = options.timeoutMs ?? 30_000;

    if (this.identityBindingEnabled && !this.expectedPartnerAic) {
      throw new Error("expected_partner_aic is required when identity_binding_enabled=True");
    }
    if (!this.identityBindingEnabled) {
      logger.warn(`AIP identity binding disabled for RPC client partner_url=${this.partnerUrl}`);
    }
    this.transport = options.transport ?? createNodeTransport(options.tls ?? undefined);
  }

  /** Sends a task command to the Partner and returns the response. */
  async sendRequest(command: TaskCommand): Promise<RpcResponse> {
    const requestId = randomUUID();
    const rpcRequest: RpcRequest = {
      jsonrpc: "2.0",
      method: "rpc",
      id: requestId,
      params: { command },
    };
    const requestBody = JSON.stringify(rpcRequest);
    const requestBytes = Buffer.byteLength(requestBody, "utf-8");
    const method = String(command.command);

    const headers: Record<string, string> = { "Content-Type": "application/json" };
    let traceId: string | null = null;
    let spanId: string | null = null;
    const parentSpanId = "";

    if (this.accessEmitter) {
      const parent = this.traceContextProvider?.() ?? null;
      traceId = parent ? parent.traceId : newTraceId();
      spanId = newSpanId();
      headers[TRACEPARENT_HEADER] = formatTraceparent({ traceId, spanId, sampled: true });
    }

    const startedAt = performance.now();
    let statusCode = 500;
    let responseBytes = 0;
    let errorInfo: ErrorInfo | null = null;

    try {
      if (this.identityBindingEnabled) {
        assertSenderMatchesExpected(command, this.leaderId);
      }
      const response = await this.transport({
        url: this.partnerUrl,
        method: "POST",
        headers,
        body: requestBody,
        timeoutMs: this.timeoutMs,
      });

      let remoteAic: string | null = null;
      if (this.identityBindingEnabled) {
        remoteAic = extractPeerIdentity(response.peerCertificate)?.aic ?? null;
        remoteAic = assertAicMatchesExpected(remoteAic, this.expectedPartnerAic, {
          actualLabel: "TLS server AIC",
          expectedLabel: "expected_partner_aic",
        });
      }

      if (response.status < 200 || response.status >= 300) {
        statusCode = response.status;
        responseBytes = response.body.length;
        errorInfo = { code: statusCode, message: response.body.toString("utf-8").slice(0, 500) };
        throw new HttpStatusError(response.status, response.body);
      }

      responseBytes = response.body.length;
      const rpcResponse = RpcResponseSchema.parse(JSON.parse(response.body.toString("utf-8")));
      statusCode = statusFromRpcResponse(rpcResponse);
      errorInfo = errorInfoFromRpcResponse(rpcResponse);

      if (rpcResponse.error) {
        throw new Error(`RPC Error: ${rpcResponse.error.code} - ${rpcResponse.error.message}`);
      }
      if (rpcResponse.id !== requestId) {
        throw new Error("RPC Error: Response ID does not match request ID.");
      }
      if (this.identityBindingEnabled && rpcResponse.result) {
        assertSenderMatchesPeer(rpcResponse.result, remoteAic);
      }
      return rpcResponse;
    } catch (error) {
      if (errorInfo === null) {
        errorInfo = { code: statusCode, message: "rpc failed" };
      }
      throw error;
    } finally {
      if (this.accessEmitter && traceId !== null && spanId !== null) {
        const body: AccessBody = {
          request: {
            method,
            url: this.partnerUrl,
            route: "/rpc",
            headers: {
              ...Object.fromEntries(
                Object.entries(headers).filter(([name]) => name.toLowerCase() !== "content-type"),
              ),
              ...(headers["Content-Type"] ? { "content-type": headers["Content-Type"] } : {}),
            },
            bodySizeBytes: requestBytes,
          },
          response: { statusCode, bodySizeBytes: responseBytes },
          caller: { aic: this.leaderId, serviceName: this.callerService },
          callee: { aic: this.calleeAic, serviceName: this.calleeService },
          error: errorInfo ?? undefined,
          durationMs: Math.trunc(performance.now() - startedAt),
        };
        try {
          await this.accessEmitter.emit(body, {
            traceId,
            spanId,
            parentSpanId,
            correlationId: command.sessionId,
          });
        } catch {
          // 埋点失败不影响业务
        }
      }
    }
  }

  /** Helper to create a new TaskCommand object. */
  createTaskCommand(
    command: TaskCommandType,
    taskId: string,
    sessionId: string,
    textContent?: string | null,
  ): TaskCommand {
    const dataItems: DataItem[] = textContent ? [{ type: "text", text: textContent }] : [];
    return {
      type: "task-command",
      id: `cmd-${randomUUID()}`,
      sentAt: utcNowIso(),
      senderRole: "leader",
      senderId: this.leaderId,
      dataItems: dataItems.length > 0 ? dataItems : undefined,
      sessionId,
      command,
      taskId,
    };
  }

  private async resultOf(command: TaskCommand): Promise<TaskResult> {
    const response = await this.sendRequest(command);
    if (response.result) {
      return response.result;
    }
    throw new TypeError(`Expected TaskResult, got ${typeof response.result}`);
  }

  /** Starts a new task with the Partner. */
  async startTask(
    sessionId: string,
    userInput: string,
    taskId?: string | null,
  ): Promise<TaskResult> {
    const id = taskId || `task-${randomUUID()}`;
    return this.resultOf(this.createTaskCommand(TaskCommandType.Start, id, sessionId, userInput));
  }

  /** Continues a task that is in a waiting state. */
  async continueTask(taskId: string, sessionId: string, userInput: string): Promise<TaskResult> {
    return this.resultOf(
      this.createTaskCommand(TaskCommandType.Continue, taskId, sessionId, userInput),
    );
  }

  /** Marks a task as completed. */
  async completeTask(taskId: string, sessionId: string): Promise<TaskResult> {
    return this.resultOf(this.createTaskCommand(TaskCommandType.Complete, taskId, sessionId));
  }

  /** Cancels a task that is in a non-terminal state. */
  async cancelTask(taskId: string, sessionId: string): Promise<TaskResult> {
    return this.resultOf(this.createTaskCommand(TaskCommandType.Cancel, taskId, sessionId));
  }

  /** Retrieves the current state of a task. */
  async getTask(taskId: string, sessionId: string): Promise<TaskResult> {
    return this.resultOf(this.createTaskCommand(TaskCommandType.Get, taskId, sessionId));
  }

  /** Closes the client（Node 版每次请求独立连接，无需释放；保留以对齐 Python API）。 */
  async close(): Promise<void> {
    return undefined;
  }
}
