/**
 * 群组模式 Partner 客户端模块（对应 Python `acps_sdk.aip.aip_group_partner`）
 *
 * 本模块实现了 AIP 协议中群组模式（Group Mode）下 Partner 角色的核心功能：
 * 1. 接收 Leader 发送的群组邀请（RabbitMQRequest 直连 RPC，或 inbox.topic 邀请）
 * 2. 连接到 RabbitMQ 消息队列服务器
 * 3. 创建自己的队列并绑定到群组 Exchange
 * 4. 接收和处理群组内的消息（任务消息、管理消息）
 * 5. 发送任务状态更新和产出物到群组
 * 6. 响应群组管理命令（状态查询、退出、静音等）
 */

import { AsyncLocalStorage } from "node:async_hooks";
import { randomUUID } from "node:crypto";

import { buildReceiveBody, buildSendBody, buildSettlementBody } from "../amp/messageTap.js";
import type { MessageBody } from "../amp/models.js";
import { pyRepr, pyStr, pythonJsonDumps } from "../internal/pyCompat.js";
import {
  TRACEPARENT_HEADER,
  type TraceContext,
  formatTraceparent,
  newSpanId,
  newTraceId,
  parseTraceparent,
} from "../amp/traceContext.js";
import type { TlsClientOptions } from "../internal/http.js";
import { getLogger } from "../internal/logger.js";
import { utcNowIso } from "../internal/time.js";
import { normalizeAic } from "./identity.js";
import {
  type DataItem,
  type Product,
  type TaskCommand,
  type TaskResult,
  TaskCommandSchema,
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
  DeliveryMode,
  ExchangeType,
  channelNeedsClose,
  defaultAmqpConnector,
} from "./groupAmqp.js";
import {
  assertIncomingGroupMessageIdentity,
  buildOutgoingAmqpUserId,
  extractAmqpUserId,
} from "./groupIdentity.js";
import type { MessageEmitterLike, TraceContextProvider } from "./groupLeader.js";
import {
  type AMQPConfig,
  type GroupInfo,
  type GroupInvitationError,
  type GroupInvitationErrorInput,
  type GroupMgmtResult,
  type InboxGroupInvitation,
  type RabbitMQRequest,
  type RabbitMQResponse,
  type RabbitMQResponseResult,
  type RabbitMQServerConfig,
  GroupInvitationErrorSchema,
  GroupMgmtCommand,
  GroupMgmtCommandSchema,
  GroupMgmtCommandType,
  GroupMgmtResultSchema,
  InboxGroupInvitationErrorSchema,
  InboxGroupInvitationSchema,
  RabbitMQResponseResultSchema,
  RabbitMQResponseSchema,
  dumpGroupMessage,
} from "./groupModel.js";
import {
  DEFAULT_INVITATION_TIMEOUT_SECONDS,
  INBOX_EXCHANGE_NAME,
  INBOX_MESSAGE_TTL_MS,
  INBOX_QUEUE_EXPIRES_MS,
  JSONDecodeError,
  buildExternalConnectionUrl,
  buildGroupQueueName,
  buildInboxQueueName,
  buildPlainConnectionUrl,
  calculateInvitationExpiry,
  decodeJsonBody,
  invitationIsExpired,
  pyErrorStr,
  pySlice,
  asOptionalString,
  dictGet,
  headerValue,
  requireJsonDict,
} from "./groupRuntime.js";

const logger = getLogger("acps_sdk.aip.group_partner");

const DATA_PLANE_TYPES: ReadonlySet<unknown> = new Set(["task-command", "task-result"]);

/**
 * 当前正在处理的 task-command 的 trace 上下文（对应 Python ContextVar `_active_cmd_trace`）。
 *
 * 命令处理回调内调用 `_publishMessage` 时，send 事件继承该 trace（O-M6 链内继承）。
 * @internal
 */
export const activeCmdTrace = new AsyncLocalStorage<TraceContext | null>();

/** Partner 群组连接状态 */
export const PartnerGroupState = {
  /** 未连接 */
  DISCONNECTED: "disconnected",
  /** 已加入 */
  JOINED: "joined",
  /** 正在退出 */
  LEAVING: "leaving",
} as const;
export type PartnerGroupState = (typeof PartnerGroupState)[keyof typeof PartnerGroupState];

// 消息处理回调类型
/** (command, isMentioned) => Promise<void> */
export type TaskCommandHandler = (command: TaskCommand, isMentioned: boolean) => Promise<void>;
export type TaskResultHandler = (result: TaskResult) => Promise<void>;
export type MgmtCommandHandler = (command: GroupMgmtCommand) => Promise<void>;
export type InvitationHandler = (invitation: InboxGroupInvitation) => Promise<boolean>;
export type DisconnectHandler = (
  client: GroupPartnerMqClient,
  groupId: string | null,
) => Promise<void> | void;

type EmitOptions = NonNullable<Parameters<MessageEmitterLike["emit"]>[1]>;

export interface GroupPartnerMqClientOptions {
  /** 默认 localhost（加入群组时由邀请中的 server 配置覆盖） */
  rabbitmqHost?: string;
  /** 默认 5672 */
  rabbitmqPort?: number;
  /** 默认 "/" */
  rabbitmqVhost?: string;
  /** 默认 "guest"；null 表示 AMQPS + EXTERNAL */
  rabbitmqUser?: string | null;
  /** 默认 "guest" */
  rabbitmqPassword?: string | null;
  /** 对应 Python `ssl_context`：EXTERNAL 认证用的 mTLS 证书 */
  sslContext?: TlsClientOptions | null;
  connection?: AmqpConnection | null;
  /** 默认 true */
  connectionOwner?: boolean;
  /** 是否使用自动重连连接（`connect_robust`），默认 true */
  robustConnection?: boolean;
  messageEmitter?: MessageEmitterLike | null;
  /** 默认 "rabbitmq" */
  messageSystem?: string;
  traceContextProvider?: TraceContextProvider | null;
  /** 默认 true */
  identityBindingEnabled?: boolean;
  /** AMQP 连接器（默认 amqplib，测试可替换；对应 monkeypatch `aio_pika.connect(_robust)`） */
  connector?: AmqpConnector;
}

