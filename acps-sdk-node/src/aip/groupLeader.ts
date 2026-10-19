/**
 * 群组模式客户端模块 - Leader 角色的消息队列通信实现（对应 Python `acps_sdk.aip.aip_group_leader`）
 *
 * 本模块实现了 AIP 协议中群组模式（Group Mode）下 Leader 角色的核心功能：
 * 1. 连接 RabbitMQ 消息队列服务器
 * 2. 创建群组 Exchange 和队列
 * 3. 发送群组消息（任务消息、管理消息）
 * 4. 接收和处理群组内的消息
 * 5. 管理群组成员（邀请加入、踢出、静音等）
 *
 * AMQP 访问经由 `./groupAmqp.js` 的 aio-pika 形状接口（默认 amqplib 实现）；
 * 直连 RPC 邀请走 `src/internal/http.ts` 的传输层（可注入 `transport`）。
 */

import { randomUUID } from "node:crypto";
import { STATUS_CODES } from "node:http";

import type { MessageEmitter } from "../amp/messageEmitter.js";
import { buildReceiveBody, buildSendBody, buildSettlementBody } from "../amp/messageTap.js";
import type { MessageBody } from "../amp/models.js";
import { pyRepr, pyStr, pyStrip, pyTruthy, pythonJsonDumps } from "../internal/pyCompat.js";
import {
  TRACEPARENT_HEADER,
  type TraceContext,
  formatTraceparent,
  newSpanId,
  newTraceId,
  parseTraceparent,
} from "../amp/traceContext.js";
import {
  type HttpResponseData,
  type HttpTransport,
  type TlsClientOptions,
  HttpStatusError,
  createNodeTransport,
} from "../internal/http.js";
import { getLogger } from "../internal/logger.js";
import { utcNowIso } from "../internal/time.js";
import {
  type DataItem,
  type TaskCommand,
  type TaskResult,
  type TextDataItem,
  TaskCommandSchema,
  TaskCommandType,
  TaskResultSchema,
  TaskState,
} from "./baseModel.js";
import {
  type AmqpChannel,
  type AmqpConnection,
  type AmqpConnector,
  type AmqpExchange,
  type AmqpIncomingMessage,
  type AmqpQueue,
  AmqpMessage,
  DeliveryError,
  DeliveryMode,
  ExchangeType,
  channelNeedsClose,
  defaultAmqpConnector,
} from "./groupAmqp.js";
import { type GroupAclClientLike, GroupAclClient } from "./groupAuth.js";
import {
  assertIncomingGroupMessageIdentity,
  buildOutgoingAmqpUserId,
  extractAmqpUserId,
} from "./groupIdentity.js";
import {
  type ACSObject,
  type GroupMgmtCommand,
  type GroupMgmtResult,
  type InboxGroupInvitation,
  type InboxGroupInvitationError,
  type RabbitMQRequest,
  GroupMgmtCommandSchema,
  GroupMgmtCommandType,
  GroupMgmtResultSchema,
  InboxGroupInvitationErrorSchema,
  InboxGroupInvitationSchema,
  RabbitMQRequestSchema,
  RabbitMQResponseSchema,
  dumpGroupMessage,
} from "./groupModel.js";
import {
  type CancellableTask,
  AsyncEvent,
  AsyncLock,
  CancelledError,
  DEFAULT_INVITATION_TIMEOUT_SECONDS,
  Deferred,
  INBOX_EXCHANGE_NAME,
  INBOX_MESSAGE_TTL_MS,
  INBOX_QUEUE_EXPIRES_MS,
  JSONDecodeError,
  buildExternalConnectionUrl,
  buildGroupExchangeName,
  buildGroupQueueName,
  buildInboxQueueName,
  buildPlainConnectionUrl,
  calculateInvitationExpiry,
  createInvitationToken,
  createTask,
  decodeJsonBody,
  ensureValidGroupId,
  getEndpointUrl,
  normalizeGroupId,
  parseAmqpEndpointUrl,
  pyErrorStr,
  pySlice,
  redactToken,
  redactUrl,
  asOptionalString,
  dictGet,
  headerValue,
  requireJsonDict,
  sleep,
} from "./groupRuntime.js";

const logger = getLogger("acps_sdk.aip.aip_group_leader");

const DATA_PLANE_TYPES: ReadonlySet<unknown> = new Set(["task-command", "task-result"]);

/** 群组 Leader 状态枚举 */
export const GroupLeaderState = {
  DISCONNECTED: "DISCONNECTED",
  READY: "READY",
  DISSOLVING: "DISSOLVING",
} as const;
export type GroupLeaderState = (typeof GroupLeaderState)[keyof typeof GroupLeaderState];

/** Partner 邀请失败基类 */
export class PartnerInviteError extends Error {
  override name = "PartnerInviteError";
}

/** Partner 邀请网络错误 */
export class PartnerNetworkError extends PartnerInviteError {
  override name = "PartnerNetworkError";
}

/** Partner 返回错误响应 */
export class PartnerResponseError extends PartnerInviteError {
  override name = "PartnerResponseError";
}

/** 等待 Partner 通过 inbox 接受邀请的挂起记录（对应 Python `PendingInvitation` dataclass）。 */
export interface PendingInvitation {
  sessionId: string;
  partnerAic: string;
  invitationToken: string;
  future: Deferred<PartnerConnectionInfo>;
  timeoutTask: CancellableTask;
}

/** Partner 连接信息 */
export class PartnerConnectionInfo {
  aic: string;
  connectionName: string;
  vhost: string;
  nodeName: string;
  queueName: string;
  processId: string | null;
  connected = true;
  muted = false;
  joinedAt: string;

  constructor(
    aic: string,
    connectionName: string,
    vhost: string,
    nodeName: string,
    queueName: string,
    processId: string | null = null,
  ) {
    this.aic = aic;
    this.connectionName = connectionName;
    this.vhost = vhost;
    this.nodeName = nodeName;
    this.queueName = queueName;
    this.processId = processId;
    this.joinedAt = utcNowIso();
  }

  /** 与 Python `to_dict()` 相同的 snake_case 键。 */
  toDict(): Record<string, unknown> {
    return {
      aic: this.aic,
      connection_name: this.connectionName,
      vhost: this.vhost,
      node_name: this.nodeName,
      queue_name: this.queueName,
      process_id: this.processId,
      connected: this.connected,
      muted: this.muted,
      joined_at: this.joinedAt,
    };
  }
}

/** 群组内可收发的消息 */
export type GroupMessage = TaskCommand | TaskResult | GroupMgmtCommand | GroupMgmtResult;

/** 消息处理回调类型 */
export type MessageHandler = (message: GroupMessage) => Promise<void>;

/** `MessageEmitter` 中本模块用到的部分（便于测试替换）。 */
export type MessageEmitterLike = Pick<MessageEmitter, "emit">;

/** 返回当前 trace 上下文（对应 Python `trace_context_provider`）。 */
export type TraceContextProvider = () => TraceContext | null | undefined;

/** mTLS 客户端证书（对应 httpx `cert=(certfile, keyfile)`，这里直接给 PEM 内容）。 */
export type ClientCertificate = Pick<TlsClientOptions, "cert" | "key">;

type EmitOptions = NonNullable<Parameters<MessageEmitter["emit"]>[1]>;

function mergeTls(
  sslContext: TlsClientOptions | null | undefined,
  clientCert: ClientCertificate | null | undefined,
): TlsClientOptions | undefined {
  if (!sslContext && !clientCert) return undefined;
  return { ...(sslContext ?? {}), ...(clientCert ?? {}) };
}

/** 对应 httpx `HTTPStatusError` 的文本。 */
function httpxStatusErrorMessage(
  status: number,
  url: string,
  headers: HttpResponseData["headers"],
): string {
  const kinds: Record<number, string> = {
    1: "Informational response",
    3: "Redirect response",
    4: "Client error",
    5: "Server error",
  };
  const kind = kinds[Math.floor(status / 100)] ?? "Invalid status code";
  let message =
    `${kind} '${status} ${STATUS_CODES[status] ?? ""}' for url '${url}'\n` +
    `For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/${status}`;
  if (Math.floor(status / 100) === 3 && headers.location) {
    message += `\nRedirect location: '${String(headers.location)}'`;
  }
  return message;
}

/** 对应 httpx `RequestError`：传输层失败（连接、超时等）。 */
class HttpRequestError extends Error {
  override name = "HttpRequestError";
}

const MESSAGE_CLASS_NAMES: Record<string, string> = {
  "task-command": "TaskCommand",
  "task-result": "TaskResult",
  "group-mgmt-command": "GroupMgmtCommand",
  "group-mgmt-result": "GroupMgmtResult",
};

export interface GroupLeaderMqClientOptions {
  /** RabbitMQ 服务器地址，默认 localhost */
  rabbitmqHost?: string;
  /** RabbitMQ 服务器端口，默认 5672 */
  rabbitmqPort?: number;
  /** 虚拟主机，默认 "/" */
  rabbitmqVhost?: string;
  /** 用户名，默认 "guest"；传 null 表示走 AMQPS + EXTERNAL（需 sslContext） */
  rabbitmqUser?: string | null;
  /** 密码，默认 "guest" */
  rabbitmqPassword?: string | null;
  /** 对应 Python `ssl_context`（EXTERNAL 认证时用作 mTLS 证书） */
  sslContext?: TlsClientOptions | null;
  /** 共享连接 */
  connection?: AmqpConnection | null;
  /** 是否由本客户端负责关闭连接，默认 true */
  connectionOwner?: boolean;
  messageEmitter?: MessageEmitterLike | null;
  /** 默认 "rabbitmq" */
  messageSystem?: string;
  traceContextProvider?: TraceContextProvider | null;
  /** 默认 true */
  identityBindingEnabled?: boolean;
  /** AMQP 连接器（默认 amqplib，测试可替换；对应 monkeypatch `aio_pika.connect_robust`） */
  connector?: AmqpConnector;
  /** 直连 RPC 邀请使用的 HTTP 传输层（测试注入用，对应 `httpx.MockTransport`） */
  transport?: HttpTransport;
}

export interface InvitePartnerOptions {
  /** 对应 Python `ssl_context`（httpx `verify=`） */
  sslContext?: TlsClientOptions | null;
  /** 对应 Python `client_cert`（httpx `cert=`） */
  clientCert?: ClientCertificate | null;
  /** 超时（秒），默认 30.0 */
  timeout?: number;
}

type Mentions = "all" | string[];

/**
 * 群组模式 Leader 客户端
 *
 * 负责：
 * - 连接到 RabbitMQ 服务器
 * - 创建和管理群组 Exchange
 * - 发送任务消息和管理消息到群组
 * - 接收群组内的消息
 * - 管理 Partner 成员
 */
export class GroupLeaderMqClient {
  readonly leaderAic: string;
  rabbitmqHost: string;
  rabbitmqPort: number;
  rabbitmqVhost: string;
  rabbitmqUser: string | null;
  rabbitmqPassword: string | null;
  sslContext: TlsClientOptions | null;

  /** @internal */ _connectionOwner: boolean;
  /** @internal */ _messageEmitter: MessageEmitterLike | null;
  /** @internal */ _messageSystem: string;
  /** @internal */ _traceContextProvider: TraceContextProvider | null;
  /** @internal */ _identityBindingEnabled: boolean;
  /** @internal */ _connector: AmqpConnector;
  /** @internal */ _httpTransport: HttpTransport | null;

  // 连接和通道
  /** @internal */ _connection: AmqpConnection | null;
  /** @internal */ _channel: AmqpChannel | null = null;

  // 群组信息
  /** @internal */ _groupId: string | null = null;
  /** @internal */ _exchangeName: string | null = null;
  /** @internal */ _exchange: AmqpExchange | null = null;
  /** @internal */ _leaderQueue: AmqpQueue | null = null;
  /** @internal */ _state: GroupLeaderState = GroupLeaderState.DISCONNECTED;

  // Partner 管理
  /** @internal aic -> PartnerConnectionInfo */
  _partners = new Map<string, PartnerConnectionInfo>();
  /** @internal Partner 的 ACS 信息列表 */
  _partnerAcs: ACSObject[] = [];

