/**
 * AIP v2 通知方式客户端（Leader 侧，对应 Python `acps_sdk.aip.aip_notification_client`）
 *
 * - AipNotificationClient  —— 向 Partner 通知服务发起 CRUD + start 操作
 * - NotificationReceiver   —— Leader 侧接收 Partner 推送回调的端点
 */

import { createHash, randomUUID, timingSafeEqual } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";

import {
  type HttpTransport,
  type TlsClientOptions,
  HttpStatusError,
  createNodeTransport,
} from "../internal/http.js";
import { getLogger } from "../internal/logger.js";
import { formatZodError } from "../internal/schema.js";
import { utcNowIso } from "../internal/time.js";
import {
  type TaskCommand,
  type TaskResult,
  type TaskState,
  TaskCommandType,
  TaskResultSchema,
} from "./baseModel.js";
import {
  AipIdentityError,
  AUTHENTICATION_REQUIRED_CODE,
  assertAicMatchesExpected,
  assertSenderMatchesExpected,
  assertSenderMatchesPeer,
  extractPeerIdentity,
  identityErrorToJsonRpc,
} from "./identity.js";
import {
  type NotificationConfig,
  type NotificationDeleteRequest,
  type NotificationGetRequest,
  type NotificationRequest,
  type NotificationStartRequest,
  NOTIFICATION_TOKEN_HEADER,
  NotificationConfigSchema,
} from "./notificationModel.js";
import { getRequestPeerAic } from "./peerCert.js";
import { readRequestBody, writeJson } from "./rpcServer.js";

const logger = getLogger("acps_sdk.aip.aip_notification_client");

// ===========================================================================
// AipNotificationClient
// ===========================================================================

export interface AipNotificationClientOptions {
  tls?: TlsClientOptions | null;
  transport?: HttpTransport | null;
  expectedPartnerAic?: string | null;
  identityBindingEnabled?: boolean;
}

/**
 * Leader 侧通知服务客户端：向 Partner 的 /notification/* 端点发送 JSON-RPC 请求，管理通知配置和订阅。
 */
export class AipNotificationClient {
  readonly partnerUrl: string;
  readonly leaderId: string;
  private readonly expectedPartnerAic: string | null;
  private readonly identityBindingEnabled: boolean;
  private readonly transport: HttpTransport;

  constructor(partnerUrl: string, leaderId: string, options: AipNotificationClientOptions = {}) {
    this.partnerUrl = partnerUrl.replace(/\/+$/, "");
    this.leaderId = leaderId;
    this.expectedPartnerAic = options.expectedPartnerAic ?? null;
    this.identityBindingEnabled = options.identityBindingEnabled ?? true;
    if (this.identityBindingEnabled && !this.expectedPartnerAic) {
      throw new Error("expected_partner_aic is required when identity_binding_enabled=True");
    }
    if (!this.identityBindingEnabled) {
      logger.warn(
        `AIP identity binding disabled for Notification client partner_url=${this.partnerUrl}`,
      );
    }
    this.transport = options.transport ?? createNodeTransport(options.tls ?? undefined);
  }

  /** 向 partnerUrl + path 发送 POST，返回解析后的 JSON 响应体。 */
  private async post(path: string, body: unknown): Promise<Record<string, unknown>> {
    const url = `${this.partnerUrl}/${path.replace(/^\/+/, "")}`;
    const response = await this.transport({
      url,
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      timeoutMs: 30_000,
    });
    if (this.identityBindingEnabled) {
      assertAicMatchesExpected(
        extractPeerIdentity(response.peerCertificate)?.aic ?? null,
        this.expectedPartnerAic,
        {
          actualLabel: "TLS server AIC",
          expectedLabel: "expected_partner_aic",
        },
      );
    }
    if (response.status < 200 || response.status >= 300) {
      throw new HttpStatusError(response.status, response.body);
    }
    return JSON.parse(response.body.toString("utf-8")) as Record<string, unknown>;
  }

  /** 注册或更新通知配置。 */
  async setConfig(
    taskId: string,
    callbackUrl: string,
    token: string,
    configId?: string | null,
  ): Promise<NotificationConfig> {
    const request: NotificationRequest = {
      jsonrpc: "2.0",
      method: "notification/set",
      id: randomUUID(),
      params: { id: configId ?? undefined, url: callbackUrl, token, taskId },
    };
    const resp = await this.post("notification/set", request);
    const result = resp["result"];
    if (result === undefined || result === null) {
      throw new Error(`notification/set returned no result: ${JSON.stringify(resp)}`);
    }
    return NotificationConfigSchema.parse(result);
  }