/**
 * 群组模式 Partner 客户端
 *
 * 负责：
 * - 接收群组邀请并连接到 RabbitMQ
 * - 创建自己的队列并绑定到群组 Exchange
 * - 接收和处理群组消息
 * - 发送任务状态更新和产出物
 * - 响应群组管理命令
 */
export class GroupPartnerMqClient {
  readonly partnerAic: string;
  rabbitmqHost: string;
  rabbitmqPort: number;
  rabbitmqVhost: string;
  rabbitmqUser: string | null;
  rabbitmqPassword: string | null;
  sslContext: TlsClientOptions | null;

  /** @internal */ _connectionOwner: boolean;
  /** @internal */ _useRobustConnection: boolean;
  /** @internal */ _messageEmitter: MessageEmitterLike | null;
  /** @internal */ _messageSystem: string;
  /** @internal */ _traceContextProvider: TraceContextProvider | null;
  /** @internal */ _identityBindingEnabled: boolean;
  /** @internal */ _connector: AmqpConnector;

  // 连接和通道
  /** @internal */ _connection: AmqpConnection | null;
  /** @internal */ _channel: AmqpChannel | null = null;
  /** @internal */ _inboxChannel: AmqpChannel | null = null;
  /** @internal */ _inboxQueue: AmqpQueue | null = null;
  /**
   * @internal 延迟退出标记：收到 LEAVE_GROUP 命令时不能立即断开（需等消息 ACK 完成），
   * 因此先标记 _pendingLeave，在 onMessage 回调结束后再执行实际退出。
   */
  _pendingLeave = false;
  /** @internal 暂存退出命令中的 sessionId，供延迟执行的 _handleLeaveRequest 回显使用 */
  _pendingLeaveSessionId: string | null = null;

  // 群组信息
  /** @internal */ _groupId: string | null = null;
  /** @internal */ _groupInfo: GroupInfo | null = null;
  /** @internal */ _exchangeName: string | null = null;
  /** @internal */ _exchange: AmqpExchange | null = null;
  /** @internal */ _queue: AmqpQueue | null = null;
  /** @internal */ _queueName: string | null = null;

  // 连接信息（用于返回给 Leader）
  /** @internal */ _connectionName: string | null = null;
  /** @internal */ _vhost: string | null = null;
  /** @internal */ _nodeName: string | null = null;

  // 状态
  /** @internal */ _state: PartnerGroupState = PartnerGroupState.DISCONNECTED;
  /** @internal 静音状态。注意：当前实现中，暂时没有强制限制，依靠 Partner 业务逻辑自觉遵守。 */
  _muted = false;

  // 消息处理器
  /** @internal */ _commandHandler: TaskCommandHandler | null = null;
  /** @internal */ _taskResultHandler: TaskResultHandler | null = null;
  /** @internal */ _mgmtCommandHandler: MgmtCommandHandler | null = null;
  /** @internal */ _consumerTag: string | null = null;
  /** @internal */ _invitationHandler: InvitationHandler | null = null;
  /** @internal */ _disconnectHandler: DisconnectHandler | null = null;

  /** @internal 任务跟踪：taskId -> TaskState */
  _taskStates = new Map<string, TaskState>();

  constructor(partnerAic: string, options: GroupPartnerMqClientOptions = {}) {
    this.partnerAic = partnerAic;
    this.rabbitmqHost = options.rabbitmqHost ?? "localhost";
    this.rabbitmqPort = options.rabbitmqPort ?? 5672;
    this.rabbitmqVhost = options.rabbitmqVhost ?? "/";
    this.rabbitmqUser =
      (options.rabbitmqUser === undefined ? "guest" : options.rabbitmqUser) || null;
    this.rabbitmqPassword =
      (options.rabbitmqPassword === undefined ? "guest" : options.rabbitmqPassword) || null;
    this.sslContext = options.sslContext ?? null;
    this._connectionOwner = options.connectionOwner ?? true;
    this._useRobustConnection = options.robustConnection ?? true;
    this._messageEmitter = options.messageEmitter ?? null;
    this._messageSystem = options.messageSystem ?? "rabbitmq";
    this._traceContextProvider = options.traceContextProvider ?? null;
    this._identityBindingEnabled = options.identityBindingEnabled ?? true;
    this._connector = options.connector ?? defaultAmqpConnector;
    this._connection = options.connection ?? null;
    if (!this._identityBindingEnabled) {
      logger.warn(
        `AIP identity binding disabled for GroupPartnerMqClient partner_aic=${this.partnerAic}`,
      );
    }
  }

  get groupId(): string | null {
    return this._groupId;
  }

  get state(): PartnerGroupState {
    return this._state;
  }

  /**
   * 是否在群组中且可收发。与 Python 不同，还要求群组 channel 当前可用：断线重连或拓扑恢复
   * 失败重试期间（例如群组 Exchange 暂时不存在）为 false，恢复后自动变回 true。
   */
  get isJoined(): boolean {
    return (
      this._state === PartnerGroupState.JOINED &&
      this._connection !== null &&
      !this._connection.isClosed &&
      (this._channel === null || !this._channel.isClosed)
    );
  }

  /** 群组 channel 最近一次自动恢复失败的原因（Node 版新增）；恢复成功后为 null。 */
  get groupChannelError(): Error | null {
    return this._channel?.lastRestoreError ?? null;
  }

  /** @internal inbox channel 仍在使用（离开群组时不能关闭它所依赖的连接） */
  get _inboxActive(): boolean {
    return channelNeedsClose(this._inboxChannel);
  }

  get isMuted(): boolean {
    return this._muted;
  }

  get queueName(): string | null {
    return this._queueName;
  }

  get connection(): AmqpConnection | null {
    return this._connection;
  }

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

    const connectionName = `agent-${this.partnerAic}`;
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