  // 消息处理器
  /** @internal */ _messageHandler: MessageHandler | null = null;
  /** @internal */ _consumeTask: Promise<string> | null = null;
  /** @internal `_consumeTask` 的完成状态（Python 用 task.done() / task.exception() 查询） */
  _consumeTaskState: { settled: boolean; error: unknown } | null = null;

  constructor(leaderAic: string, options: GroupLeaderMqClientOptions = {}) {
    this.leaderAic = leaderAic;
    this.rabbitmqHost = options.rabbitmqHost ?? "localhost";
    this.rabbitmqPort = options.rabbitmqPort ?? 5672;
    this.rabbitmqVhost = options.rabbitmqVhost ?? "/";
    this.rabbitmqUser =
      (options.rabbitmqUser === undefined ? "guest" : options.rabbitmqUser) || null;
    this.rabbitmqPassword =
      (options.rabbitmqPassword === undefined ? "guest" : options.rabbitmqPassword) || null;
    this.sslContext = options.sslContext ?? null;
    this._connectionOwner = options.connectionOwner ?? true;
    this._messageEmitter = options.messageEmitter ?? null;
    this._messageSystem = options.messageSystem ?? "rabbitmq";
    this._traceContextProvider = options.traceContextProvider ?? null;
    this._identityBindingEnabled = options.identityBindingEnabled ?? true;
    this._connector = options.connector ?? defaultAmqpConnector;
    this._httpTransport = options.transport ?? null;
    this._connection = options.connection ?? null;
    if (!this._identityBindingEnabled) {
      logger.warn(
        `AIP identity binding disabled for GroupLeaderMqClient leader_aic=${this.leaderAic}`,
      );
    }
  }

  get groupId(): string | null {
    return this._groupId;
  }

  get state(): GroupLeaderState {
    return this._state;
  }

  get partners(): Map<string, PartnerConnectionInfo> {
    return new Map(this._partners);
  }

  get isConnected(): boolean {
    return this._connection !== null && !this._connection.isClosed;
  }

  /**
   * 群组 channel 最近一次自动恢复失败的原因（Node 版新增）。断线重连后若群组拓扑恢复失败，
   * channel 会定时重试；这期间该值非空、发布会被拒绝，恢复成功后复位为 null。
   */
  get groupChannelError(): Error | null {
    return this._channel?.lastRestoreError ?? null;
  }

  /** 建立到 RabbitMQ 的连接 */
  async connect(): Promise<void> {
    if (this._connection && !this._connection.isClosed) {
      if (this._channel === null || this._channel.isClosed) {
        this._channel = await this._connection.channel();
      }
      return;
    }

    if (this._connection && this._connection.isClosed) {
      if (!this._connectionOwner) {
        throw new Error("Shared connection is closed; owner must recreate it");
      }
    }

    const connectionName = `agent-${this.leaderAic}`;
    let connectionUrl: string;
    let sslContext: TlsClientOptions | null = null;

    if (this.rabbitmqUser && this.rabbitmqPassword) {
      connectionUrl = buildPlainConnectionUrl({
        host: this.rabbitmqHost,
        port: this.rabbitmqPort,
        vhost: this.rabbitmqVhost,
        username: this.rabbitmqUser,
        password: this.rabbitmqPassword,
        connectionName,
      });
    } else {
      if (this.sslContext === null) {
        throw new Error("ssl_context is required for AMQPS + EXTERNAL authentication");
      }
      connectionUrl = buildExternalConnectionUrl({
        host: this.rabbitmqHost,
        port: this.rabbitmqPort,
        vhost: this.rabbitmqVhost,
        connectionName,
      });
      sslContext = this.sslContext;
    }

    try {
      this._connection = await this._connector.connectRobust(
        connectionUrl,
        sslContext ? { sslContext } : {},
      );
      this._channel = await this._connection.channel();
      logger.info(
        `event=rabbitmq_connected leader_aic=${this.leaderAic} host=${this.rabbitmqHost} vhost=${this.rabbitmqVhost}`,
      );
    } catch (error) {
      logger.error(
        `event=rabbitmq_connection_failed leader_aic=${this.leaderAic} error=${pyErrorStr(error)}`,
      );
      throw error;
    }
  }

  /**
   * 创建群组
   *
   * @returns 群组 ID
   */
  async createGroup({
    groupId = null,
    partnerAcsList = null,
  }: { groupId?: string | null; partnerAcsList?: ACSObject[] | null } = {}): Promise<string> {
    if (!this.isConnected) {
      await this.connect();
    }

    const resolvedGroupId = ensureValidGroupId(groupId || `group-${randomUUID()}`);
    this._groupId = resolvedGroupId;
    this._exchangeName = buildGroupExchangeName(this.leaderAic, resolvedGroupId);
    this._partnerAcs = partnerAcsList || [];
    const channel = this._channel;
    if (channel === null) {
      throw new Error("RabbitMQ channel is not ready");
    }

    // 创建 fanout 类型的 Exchange 用于群组广播
    // autoDelete=true：当最后一个绑定队列解绑后自动删除，避免会话结束后 Exchange 残留
    this._exchange = await channel.declareExchange(this._exchangeName, {
      type: ExchangeType.FANOUT,
      durable: true,
      autoDelete: true,
    });
    logger.info(`event=exchange_created group_id=${this._groupId} exchange=${this._exchangeName}`);

    // 创建 Leader 的队列
    const leaderQueueName = buildGroupQueueName(this.leaderAic, resolvedGroupId, this.leaderAic);
    this._leaderQueue = await channel.declareQueue(leaderQueueName, {
      durable: true,
      exclusive: false,
      autoDelete: true,
    });

    // 绑定队列到 Exchange
    await this._leaderQueue.bind(this._exchange);
    logger.info(`event=leader_queue_created group_id=${this._groupId} queue=${leaderQueueName}`);

    this._state = GroupLeaderState.READY;
    return resolvedGroupId;
  }

  /** 设置消息处理回调 */
  setMessageHandler(handler: MessageHandler): void {
    this._messageHandler = handler;
  }

  /** @internal */
  async _safeEmit(body: MessageBody, options: EmitOptions): Promise<void> {
    if (!this._messageEmitter) {
      return;
    }
    try {
      await this._messageEmitter.emit(body, options);
    } catch (error) {
      logger.warn("message emit failed", error);
    }
  }

  /** 开始消费消息 */
  async startConsuming(): Promise<void> {
    const leaderQueue = this._leaderQueue;
    if (!leaderQueue) {
      throw new Error("Group not created yet");
    }

    const onMessage = async (message: AmqpIncomingMessage): Promise<void> => {
      const t0 = performance.now();
      let recvSpan: string | null = null;
      let traceId: string | null = null;
      let body: Record<string, unknown> = {};
      let emitOk = false;
      const deliveryAttempt = message.redelivered ? 2 : 1;

      await message.process(async () => {
        try {
          const decoded = decodeJsonBody(message.body);
          body = decoded as Record<string, unknown>;
          if (this._identityBindingEnabled) {
            assertIncomingGroupMessageIdentity(
              decoded as Parameters<typeof assertIncomingGroupMessageIdentity>[0],
              {
                amqpUserId: extractAmqpUserId(message),
                expectedGroupId: this._groupId,
              },
            );
          }
          body = requireJsonDict(decoded);
          const msgType = dictGet(body, "type", "unknown");
          const senderId = dictGet(body, "senderId", "unknown");
          logger.debug(
            `[GroupLeaderMq] >>> Message received: group_id=${this._groupId} type=${pyStr(msgType)} sender=${pyStr(senderId)}`,
          );
          emitOk = Boolean(
            this._messageEmitter &&
            DATA_PLANE_TYPES.has(body.type) &&
            body.senderId !== this.leaderAic,
          );
          if (emitOk) {
            const ctx = parseTraceparent(
              message.headers ? headerValue(message.headers, TRACEPARENT_HEADER) : null,
            );
            traceId = ctx ? ctx.traceId : newTraceId();
            recvSpan = newSpanId();
            await this._safeEmit(
              buildReceiveBody({
                system: this._messageSystem,
                exchange: this._exchangeName || "",
                virtualHost: this.rabbitmqVhost,
                routingKey: message.routingKey || "",
                queueName: leaderQueue.name,
                messageId: dictGet(body, "id", "") as string,
                payloadSizeBytes: message.body.length,
                deliveryAttempt,
                attributes: {
                  groupId: this._groupId,
                  msgType: body.type,
                  senderId: body.senderId,
                },
              }),
              {
                traceId,
                spanId: recvSpan,
                parentSpanId: ctx ? ctx.spanId : "",
                correlationId: asOptionalString(body.sessionId),
              },
            );
          }
          const parsedMessage = this._parseIncomingMessage(body);
          if (parsedMessage) {
            if (this._messageHandler) {
              logger.debug(`[GroupLeaderMq] Calling message handler for type=${pyStr(msgType)}`);
              await this._messageHandler(parsedMessage);
            } else {
              logger.warn(
                `[GroupLeaderMq] No message handler set, discarding message type=${pyStr(msgType)}`,
              );
            }
          } else {
            logger.warn(`[GroupLeaderMq] Failed to parse message type=${pyStr(msgType)}`);
          }
        } catch (error) {
          if (emitOk && traceId && recvSpan) {
            await this._safeEmit(
              buildSettlementBody("nack", {
                system: this._messageSystem,
                exchange: this._exchangeName || "",
                virtualHost: this.rabbitmqVhost,
                queueName: leaderQueue.name,
                messageId: dictGet(body, "id", "") as string,
                deliveryAttempt,
                latencyMs: performance.now() - t0,
                reason: pySlice(pyErrorStr(error), 200),
              }),
              {
                traceId,
                spanId: newSpanId(),
                parentSpanId: recvSpan,
                correlationId: asOptionalString(body.sessionId),
              },
            );
          }
          logger.error(
            `event=message_processing_error group_id=${this._groupId} error=${pyErrorStr(error)}`,
          );
          logger.error(
            `[GroupLeaderMq] Traceback: ${error instanceof Error ? (error.stack ?? "") : ""}`,
          );
          throw error;
        }
        if (emitOk && traceId && recvSpan) {
          await this._safeEmit(
            buildSettlementBody("ack", {
              system: this._messageSystem,
              exchange: this._exchangeName || "",
              virtualHost: this.rabbitmqVhost,
              queueName: leaderQueue.name,
              messageId: dictGet(body, "id", "") as string,
              deliveryAttempt,
              latencyMs: performance.now() - t0,
            }),
            {
              traceId,
              spanId: newSpanId(),
              parentSpanId: recvSpan,
              correlationId: asOptionalString(body.sessionId),
            },
          );
        }
      });
    };

    // 同 Python `asyncio.create_task(queue.consume(...))`：不等待消费者注册完成
    const consumeTask = leaderQueue.consume(onMessage);
    const taskState: { settled: boolean; error: unknown } = { settled: false, error: undefined };
    consumeTask.then(
      () => {
        taskState.settled = true;
      },
      (error: unknown) => {
        taskState.settled = true;
        taskState.error = error ?? new Error("consume failed");
        logger.error(
          `event=consume_start_failed group_id=${this._groupId} error=${pyErrorStr(error)}`,
        );
      },
    );
    this._consumeTask = consumeTask;
    this._consumeTaskState = taskState;
    logger.info(`event=consuming_started group_id=${this._groupId}`);
  }

  /** @internal 解析接收到的消息 */
  _parseIncomingMessage(body: Record<string, unknown>): GroupMessage | null {
    const msgType = body.type;
    try {
      if (msgType === "group-mgmt-command") {
        return GroupMgmtCommandSchema.parse(body);
      } else if (msgType === "group-mgmt-result") {
        return GroupMgmtResultSchema.parse(body);
      } else if (msgType === "task-result") {
        return TaskResultSchema.parse(body);
      } else if (msgType === "task-command") {
        return TaskCommandSchema.parse(body);
      }
      logger.warn(`event=unknown_message_type group_id=${this._groupId} type=${pyStr(msgType)}`);
      return null;
    } catch (error) {
      logger.error(
        `event=message_parse_error group_id=${this._groupId} error=${pyErrorStr(error)} body=${pySlice(pyRepr(body), 200)}`,
      );
      return null;
    }
  }