  /** setConfig 的别名（向下兼容）。 */
  async setNotification(
    taskId: string,
    callbackUrl: string,
    token: string,
    configId?: string | null,
  ): Promise<NotificationConfig> {
    return this.setConfig(taskId, callbackUrl, token, configId);
  }

  /** 删除通知配置，返回 true 表示成功。 */
  async deleteConfig(taskId: string, configId?: string | null): Promise<boolean> {
    const request: NotificationDeleteRequest = {
      jsonrpc: "2.0",
      method: "notification/delete",
      id: randomUUID(),
      params: { taskId, notificationConfigId: configId ?? undefined },
    };
    const resp = await this.post("notification/delete", request);
    const result = (resp["result"] ?? {}) as Record<string, unknown>;
    return Boolean(result["success"] ?? false);
  }

  /** deleteConfig 的别名（向下兼容）。 */
  async deleteNotification(taskId: string, configId?: string | null): Promise<boolean> {
    return this.deleteConfig(taskId, configId);
  }

  /** 查询通知配置列表。 */
  async getConfigs(taskId: string, configId?: string | null): Promise<NotificationConfig[]> {
    const request: NotificationGetRequest = {
      jsonrpc: "2.0",
      method: "notification/get",
      id: randomUUID(),
      params: { taskId, notificationConfigId: configId ?? undefined },
    };
    const resp = await this.post("notification/get", request);
    const result = (resp["result"] ?? []) as unknown[];
    return result.map((item) => NotificationConfigSchema.parse(item));
  }

  /** getConfigs 的别名（向下兼容）。 */
  async getNotifications(taskId: string, configId?: string | null): Promise<NotificationConfig[]> {
    return this.getConfigs(taskId, configId);
  }

  /** 启动通知订阅（注册该任务对应的配置到 Partner 的 registry）。 */
  async startNotification(
    taskId: string,
    configId: string,
    sessionId: string,
    notifyOnStates?: Array<TaskState | string> | null,
  ): Promise<boolean> {
    const commandParams: Record<string, unknown> = { notificationConfigId: configId };
    if (notifyOnStates && notifyOnStates.length > 0) {
      commandParams["notifyOnStates"] = [...notifyOnStates];
    }
    const command: TaskCommand = {
      type: "task-command",
      id: `cmd-${randomUUID()}`,
      sentAt: utcNowIso(),
      senderRole: "leader",
      senderId: this.leaderId,
      sessionId,
      command: TaskCommandType.Start,
      commandParams,
      taskId,
    };
    if (this.identityBindingEnabled) {
      assertSenderMatchesExpected(command, this.leaderId);
    }
    const request: NotificationStartRequest = {
      jsonrpc: "2.0",
      method: "notification/start",
      id: randomUUID(),
      params: { message: command },
    };
    const resp = await this.post("notification/start", request);
    return Boolean(resp["result"] ?? false);
  }

  /** 释放资源（保留以对齐 Python API）。 */
  async close(): Promise<void> {
    return undefined;
  }
}

// ===========================================================================
// NotificationReceiver
// ===========================================================================

/** 时序安全的字符串比较（对应 Python `secrets.compare_digest`，长度不同也不提前返回）。 */
export function constantTimeEquals(a: string, b: string): boolean {
  const left = createHash("sha256").update(a, "utf-8").digest();
  const right = createHash("sha256").update(b, "utf-8").digest();
  return timingSafeEqual(left, right) && a.length === b.length;
}

export interface NotificationReceiverOptions {
  /** 自定义 token 校验器（多任务场景，按任务 token 校验） */
  tokenValidator?: ((provided: string, taskResult: TaskResult) => boolean) | null;
  identityBindingEnabled?: boolean;
}

/**
 * Leader 侧通知回调接收器。
 *
 * 对收到的 POST 请求：
 * 1. 请求体解析为 TaskResult
 * 2. 身份绑定校验（senderId == mTLS 对端 AIC）
 * 3. Token 时序安全校验（固定 token 或自定义 tokenValidator）
 * 4. 异步调用注册的 handler（fire-and-forget）
 */
export class NotificationReceiver {
  private readonly tokenValidator: ((provided: string, taskResult: TaskResult) => boolean) | null;
  private readonly identityBindingEnabled: boolean;