    const connectOptions = sslContext ? { sslContext } : {};
    this._connection = this._useRobustConnection
      ? await this._connector.connectRobust(connectionUrl, connectOptions)
      : await this._connector.connect(connectionUrl, connectOptions);
    this._channel = await this._connection.channel();
  }

  async ensureInboxQueue(): Promise<AmqpQueue> {
    await this.connect();
    if (this._inboxQueue && !(this._inboxQueue.isClosed ?? false)) {
      logger.debug(
        `event=partner_inbox_queue_reuse partner_aic=${this.partnerAic} queue=${this._inboxQueue.name}`,
      );
      return this._inboxQueue;
    }

    const inboxName = buildInboxQueueName(this.partnerAic);
    const connection = this._connection;
    if (connection === null) {
      throw new Error("RabbitMQ connection is not ready");
    }

    if (channelNeedsClose(this._inboxChannel)) {
      // 旧的 inbox channel 已失效：关闭它，避免重连后恢复出第二个消费者
      await this._inboxChannel.close().catch(() => undefined);
    }
    this._inboxChannel = null;
    this._inboxQueue = null;
    const inboxChannel = await connection.channel();
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
      // 半初始化的 inbox 不保留：下次调用会重新建立（Python 版会把半成品当作可用）
      await inboxChannel.close().catch(() => undefined);
      throw error;
    }
    this._inboxChannel = inboxChannel;
    this._inboxQueue = inboxQueue;
    logger.info(
      `event=partner_inbox_queue_ready partner_aic=${this.partnerAic} queue=${inboxName} expires_ms=${INBOX_QUEUE_EXPIRES_MS} message_ttl_ms=${INBOX_MESSAGE_TTL_MS}`,
    );
    return inboxQueue;
  }

  async startInboxConsuming(
    handler: (invitation: InboxGroupInvitation) => Promise<void>,
  ): Promise<void> {
    const inboxQueue = await this.ensureInboxQueue();
    logger.info(
      `event=partner_inbox_consumer_start partner_aic=${this.partnerAic} queue=${pyStr(inboxQueue.name)}`,
    );

    const onMessage = async (message: AmqpIncomingMessage): Promise<void> => {
      await message.process(async () => {
        let decoded: unknown;
        try {
          decoded = decodeJsonBody(message.body);
        } catch (error) {
          if (error instanceof JSONDecodeError) {
            logger.warn(`event=partner_inbox_invalid_json partner_aic=${this.partnerAic}`);
            return;
          }
          throw error;
        }
        const body = requireJsonDict(decoded);

        if (body.type !== "group-invitation") {
          logger.debug(
            `event=partner_inbox_ignored partner_aic=${this.partnerAic} type=${pyStr(body.type)}`,
          );
          return;
        }

        let invitation: InboxGroupInvitation;
        try {
          invitation = InboxGroupInvitationSchema.parse(body);
          if (this._identityBindingEnabled) {
            assertIncomingGroupMessageIdentity(invitation, {
              amqpUserId: extractAmqpUserId(message),
              expectedGroupId: invitation.groupId,
            });
          }
        } catch (error) {
          logger.warn(
            `event=partner_inbox_invalid_message partner_aic=${this.partnerAic} error=${pyErrorStr(error)}`,
          );
          return;
        }
        logger.info(
          `event=partner_inbox_invitation_received partner_aic=${this.partnerAic} group_id=${invitation.group.groupId} leader_aic=${invitation.group.leader.aic}`,
        );
        await handler(invitation);
      });
    };

    await inboxQueue.consume(onMessage);
  }

  setInvitationHandler(handler: InvitationHandler): void {
    this._invitationHandler = handler;
    logger.debug(`event=partner_invitation_handler_set partner_aic=${this.partnerAic}`);
  }

  setDisconnectHandler(handler: DisconnectHandler): void {
    this._disconnectHandler = handler;
  }

  /**
   * 设置任务命令处理回调（必须）。Leader 发出的命令消息需要在此回调函数处理。
   *
   * @param handler 回调函数，接收 (command, isMentioned)：
   *   - command: TaskCommand 对象
   *   - isMentioned: 是否被提及（即是否需要作为任务处理）
   */
  setCommandHandler(handler: TaskCommandHandler): void {
    this._commandHandler = handler;
  }

  /** 设置任务结果处理回调（可选）。其它 Partner 发出的任务状态更新通过此回调处理（通常不需要处理，避免混乱自己的状态机）。 */
  setTaskResultHandler(handler: TaskResultHandler): void {
    this._taskResultHandler = handler;
  }

  /** 设置管理命令处理回调（可选）。用于处理群组管理消息的额外操作。通用操作如静音、状态查询等已经内置处理。 */
  setMgmtCommandHandler(handler: MgmtCommandHandler): void {
    this._mgmtCommandHandler = handler;
  }

  /**
   * 处理群组加入邀请（直连 RPC）
   *
   * 流程：接收邀请 → 建立连接 → 获取 Exchange → 创建队列 → 绑定 Exchange → 开始监听
   * → 返回连接信息 → 通知加入（发送 GroupMgmtResult）
   *
   * @returns RabbitMQResponse，包含连接信息或错误
   */
  async joinGroup(request: RabbitMQRequest): Promise<RabbitMQResponse> {
    logger.info(
      `event=partner_join_invite_received partner_aic=${this.partnerAic} group_id=${request.params.group.groupId} exchange=${request.params.amqp.exchange} host=${request.params.server.host}`,
    );

    const params = request.params;
    const server = params.server;
    const amqp = params.amqp;
    const group = params.group;

    // 与 Python 不同：已在群组中（或正在退出）时拒绝新邀请，而不是再建一套队列 / 消费者并泄漏旧的
    if (this._state !== PartnerGroupState.DISCONNECTED) {
      logger.warn(
        `event=partner_invitation_rejected partner_aic=${this.partnerAic} group_id=${group.groupId} route=rpc reason=already_in_group current_group_id=${pyStr(this._groupId)}`,
      );
      return RabbitMQResponseSchema.parse({
        jsonrpc: "2.0",
        id: request.id ?? null,
        error: {
          code: -32020,
          message: "Invitation rejected by partner policy",
          data: {
            errorType: "INVITATION_REJECTED",
            details: `Partner is already in group ${pyStr(this._groupId)}; leave it first`,
          },
        },
      });
    }

    try {
      const syntheticInvitation = this._buildDirectRpcInvitation(request);
      if (this._invitationHandler && !(await this._invitationHandler(syntheticInvitation))) {
        logger.warn(
          `event=partner_invitation_rejected partner_aic=${this.partnerAic} group_id=${group.groupId} route=rpc`,
        );
        return RabbitMQResponseSchema.parse({
          jsonrpc: "2.0",
          id: request.id ?? null,
          error: {
            code: -32020,
            message: "Invitation rejected by partner policy",
            data: {
              errorType: "INVITATION_REJECTED",
              details: "Partner invitation handler rejected direct RPC invite",
            },
          },
        });
      }

      this._prepareConnectionFromServer(server);
      const result = await this._joinGroupCommon({ group, amqp });
      logger.info(
        `event=partner_invitation_accepted partner_aic=${this.partnerAic} group_id=${group.groupId} route=rpc`,
      );
      return RabbitMQResponseSchema.parse({ jsonrpc: "2.0", id: request.id ?? null, result });
    } catch (error) {
      logger.error(
        `event=join_group_failed partner_aic=${this.partnerAic} error=${pyErrorStr(error)}`,
      );
      try {
        await this._cleanupGroupResources();
      } catch {
        // contextlib.suppress(Exception)
      }
      this._state = PartnerGroupState.DISCONNECTED;

      return RabbitMQResponseSchema.parse({
        jsonrpc: "2.0",
        id: request.id ?? null,
        error: {
          code: -32021,
          message: "Connection failed",
          data: {
            errorType: "CONNECTION_FAILED",
            details: {
              host: server.host,
              port: server.port,
              reason: pyErrorStr(error),
            },
          },
        },
      });
    }
  }

  async joinGroupFromInvitation(invitation: InboxGroupInvitation): Promise<boolean> {
    if (invitationIsExpired(invitation.expiresAt)) {
      logger.warn(
        `event=group_invitation_expired partner_aic=${this.partnerAic} group_id=${invitation.group.groupId} expires_at=${invitation.expiresAt}`,
      );
      return false;
    }

    if (this._state !== PartnerGroupState.DISCONNECTED) {
      // 与 Python 不同：已在群组中时拒绝（见 joinGroup）
      logger.warn(
        `event=partner_invitation_rejected partner_aic=${this.partnerAic} group_id=${invitation.group.groupId} route=inbox reason=already_in_group current_group_id=${pyStr(this._groupId)}`,
      );
      await this.sendInvitationError(invitation, {
        code: -32020,
        message: "Invitation rejected by partner policy",
        data: {
          errorType: "INVITATION_REJECTED",
          details: `Partner is already in group ${pyStr(this._groupId)}; leave it first`,
        },
      });
      return false;
    }

    try {
      logger.info(
        `event=partner_invitation_processing partner_aic=${this.partnerAic} group_id=${invitation.group.groupId} route=inbox`,
      );
      if (this._invitationHandler && !(await this._invitationHandler(invitation))) {
        logger.warn(
          `event=partner_invitation_rejected partner_aic=${this.partnerAic} group_id=${invitation.group.groupId} route=inbox`,
        );
        await this.sendInvitationError(invitation, {
          code: -32020,
          message: "Invitation rejected by partner policy",
          data: {
            errorType: "INVITATION_REJECTED",
            details: "Partner invitation handler rejected inbox invite",
          },
        });
        return false;
      }

      await this._joinGroupCommon({ group: invitation.group, amqp: invitation.amqp });
      logger.info(
        `event=partner_invitation_accepted partner_aic=${this.partnerAic} group_id=${invitation.group.groupId} route=inbox`,
      );
      return true;
    } catch (error) {
      logger.error(
        `event=join_group_from_inbox_failed partner_aic=${this.partnerAic} group_id=${invitation.group.groupId} error=${pyErrorStr(error)}`,
      );
      try {
        await this._cleanupGroupResources();
      } catch {
        // contextlib.suppress(Exception)
      }
      await this.sendInvitationError(invitation, {
        code: -32021,
        message: "Connection failed",
        data: {
          errorType: "CONNECTION_FAILED",
          details: pyErrorStr(error),
        },
      });
      return false;
    }
  }

  async sendInvitationError(
    invitation: InboxGroupInvitation,
    error: GroupInvitationError | GroupInvitationErrorInput,
  ): Promise<void> {
    await this.connect();
    if (!this._connection) {
      throw new Error("Connection is not available");
    }

    let channel = this._inboxChannel;
    let temporaryChannel: AmqpChannel | null = null;
    if (channel === null || channel.isClosed) {
      temporaryChannel = await this._connection.channel();
      channel = temporaryChannel;
    }
    try {
      await this._publishInvitationError(channel, invitation, error);
    } finally {
      // 临时 channel 用完即关（Python 版会泄漏）
      if (temporaryChannel !== null) await temporaryChannel.close().catch(() => undefined);
    }
  }

  /** @internal */
  async _publishInvitationError(
    channel: AmqpChannel,
    invitation: InboxGroupInvitation,
    error: GroupInvitationError | GroupInvitationErrorInput,
  ): Promise<void> {
    const exchange = await channel.declareExchange(INBOX_EXCHANGE_NAME, {
      type: ExchangeType.TOPIC,
      durable: true,
      autoDelete: false,
    });
    const leaderInbox = buildInboxQueueName(invitation.group.leader.aic);
    const payload = InboxGroupInvitationErrorSchema.parse({
      id: `invite-err-${randomUUID()}`,
      sentAt: utcNowIso(),
      senderRole: "partner",
      senderId: this.partnerAic,
      groupId: invitation.group.groupId,
      invitationToken: invitation.invitationToken,
      error: GroupInvitationErrorSchema.parse(error),
    });
    await exchange.publish(
      new AmqpMessage(Buffer.from(pythonJsonDumps(payload), "utf-8"), {
        contentType: "application/json",
        deliveryMode: DeliveryMode.PERSISTENT,
        userId: buildOutgoingAmqpUserId(payload, {
          localAic: this.partnerAic,
          identityBindingEnabled: this._identityBindingEnabled,
        }),
      }),
      leaderInbox,
    );
  }

  /** @internal */
  _buildDirectRpcInvitation(request: RabbitMQRequest): InboxGroupInvitation {
    return InboxGroupInvitationSchema.parse({
      id: `invite-${randomUUID()}`,
      sentAt: utcNowIso(),
      senderRole: "leader",
      senderId: request.params.group.leader.aic,
      groupId: request.params.group.groupId,
      protocol: request.params.protocol,
      expiresAt: calculateInvitationExpiry(DEFAULT_INVITATION_TIMEOUT_SECONDS),
      invitationToken: `rpc-${randomUUID()}`,
      group: request.params.group,
      amqp: request.params.amqp,
    });
  }

  /**
   * @internal 根据邀请中的 server 配置准备连接参数（accessToken 四态）：
   * 缺省 / null → EXTERNAL（需要 sslContext）；`"user:password"` → PLAIN；`""` 或无冒号 → 报错。
   */
  _prepareConnectionFromServer(server: RabbitMQServerConfig): void {
    const accessToken = server.accessToken;

    this.rabbitmqHost = server.host;
    this.rabbitmqPort = server.port;
    this.rabbitmqVhost = server.vhost;
    this._vhost = server.vhost;

    if (accessToken === "") {
      throw new Error("Malformed accessToken: empty string");
    }

    if (accessToken) {
      if (!accessToken.includes(":")) {
        throw new Error("Malformed accessToken: expected user:password");
      }
      if (this._connection && !this._connectionOwner) {
        throw new Error("Cannot use shared EXTERNAL connection with accessToken auth");
      }
      const separator = accessToken.indexOf(":");
      this.rabbitmqUser = accessToken.slice(0, separator);
      this.rabbitmqPassword = accessToken.slice(separator + 1);
      return;
    }

    this.rabbitmqUser = null;
    this.rabbitmqPassword = null;
    if (this.sslContext === null) {
      throw new Error("ssl_context is required when accessToken is missing or null");
    }
  }

  /** @internal */
  async _joinGroupCommon({
    group,
    amqp,
  }: {
    group: GroupInfo;
    amqp: AMQPConfig;
  }): Promise<RabbitMQResponseResult> {
    this._groupId = group.groupId;
    this._groupInfo = group;
    this._exchangeName = amqp.exchange;
    this._connectionName = `agent-${this.partnerAic}`;
    this._nodeName = "unknown";
    this._vhost = this.rabbitmqVhost;

    await this.connect();
    const channel = this._channel;
    if (channel === null) {
      throw new Error("RabbitMQ channel is not ready");
    }

    let exchange: AmqpExchange;
    try {
      exchange = await channel.getExchange(this._exchangeName, { ensure: false });
    } catch {
      exchange = await channel.declareExchange(this._exchangeName, {
        type: ExchangeType.FANOUT,
        durable: true,
        autoDelete: true,
        passive: true,
      });
    }
    this._exchange = exchange;

    const leaderAic = group.leader ? group.leader.aic : "unknown";
    const queueName = buildGroupQueueName(leaderAic, group.groupId, this.partnerAic);
    this._queueName = queueName;
    const queue = await channel.declareQueue(queueName, {
      durable: true,
      exclusive: false,
      autoDelete: true,
    });
    this._queue = queue;
    await queue.bind(exchange);

    logger.info(
      `event=queue_created partner_aic=${this.partnerAic} queue=${queueName} exchange=${this._exchangeName}`,
    );

    await this._startConsuming();
    this._state = PartnerGroupState.JOINED;
    await this._notifyJoined();

    return RabbitMQResponseResultSchema.parse({
      connectionName: this._connectionName,
      vhost: this._vhost || this.rabbitmqVhost,
      nodeName: this._nodeName,
      queueName,
      processId: `pid-${process.pid}`,
    });
  }

  /** @internal 开始消费消息 */
  async _startConsuming(): Promise<void> {
    if (!this._queue) {
      throw new Error("Queue not created yet");
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
          logger.info(
            `event=queue_message_received partner_aic=${this.partnerAic} msg_type=${pyStr(body.type)} sender=${pyStr(body.senderId)}`,
          );
          emitOk = Boolean(
            this._messageEmitter &&
            body.type === "task-command" &&
            body.senderId !== this.partnerAic,
          );
          let commandTrace: TraceContext | null = null;
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
                queueName: this._queueName || "",
                messageId: dictGet(body, "id", "") as string,
                payloadSizeBytes: message.body.length,
                deliveryAttempt,
                attributes: {
                  groupId: this._groupId,
                  msgType: "task-command",
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
            commandTrace = { traceId, spanId: recvSpan, sampled: true };
          }
          // Python 在此处设置 ContextVar，并在 ack / nack 事件之后复位；
          // AsyncLocalStorage.run 把作用域限定在命令处理期间，语义相同。
          const handledBody = body;
          if (commandTrace) {
            await activeCmdTrace.run(commandTrace, () => this._handleIncomingMessage(handledBody));
          } else {
            await this._handleIncomingMessage(handledBody);
          }
        } catch (error) {
          if (emitOk && traceId && recvSpan) {
            await this._safeEmit(
              buildSettlementBody("nack", {
                system: this._messageSystem,
                exchange: this._exchangeName || "",
                virtualHost: this.rabbitmqVhost,
                queueName: this._queueName || "",
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
            `event=message_processing_error partner_aic=${this.partnerAic} error=${pyErrorStr(error)}`,
          );
          throw error;
        }
        if (emitOk && traceId && recvSpan) {
          await this._safeEmit(
            buildSettlementBody("ack", {
              system: this._messageSystem,
              exchange: this._exchangeName || "",
              virtualHost: this.rabbitmqVhost,
              queueName: this._queueName || "",
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
      if (this._pendingLeave) {
        this._pendingLeave = false;
        await this._handleLeaveRequest();
      }
    };

    this._consumerTag = await this._queue.consume(onMessage);
    logger.info(`event=consuming_started partner_aic=${this.partnerAic} group_id=${this._groupId}`);
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

  /** @internal 处理接收到的消息 */
  async _handleIncomingMessage(body: Record<string, unknown>): Promise<void> {
    const msgType = body.type;
    const senderId = body.senderId;

    // 忽略自己发送的消息
    if (senderId === this.partnerAic) {
      return;
    }

    // 与 Python 不同（安全修复，AIP §4.4 / §7.2.2）：任务命令与群组管理命令只能由 Leader 发出。
    // 启用身份绑定时 senderId 已与 broker 认证身份核对过，非 Leader 发来的这类命令直接丢弃，
    // 不交给业务回调，也不执行内置的退出 / 静音 / 状态响应。
    if (
      (msgType === "task-command" || msgType === "group-mgmt-command") &&
      this._identityBindingEnabled &&
      !this._isFromGroupLeader(senderId)
    ) {
      logger.warn(
        `event=non_leader_command_ignored partner_aic=${this.partnerAic} group_id=${this._groupId} type=${pyStr(msgType)} sender=${pyStr(senderId)} leader=${pyStr(this._groupInfo?.leader.aic)}`,
      );
      return;
    }

    try {
      if (msgType === "group-mgmt-command") {
        const mgmtCmd = GroupMgmtCommandSchema.parse(body);
        logger.info(
          `event=mgmt_command_received partner_aic=${this.partnerAic} command=${mgmtCmd.command} sender=${mgmtCmd.senderId}`,
        );
        await this._handleMgmtCommand(mgmtCmd);
      } else if (msgType === "task-result") {
        const taskResult = TaskResultSchema.parse(body);
        if (this._taskResultHandler) {
          await this._taskResultHandler(taskResult);
        }
      } else if (msgType === "task-command") {
        const command = TaskCommandSchema.parse(body);
        logger.info(
          `event=task_command_received partner_aic=${this.partnerAic} command=${command.command} sender=${command.senderId} task_id=${pyStr(command.taskId)}`,
        );
        await this._handleTaskCommand(command);
      } else if (msgType === "group-mgmt-result") {
        // 状态广播会发给所有成员，Partner 本地无需再处理。
        return;
      } else {
        logger.warn(
          `event=unknown_message_type partner_aic=${this.partnerAic} type=${pyStr(msgType)}`,
        );
      }
    } catch (error) {
      logger.error(
        `event=message_parse_error partner_aic=${this.partnerAic} error=${pyErrorStr(error)} body=${pySlice(pyRepr(body), 200)}`,
      );
      throw error;
    }
  }

  /** @internal 发送者是否为当前群组的 Leader */
  _isFromGroupLeader(senderId: unknown): boolean {
    const leaderAic = normalizeAic(this._groupInfo?.leader.aic);
    return (
      leaderAic !== null && typeof senderId === "string" && normalizeAic(senderId) === leaderAic
    );
  }

  /** @internal 处理群组管理命令 */
  async _handleMgmtCommand(command: GroupMgmtCommand): Promise<void> {
    // 检查是否需要响应
    const isTargeted = GroupMgmtCommand.mustRespond(command, this.partnerAic);
    // 回显收到的命令中的 sessionId，而非自行推导
    const echoSessionId = command.sessionId ?? null;

    if (command.command === GroupMgmtCommandType.GET_STATUS) {
      if (isTargeted) {
        await this._sendStatusResponse({ sessionId: echoSessionId });
      }
    } else if (
      command.command === GroupMgmtCommandType.LEAVE_GROUP ||
      command.command === GroupMgmtCommandType.DISBAND
    ) {
      if (isTargeted) {
        this._pendingLeave = true; // 延后执行，避免在 ack 前关闭 channel
        this._pendingLeaveSessionId = echoSessionId;
      }
    } else if (command.command === GroupMgmtCommandType.MUTE) {
      if (isTargeted) {
        this._muted = true;
        await this._sendStatusResponse({ sessionId: echoSessionId });
        logger.info(`event=partner_muted partner_aic=${this.partnerAic} group_id=${this._groupId}`);
      }
    } else if (command.command === GroupMgmtCommandType.UNMUTE) {
      if (isTargeted) {
        this._muted = false;
        await this._sendStatusResponse({ sessionId: echoSessionId });
        logger.info(
          `event=partner_unmuted partner_aic=${this.partnerAic} group_id=${this._groupId}`,
        );
      }
    }

    // 调用自定义处理器
    if (this._mgmtCommandHandler) {
      await this._mgmtCommandHandler(command);
    }
  }

  /** @internal 处理任务命令 */
  async _handleTaskCommand(command: TaskCommand): Promise<void> {
    // 检查是否被提及
    const mentions = command.mentions;
    let isMentioned = false;

    if (
      mentions === undefined ||
      mentions === null ||
      (Array.isArray(mentions) && mentions.length === 0)
    ) {
      // 未指定 mentions，所有 Partner 都可以响应
      isMentioned = true;
    } else if (mentions === "all") {
      isMentioned = true;
    } else if (Array.isArray(mentions) && mentions.includes(this.partnerAic)) {
      isMentioned = true;
    }

    // 统一调用命令处理器
    if (this._commandHandler) {
      await this._commandHandler(command, isMentioned);
    }
  }

  /** @internal 通知群组自己已加入（加入通知时尚未有会话上下文，不传 sessionId） */
  async _notifyJoined(): Promise<void> {
    await this.sendStatusResult(true, false);
    logger.info(
      `event=partner_join_notified partner_aic=${this.partnerAic} group_id=${this._groupId} queue=${this._queueName}`,
    );
  }

  /** @internal 发送状态响应 */
  async _sendStatusResponse({
    sessionId = null,
  }: { sessionId?: string | null } = {}): Promise<void> {
    await this.sendStatusResult(this._state === PartnerGroupState.JOINED, this._muted, {
      sessionId,
    });
    logger.info(
      `event=partner_status_response partner_aic=${this.partnerAic} connected=${
        this._state === PartnerGroupState.JOINED ? "True" : "False"
      } muted=${this._muted ? "True" : "False"}`,
    );
  }

  /** @internal 处理退出请求 */
  async _handleLeaveRequest(): Promise<void> {
    this._state = PartnerGroupState.LEAVING;

    // 发送断开状态消息，回显之前收到的 sessionId
    const leaveSessionId = this._pendingLeaveSessionId;
    try {
      await this.sendStatusResult(false, this._muted, { sessionId: leaveSessionId });
    } catch (error) {
      logger.warn(
        `event=partner_leave_status_failed partner_aic=${this.partnerAic} group_id=${this._groupId} error=${pyErrorStr(error)}`,
      );
    } finally {
      this._pendingLeaveSessionId = null;
    }

    // 清理资源
    await this._cleanupGroupResources();
  }

  /**
   * 发送状态结果消息
   *
   * @param options.sessionId 会话 ID（应回显收到的命令中的 sessionId，而非自行推导）
   */
  async sendStatusResult(
    connected: boolean,
    muted: boolean,
    { sessionId = null }: { sessionId?: string | null } = {},
  ): Promise<void> {
    if (!this._exchange) {
      return;
    }
    if (this._groupId === null) {
      throw new Error("Group not joined yet");
    }

    const result = GroupMgmtResultSchema.parse({
      id: `mgmt-result-${randomUUID()}`,
      sentAt: utcNowIso(),
      senderRole: "partner",
      senderId: this.partnerAic,
      dataItems: [],
      status: { connected, muted },
      groupId: this._groupId,
      sessionId,
    });

    await this._publishMessage(result);
  }

  /**
   * @internal 发布消息到群组
   *
   * 注意：此方法目前未检查 _muted 状态。即使 Partner 被静音，仍然可以调用此方法发送消息。
   * 请业务层自觉遵守静音规则。
   */
  async _publishMessage(
    message: TaskCommand | TaskResult | GroupMgmtCommand | GroupMgmtResult,
  ): Promise<void> {
    if (!this._exchange) {
      throw new Error("Not connected to group");
    }

    const body = dumpGroupMessage(message);
    const jsonBytes = Buffer.from(pythonJsonDumps(body), "utf-8");
    const headers: Record<string, string> = {};
    let traceId: string | null = null;
    let spanId: string | null = null;
    let parentSpan = "";

    const emitOk = Boolean(this._messageEmitter && DATA_PLANE_TYPES.has(body.type));
    if (emitOk) {
      const parent =
        activeCmdTrace.getStore() ||
        (this._traceContextProvider ? this._traceContextProvider() : null);
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
        localAic: this.partnerAic,
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
    logger.debug(
      `event=message_published partner_aic=${this.partnerAic} type=${pyStr(body.type)} id=${pyStr(body.id)}`,
    );
  }

  /**
   * 发送任务状态更新
   *
   * @param options.products 产出物列表（AwaitingCompletion 时使用）
   * @param options.statusDataItems 状态附带的数据项（如错误信息、提示等）
   */
  async sendTaskResult(
    taskId: string,
    sessionId: string,
    state: TaskState,
    {
      products = null,
      statusDataItems = null,
    }: { products?: Product[] | null; statusDataItems?: DataItem[] | null } = {},
  ): Promise<void> {
    this._taskStates.set(taskId, state);

    const result = TaskResultSchema.parse({
      id: `result-${randomUUID()}`,
      sentAt: utcNowIso(),
      senderRole: "partner",
      senderId: this.partnerAic,
      taskId,
      status: {
        state,
        stateChangedAt: utcNowIso(),
        dataItems: statusDataItems,
      },
      products,
      groupId: this._groupId,
      sessionId,
    });

    await this._publishMessage(result);
    logger.info(
      `event=task_result_sent partner_aic=${this.partnerAic} task_id=${taskId} state=${state}`,
    );
  }

  /** 接受任务 */
  async acceptTask(taskId: string, sessionId: string): Promise<void> {
    await this.sendTaskResult(taskId, sessionId, TaskState.Accepted);
  }

  /** 开始工作 */
  async startWorking(taskId: string, sessionId: string): Promise<void> {
    await this.sendTaskResult(taskId, sessionId, TaskState.Working);
  }

  /** 请求更多输入 */
  async requestInput(taskId: string, sessionId: string, prompt: string): Promise<void> {
    await this.sendTaskResult(taskId, sessionId, TaskState.AwaitingInput, {
      statusDataItems: [{ type: "text", text: prompt }],
    });
  }

  /** 提交产出物等待完成确认 */
  async submitForCompletion(taskId: string, sessionId: string, products: Product[]): Promise<void> {
    await this.sendTaskResult(taskId, sessionId, TaskState.AwaitingCompletion, { products });
  }

  /** 完成任务 */
  async completeTask(taskId: string, sessionId: string): Promise<void> {
    await this.sendTaskResult(taskId, sessionId, TaskState.Completed);
  }

  /** 拒绝任务 */
  async rejectTask(taskId: string, sessionId: string, reason: string): Promise<void> {
    await this.sendTaskResult(taskId, sessionId, TaskState.Rejected, {
      statusDataItems: [{ type: "text", text: reason }],
    });
  }

  /** 任务失败 */
  async failTask(taskId: string, sessionId: string, error: string): Promise<void> {
    await this.sendTaskResult(taskId, sessionId, TaskState.Failed, {
      statusDataItems: [{ type: "text", text: error }],
    });
  }

  /** 取消任务 */
  async cancelTask(taskId: string, sessionId: string): Promise<void> {
    await this.sendTaskResult(taskId, sessionId, TaskState.Canceled);
  }

  /** @internal */
  _resetGroupState({ keepConnection = false }: { keepConnection?: boolean } = {}): void {
    this._state = PartnerGroupState.DISCONNECTED;
    if (!keepConnection) {
      this._connection = null;
    }
    this._channel = null;
    this._exchange = null;
    this._queue = null;
    this._queueName = null;
    this._consumerTag = null;
    this._groupId = null;
    this._groupInfo = null;
    this._exchangeName = null;
    this._connectionName = null;
    this._vhost = null;
    this._nodeName = null;
    this._pendingLeave = false;
    this._pendingLeaveSessionId = null;
    this._muted = false;
    this._taskStates.clear();
  }

  /** @internal 清理群组本地资源。 */
  async _cleanupGroupResources(): Promise<void> {
    if (
      this._state === PartnerGroupState.DISCONNECTED &&
      this._channel === null &&
      this._queue === null &&
      (this._connection === null || this._inboxActive)
    ) {
      return;
    }

    const leftGroupId = this._groupId;
    const disconnectHandler = this._disconnectHandler;
    this._state = PartnerGroupState.LEAVING;

    // 停止消费
    if (this._queue && this._consumerTag) {
      try {
        await this._queue.cancel(this._consumerTag);
      } catch (error) {
        logger.warn(
          `event=consumer_cancel_failed partner_aic=${this.partnerAic} group_id=${this._groupId} error=${pyErrorStr(error)}`,
        );
      } finally {
        this._consumerTag = null;
      }
    }
    logger.info(`event=consumer_stopped partner_aic=${this.partnerAic} group_id=${this._groupId}`);

    // 删除队列（允许强制删除，避免 consumer 占用导致 PRECONDITION_FAILED）
    try {
      if (this._queue) {
        logger.info(
          `event=queue_delete_attempt partner_aic=${this.partnerAic} queue=${this._queue.name}`,
        );
        await this._queue.delete({ ifUnused: false, ifEmpty: false });
      }
    } catch (error) {
      logger.warn(
        `event=queue_delete_failed partner_aic=${this.partnerAic} error=${pyErrorStr(error)}`,
      );
    }

    // 关闭群组 channel：只要还没关过就关（断线 / 恢复中的 channel 也要关，否则重连后会“复活”消费者）
    if (channelNeedsClose(this._channel)) {
      await this._channel.close();
    }
    // 与 Python 不同：inbox 仍在使用这条连接时不关闭它（否则离开群组后 inbox 失效，
    // 正在处理的邀请消息也无法 ack）
    const keepConnection = this._inboxActive;
    if (
      this._connectionOwner &&
      this._connection &&
      !this._connection.isClosed &&
      !keepConnection
    ) {
      await this._connection.close();
    }

    logger.info(`event=connection_closed partner_aic=${this.partnerAic} group_id=${this._groupId}`);

    this._resetGroupState({ keepConnection });
    logger.info(`event=left_group partner_aic=${this.partnerAic} group_id=${leftGroupId}`);
    if (disconnectHandler && leftGroupId) {
      await disconnectHandler(this, leftGroupId);
    }
  }

  /**
   * 主动退出群组。
   *
   * 主动退出时先广播 LEAVE_GROUP 命令，再发送 connected=false 状态，最后执行本地资源清理。
   */
  async leaveGroup({ sessionId = null }: { sessionId?: string | null } = {}): Promise<void> {
    if (this._state === PartnerGroupState.DISCONNECTED) {
      return;
    }

    this._state = PartnerGroupState.LEAVING;

    if (this._groupId && this._exchange) {
      const leaveCommand = GroupMgmtCommandSchema.parse({
        id: `mgmt-cmd-${randomUUID()}`,
        sentAt: utcNowIso(),
        senderRole: "partner",
        senderId: this.partnerAic,
        dataItems: [],
        command: GroupMgmtCommandType.LEAVE_GROUP,
        mentions: [this.partnerAic],
        groupId: this._groupId,
        sessionId,
      });
      try {
        await this._publishMessage(leaveCommand);
      } catch (error) {
        logger.warn(
          `event=partner_leave_command_failed partner_aic=${this.partnerAic} group_id=${this._groupId} error=${pyErrorStr(error)}`,
        );
      }

      try {
        await this.sendStatusResult(false, this._muted, { sessionId });
      } catch (error) {
        logger.warn(
          `event=partner_leave_status_failed partner_aic=${this.partnerAic} group_id=${this._groupId} error=${pyErrorStr(error)}`,
        );
      }
    }

    await this._cleanupGroupResources();
  }

  /** 关闭客户端 */
  async close(): Promise<void> {
    if (this._state !== PartnerGroupState.DISCONNECTED) {
      await this.leaveGroup();
    }
    // connect() 在未加入群组时也可能打开了群组 channel（例如只用 inbox 时）：一并关闭
    if (channelNeedsClose(this._channel)) {
      await this._channel.close();
    }
    if (channelNeedsClose(this._inboxChannel)) {
      await this._inboxChannel.close();
    }
    if (this._connectionOwner && this._connection && !this._connection.isClosed) {
      await this._connection.close();
    }
  }
}

/** Partner 本地任务记录（键名同 Python）。 */
export interface PartnerTaskRecord {
  state?: TaskState;
  updated_at?: string;
  products?: Product[];
}

/**
 * Partner 群组会话
 *
 * 封装单个 Partner 在群组中的会话状态和任务管理
 */
export class PartnerGroupSession {
  readonly partnerAic: string;
  readonly client: GroupPartnerMqClient;
  readonly createdAt: string;
  /** 任务状态跟踪：taskId -> { state, products, ... } */
  tasks: Record<string, PartnerTaskRecord> = Object.create(null) as Record<
    string,
    PartnerTaskRecord
  >;
  /** 消息历史 */
  messageHistory: Array<TaskCommand | TaskResult | GroupMgmtCommand> = [];

  constructor(partnerAic: string, client: GroupPartnerMqClient) {
    this.partnerAic = partnerAic;
    this.client = client;
    this.createdAt = utcNowIso();
  }

  /** 记录消息 */
  recordMessage(message: TaskCommand | TaskResult | GroupMgmtCommand): void {
    this.messageHistory.push(message);
  }

  /** 更新任务状态 */
  updateTask(
    taskId: string,
    state: TaskState,
    { products = null }: { products?: Product[] | null } = {},
  ): void {
    let task = this.tasks[taskId];
    if (task === undefined) {
      task = {};
      this.tasks[taskId] = task;
    }
    task.state = state;
    task.updated_at = utcNowIso();
    if (products && products.length > 0) {
      task.products = products;
    }
  }

  /** 获取任务状态 */
  getTaskState(taskId: string): TaskState | null {
    return this.tasks[taskId]?.state ?? null;
  }
}

/**
 * 辅助函数：从任务命令中提取文本内容
 *
 * 将命令中所有 TextDataItem 的文本拼接起来
 */
export function extractTextFromCommand(command: TaskCommand): string {
  const texts: string[] = [];
  if (command.dataItems) {
    for (const item of command.dataItems) {
      const text = "text" in item ? item.text : undefined;
      if (text) {
        texts.push(String(text));
      }
    }
  }
  return texts.join("\n");
}