  /**
   * 邀请 Partner 加入群组
   *
   * 通过 RPC 方式向 Partner 发送群组邀请请求
   *
   * @throws PartnerNetworkError 网络请求失败
   * @throws PartnerResponseError Partner 拒绝或返回错误
   * @throws PartnerInviteError 其他邀请错误
   */
  async invitePartner(
    partnerAcs: ACSObject,
    partnerRpcUrl: string,
    { sslContext = null, clientCert = null, timeout = 30.0 }: InvitePartnerOptions = {},
  ): Promise<PartnerConnectionInfo> {
    if (this._state !== GroupLeaderState.READY) {
      throw new Error(`Cannot invite partner in state: ${this._state}`);
    }

    const groupId = this._groupId;
    const exchangeName = this._exchangeName;
    if (groupId === null || exchangeName === null) {
      throw new Error("Group not created yet");
    }

    let accessToken: string | undefined;
    if (this.rabbitmqUser && this.rabbitmqPassword) {
      accessToken = `${this.rabbitmqUser}:${this.rabbitmqPassword}`;
    }

    const request: RabbitMQRequest = RabbitMQRequestSchema.parse({
      id: randomUUID(),
      params: {
        protocol: "rabbitmq:4.0",
        group: {
          groupId,
          leader: { aic: this.leaderAic },
          partners: [...this._partnerAcs, partnerAcs],
        },
        server: {
          host: this.rabbitmqHost,
          port: this.rabbitmqPort,
          vhost: this.rabbitmqVhost,
          accessToken,
        },
        amqp: { exchange: exchangeName, exchangeType: "fanout", routingKey: "" },
      },
    });

    // 发送 RPC 请求（带重试）
    const maxAttempts = 3;
    let backoffSeconds = 1.0;
    const transport = this._httpTransport ?? createNodeTransport(mergeTls(sslContext, clientCert));
    // httpx `json=`：紧凑分隔符、ensure_ascii=False
    const payload = JSON.stringify(request);

    try {
      let response: HttpResponseData | null = null;
      for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
        try {
          logger.info(
            `event=partner_invite_request partner_aic=${partnerAcs.aic} url=${redactUrl(partnerRpcUrl)} attempt=${attempt}`,
          );
          let result: HttpResponseData;
          try {
            result = await transport({
              url: partnerRpcUrl,
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: payload,
              timeoutMs: timeout * 1000,
            });
          } catch (transportError) {
            throw new HttpRequestError(pyErrorStr(transportError), { cause: transportError });
          }
          if (result.status < 200 || result.status >= 300) {
            const statusError = new HttpStatusError(result.status, result.body);
            statusError.message = httpxStatusErrorMessage(
              result.status,
              partnerRpcUrl,
              result.headers,
            );
            throw statusError;
          }
          response = result;
          break;
        } catch (error) {
          if (error instanceof HttpStatusError) {
            const status = error.status;
            if (status >= 500 && attempt < maxAttempts) {
              logger.warn(
                `event=partner_invite_retry partner_aic=${partnerAcs.aic} url=${redactUrl(partnerRpcUrl)} status=${status} attempt=${attempt}`,
              );
              await sleep(backoffSeconds * 1000);
              backoffSeconds *= 2;
              continue;
            }
            throw error;
          }
          if (error instanceof HttpRequestError && attempt < maxAttempts) {
            logger.warn(
              `event=partner_invite_retry partner_aic=${partnerAcs.aic} url=${redactUrl(partnerRpcUrl)} attempt=${attempt}`,
            );
            await sleep(backoffSeconds * 1000);
            backoffSeconds *= 2;
            continue;
          }
          throw error;
        }
      }
      if (response === null) {
        throw new PartnerInviteError("Unexpected error: no response");
      }

      const rpcResponse = RabbitMQResponseSchema.parse(JSON.parse(response.body.toString("utf-8")));

      if (rpcResponse.error) {
        const errorMsg = rpcResponse.error.message;
        logger.error(`event=partner_invite_failed partner_aic=${partnerAcs.aic} error=${errorMsg}`);
        throw new PartnerResponseError(`Partner refused: ${errorMsg}`);
      }

      if (rpcResponse.result) {
        const result = rpcResponse.result;
        const partnerInfo = new PartnerConnectionInfo(
          partnerAcs.aic,
          result.connectionName,
          result.vhost,
          result.nodeName,
          result.queueName,
          result.processId ?? null,
        );
        this._partners.set(partnerAcs.aic, partnerInfo);
        this._partnerAcs.push(partnerAcs);

        logger.info(`event=partner_joined group_id=${this._groupId} partner_aic=${partnerAcs.aic}`);
        return partnerInfo;
      }

      throw new PartnerResponseError("Empty result in response");
    } catch (error) {
      if (error instanceof HttpRequestError || error instanceof HttpStatusError) {
        logger.error(
          `event=partner_invite_network_error partner_aic=${partnerAcs.aic} url=${redactUrl(partnerRpcUrl)} error=${redactUrl(error.message)}`,
        );
        throw new PartnerNetworkError(`Network error: ${error.message}`, { cause: error });
      }
      if (error instanceof PartnerInviteError) {
        throw error;
      }
      logger.error(
        `event=partner_invite_exception partner_aic=${partnerAcs.aic} url=${redactUrl(partnerRpcUrl)} error_type=${
          error instanceof Error ? error.name : typeof error
        } error=${pyErrorStr(error)}`,
      );
      throw new PartnerInviteError(`Unexpected error: ${pyErrorStr(error)}`, { cause: error });
    }
  }

  /** 通过 `inbox.topic` 向 Partner 的 inbox 队列发送群组邀请。 */
  async inviteViaInbox(
    partnerAcs: ACSObject,
    {
      partnerInbox,
      invitationToken = null,
      expiresAt = null,
      timeoutSeconds = DEFAULT_INVITATION_TIMEOUT_SECONDS,
    }: {
      partnerInbox: string;
      invitationToken?: string | null;
      expiresAt?: string | null;
      timeoutSeconds?: number;
    },
  ): Promise<InboxGroupInvitation> {
    if (this._state !== GroupLeaderState.READY) {
      throw new Error(`Cannot invite partner in state: ${this._state}`);
    }
    if (!this._channel || !this._exchangeName || !this._groupId) {
      throw new Error("Group not created yet");
    }

    const invitation = InboxGroupInvitationSchema.parse({
      id: `invite-${randomUUID()}`,
      sentAt: utcNowIso(),
      senderRole: "leader",
      senderId: this.leaderAic,
      groupId: this._groupId,
      protocol: "rabbitmq:4.2",
      expiresAt: expiresAt || calculateInvitationExpiry(timeoutSeconds),
      invitationToken: invitationToken || createInvitationToken(),
      group: {
        groupId: this._groupId,
        leader: { aic: this.leaderAic },
        partners: [...this._partnerAcs, partnerAcs],
      },
      amqp: {
        exchange: this._exchangeName,
        exchangeType: ExchangeType.FANOUT,
        routingKey: "",
      },
    });

    const inboxExchange = await this._channel.declareExchange(INBOX_EXCHANGE_NAME, {
      type: ExchangeType.TOPIC,
      durable: true,
      autoDelete: false,
    });
    const message = new AmqpMessage(Buffer.from(pythonJsonDumps(invitation), "utf-8"), {
      contentType: "application/json",
      deliveryMode: DeliveryMode.PERSISTENT,
      userId: buildOutgoingAmqpUserId(invitation, {
        localAic: this.leaderAic,
        identityBindingEnabled: this._identityBindingEnabled,
      }),
    });
    await inboxExchange.publish(message, partnerInbox);
    logger.info(
      `event=partner_invite_inbox_sent group_id=${this._groupId} partner_aic=${partnerAcs.aic} inbox=${partnerInbox}`,
    );
    return invitation;
  }

  /** 发布消息到群组 */
  async publishMessage(message: TaskCommand | TaskResult | GroupMgmtCommand): Promise<void> {
    if (!this._exchange) {
      throw new Error("Group not created yet");
    }

    const body = dumpGroupMessage(message);
    const jsonBytes = Buffer.from(pythonJsonDumps(body), "utf-8");
    const headers: Record<string, string> = {};
    let traceId: string | null = null;
    let spanId: string | null = null;
    let parentSpan = "";

    const emitOk = Boolean(this._messageEmitter && DATA_PLANE_TYPES.has(body.type));
    if (emitOk) {
      const parent = this._traceContextProvider ? this._traceContextProvider() : null;
      traceId = parent ? parent.traceId : newTraceId();
      spanId = newSpanId();
      parentSpan = parent ? parent.spanId : "";
      headers[TRACEPARENT_HEADER] = formatTraceparent({ traceId, spanId, sampled: true });
    }

    const amqpMessage = new AmqpMessage(jsonBytes, {
      contentType: "application/json",
      deliveryMode: DeliveryMode.PERSISTENT,
      headers: Object.keys(headers).length > 0 ? headers : null,
      userId: buildOutgoingAmqpUserId(message, {
        localAic: this.leaderAic,
        identityBindingEnabled: this._identityBindingEnabled,
      }),
    });

    await this._exchange.publish(amqpMessage, "");
    if (emitOk && traceId && spanId) {
      await this._safeEmit(
        buildSendBody({
          system: this._messageSystem,
          exchange: this._exchangeName || "",
          virtualHost: this.rabbitmqVhost,
          routingKey: "",
          messageId: dictGet(body, "id", "") as string,
          payloadSizeBytes: jsonBytes.length,
          attributes: {
            groupId: this._groupId,
            msgType: body.type,
            taskId: body.taskId,
          },
        }),
        {
          traceId,
          spanId,
          parentSpanId: parentSpan,
          correlationId: asOptionalString(body.sessionId),
        },
      );
    }
    logger.info(
      `event=message_published group_id=${this._groupId} type=${pyStr(body.type)} id=${pyStr(body.id)} mentions=${pyStr(body.mentions)}`,
    );
  }

  /**
   * 发送任务命令
   *
   * @returns 发送的命令对象
   */
  async sendTaskCommand(
    command: TaskCommandType,
    taskId: string,
    sessionId: string,
    {
      textContent = null,
      dataItems = null,
      mentions = null,
    }: {
      textContent?: string | null;
      dataItems?: DataItem[] | null;
      mentions?: Mentions | null;
    } = {},
  ): Promise<TaskCommand> {
    // 与 Python `data_items or []` 相同：非空列表会被就地插入文本项
    const items: DataItem[] = dataItems && dataItems.length > 0 ? dataItems : [];
    if (textContent) {
      const textItem: TextDataItem = { type: "text", text: textContent };
      items.unshift(textItem);
    }

    const taskCommand = TaskCommandSchema.parse({
      id: `cmd-${randomUUID()}`,
      sentAt: utcNowIso(),
      senderRole: "leader",
      senderId: this.leaderAic,
      command,
      dataItems: items,
      taskId,
      groupId: this._groupId,
      sessionId,
      mentions,
    });

    await this.publishMessage(taskCommand);
    return taskCommand;
  }

  /**
   * 在群组中启动新任务
   *
   * @returns 任务 ID
   */
  async startTask(
    sessionId: string,
    textContent: string,
    { taskId = null, mentions = null }: { taskId?: string | null; mentions?: Mentions | null } = {},
  ): Promise<string> {
    const resolvedTaskId = taskId || `task-${randomUUID()}`;

    await this.sendTaskCommand(TaskCommandType.Start, resolvedTaskId, sessionId, {
      textContent,
      mentions,
    });

    logger.info(
      `event=task_started group_id=${this._groupId} task_id=${resolvedTaskId} mentions=${pyStr(mentions)}`,
    );
    return resolvedTaskId;
  }

  /** 继续任务 */
  async continueTask(
    taskId: string,
    sessionId: string,
    textContent: string,
    { mentions = null }: { mentions?: Mentions | null } = {},
  ): Promise<void> {
    await this.sendTaskCommand(TaskCommandType.Continue, taskId, sessionId, {
      textContent,
      mentions,
    });
    logger.info(
      `event=task_continue_sent group_id=${this._groupId} task_id=${taskId} mentions=${pyStr(mentions)}`,
    );
  }

  /** 完成任务 */
  async completeTask(
    taskId: string,
    sessionId: string,
    {
      textContent = null,
      mentions = null,
    }: { textContent?: string | null; mentions?: Mentions | null } = {},
  ): Promise<void> {
    await this.sendTaskCommand(TaskCommandType.Complete, taskId, sessionId, {
      textContent,
      mentions,
    });
    logger.info(
      `event=task_complete_sent group_id=${this._groupId} task_id=${taskId} mentions=${pyStr(mentions)}`,
    );
  }

  /** 取消任务 */
  async cancelTask(
    taskId: string,
    sessionId: string,
    { reason = null, mentions = null }: { reason?: string | null; mentions?: Mentions | null } = {},
  ): Promise<void> {
    await this.sendTaskCommand(TaskCommandType.Cancel, taskId, sessionId, {
      textContent: reason,
      mentions,
    });
  }

  /**
   * 发送群组管理命令
   *
   * @param options.sessionId 会话 ID（可选），用于将管理操作关联到特定会话
   * @returns 发送的管理命令对象
   */
  async sendMgmtCommand(
    command: GroupMgmtCommandType,
    {
      mentions = null,
      sessionId = null,
    }: { mentions?: Mentions | null; sessionId?: string | null } = {},
  ): Promise<GroupMgmtCommand> {
    const mgmtCommand = this._buildMgmtCommand(command, { mentions, sessionId });
    await this.publishMessage(mgmtCommand);
    return mgmtCommand;
  }

  /** @internal */
  _buildMgmtCommand(
    command: GroupMgmtCommandType,
    {
      mentions = null,
      sessionId = null,
    }: { mentions?: Mentions | null; sessionId?: string | null } = {},
  ): GroupMgmtCommand {
    if (!this._groupId) {
      throw new Error("Group not created yet");
    }

    return GroupMgmtCommandSchema.parse({
      id: `mgmt-cmd-${randomUUID()}`,
      sentAt: utcNowIso(),
      senderRole: "leader",
      senderId: this.leaderAic,
      dataItems: [],
      command,
      mentions,
      groupId: this._groupId,
      sessionId,
    });
  }

  /**
   * 获取 Partner 状态
   *
   * @param options.partnerAic 指定 Partner 的 AIC，如果不提供则查询所有 Partner
   * @param options.sessionId 会话 ID，用于将管理操作关联到特定会话
   */
  async getPartnerStatus({
    partnerAic = null,
    sessionId = null,
  }: { partnerAic?: string | null; sessionId?: string | null } = {}): Promise<GroupMgmtCommand> {
    const mentions: Mentions = partnerAic ? [partnerAic] : "all";
    return this.sendMgmtCommand(GroupMgmtCommandType.GET_STATUS, { mentions, sessionId });
  }

  /**
   * 要求 Partner 退出群组
   *
   * 发布失败（broker nack，`DeliveryError`）时最多重试 3 次；若期间 Partner 已断开则视为无操作。
   */
  async requestPartnerLeave(
    partnerAic: string,
    { sessionId = null }: { sessionId?: string | null } = {},
  ): Promise<GroupMgmtCommand> {
    const maxAttempts = 3;
    const retryDelaySeconds = 0.2;
    let lastError: DeliveryError | null = null;

    const buildLeaveNoop = (reason: string): GroupMgmtCommand => {
      logger.info(
        `event=partner_leave_noop group_id=${this._groupId} partner_aic=${partnerAic} session_id=${pyStr(sessionId)} reason=${reason}`,
      );
      return this._buildMgmtCommand(GroupMgmtCommandType.LEAVE_GROUP, {
        mentions: [partnerAic],
        sessionId,
      });
    };

    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      let partner = this._partners.get(partnerAic);
      if (partner !== undefined && !partner.connected) {
        return buildLeaveNoop("already_disconnected");
      }

      try {
        return await this.sendMgmtCommand(GroupMgmtCommandType.LEAVE_GROUP, {
          mentions: [partnerAic],
          sessionId,
        });
      } catch (error) {
        if (!(error instanceof DeliveryError)) {
          throw error;
        }
        lastError = error;
        logger.warn(
          `event=partner_leave_publish_retry group_id=${this._groupId} partner_aic=${partnerAic} session_id=${pyStr(sessionId)} attempt=${attempt} max_attempts=${maxAttempts} error=${error.message}`,
        );
        partner = this._partners.get(partnerAic);
        if (partner !== undefined && !partner.connected) {
          return buildLeaveNoop("disconnected_after_publish_error");
        }
        if (attempt === maxAttempts) {
          break;
        }
        await sleep(retryDelaySeconds * attempt * 1000);
      }
    }

    if (lastError !== null) {
      const partner = this._partners.get(partnerAic);
      if (partner !== undefined && !partner.connected) {
        return buildLeaveNoop("disconnected_after_retries");
      }
      throw lastError;
    }

    throw new Error("partner leave publish failed without a DeliveryError");
  }

  /** 删除指定 Partner 的群组队列。 */
  async deletePartnerQueue(partnerAic: string): Promise<boolean> {
    if (!this._groupId) {
      throw new Error("Group not created yet");
    }
    if (this._channel === null) {
      throw new Error("RabbitMQ channel is not ready");
    }

    const queueName = buildGroupQueueName(this.leaderAic, this._groupId, partnerAic);
    try {
      await this._channel.queueDelete(queueName, { ifUnused: false, ifEmpty: false });
    } catch (error) {
      logger.warn(
        `event=partner_queue_delete_failed group_id=${this._groupId} partner_aic=${partnerAic} queue=${queueName} error=${pyErrorStr(error)}`,
      );
      return false;
    }

    logger.info(
      `event=partner_queue_deleted group_id=${this._groupId} partner_aic=${partnerAic} queue=${queueName}`,
    );
    return true;
  }

  /** 从 Leader 本地状态中移除指定 Partner。 */
  removePartnerTracking(partnerAic: string): void {
    const removed = this._partners.get(partnerAic);
    this._partners.delete(partnerAic);
    this._partnerAcs = this._partnerAcs.filter((acs) => acs.aic !== partnerAic);
    logger.info(
      `event=partner_tracking_removed group_id=${this._groupId} partner_aic=${partnerAic} had_runtime_state=${
        removed !== undefined ? "True" : "False"
      }`,
    );
  }

  /** 静音 Partner */
  async mutePartner(
    partnerAic: string,
    { sessionId = null }: { sessionId?: string | null } = {},
  ): Promise<GroupMgmtCommand> {
    const msg = await this.sendMgmtCommand(GroupMgmtCommandType.MUTE, {
      mentions: [partnerAic],
      sessionId,
    });
    const partner = this._partners.get(partnerAic);
    if (partner) {
      partner.muted = true;
    }
    return msg;
  }

  /** 取消静音 Partner */
  async unmutePartner(
    partnerAic: string,
    { sessionId = null }: { sessionId?: string | null } = {},
  ): Promise<GroupMgmtCommand> {
    const msg = await this.sendMgmtCommand(GroupMgmtCommandType.UNMUTE, {
      mentions: [partnerAic],
      sessionId,
    });
    const partner = this._partners.get(partnerAic);
    if (partner) {
      partner.muted = false;
    }
    return msg;
  }

  /**
   * 处理 Partner 状态更新消息
   *
   * @param mgmtResult Partner 发送的状态消息（GroupMgmtResult 包含 status）
   */
  handlePartnerStatusUpdate(mgmtResult: GroupMgmtCommand | GroupMgmtResult): void {
    const senderId = mgmtResult.senderId;
    // GroupMgmtResult 才有 status 属性
    const status = "status" in mgmtResult ? mgmtResult.status : undefined;
    if (senderId && status) {
      let partner = this._partners.get(senderId);
      if (partner === undefined && this._groupId) {
        partner = new PartnerConnectionInfo(
          senderId,
          `agent-${senderId}`,
          this.rabbitmqVhost,
          "unknown",
          buildGroupQueueName(this.leaderAic, this._groupId, senderId),
        );
        this._partners.set(senderId, partner);
      }

      if (partner === undefined) {
        return;
      }

      partner.connected = status.connected;
      partner.muted = status.muted;

      if (!status.connected) {
        logger.info(`event=partner_disconnected group_id=${this._groupId} partner_aic=${senderId}`);
      }

      if (
        mgmtResult.type === "group-mgmt-result" &&
        status.connected &&
        this._partnerAcs.every((acs) => acs.aic !== senderId)
      ) {
        this._partnerAcs.push({ aic: senderId });
      }
    }
  }

  async broadcastGroupShutdown(): Promise<void> {
    if (this._state === GroupLeaderState.DISCONNECTED) {
      return;
    }

    try {
      await this.sendMgmtCommand(GroupMgmtCommandType.DISBAND, { mentions: "all" });
      await sleep(2000);
    } catch (error) {
      logger.warn(
        `event=dissolve_notify_failed group_id=${this._groupId} error=${pyErrorStr(error)}`,
      );
    }
  }

  async cleanupGroupResources(): Promise<void> {
    if (this._consumeTask) {
      // 同 Python `task.cancel(); await task`：仍在进行的 consume 视为已取消（不等待）；
      // 已经以异常结束的，异常从这里抛出
      const state = this._consumeTaskState;
      this._consumeTask = null;
      this._consumeTaskState = null;
      if (state?.settled && state.error !== undefined && !(state.error instanceof CancelledError)) {
        throw state.error;
      }
    }

    if (this._leaderQueue) {
      const queueName = this._leaderQueue.name;
      try {
        if (this._channel === null) {
          throw new Error("RabbitMQ channel is not ready");
        }
        logger.info(`event=queue_delete_attempt group_id=${this._groupId} queue=${queueName}`);
        await this._channel.queueDelete(queueName, { ifUnused: false, ifEmpty: false });
      } catch (error) {
        logger.warn(
          `event=queue_cleanup_failed group_id=${this._groupId} queue=${queueName} error=${pyErrorStr(error)}`,
        );
      } finally {
        this._leaderQueue = null;
      }
    }

    if (this._exchangeName) {
      try {
        if (this._channel === null) {
          throw new Error("RabbitMQ channel is not ready");
        }
        logger.info(
          `event=exchange_delete_attempt group_id=${this._groupId} exchange=${this._exchangeName}`,
        );
        await this._channel.exchangeDelete(this._exchangeName, { ifUnused: false });
      } catch (error) {
        logger.warn(
          `event=exchange_cleanup_failed group_id=${this._groupId} exchange=${this._exchangeName} error=${pyErrorStr(error)}`,
        );
      } finally {
        this._exchange = null;
        this._exchangeName = null;
      }
    }
  }

  /** 解散群组 */
  async dissolveGroup(): Promise<void> {
    if (this._state === GroupLeaderState.DISCONNECTED) {
      return;
    }

    this._state = GroupLeaderState.DISSOLVING;

    await this.broadcastGroupShutdown();

    await this.cleanupGroupResources();

    if (channelNeedsClose(this._channel)) {
      await this._channel.close();
    }
    if (this._connectionOwner && this._connection && !this._connection.isClosed) {
      await this._connection.close();
    }

    this._state = GroupLeaderState.DISCONNECTED;
    this._partners.clear();

    logger.info(`event=group_dissolved group_id=${this._groupId}`);
  }

  /** 关闭客户端连接 */
  async close(): Promise<void> {
    if (this._state !== GroupLeaderState.DISCONNECTED) {
      await this.dissolveGroup();
    }
  }
}