  constructor(
    private readonly token: string,
    private readonly handler: (taskResult: TaskResult) => Promise<void>,
    { tokenValidator = null, identityBindingEnabled = true }: NotificationReceiverOptions = {},
  ) {
    this.tokenValidator = tokenValidator;
    this.identityBindingEnabled = identityBindingEnabled;
    if (!identityBindingEnabled) {
      logger.warn("AIP identity binding disabled for Notification receiver");
    }
  }

  /** Token 校验：时序安全。注入了 tokenValidator 时委托给它。 */
  validateToken(provided: string | null | undefined, taskResult?: TaskResult | null): boolean {
    if (!provided) {
      return false;
    }
    if (this.tokenValidator && taskResult) {
      return this.tokenValidator(provided, taskResult);
    }
    return constantTimeEquals(this.token, provided);
  }

  /**
   * 与框架无关的回调处理：返回 `{status, body}`。
   * 先解析请求体获取 TaskResult，再校验 token（使得 tokenValidator 可用 taskResult）。
   */
  async handle(context: {
    /** 原始请求体；与 parsedBody 二选一 */
    rawBody?: Buffer | string;
    /** 框架已解析好的 JSON 请求体（如 Express `express.json()` 写入的 `req.body`） */
    parsedBody?: unknown;
    getPeerAic: () => string | null;
    getHeader: (name: string) => string | undefined;
  }): Promise<{ status: number; body: unknown }> {
    let taskResult: TaskResult;
    try {
      let payload: unknown;
      if (context.parsedBody !== undefined) {
        payload = context.parsedBody;
      } else {
        const raw = context.rawBody ?? "";
        payload = JSON.parse(typeof raw === "string" ? raw : raw.toString("utf-8"));
      }
      const parsed = TaskResultSchema.safeParse(payload);
      if (!parsed.success) {
        return { status: 400, body: { detail: `Parse error: ${formatZodError(parsed.error)}` } };
      }
      taskResult = parsed.data;
    } catch (error) {
      return {
        status: 400,
        body: { detail: `Parse error: ${error instanceof Error ? error.message : String(error)}` },
      };
    }

    if (this.identityBindingEnabled) {
      try {
        assertSenderMatchesPeer(taskResult, context.getPeerAic());
      } catch (error) {
        if (error instanceof AipIdentityError) {
          const rpcError = identityErrorToJsonRpc(error);
          return {
            status: rpcError.code === AUTHENTICATION_REQUIRED_CODE ? 401 : 403,
            body: { detail: rpcError },
          };
        }
        throw error;
      }
    }

    if (!this.validateToken(context.getHeader(NOTIFICATION_TOKEN_HEADER), taskResult)) {
      return { status: 401, body: { detail: "Invalid or missing notification token" } };
    }

    void Promise.resolve()
      .then(() => this.handler(taskResult))
      .catch((error: unknown) => logger.error("notification handler failed", error));
    return { status: 200, body: { ok: true } };
  }

  /** 返回 `(req, res)` 监听函数（对应 Python `receiver.mount(app, path)`，路径匹配交给调用方）。 */
  createRequestListener(): (req: IncomingMessage, res: ServerResponse) => Promise<void> {
    return async (req, res) => {
      try {
        // 兼容 Express 等框架已读取并解析的请求体
        const preParsed = (req as IncomingMessage & { body?: unknown }).body;
        const bodySource =
          preParsed !== undefined && typeof preParsed !== "string" && !Buffer.isBuffer(preParsed)
            ? { parsedBody: preParsed }
            : { rawBody: preParsed ?? (await readRequestBody(req)) };
        const result = await this.handle({
          ...bodySource,
          getPeerAic: () => getRequestPeerAic(req),
          getHeader: (name) => {
            const value = req.headers[name.toLowerCase()];
            return Array.isArray(value) ? value[0] : value;
          },
        });
        writeJson(res, result.status, result.body);
      } catch (error) {
        logger.error("notification receiver failed", error);
        writeJson(res, 500, { detail: "Internal Server Error" });
      }
    };
  }
}

/** 对 receiver.createRequestListener 的命名包装（对应 Python `add_aip_notification_receiver_router`）。 */
export function createAipNotificationReceiverListener(
  receiver: NotificationReceiver,
): (req: IncomingMessage, res: ServerResponse) => Promise<void> {
  return receiver.createRequestListener();
}