/** 单个 Partner 在某任务中的状态快照（键名同 Python）。 */
export interface PartnerTaskSnapshot {
  task_id: string;
  state: TaskState;
  product_text: string | undefined;
  awaiting_prompt: string | null | undefined;
}

/** 任务摘要（键名同 Python）。 */
export interface TaskSummary {
  task_id: string;
  states: Record<string, TaskState>;
  products: Record<string, string>;
  completed_count: number;
  working_count: number;
}

function createDict<T>(): Record<string, T> {
  return Object.create(null) as Record<string, T>;
}

/**
 * 群组会话管理
 *
 * 封装一个完整的群组会话，包含：
 * - 群组客户端
 * - 会话状态
 * - 任务状态跟踪
 * - 消息历史
 */
export class GroupLeaderSession {
  readonly MAX_HISTORY = 1000;
  readonly sessionId: string;
  readonly groupId: string;
  readonly leaderMqClient: GroupLeaderMqClient;
  readonly createdAt: string;
  lastModified: Date;

  /** 任务状态跟踪：taskId -> { partnerAic -> TaskState } */
  taskStates: Record<string, Record<string, TaskState>> = createDict();
  /** Partner 产出物：taskId -> { partnerAic -> productText } */
  taskProducts: Record<string, Record<string, string>> = createDict();
  /** Partner 等待输入的提示信息：taskId -> { partnerAic -> AwaitingInput 提示 } */
  taskPrompts: Record<string, Record<string, string | null>> = createDict();
  /** 消息历史 */
  messageHistory: GroupMessage[] = [];
  /** 状态更新事件 */
  readonly stateUpdateEvent = new AsyncEvent();
  /** Partner 最近一次邀请路由 */
  readonly invitationRoutes = new Map<string, string>();

  constructor(sessionId: string, groupId: string, leaderMqClient: GroupLeaderMqClient) {
    this.sessionId = sessionId;
    this.groupId = groupId;
    this.leaderMqClient = leaderMqClient;
    this.createdAt = utcNowIso();
    this.lastModified = new Date();
  }

  /** 更新最后修改时间 */
  touch(): void {
    this.lastModified = new Date();
  }

  /** 记录 Partner 的邀请路由。 */
  recordInvitationRoute(partnerAic: string, route: string): void {
    this.touch();
    this.invitationRoutes.set(partnerAic, route);
  }

  /** 记录任务状态 */
  recordTaskState(
    taskId: string,
    partnerAic: string,
    state: TaskState,
    {
      productText = null,
      awaitingPrompt = null,
    }: { productText?: string | null; awaitingPrompt?: string | null } = {},
  ): void {
    this.touch();
    let states = this.taskStates[taskId];
    if (states === undefined) {
      states = createDict();
      this.taskStates[taskId] = states;
      logger.debug(
        `[GroupLeaderSession] Created new task entry: session=${this.sessionId} task_id=${taskId}`,
      );
    }

    const oldState = states[partnerAic];
    states[partnerAic] = state;

    logger.info(
      `[GroupLeaderSession] Task state updated: session=${this.sessionId} task_id=${taskId} partner=${partnerAic} old_state=${pyStr(oldState)} new_state=${state} total_partners=${Object.keys(states).length}`,
    );

    if (productText) {
      let products = this.taskProducts[taskId];
      if (products === undefined) {
        products = createDict();
        this.taskProducts[taskId] = products;
      }
      products[partnerAic] = productText;
    }

    // 处理额外的 prompt 信息
    if (awaitingPrompt !== null && awaitingPrompt !== undefined) {
      let prompts = this.taskPrompts[taskId];
      if (prompts === undefined) {
        prompts = createDict();
        this.taskPrompts[taskId] = prompts;
      }
      prompts[partnerAic] = awaitingPrompt;
    } else if (this.taskPrompts[taskId] !== undefined) {
      delete this.taskPrompts[taskId][partnerAic];
    }

    // 触发状态更新事件
    this.stateUpdateEvent.set();
  }

  /**
   * 获取特定 Partner 在指定任务中的状态快照
   *
   * 包含：当前状态（state）、产出物（product_text）、等待输入的提示（awaiting_prompt）
   */
  getPartnerTaskSnapshot(taskId: string, partnerAic: string): PartnerTaskSnapshot | null {
    const state = this.taskStates[taskId]?.[partnerAic];
    if (!state) {
      return null;
    }
    return {
      task_id: taskId,
      state,
      product_text: this.taskProducts[taskId]?.[partnerAic],
      awaiting_prompt: this.taskPrompts[taskId]?.[partnerAic],
    };
  }

  /** 获取任务摘要 */
  getTaskSummary(taskId: string): TaskSummary {
    const states = this.taskStates[taskId] ?? {};
    const values = Object.values(states);
    return {
      task_id: taskId,
      states,
      products: this.taskProducts[taskId] ?? {},
      completed_count: values.filter((s) => s === TaskState.Completed).length,
      working_count: values.filter((s) => s === TaskState.Accepted || s === TaskState.Working)
        .length,
    };
  }

  /** 记录消息到历史 */
  recordMessage(message: GroupMessage): void {
    this.touch();
    this.messageHistory.push(message);
    if (this.messageHistory.length > this.MAX_HISTORY) {
      this.messageHistory = this.messageHistory.slice(-this.MAX_HISTORY);
    }
  }
}

/** `GroupLeader` 的 RabbitMQ 配置（键名同 Python `rabbitmq_config` dict）。 */
export interface GroupLeaderRabbitMQConfig {
  host?: string;
  port?: number;
  vhost?: string;
  user?: string | null;
  password?: string | null;
  /** mq-auth-server 地址；配置后启用群组 ACL（inbox 邀请与强制移除必需） */
  auth_service_url?: string | null;
  /** `auth_service_url` 的 camelCase 写法 */
  authServiceUrl?: string | null;
}

export interface GroupLeaderOptions {
  /** 对应 Python `ssl_context`：AMQPS + EXTERNAL 的 mTLS 证书，以及 HTTPS 校验用 CA */
  sslContext?: TlsClientOptions | null;
  /** 对应 Python `ssl_cert`：直连 RPC 邀请时的客户端证书 */
  sslCert?: ClientCertificate | null;
  /** inbox 邀请超时（秒），默认 300 */
  invitationTimeoutSeconds?: number;
  /** 默认 true */
  identityBindingEnabled?: boolean;
  messageEmitter?: MessageEmitterLike | null;
  traceContextProvider?: TraceContextProvider | null;
  /** AMQP 连接器（默认 amqplib） */
  connector?: AmqpConnector;
  /** 直连 RPC 邀请的 HTTP 传输层（测试注入） */
  transport?: HttpTransport;
  /** ACL 客户端的 HTTP 传输层（测试注入） */
  aclTransport?: HttpTransport;
}

/** `GroupLeader.getGroupRuntime()` 中的成员条目（键名同 Python）。 */
export interface GroupRuntimeMember {
  partner_aic: string;
  invitation_route: string | null;
  connected: boolean;
  muted: boolean;
  connection_name: string;
  vhost: string;
  node_name: string;
  queue_name: string;
  joined_at: string;
}

/** `GroupLeader.getGroupRuntime()` 的结果（键名同 Python）。 */
export interface GroupRuntimeInfo {
  session_id: string;
  group_id: string;
  leader_aic: string;
  state: GroupLeaderState;
  total_members: number;
  connected_members: number;
  pending_invitations: string[];
  members: GroupRuntimeMember[];
}

/** `GroupLeader.forceRemovePartner()` 的结果（键名同 Python）。 */
export interface ForceRemovePartnerResult {
  session_id: string;
  group_id: string;
  partner_aic: string;
  queue_deleted: boolean;
}

function compareStrings(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

/**
 * 通用群组 Leader 实现
 *
 * 提供：
 * - MQ 连接管理
 * - 会话（Session）管理
 * - 任务生命周期管理
 * - 成员管理
 */
export class GroupLeader {
  readonly leaderAic: string;
  sslContext: TlsClientOptions | null;
  sslCert: ClientCertificate | null;
  rabbitmqHost: string;
  rabbitmqPort: number;
  rabbitmqVhost: string;
  rabbitmqUser: string | null;
  rabbitmqPassword: string | null;
  authServiceUrl: string | null;
  readonly groupSessions = new Map<string, GroupLeaderSession>();

  /** @internal */ _messageEmitter: MessageEmitterLike | null;
  /** @internal */ _traceContextProvider: TraceContextProvider | null;
  /** @internal */ _invitationTimeoutSeconds: number;
  /** @internal */ _identityBindingEnabled: boolean;
  /** @internal */ _connector: AmqpConnector;
  /** @internal */ _httpTransport: HttpTransport | null;
  /** @internal */ _connection: AmqpConnection | null = null;
  /** @internal */ _connectionLock = new AsyncLock();
  /** @internal */ _inboxChannel: AmqpChannel | null = null;
  /** @internal */ _inboxQueue: AmqpQueue | null = null;
  /** @internal */ _pendingInvitations = new Map<string, PendingInvitation>();
  /** @internal */ _cleanupTask: CancellableTask | null = null;
  /** @internal */ _groupAclClient: GroupAclClientLike | null = null;
  /** @internal */ _forceRemoveGracePeriodSeconds = 1.0;
  /** @internal */ _forceRemovePollIntervalSeconds = 0.05;
  /** @internal 正在创建中的 sessionId（防止并发重复创建） */
  _creatingSessions = new Set<string>();

  constructor(
    leaderAic: string,
    rabbitmqConfig: GroupLeaderRabbitMQConfig,
    options: GroupLeaderOptions = {},
  ) {
    this.leaderAic = leaderAic;
    this.sslContext = options.sslContext ?? null;
    this.sslCert = options.sslCert ?? null;
    this._messageEmitter = options.messageEmitter ?? null;
    this._traceContextProvider = options.traceContextProvider ?? null;
    this.rabbitmqHost = rabbitmqConfig.host ?? "localhost";
    this.rabbitmqPort = rabbitmqConfig.port ?? 5672;
    this.rabbitmqVhost = rabbitmqConfig.vhost ?? "/";
    this.rabbitmqUser = rabbitmqConfig.user ?? null;
    this.rabbitmqPassword = rabbitmqConfig.password ?? null;
    this.authServiceUrl = rabbitmqConfig.auth_service_url ?? rabbitmqConfig.authServiceUrl ?? null;
    this._invitationTimeoutSeconds =
      options.invitationTimeoutSeconds ?? DEFAULT_INVITATION_TIMEOUT_SECONDS;
    this._identityBindingEnabled = options.identityBindingEnabled ?? true;
    this._connector = options.connector ?? defaultAmqpConnector;
    this._httpTransport = options.transport ?? null;
    if (typeof this.authServiceUrl === "string" && pyStrip(this.authServiceUrl)) {
      this._groupAclClient = new GroupAclClient({
        baseUrl: pyStrip(this.authServiceUrl),
        sslContext: this.sslContext,
        ...(options.aclTransport ? { transport: options.aclTransport } : {}),
      });
    }
  }

  /** @internal */
  async _ensureConnection(): Promise<AmqpConnection> {
    return this._connectionLock.runExclusive(async () => {
      if (this._connection && !this._connection.isClosed) {
        await this._ensureInbox();
        return this._connection;
      }

      const connectionName = `agent-${this.leaderAic}`;
      let connectionUrl: string;
      let sslContext: TlsClientOptions | null = null;
      if (this.rabbitmqUser && this.rabbitmqPassword) {
        connectionUrl = buildPlainConnectionUrl({
          host: this.rabbitmqHost,
          port: this.rabbitmqPort,
          vhost: this.rabbitmqVhost,
          username: this.rabbitmqUser,
          password: this.rabbitmqPassword,
          connectionName,
        });
      } else {
        if (this.sslContext === null) {
          throw new Error("ssl_context is required for AMQPS + EXTERNAL authentication");
        }
        connectionUrl = buildExternalConnectionUrl({
          host: this.rabbitmqHost,
          port: this.rabbitmqPort,
          vhost: this.rabbitmqVhost,
          connectionName,
        });
        sslContext = this.sslContext;
      }

      this._connection = await this._connector.connectRobust(
        connectionUrl,
        sslContext ? { sslContext } : {},
      );
      await this._ensureInbox();
      logger.info(
        `event=rabbitmq_shared_connection_ready leader_aic=${this.leaderAic} host=${this.rabbitmqHost} vhost=${this.rabbitmqVhost}`,
      );
      return this._connection;
    });
  }

  /** @internal */
  async _ensureInbox(): Promise<void> {
    if (this._inboxQueue && !(this._inboxQueue.isClosed ?? false)) {
      return;
    }
    if (!this._connection) {
      throw new Error("RabbitMQ connection is not ready");
    }

    const inboxName = buildInboxQueueName(this.leaderAic);
    if (channelNeedsClose(this._inboxChannel)) {
      // 旧的 inbox channel 已失效：关闭它，避免重连后恢复出第二个消费者
      await this._inboxChannel.close().catch(() => undefined);
    }
    this._inboxChannel = null;
    this._inboxQueue = null;
    const inboxChannel = await this._connection.channel();
    let inboxQueue: AmqpQueue;
    try {
      inboxQueue = await inboxChannel.declareQueue(inboxName, {
        durable: true,
        exclusive: false,
        autoDelete: false,
        arguments: {
          "x-expires": INBOX_QUEUE_EXPIRES_MS,
          "x-message-ttl": INBOX_MESSAGE_TTL_MS,
        },
      });
      const inboxExchange = await inboxChannel.declareExchange(INBOX_EXCHANGE_NAME, {
        type: ExchangeType.TOPIC,
        durable: true,
        autoDelete: false,
      });
      await inboxQueue.bind(inboxExchange, { routingKey: inboxName });
    } catch (error) {
      // 半初始化的 inbox 不保留：下次 _ensureInbox 会重新建立（Python 版会把半成品当作可用）
      await inboxChannel.close().catch(() => undefined);
      throw error;
    }

    const onMessage = async (message: AmqpIncomingMessage): Promise<void> => {
      await message.process(async () => {
        let decoded: unknown;
        try {
          decoded = decodeJsonBody(message.body);
        } catch (error) {
          if (error instanceof JSONDecodeError) {
            logger.warn(`event=leader_inbox_invalid_json leader_aic=${this.leaderAic}`);
            return;
          }
          throw error;
        }
        const body = requireJsonDict(decoded);

        if (body.type !== "group-invitation-error") {
          logger.debug(
            `event=leader_inbox_ignored leader_aic=${this.leaderAic} type=${pyStr(body.type)}`,
          );
          return;
        }

        let errorMessage: InboxGroupInvitationError;
        try {
          errorMessage = InboxGroupInvitationErrorSchema.parse(body);
        } catch (error) {
          logger.warn(
            `event=leader_inbox_invalid_message leader_aic=${this.leaderAic} error=${pyErrorStr(error)}`,
          );
          return;
        }

        if (this._identityBindingEnabled) {
          assertIncomingGroupMessageIdentity(errorMessage, {
            amqpUserId: extractAmqpUserId(message),
            expectedGroupId: errorMessage.groupId,
          });
        }

        await this._handleInvitationError(errorMessage);
      });
    };

    try {
      await inboxQueue.consume(onMessage);
    } catch (error) {
      await inboxChannel.close().catch(() => undefined);
      throw error;
    }
    this._inboxChannel = inboxChannel;
    this._inboxQueue = inboxQueue;
  }

  /** @internal */
  _getSession(sessionId: string | null | undefined): GroupLeaderSession {
    if (sessionId) {
      const session = this.groupSessions.get(sessionId);
      if (!session) {
        throw new Error(`Group session not found: ${sessionId}`);
      }
      return session;
    }

    if (this.groupSessions.size === 1) {
      return [...this.groupSessions.values()][0] as GroupLeaderSession;
    }

    throw new Error("Multiple sessions active; please specify session_id");
  }

  // ------------------------------------------------------------------
  // 群组初始化与成员邀请
  // ------------------------------------------------------------------

  /** 创建群组会话 */
  async createGroupSession(
    sessionId: string,
    initialPartners: ACSObject[],
  ): Promise<GroupLeaderSession> {
    // 与 Python 不同：同一 sessionId 不允许重复创建（Python 会静默替换，泄漏旧 channel / 消费者）
    if (this.groupSessions.has(sessionId) || this._creatingSessions.has(sessionId)) {
      throw new Error(`Group session already exists: ${sessionId}`);
    }
    this._creatingSessions.add(sessionId);
    try {
      return await this._createGroupSession(sessionId, initialPartners);
    } finally {
      this._creatingSessions.delete(sessionId);
    }
  }

  /** @internal */
  async _createGroupSession(
    sessionId: string,
    initialPartners: ACSObject[],
  ): Promise<GroupLeaderSession> {
    const connection = await this._ensureConnection();
    const groupIdSeed = normalizeGroupId(`group-${sessionId}`);

    const sessionClient = new GroupLeaderMqClient(this.leaderAic, {
      rabbitmqHost: this.rabbitmqHost,
      rabbitmqPort: this.rabbitmqPort,
      rabbitmqVhost: this.rabbitmqVhost,
      rabbitmqUser: this.rabbitmqUser,
      rabbitmqPassword: this.rabbitmqPassword,
      sslContext: this.sslContext,
      connection,
      connectionOwner: false,
      identityBindingEnabled: this._identityBindingEnabled,
      messageEmitter: this._messageEmitter,
      traceContextProvider: this._traceContextProvider,
      connector: this._connector,
      ...(this._httpTransport ? { transport: this._httpTransport } : {}),
    });

    await sessionClient.connect();

    let groupId: string;
    try {
      if (this._groupAclClient) {
        await this._groupAclClient.addMember({
          leaderAic: this.leaderAic,
          groupId: groupIdSeed,
          memberAic: this.leaderAic,
        });
        for (const partner of initialPartners) {
          await this._groupAclClient.addMember({
            leaderAic: this.leaderAic,
            groupId: groupIdSeed,
            memberAic: partner.aic,
          });
        }
      }

      groupId = await sessionClient.createGroup({
        groupId: groupIdSeed,
        partnerAcsList: initialPartners,
      });
    } catch (error) {
      if (this._groupAclClient) {
        try {
          await this._groupAclClient.deleteGroup({
            leaderAic: this.leaderAic,
            groupId: groupIdSeed,
          });
        } catch (rollbackError) {
          // 回滚失败只记录，始终抛出原始异常（Python 只捕获 GroupAclError，其它异常会覆盖原始异常）
          logger.warn(
            `event=group_acl_rollback_failed leader_aic=${this.leaderAic} group_id=${groupIdSeed} error=${pyErrorStr(rollbackError)}`,
          );
        }
      }
      if (channelNeedsClose(sessionClient._channel)) {
        await sessionClient._channel.close().catch(() => undefined);
      }
      throw error;
    }

    // 与 Python 不同：每个会话的客户端只把消息交给本会话（不按不可信的 payload sessionId 路由）
    const session = new GroupLeaderSession(sessionId, groupId, sessionClient);
    sessionClient.setMessageHandler((message) => this._handleGroupMessage(message, session));
    try {
      await sessionClient.startConsuming();
    } catch (error) {
      // 如果消费启动失败，尝试清理由刚创建的资源，保持一致性
      try {
        if (this._groupAclClient) {
          try {
            await this._groupAclClient.deleteGroup({ leaderAic: this.leaderAic, groupId });
          } catch (aclError) {
            logger.warn(
              `event=group_acl_delete_failed_after_start_error group_id=${groupId} error=${pyErrorStr(aclError)}`,
            );
          }
        }
        await sessionClient.dissolveGroup();
      } finally {
        // 与 Python `finally: raise` 相同：始终抛出原始异常
        // eslint-disable-next-line no-unsafe-finally
        throw error;
      }
    }

    if (this._cleanupTask === null || this._cleanupTask.done()) {
      this._cleanupTask = createTask((signal) => this._cleanupLoop(signal));
    }

    this.groupSessions.set(sessionId, session);
    logger.info(
      `event=group_session_created session_id=${sessionId} group_id=${groupId} partner_count=${initialPartners.length}`,
    );
    return session;
  }

  /**
   * 邀请 Partner 加入会话
   *
   * 优先使用 Partner ACS 中与 Leader 同一 broker 的 AMQP 端点（inbox 邀请），否则回退到 JSONRPC 直连邀请。
   */
  async invitePartner(
    sessionId: string,
    partnerAcs: ACSObject,
    {
      partnerRpcUrl = null,
      partnerAcsData = null,
    }: { partnerRpcUrl?: string | null; partnerAcsData?: Record<string, unknown> | null } = {},
  ): Promise<boolean> {
    const session = this.groupSessions.get(sessionId);
    if (!session) {
      throw new Error(`Group session not found: ${sessionId}`);
    }

    const amqpUrl = getEndpointUrl(partnerAcsData, "AMQP", { aic: partnerAcs.aic });
    const rpcUrl =
      partnerRpcUrl || getEndpointUrl(partnerAcsData, "JSONRPC", { aic: partnerAcs.aic });

    if (amqpUrl) {
      const amqpEndpoint = parseAmqpEndpointUrl(amqpUrl, { aic: partnerAcs.aic });
      // 把 compose 中的占位主机名 "rabbitmq" 视为与本地相同的 broker
      const amqpHosts = new Set([this.rabbitmqHost, "rabbitmq"]);
      if (
        amqpHosts.has(amqpEndpoint.host) &&
        amqpEndpoint.port === this.rabbitmqPort &&
        amqpEndpoint.vhost === this.rabbitmqVhost
      ) {
        session.recordInvitationRoute(partnerAcs.aic, "inbox");
        logger.info(
          `event=partner_invite_route_selected session_id=${session.sessionId} partner=${partnerAcs.aic} route=inbox inbox=${amqpEndpoint.inbox}`,
        );
        await this._invitePartnerViaInbox({
          session,
          partnerAcs,
          partnerInbox: amqpEndpoint.inbox,
        });
        return true;
      }

      logger.warn(
        `event=partner_amqp_endpoint_mismatch partner=${partnerAcs.aic} endpoint=${redactUrl(amqpUrl)}`,
      );
    }

    if (rpcUrl) {
      session.recordInvitationRoute(partnerAcs.aic, "rpc");
      logger.info(
        `event=partner_invite_route_selected session_id=${session.sessionId} partner=${partnerAcs.aic} route=rpc url=${redactUrl(rpcUrl)}`,
      );
      await this._invitePartnerViaRpc({ session, partnerAcs, rpcUrl });
      return true;
    }

    throw new PartnerInviteError(
      `No AMQP or JSONRPC invitation endpoint for partner ${partnerAcs.aic}`,
    );
  }

  /** @internal */
  async _invitePartnerViaRpc({
    session,
    partnerAcs,
    rpcUrl,
  }: {
    session: GroupLeaderSession;
    partnerAcs: ACSObject;
    rpcUrl: string;
  }): Promise<void> {
    logger.info(
      `event=partner_invite_begin session_id=${session.sessionId} partner=${partnerAcs.aic} route=rpc url=${redactUrl(rpcUrl)}`,
    );

    if (this._groupAclClient) {
      await this._groupAclClient.addMember({
        leaderAic: this.leaderAic,
        groupId: session.groupId,
        memberAic: partnerAcs.aic,
      });
    }

    try {
      await session.leaderMqClient.invitePartner(partnerAcs, rpcUrl, {
        sslContext: this.sslContext,
        clientCert: this.sslCert,
      });
    } catch (error) {
      if (this._groupAclClient) {
        try {
          await this._groupAclClient.removeMember({
            leaderAic: this.leaderAic,
            groupId: session.groupId,
            memberAic: partnerAcs.aic,
          });
        } catch (aclError) {
          // 回滚失败只记录，始终抛出原始异常
          logger.warn(
            `event=partner_acl_rollback_failed session_id=${session.sessionId} partner=${partnerAcs.aic} error=${pyErrorStr(aclError)}`,
          );
        }
      }
      throw error;
    }

    logger.info(
      `event=partner_invited session_id=${session.sessionId} partner=${partnerAcs.aic} route=rpc`,
    );
  }

  /** @internal */
  async _invitePartnerViaInbox({
    session,
    partnerAcs,
    partnerInbox,
  }: {
    session: GroupLeaderSession;
    partnerAcs: ACSObject;
    partnerInbox: string;
  }): Promise<void> {
    const aclClient = this._groupAclClient;
    if (aclClient === null) {
      throw new PartnerInviteError("auth_service_url is required for inbox-based invitations");
    }

    const expectedInbox = buildInboxQueueName(partnerAcs.aic);
    if (partnerInbox !== expectedInbox) {
      throw new PartnerInviteError(`Partner inbox mismatch for ${partnerAcs.aic}: ${partnerInbox}`);
    }

    if (this._pendingInvitations.has(partnerAcs.aic)) {
      throw new PartnerInviteError(`Partner ${partnerAcs.aic} already has a pending invitation`);
    }

    // 与 Python 不同：在第一个 await 之前同步占住挂起记录，否则并发的第二次邀请会通过上面的检查并覆盖
    // 第一次的记录（第一次 invitePartner 永远等待，ACL 记录泄漏）
    const future = new Deferred<PartnerConnectionInfo>();
    const invitationToken = createInvitationToken();
    const expiresAt = calculateInvitationExpiry(this._invitationTimeoutSeconds);
    const pending: PendingInvitation = {
      sessionId: session.sessionId,
      partnerAic: partnerAcs.aic,
      invitationToken,
      future,
      timeoutTask: createTask((signal) =>
        this._watchInvitationTimeout(
          {
            sessionId: session.sessionId,
            groupId: session.groupId,
            partnerAic: partnerAcs.aic,
            invitationToken,
          },
          signal,
        ).catch((error: unknown) => {
          if (!(error instanceof CancelledError)) {
            logger.error(
              `event=invitation_timeout_watch_failed session_id=${session.sessionId} partner=${partnerAcs.aic} error=${pyErrorStr(error)}`,
            );
          }
          throw error;
        }),
      ),
    };
    this._pendingInvitations.set(partnerAcs.aic, pending);

    try {
      await aclClient.addMember({
        leaderAic: this.leaderAic,
        groupId: session.groupId,
        memberAic: partnerAcs.aic,
      });
    } catch (error) {
      if (!future.done()) future.setException(error);
      this._clearPendingInvitation(partnerAcs.aic, pending);
      throw error;
    }

    try {
      await session.leaderMqClient.inviteViaInbox(partnerAcs, {
        partnerInbox,
        invitationToken,
        expiresAt,
        timeoutSeconds: this._invitationTimeoutSeconds,
      });
      await future.promise;
      this._clearPendingInvitation(partnerAcs.aic, pending);
    } catch (error) {
      this._clearPendingInvitation(partnerAcs.aic, pending);
      try {
        await aclClient.removeMember({
          leaderAic: this.leaderAic,
          groupId: session.groupId,
          memberAic: partnerAcs.aic,
        });
      } catch (aclError) {
        // 回滚失败只记录，始终抛出原始异常
        logger.warn(
          `event=partner_acl_rollback_failed session_id=${session.sessionId} partner=${partnerAcs.aic} route=inbox error=${pyErrorStr(aclError)}`,
        );
      }
      throw error;
    }

    logger.info(
      `event=partner_invited session_id=${session.sessionId} partner=${partnerAcs.aic} route=inbox`,
    );
  }

  /** @internal 邀请超时监视；`signal` 中止时以 `CancelledError` 结束（对应 task.cancel()）。 */
  async _watchInvitationTimeout(
    {
      sessionId,
      groupId,
      partnerAic,
      invitationToken,
    }: { sessionId: string; groupId: string; partnerAic: string; invitationToken: string },
    signal?: AbortSignal,
  ): Promise<void> {
    await sleep(this._invitationTimeoutSeconds * 1000, signal);
    const pending = this._pendingInvitations.get(partnerAic);
    if (!pending || pending.invitationToken !== invitationToken) {
      return;
    }
    if (pending.future.done()) {
      return;
    }

    // 与 Python 不同：先结束邀请并移除挂起记录，再清理 ACL。这样 ACL 清理期间迟到的
    // “connected” 结果不会再把这次邀请当作成功（Python 的顺序会让它“成功”，而 ACL 随即被删）；
    // 而且无论 ACL 清理是否失败，invitePartner 都不会永远等待。
    pending.future.setException(
      new PartnerInviteError(`Invitation timed out for partner ${partnerAic} in group ${groupId}`),
    );
    logger.warn(
      `event=invitation_timeout session_id=${sessionId} partner_aic=${partnerAic} group_id=${groupId}`,
    );
    this._clearPendingInvitation(partnerAic, pending);

    try {
      if (this._groupAclClient) {
        await this._groupAclClient.removeMember({
          leaderAic: this.leaderAic,
          groupId,
          memberAic: partnerAic,
        });
      }
    } catch (error) {
      logger.warn(
        `event=invitation_timeout_acl_cleanup_failed session_id=${sessionId} partner=${partnerAic} error=${pyErrorStr(error)}`,
      );
    }
  }

  /** @internal */
  async _handleInvitationError(errorMessage: InboxGroupInvitationError): Promise<void> {
    const partnerAic = (errorMessage.senderId || errorMessage.partnerAic) as string;
    const pending = this._pendingInvitations.get(partnerAic);
    if (!pending) {
      logger.warn(
        `event=invitation_error_without_pending partner=${partnerAic} group_id=${errorMessage.groupId}`,
      );
      return;
    }

    if (pending.invitationToken !== errorMessage.invitationToken) {
      // 邀请令牌是一次性凭据：日志里只记录摘要（Python 版原样记录令牌）
      logger.warn(
        `event=invitation_error_token_mismatch partner=${partnerAic} expected=${redactToken(pending.invitationToken)} actual=${redactToken(errorMessage.invitationToken)}`,
      );
      return;
    }

    // 与 Python 不同：先结束邀请并移除挂起记录，再清理 ACL（理由同 _watchInvitationTimeout）
    const errorData = errorMessage.error.data;
    const details = errorData?.details;
    const errorType = errorData?.errorType;
    let exc: PartnerInviteError;
    if (errorMessage.error.code === -32020 || errorType === "INVITATION_REJECTED") {
      exc = new PartnerResponseError(errorMessage.error.message);
    } else {
      const detailSuffix = pyTruthy(details) ? `: ${pyStr(details)}` : "";
      exc = new PartnerNetworkError(`${errorMessage.error.message}${detailSuffix}`);
    }
    if (!pending.future.done()) {
      pending.future.setException(exc);
    }
    this._clearPendingInvitation(partnerAic, pending);

    if (this._groupAclClient) {
      try {
        await this._groupAclClient.removeMember({
          leaderAic: this.leaderAic,
          groupId: errorMessage.groupId,
          memberAic: partnerAic,
        });
      } catch (error) {
        logger.warn(
          `event=invitation_error_acl_cleanup_failed partner=${partnerAic} group_id=${errorMessage.groupId} error=${pyErrorStr(error)}`,
        );
      }
    }
  }

  /**
   * @internal 移除挂起邀请并取消其超时任务。
   * 传入 `pending` 时只在它仍是当前记录时移除（避免旧邀请的清理误删同一 Partner 的新邀请）。
   */
  _clearPendingInvitation(partnerAic: string, pending?: PendingInvitation): void {
    const current = this._pendingInvitations.get(partnerAic);
    if (pending !== undefined && current !== pending) {
      pending.timeoutTask.cancel();
      return;
    }
    this._pendingInvitations.delete(partnerAic);
    if (current) {
      current.timeoutTask.cancel();
    }
  }

  // ------------------------------------------------------------------
  // 任务控制
  // ------------------------------------------------------------------

  /** 启动任务 */
  async startTask(
    sessionId: string,
    {
      taskContent,
      taskId = null,
      targetPartners = null,
    }: { taskContent: string; taskId?: string | null; targetPartners?: string[] | null },
  ): Promise<string> {
    const session = this.groupSessions.get(sessionId);
    if (!session) {
      throw new Error(`Group session not found: ${sessionId}`);
    }

    return session.leaderMqClient.startTask(sessionId, taskContent, {
      taskId,
      mentions: targetPartners,
    });
  }

  /** 继续任务 */
  async continueTask(
    sessionId: string,
    taskId: string,
    content: string,
    { targetPartner = null }: { targetPartner?: string | null } = {},
  ): Promise<void> {
    const mentions = targetPartner ? [targetPartner] : null;
    const session = this.groupSessions.get(sessionId);
    if (!session) {
      throw new Error(`Group session not found: ${sessionId}`);
    }

    await session.leaderMqClient.continueTask(taskId, sessionId, content, { mentions });
  }

  /** 完成任务 */
  async completeTask(
    sessionId: string,
    taskId: string,
    { targetPartner = null }: { targetPartner?: string | null } = {},
  ): Promise<void> {
    const mentions = targetPartner ? [targetPartner] : null;
    const session = this.groupSessions.get(sessionId);
    if (!session) {
      throw new Error(`Group session not found: ${sessionId}`);
    }

    await session.leaderMqClient.completeTask(taskId, sessionId, { mentions });
  }

  /** 取消任务 */
  async cancelTask(
    sessionId: string,
    taskId: string,
    {
      reason = null,
      targetPartner = null,
    }: { reason?: string | null; targetPartner?: string | null } = {},
  ): Promise<void> {
    const mentions = targetPartner ? [targetPartner] : null;
    const session = this.groupSessions.get(sessionId);
    if (!session) {
      throw new Error(`Group session not found: ${sessionId}`);
    }

    await session.leaderMqClient.cancelTask(taskId, sessionId, { reason, mentions });
  }

  // ------------------------------------------------------------------
  // 群组管理命令
  // ------------------------------------------------------------------

  async checkPartnerStatus({
    partnerAic = null,
    sessionId = null,
  }: { partnerAic?: string | null; sessionId?: string | null } = {}): Promise<GroupMgmtCommand> {
    const session = this._getSession(sessionId);
    return session.leaderMqClient.getPartnerStatus({ partnerAic, sessionId: session.sessionId });
  }

  async requestPartnerLeave(
    partnerAic: string,
    { sessionId = null }: { sessionId?: string | null } = {},
  ): Promise<GroupMgmtCommand> {
    const session = this._getSession(sessionId);
    return session.leaderMqClient.requestPartnerLeave(partnerAic, { sessionId: session.sessionId });
  }

  getGroupRuntime({ sessionId = null }: { sessionId?: string | null } = {}): GroupRuntimeInfo {
    const session = this._getSession(sessionId);
    const members: GroupRuntimeMember[] = [...session.leaderMqClient.partners.entries()]
      .sort(([a], [b]) => compareStrings(a, b))
      .map(([, partner]) => ({
        partner_aic: partner.aic,
        invitation_route: session.invitationRoutes.get(partner.aic) ?? null,
        connected: partner.connected,
        muted: partner.muted,
        connection_name: partner.connectionName,
        vhost: partner.vhost,
        node_name: partner.nodeName,
        queue_name: partner.queueName,
        joined_at: partner.joinedAt,
      }));
    const pendingInvitations = [...this._pendingInvitations.entries()]
      .filter(([, pending]) => pending.sessionId === session.sessionId)
      .map(([partnerAic]) => partnerAic)
      .sort(compareStrings);
    const connectedMembers = members.filter((member) => member.connected).length;
    return {
      session_id: session.sessionId,
      group_id: session.groupId,
      leader_aic: this.leaderAic,
      state: session.leaderMqClient.state,
      total_members: members.length,
      connected_members: connectedMembers,
      pending_invitations: pendingInvitations,
      members,
    };
  }

  /** @internal */
  async _waitForPartnerDisconnect(
    session: GroupLeaderSession,
    partnerAic: string,
  ): Promise<boolean> {
    const deadline = performance.now() + this._forceRemoveGracePeriodSeconds * 1000;
    for (;;) {
      const partner = session.leaderMqClient.partners.get(partnerAic);
      if (partner !== undefined && !partner.connected) {
        return true;
      }
      if (performance.now() >= deadline) {
        return false;
      }
      await sleep(this._forceRemovePollIntervalSeconds * 1000);
    }
  }

  async forceRemovePartner(
    partnerAic: string,
    { sessionId = null }: { sessionId?: string | null } = {},
  ): Promise<ForceRemovePartnerResult> {
    const session = this._getSession(sessionId);
    const aclClient = this._groupAclClient;
    if (aclClient === null) {
      throw new PartnerInviteError("auth_service_url is required for force-remove operations");
    }

    logger.info(
      `event=partner_force_remove_begin session_id=${session.sessionId} group_id=${session.groupId} partner_aic=${partnerAic}`,
    );

    let leaveRequested = false;
    try {
      await session.leaderMqClient.requestPartnerLeave(partnerAic, {
        sessionId: session.sessionId,
      });
      leaveRequested = true;
    } catch (error) {
      logger.warn(
        `event=partner_force_remove_leave_request_failed session_id=${session.sessionId} group_id=${session.groupId} partner_aic=${partnerAic} error=${pyErrorStr(error)}`,
      );
    }
    if (leaveRequested) {
      const gracefulDisconnect = await this._waitForPartnerDisconnect(session, partnerAic);
      if (gracefulDisconnect) {
        await aclClient.removeMember({
          leaderAic: this.leaderAic,
          groupId: session.groupId,
          memberAic: partnerAic,
        });
        session.leaderMqClient.removePartnerTracking(partnerAic);
        logger.info(
          `event=partner_force_removed session_id=${session.sessionId} group_id=${session.groupId} partner_aic=${partnerAic} queue_deleted=False mode=graceful`,
        );
        return {
          session_id: session.sessionId,
          group_id: session.groupId,
          partner_aic: partnerAic,
          queue_deleted: false,
        };
      }
    }

    await aclClient.removeMember({
      leaderAic: this.leaderAic,
      groupId: session.groupId,
      memberAic: partnerAic,
    });
    const queueDeleted = await session.leaderMqClient.deletePartnerQueue(partnerAic);
    await aclClient.closeMemberConnection({
      leaderAic: this.leaderAic,
      groupId: session.groupId,
      memberAic: partnerAic,
    });

    session.leaderMqClient.removePartnerTracking(partnerAic);
    logger.info(
      `event=partner_force_removed session_id=${session.sessionId} group_id=${session.groupId} partner_aic=${partnerAic} queue_deleted=${
        queueDeleted ? "True" : "False"
      } mode=forced`,
    );
    return {
      session_id: session.sessionId,
      group_id: session.groupId,
      partner_aic: partnerAic,
      queue_deleted: queueDeleted,
    };
  }

  async dissolveGroupSession(sessionId: string): Promise<void> {
    const session = this.groupSessions.get(sessionId);
    this.groupSessions.delete(sessionId);
    if (!session) {
      return;
    }
    for (const [partnerAic, pending] of [...this._pendingInvitations.entries()]) {
      if (pending.sessionId !== sessionId) {
        continue;
      }
      if (!pending.future.done()) {
        pending.future.setException(
          new PartnerInviteError(
            `Group session ${sessionId} dissolved before invitation completed`,
          ),
        );
      }
      this._clearPendingInvitation(partnerAic);
    }

    const client = session.leaderMqClient;
    client._state = GroupLeaderState.DISSOLVING;
    await client.broadcastGroupShutdown();
    if (this._groupAclClient) {
      try {
        await this._groupAclClient.deleteGroup({
          leaderAic: this.leaderAic,
          groupId: session.groupId,
        });
      } catch (error) {
        // 清理失败只记录，继续解散（Python 只捕获 GroupAclError，其它异常会中断解散）
        logger.warn(
          `event=group_acl_delete_failed session_id=${sessionId} group_id=${session.groupId} error=${pyErrorStr(error)}`,
        );
      }
    }

    await client.cleanupGroupResources();
    if (channelNeedsClose(client._channel)) {
      await client._channel.close();
    }
    client._partners.clear();
    client._state = GroupLeaderState.DISCONNECTED;
  }

  async close(): Promise<void> {
    if (this._cleanupTask) {
      this._cleanupTask.cancel();
      try {
        await this._cleanupTask.promise;
      } catch (error) {
        if (!(error instanceof CancelledError)) throw error;
      }
      this._cleanupTask = null;
    }

    for (const [partnerAic, pending] of [...this._pendingInvitations.entries()]) {
      if (!pending.future.done()) {
        pending.future.setException(new PartnerInviteError("Leader is shutting down"));
      }
      this._clearPendingInvitation(partnerAic);
    }

    for (const sessionId of [...this.groupSessions.keys()]) {
      await this.dissolveGroupSession(sessionId);
    }

    if (channelNeedsClose(this._inboxChannel)) {
      await this._inboxChannel.close();
    }

    if (this._connection && !this._connection.isClosed) {
      await this._connection.close();
    }

    if (this._groupAclClient !== null) {
      await this._groupAclClient.aclose();
    }
  }

  // ------------------------------------------------------------------
  // 会话清理机制
  // ------------------------------------------------------------------

  /** @internal 定期清理过期会话 */
  async _cleanupLoop(signal?: AbortSignal): Promise<void> {
    logger.info("event=cleanup_task_started");
    for (;;) {
      try {
        await sleep(300_000, signal); // 每 5 分钟检查一次
        await this._cleanupExpiredSessions();
        if (this.groupSessions.size === 0) {
          logger.info("event=cleanup_task_stopped reason=no_active_sessions");
          this._cleanupTask = null;
          break;
        }
      } catch (error) {
        if (error instanceof CancelledError) {
          logger.info("event=cleanup_task_stopped");
          this._cleanupTask = null;
          break;
        }
        logger.error(`event=cleanup_task_error error=${pyErrorStr(error)}`);
        await sleep(60_000, signal);
      }
    }
  }

  /** @internal 清理超时未活动的会话 */
  async _cleanupExpiredSessions(): Promise<void> {
    const timeoutSeconds = 3600; // 1 小时超时
    const now = Date.now();
    const expiredSessions: string[] = [];

    for (const [sessionId, session] of this.groupSessions) {
      if ((now - session.lastModified.getTime()) / 1000 > timeoutSeconds) {
        expiredSessions.push(sessionId);
      }
    }

    for (const sessionId of expiredSessions) {
      logger.info(`event=session_expired_cleanup session_id=${sessionId} reason=inactive_timeout`);
      await this.dissolveGroupSession(sessionId);
    }
  }

  // ------------------------------------------------------------------
  // RabbitMQ 消息回调
  // ------------------------------------------------------------------

  /** @internal */
  async _handleGroupMessage(
    message: GroupMessage,
    receivingSession?: GroupLeaderSession,
  ): Promise<void> {
    const msgType = MESSAGE_CLASS_NAMES[message.type] ?? message.type;
    const senderId = message.senderId ?? "unknown";
    const sessionId = message.sessionId;
    const groupId = message.groupId;

    // 与 Python 不同（安全修复）：payload 里的 sessionId 未经认证，不能用来把消息路由到别的会话。
    // - 从会话自己的群组队列收到的消息（receivingSession）只交给该会话，且 groupId 必须一致；
    // - 直接调用（无 receivingSession）时按 sessionId 查找，但要求该会话的 groupId 与消息一致，
    //   否则按 groupId 反查。
    let session: GroupLeaderSession | undefined;
    if (receivingSession) {
      if (!groupId || groupId === receivingSession.groupId) {
        session = receivingSession;
      }
    } else {
      if (sessionId) {
        const candidate = this.groupSessions.get(sessionId);
        if (candidate && (!groupId || candidate.groupId === groupId)) {
          session = candidate;
        }
      }
      if (!session && groupId) {
        for (const candidate of this.groupSessions.values()) {
          if (candidate.groupId === groupId) {
            session = candidate;
            break;
          }
        }
      }
    }

    logger.debug(
      `[GroupLeader] >>> _handle_group_message: type=${msgType} sender=${senderId} session_id=${pyStr(sessionId)} group_id=${pyStr(groupId)} routed=${pyStr(session?.sessionId)}`,
    );

    if (!session) {
      logger.warn(
        `[GroupLeader] Cannot route message: type=${msgType} sender=${senderId} session_id=${pyStr(sessionId)} group_id=${pyStr(groupId)}`,
      );
      return;
    }

    session.recordMessage(message);

    if (message.type === "task-result") {
      logger.info(
        `[GroupLeader] Processing TaskResult: session_id=${session.sessionId} sender=${senderId} task_id=${message.taskId} state=${message.status ? message.status.state : "no_status"}`,
      );
      await this._handleTaskUpdate(session, message);
    } else if (message.type === "group-mgmt-command") {
      // 更新本地的 partner 状态（连接/静音等），并做额外日志处理
      session.leaderMqClient.handlePartnerStatusUpdate(message);
      this._handleMgmtCommand(message);
      logger.info(
        `event=mgmt_command_leader_received session_id=${session.sessionId} command=${message.command} sender=${message.senderId}`,
      );
    } else if (message.type === "group-mgmt-result") {
      session.leaderMqClient.handlePartnerStatusUpdate(message);
      if (message.status.connected) {
        const pending = this._pendingInvitations.get(message.senderId);
        if (pending && pending.sessionId === session.sessionId) {
          let partnerInfo = session.leaderMqClient.partners.get(message.senderId);
          if (partnerInfo === undefined) {
            partnerInfo = new PartnerConnectionInfo(
              message.senderId,
              `agent-${message.senderId}`,
              this.rabbitmqVhost,
              "unknown",
              buildGroupQueueName(this.leaderAic, session.groupId, message.senderId),
            );
          }
          if (!pending.future.done()) {
            pending.future.setResult(partnerInfo);
          }
          this._clearPendingInvitation(message.senderId);
        }
      } else if (this._groupAclClient !== null) {
        try {
          await this._groupAclClient.removeMember({
            leaderAic: this.leaderAic,
            groupId: session.groupId,
            memberAic: message.senderId,
          });
        } catch (error) {
          logger.warn(
            `event=partner_leave_acl_cleanup_failed session_id=${session.sessionId} partner_aic=${message.senderId} group_id=${session.groupId} error=${pyErrorStr(error)}`,
          );
        }
      }
      logger.debug(
        `event=mgmt_result_leader_received session_id=${session.sessionId} sender=${message.senderId} connected=${
          message.status.connected ? "True" : "False"
        } muted=${message.status.muted ? "True" : "False"}`,
      );
    }
  }

  /** @internal */
  async _handleTaskUpdate(groupSession: GroupLeaderSession, task: TaskResult): Promise<void> {
    const senderId = task.senderId;
    if (!senderId) {
      logger.warn("[GroupLeader] TaskResult has no senderId, skipping");
      return;
    }
    const taskId = task.taskId;
    const state = task.status.state;
    const productText = GroupLeader._extractTextFromProducts(task);
    const awaitingPrompt = GroupLeader._extractTextFromStatus(task);

    // 调试日志：显示提取到的内容
    const shortSender = senderId.length > 8 ? senderId.slice(-8) : senderId;
    logger.debug(
      `[GroupLeader] Task update details: partner=${shortSender} state=${state} has_products=${
        task.products !== undefined && task.products.length > 0 ? "True" : "False"
      } product_len=${productText ? Array.from(productText).length : 0} has_prompt=${
        awaitingPrompt !== null ? "True" : "False"
      } prompt_len=${awaitingPrompt ? Array.from(awaitingPrompt).length : 0}`,
    );

    // 额外调试：显示 product 内容的前 100 字符
    if (productText) {
      logger.info(
        `[GroupLeader] Product content preview for ${shortSender}: ${pySlice(productText, 100)}`,
      );
    }

    logger.info(
      `[GroupLeader] Recording task state: session=${groupSession.sessionId} task_id=${taskId} partner=${senderId} state=${state}`,
    );

    groupSession.recordTaskState(taskId, senderId, state, { productText, awaitingPrompt });

    logger.info(
      `[GroupLeader] Task state recorded successfully: session=${groupSession.sessionId} task_id=${taskId} partner=${senderId}`,
    );
  }

  /** @internal */
  _handleMgmtCommand(message: GroupMgmtCommand): void {
    const commands: readonly string[] = [
      GroupMgmtCommandType.MUTE,
      GroupMgmtCommandType.UNMUTE,
      GroupMgmtCommandType.LEAVE_GROUP,
      GroupMgmtCommandType.DISBAND,
    ];
    if (commands.includes(message.command)) {
      logger.info(`event=group_mgmt_command command=${message.command} sender=${message.senderId}`);
    }
  }

  /** @internal */
  static _extractTextFromProducts(task: TaskResult): string | null {
    if (!task.products || task.products.length === 0) {
      return null;
    }
    const parts: string[] = [];
    for (const product of task.products) {
      for (const item of product.dataItems) {
        const text = "text" in item ? item.text : undefined;
        if (text) {
          parts.push(String(text));
        }
      }
    }
    return parts.length > 0 ? parts.join("\n") : null;
  }

  /** @internal */
  static _extractTextFromStatus(task: TaskResult): string | null {
    const items = task.status.dataItems ?? [];
    const texts = items
      .map((item) => ("text" in item ? item.text : undefined))
      .filter((text): text is string => typeof text === "string");
    return texts.length > 0 ? texts.join("\n") : null;
  }
}
