/**
 * 群组模式使用的 AMQP 抽象层（Node 版独有，替代 Python 依赖的 aio-pika）
 *
 * Python 版直接使用 aio-pika 的 Connection / Channel / Exchange / Queue / IncomingMessage 对象。
 * 这里定义同形状的最小接口（groupLeader / groupPartner 只依赖这些接口，测试用普通对象替换），
 * 并基于 amqplib v2 提供默认实现，逐项对齐 aio-pika 的线上行为：
 *
 * - 所有 channel 都是 publisher-confirm channel（aio-pika `connection.channel()` 默认
 *   `publisher_confirms=True`）；broker nack 时 `publish()` 以 `DeliveryError` 拒绝。
 * - `publish()` 默认 `mandatory=true`（aio-pika `Exchange.publish` 默认值），被退回的消息只记日志不报错
 *   （aio-pika `on_return_raises=False`）。
 * - 未设置 `message_id` 时自动生成 32 位十六进制 UUID（aiormq `basic_publish` 的行为）；
 *   `priority` 默认 0、`delivery_mode` 默认 1、`headers` 默认空表（aio-pika `Message` 默认值）。
 * - `Queue.bind(exchange)` 未给 routing key 时使用队列名（aio-pika `Queue.bind`）。
 * - `IncomingMessage.process()`：回调成功则 ack；抛错则 `reject(requeue=false)` 后继续抛出。
 * - 连接 URL 沿用 aio-pika 风格（`?auth=external&name=...`），在这里翻译为 amqplib 的
 *   EXTERNAL 凭据与 `connection_name` 客户端属性；心跳 60 秒、连接超时 60 秒同 aiormq 默认。
 * - `connectRobust()` 对应 `aio_pika.connect_robust`：首次连接失败立即报错（fail_fast），
 *   断线后每 5 秒重连；重连成功后按 aio-pika RobustChannel 的方式恢复 channel、已声明的
 *   exchange / queue、绑定与消费者。channel 级关闭（如 404）且连接仍在时，同样尝试恢复一次。
 */

import { randomUUID } from "node:crypto";
import { isIP } from "node:net";

import {
  connect as amqplibConnect,
  credentials as amqplibCredentials,
  type ChannelModel,
  type ConfirmChannel,
  type ConsumeMessage,
  type Options,
  type RecoveringChannelModel,
  type SocketOptions,
} from "amqplib";

import type { TlsClientOptions } from "../internal/http.js";
import { getLogger } from "../internal/logger.js";
import { pyParseQs, pyUnquote, pyUrlparse } from "./groupRuntime.js";

const logger = getLogger("acps_sdk.aip.group_amqp");

// =============================================================================
// 常量与错误
// =============================================================================

/** 对应 `aio_pika.ExchangeType`。 */
export const ExchangeType = {
  DIRECT: "direct",
  FANOUT: "fanout",
  TOPIC: "topic",
  HEADERS: "headers",
} as const;
export type ExchangeType = (typeof ExchangeType)[keyof typeof ExchangeType];

/** 对应 `aio_pika.DeliveryMode`。 */
export const DeliveryMode = {
  NOT_PERSISTENT: 1,
  PERSISTENT: 2,
} as const;
export type DeliveryMode = (typeof DeliveryMode)[keyof typeof DeliveryMode];

/** broker 对发布的消息回复 basic.nack（对应 `aio_pika.exceptions.DeliveryError`）。 */
export class DeliveryError extends Error {
  override name = "DeliveryError";
}

/** 在已关闭的 channel / connection 上操作（对应 `aiormq.exceptions.ChannelInvalidStateError`）。 */
export class ChannelInvalidStateError extends Error {
  override name = "ChannelInvalidStateError";
}

/** 等待确认期间 channel 被关闭（对应 aiormq 的 `ChannelClosed` 系列异常）。 */
export class ChannelClosedError extends Error {
  override name = "ChannelClosedError";
}

/** 消息已经 ack / reject 过（对应 `aio_pika.exceptions.MessageProcessError`）。 */
export class MessageProcessError extends Error {
  override name = "MessageProcessError";
}

// =============================================================================
// 消息
// =============================================================================

export interface AmqpMessageOptions {
  headers?: Record<string, unknown> | null;
  contentType?: string | null;
  contentEncoding?: string | null;
  deliveryMode?: number | null;
  priority?: number | null;
  correlationId?: string | null;
  replyTo?: string | null;
  messageId?: string | null;
  type?: string | null;
  userId?: string | null;
  appId?: string | null;
}

/** 待发布的消息（对应 `aio_pika.Message`，默认值相同）。 */
export class AmqpMessage {
  readonly body: Buffer;
  readonly headers: Record<string, unknown>;
  readonly contentType: string | null;
  readonly contentEncoding: string | null;
  readonly deliveryMode: number;
  readonly priority: number;
  readonly correlationId: string | null;
  readonly replyTo: string | null;
  readonly messageId: string | null;
  readonly type: string | null;
  readonly userId: string | null;
  readonly appId: string | null;

  constructor(body: Buffer | Uint8Array | string, options: AmqpMessageOptions = {}) {
    this.body = Buffer.isBuffer(body) ? body : Buffer.from(body);
    this.headers = options.headers ?? {};
    this.contentType = options.contentType ?? null;
    this.contentEncoding = options.contentEncoding ?? null;
    this.deliveryMode = options.deliveryMode ?? DeliveryMode.NOT_PERSISTENT;
    this.priority = options.priority ?? 0;
    this.correlationId = options.correlationId ?? null;
    this.replyTo = options.replyTo ?? null;
    this.messageId = options.messageId ?? null;
    this.type = options.type ?? null;
    this.userId = options.userId ?? null;
    this.appId = options.appId ?? null;
  }
}

/** 投递给消费者的消息（对应 `aio_pika.abc.AbstractIncomingMessage` 的子集）。 */
export interface AmqpIncomingMessage {
  readonly body: Buffer;
  readonly headers: Record<string, unknown> | null | undefined;
  readonly routingKey: string | null | undefined;
  readonly redelivered: boolean | null | undefined;
  /** AMQP `user_id` 属性（RabbitMQ 已校验其等于连接认证用户名） */
  readonly userId: string | Buffer | null | undefined;
  readonly messageId?: string | null;
  /**
   * 对应 `async with message.process():`——回调正常返回则 ack；
   * 抛出异常则 `reject(requeue)`（默认 false）并继续抛出。
   *
   * 与 aio-pika 相同：`noAck` 消费者收到的消息不能 ack / reject，`process()` 会抛 `TypeError`
   * （除非传 `ignoreProcessed: true`）。
   */
  process<T>(
    callback: () => Promise<T>,
    options?: { requeue?: boolean; ignoreProcessed?: boolean },
  ): Promise<T>;
}

export type AmqpConsumeCallback = (message: AmqpIncomingMessage) => Promise<void>;

// =============================================================================
// Exchange / Queue / Channel / Connection 接口
// =============================================================================

export interface AmqpExchange {
  readonly name: string;
  publish(
    message: AmqpMessage,
    routingKey: string,
    options?: { mandatory?: boolean },
  ): Promise<void>;
}

export interface AmqpQueue {
  readonly name: string;
  /** aio-pika 的 Queue 没有该属性，Python 代码用 `getattr(queue, "is_closed", False)` 读取 */
  readonly isClosed?: boolean;
  bind(
    exchange: AmqpExchange | string,
    options?: { routingKey?: string | null; arguments?: Record<string, unknown> | null },
  ): Promise<void>;
  /** 开始消费，返回 consumer tag */
  consume(
    callback: AmqpConsumeCallback,
    options?: {
      noAck?: boolean;
      exclusive?: boolean;
      consumerTag?: string | null;
      arguments?: Record<string, unknown> | null;
    },
  ): Promise<string>;
  cancel(consumerTag: string): Promise<void>;
  /** 默认 `ifUnused=true, ifEmpty=true`（同 aio-pika `Queue.delete`） */
  delete(options?: { ifUnused?: boolean; ifEmpty?: boolean }): Promise<void>;
}

export interface DeclareExchangeOptions {
  type?: ExchangeType | string;
  durable?: boolean;
  autoDelete?: boolean;
  internal?: boolean;
  passive?: boolean;
  arguments?: Record<string, unknown> | null;
  /** false 表示重连后不恢复该 exchange（同 aio-pika `robust` 参数） */
  robust?: boolean;
}

export interface DeclareQueueOptions {
  durable?: boolean;
  exclusive?: boolean;
  passive?: boolean;
  autoDelete?: boolean;
  arguments?: Record<string, unknown> | null;
  robust?: boolean;
}

export interface AmqpChannel {
  /** 当前不可用（已关闭、断线，或 robust 连接上正在恢复拓扑） */
  readonly isClosed: boolean;
  /**
   * 是否已被显式 `close()`（可选实现）。与 `isClosed` 不同：断线 / 恢复中的 robust channel
   * 会自动恢复，清理时仍需 `close()` 才能停止恢复（否则重连后会“复活”消费者）。
   */
  readonly closeCalled?: boolean;
  /** 最近一次自动恢复（重连后）失败的原因；未失败或已恢复时为 null（可选实现） */
  readonly lastRestoreError?: Error | null;
  declareExchange(name: string, options?: DeclareExchangeOptions): Promise<AmqpExchange>;
  /** `ensure=false` 时不与 broker 交互，直接返回 exchange 句柄（同 aio-pika） */
  getExchange(name: string, options?: { ensure?: boolean }): Promise<AmqpExchange>;
  declareQueue(name: string, options?: DeclareQueueOptions): Promise<AmqpQueue>;
  queueDelete(
    queueName: string,
    options?: { ifUnused?: boolean; ifEmpty?: boolean },
  ): Promise<void>;
  exchangeDelete(exchangeName: string, options?: { ifUnused?: boolean }): Promise<void>;
  close(): Promise<void>;
}

export interface AmqpConnection {
  readonly isClosed: boolean;
  channel(): Promise<AmqpChannel>;
  close(): Promise<void>;
}

export interface AmqpConnectOptions {
  /** 对应 Python `ssl_context`：amqps 连接的 TLS 选项（mTLS 客户端证书 + CA） */
  sslContext?: TlsClientOptions | null;
}

/** 对应 `aio_pika.connect` / `aio_pika.connect_robust`。 */
export interface AmqpConnector {
  connect(url: string, options?: AmqpConnectOptions): Promise<AmqpConnection>;
  connectRobust(url: string, options?: AmqpConnectOptions): Promise<AmqpConnection>;
}

// =============================================================================
// URL 翻译
// =============================================================================

/** Python `int(str)`：允许首尾空白、正负号与数字间下划线；非法时返回 0（同 aiormq `parse_int`）。 */
function parseAiormqInt(value: string): number {
  const text = value.trim();
  if (!/^[+-]?\d+(?:_\d+)*$/.test(text)) return 0;
  return Number(text.replace(/_/g, ""));
}

/** aiormq `parse_heartbeat`：`0 <= v < 65535`，否则为 0。 */
function parseAiormqHeartbeat(value: string): number {
  const result = parseAiormqInt(value);
  return result >= 0 && result < 65535 ? result : 0;
}

/** aiormq `parse_timeout`：含 `.` 时按 float 解析，否则按 int；非法时为 0。单位：秒。 */
function parseAiormqTimeout(value: string): number {
  if (value.includes(".")) {
    const text = value.trim();
    const result = Number(text.replace(/(\d)_(?=\d)/g, "$1"));
    return text !== "" && Number.isFinite(result) ? result : 0;
  }
  return parseAiormqInt(value);
}

/** aio-pika 风格 URL 翻译为 amqplib 参数的结果。 @internal */
export interface AmqplibConnectParams {
  url: Options.Connect;
  socketOptions: SocketOptions;
}

/**
 * 把 aio-pika / aiormq 风格的连接 URL 翻译成 amqplib 的连接参数。
 *
 * - `auth=external` → SASL EXTERNAL；否则 PLAIN（用户名 / 密码缺省为 guest）
 * - `name=` → 客户端属性 `connection_name`
 * - `heartbeat=`（默认 60；非法或超出 0..65534 时为 0，同 aiormq `parse_heartbeat`）
 * - `timeout=`（秒，默认 60，可为小数；非法时为 0，同 aiormq `parse_timeout`）：
 *   aiormq 解析了该参数却未使用，这里把它用作 socket 连接超时
 * - vhost：路径去掉首个 `/` 后百分号解码，空则为 `/`
 * @internal
 */
export function parseAmqpConnectionUrl(
  url: string,
  options: AmqpConnectOptions = {},
): AmqplibConnectParams {
  const parts = pyUrlparse(url);
  if (parts.scheme !== "amqp" && parts.scheme !== "amqps") {
    throw new Error(`Expected amqp: or amqps: as the protocol; got ${parts.scheme}:`);
  }
  const tlsEnabled = parts.scheme === "amqps";
  const hostname = parts.hostname ?? "localhost";
  const port = parts.port ?? (tlsEnabled ? 5671 : 5672);
  const query = pyParseQs(parts.query);
  const first = (key: string): string | undefined => query.get(key)?.[0];

  const vhost = parts.path === "/" || !parts.path ? "/" : pyUnquote(parts.path.slice(1));
  const heartbeat = parseAiormqHeartbeat(first("heartbeat") ?? "60");
  const timeoutSeconds = parseAiormqTimeout(first("timeout") ?? "60");
  const auth = (first("auth") ?? "plain").toUpperCase();
  const username = parts.username !== null ? pyUnquote(parts.username) : "";
  const password = parts.password !== null ? pyUnquote(parts.password) : "";

  let credentials: SocketOptions["credentials"];
  if (auth === "EXTERNAL") {
    credentials = amqplibCredentials.external();
  } else if (auth === "AMQPLAIN") {
    credentials = amqplibCredentials.amqplain(username || "guest", password || "guest");
  } else {
    credentials = amqplibCredentials.plain(username || "guest", password || "guest");
  }

  const connectionName = first("name");
  const socketOptions: SocketOptions = {
    credentials,
    // 0（或非法值）表示不设置连接超时；小数秒按毫秒取整
    timeout: Math.max(0, Math.round(timeoutSeconds * 1000)),
    clientProperties: connectionName ? { connection_name: connectionName } : {},
  };
  const tls = options.sslContext;
  if (tlsEnabled && tls) {
    if (tls.cert !== undefined) socketOptions.cert = tls.cert;
    if (tls.key !== undefined) socketOptions.key = tls.key;
    if (tls.ca !== undefined) socketOptions.ca = tls.ca;
    if (tls.rejectUnauthorized !== undefined)
      socketOptions.rejectUnauthorized = tls.rejectUnauthorized;
    if (tls.servername !== undefined) socketOptions.servername = tls.servername;
  }
  if (tlsEnabled && socketOptions.servername === undefined && !isIP(hostname)) {
    socketOptions.servername = hostname;
  }

  return {
    url: {
      protocol: parts.scheme,
      hostname,
      port,
      // amqplib 会对 vhost 再做一次 querystring.unescape，这里先转义以原样传递
      vhost: encodeURIComponent(vhost),
      heartbeat,
      username: username || "guest",
      password: password || "guest",
    },
    socketOptions,
  };
}

// =============================================================================
// amqplib 实现
// =============================================================================

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** AMQP 0-9-1 硬错误（连接级）reply code；软错误（311/313/403/404/405/406）只关闭 channel。 */
const AMQP_HARD_ERROR_CODES: ReadonlySet<number> = new Set([
  320, 402, 501, 502, 503, 504, 505, 506, 530, 540, 541,
]);

/** 表示连接（socket）断开的 Node 系统错误码。 */
const SOCKET_ERROR_CODES: ReadonlySet<string> = new Set([
  "ECONNRESET",
  "ECONNREFUSED",
  "ECONNABORTED",
  "EPIPE",
  "ETIMEDOUT",
  "EHOSTUNREACH",
  "ENETUNREACH",
  "ENOTFOUND",
  "EAI_AGAIN",
]);

/**
 * 按错误码判断是否为连接级失败（而非 channel 级拓扑错误）：amqplib 把 broker 的关闭帧转成带
 * `code`（reply code）的错误，socket 错误带 Node 系统错误码。不带码的错误（例如
 * “Channel ended, no reply will be forthcoming”）不在这里判断——连接断开由 `_setup`
 * 通过连接的 `close` 事件得知，避免把 channel 级关闭误判为断线而无限重连。
 */
function isConnectionLevelError(error: unknown): boolean {
  const code = (error as { code?: unknown } | null | undefined)?.code;
  if (typeof code === "number") return AMQP_HARD_ERROR_CODES.has(code);
  if (typeof code === "string") return SOCKET_ERROR_CODES.has(code);
  return false;
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

/** amqplib `connect` 的签名（测试可注入假实现）。 @internal */
export type AmqplibConnectFn = (
  url: Options.Connect,
  socketOptions?: SocketOptions & { recovery?: unknown },
) => Promise<unknown>;

interface ExchangeDeclaration {
  name: string;
  type: string;
  options: DeclareExchangeOptions;
}

class AmqplibIncomingMessage implements AmqpIncomingMessage {
  readonly body: Buffer;
  readonly headers: Record<string, unknown>;
  readonly routingKey: string;
  readonly redelivered: boolean;
  readonly userId: string | null;
  readonly messageId: string | null;
  private processed: boolean;

  constructor(
    private readonly raw: ConsumeMessage,
    private readonly rawChannel: ConfirmChannel,
    private readonly owner: AmqplibChannel,
    private readonly noAck = false,
  ) {
    // 同 aio-pika IncomingMessage：no_ack 消息一开始就视为已处理
    this.processed = noAck;
    this.body = raw.content;
    this.headers = (raw.properties.headers as Record<string, unknown> | undefined) ?? {};
    this.routingKey = raw.fields.routingKey;
    this.redelivered = raw.fields.redelivered;
    this.userId = (raw.properties.userId as string | undefined) ?? null;
    this.messageId = (raw.properties.messageId as string | undefined) ?? null;
  }

  private get channelClosed(): boolean {
    return this.owner._rawChannel !== this.rawChannel;
  }

  async ack(): Promise<void> {
    if (this.noAck) throw new TypeError('Can\'t ack message with "no_ack" flag');
    if (this.processed) throw new MessageProcessError("Message already processed");
    if (this.channelClosed) throw new ChannelInvalidStateError("Channel closed");
    this.rawChannel.ack(this.raw);
    this.processed = true;
  }

  async reject(requeue = false): Promise<void> {
    if (this.noAck) throw new TypeError('This message has "no_ack" flag.');
    if (this.processed) throw new MessageProcessError("Message already processed");
    if (this.channelClosed) throw new ChannelInvalidStateError("Channel closed");
    this.rawChannel.reject(this.raw, requeue);
    this.processed = true;
  }

  async nack(requeue = true): Promise<void> {
    if (this.noAck) throw new TypeError('Can\'t nack message with "no_ack" flag');
    if (this.processed) throw new MessageProcessError("Message already processed");
    if (this.channelClosed) throw new ChannelInvalidStateError("Channel closed");
    this.rawChannel.nack(this.raw, false, requeue);
    this.processed = true;
  }

  /**
   * 对应 aio-pika `ProcessContext`：成功 ack；异常时 reject(requeue) 后继续抛出原异常。
   * 已处理（含 no_ack）的消息在 `ignoreProcessed=false` 时仍会尝试 ack / reject，
   * 因而像 aio-pika 一样抛出 `TypeError` / `MessageProcessError`，而不是让 broker 以 406 关闭 channel。
   */
  async process<T>(
    callback: () => Promise<T>,
    {
      requeue = false,
      ignoreProcessed = false,
    }: { requeue?: boolean; ignoreProcessed?: boolean } = {},
  ): Promise<T> {
    // 同 aio-pika `ProcessContext.__aexit__`：退出时（回调结束后）才判断是否需要结算，
    // 这样回调里手动 ack / reject 过的消息在 ignoreProcessed=true 时不会再次结算
    let result: T;
    try {
      result = await callback();
    } catch (error) {
      if (!ignoreProcessed || !this.processed) {
        if (this.channelClosed) {
          logger.warn("Reject is not sent since channel is closed");
        } else {
          try {
            await this.reject(requeue);
          } catch (settleError) {
            if (settleError instanceof Error && settleError.cause === undefined) {
              (settleError as Error & { cause?: unknown }).cause = error;
            }
            throw settleError;
          }
        }
      }
      throw error;
    }
    if (!ignoreProcessed || !this.processed) {
      await this.ack();
    }
    return result;
  }
}

interface ConsumerRecord {
  callback: AmqpConsumeCallback;
  options: { noAck: boolean; exclusive: boolean; arguments: Record<string, unknown> | null };
}

class AmqplibExchange implements AmqpExchange {
  constructor(
    private readonly channel: AmqplibChannel,
    readonly name: string,
  ) {}

  publish(
    message: AmqpMessage,
    routingKey: string,
    { mandatory = true }: { mandatory?: boolean } = {},
  ): Promise<void> {
    return this.channel._publish(this.name, routingKey, message, mandatory);
  }
}

class AmqplibQueue implements AmqpQueue {
  private readonly bindings = new Map<
    string,
    { exchange: string; routingKey: string; arguments: Record<string, unknown> | null }
  >();
  private readonly consumers = new Map<string, ConsumerRecord>();
  private deleted = false;

  constructor(
    private readonly channel: AmqplibChannel,
    readonly name: string,
    private readonly declaration: DeclareQueueOptions,
  ) {}

  /**
   * 队列句柄是否已失效：队列已被删除，或所属 channel 已永久关闭。
   * 自动恢复中（断线 / 重试）的 channel 不算关闭——恢复后同一个句柄继续可用。
   */
  get isClosed(): boolean {
    return this.deleted || this.channel._permanentlyClosed;
  }

  /** @internal */
  _markDeleted(): void {
    this.deleted = true;
  }

  async bind(
    exchange: AmqpExchange | string,
    {
      routingKey = null,
      arguments: args = null,
    }: { routingKey?: string | null; arguments?: Record<string, unknown> | null } = {},
  ): Promise<void> {
    const exchangeName = typeof exchange === "string" ? exchange : exchange.name;
    const key = routingKey ?? this.name;
    const raw = await this.channel._ready();
    await raw.bindQueue(this.name, exchangeName, key, args ?? undefined);
    if (this.declaration.robust !== false) {
      this.bindings.set(`${exchangeName}\u0000${key}`, {
        exchange: exchangeName,
        routingKey: key,
        arguments: args,
      });
    }
  }

  async consume(
    callback: AmqpConsumeCallback,
    {
      noAck = false,
      exclusive = false,
      consumerTag = null,
      arguments: args = null,
    }: {
      noAck?: boolean;
      exclusive?: boolean;
      consumerTag?: string | null;
      arguments?: Record<string, unknown> | null;
    } = {},
  ): Promise<string> {
    const raw = await this.channel._ready();
    const options = { noAck, exclusive, arguments: args };
    const tag = await this.startConsumer(raw, callback, options, consumerTag);
    if (this.declaration.robust !== false) {
      this.consumers.set(tag, { callback, options });
    }
    return tag;
  }

  private async startConsumer(
    raw: ConfirmChannel,
    callback: AmqpConsumeCallback,
    options: ConsumerRecord["options"],
    consumerTag: string | null,
  ): Promise<string> {
    let tag = consumerTag ?? "";
    const reply = await raw.consume(
      this.name,
      (delivery) => {
        if (delivery === null) {
          // broker 侧取消了消费者（例如队列被删除）
          logger.warn(
            `event=amqp_consumer_cancelled_by_server queue=${this.name} consumer_tag=${tag}`,
          );
          this.consumers.delete(tag);
          return;
        }
        const message = new AmqplibIncomingMessage(delivery, raw, this.channel, options.noAck);
        // 同 aiormq：每条投递在独立任务中处理，回调异常只记录日志
        void Promise.resolve()
          .then(() => callback(message))
          .catch((error: unknown) => {
            logger.error(
              `event=amqp_consumer_callback_error queue=${this.name} consumer_tag=${tag} error=${errorMessage(error)}`,
            );
          });
      },
      {
        noAck: options.noAck,
        exclusive: options.exclusive,
        ...(consumerTag ? { consumerTag } : {}),
        ...(options.arguments ? { arguments: options.arguments } : {}),
      },
    );
    tag = reply.consumerTag;
    return tag;
  }

  async cancel(consumerTag: string): Promise<void> {
    const raw = await this.channel._ready({ teardown: true });
    await raw.cancel(consumerTag);
    this.consumers.delete(consumerTag);
  }

  async delete({
    ifUnused = true,
    ifEmpty = true,
  }: { ifUnused?: boolean; ifEmpty?: boolean } = {}): Promise<void> {
    const raw = await this.channel._ready({ teardown: true });
    await raw.deleteQueue(this.name, { ifUnused, ifEmpty });
    this.deleted = true;
    this.channel._forgetQueue(this.name, this);
  }

  /** @internal 重连后恢复声明、绑定与消费者 */
  async _restore(raw: ConfirmChannel): Promise<void> {
    await assertQueueOn(raw, this.name, this.declaration);
    for (const binding of this.bindings.values()) {
      await raw.bindQueue(
        this.name,
        binding.exchange,
        binding.routingKey,
        binding.arguments ?? undefined,
      );
    }
    for (const [tag, record] of [...this.consumers.entries()]) {
      await this.startConsumer(raw, record.callback, record.options, tag);
    }
  }
}

async function assertQueueOn(
  raw: ConfirmChannel,
  name: string,
  options: DeclareQueueOptions,
): Promise<void> {
  if (options.passive) {
    await raw.checkQueue(name);
    return;
  }
  await raw.assertQueue(name, {
    durable: options.durable ?? false,
    exclusive: options.exclusive ?? false,
    autoDelete: options.autoDelete ?? false,
    ...(options.arguments ? { arguments: options.arguments } : {}),
  });
}

async function assertExchangeOn(
  raw: ConfirmChannel,
  declaration: ExchangeDeclaration,
): Promise<void> {
  const { name, type, options } = declaration;
  if (options.passive) {
    await raw.checkExchange(name);
    return;
  }
  await raw.assertExchange(name, type, {
    durable: options.durable ?? false,
    autoDelete: options.autoDelete ?? false,
    internal: options.internal ?? false,
    ...(options.arguments ? { arguments: options.arguments } : {}),
  });
}

interface Waiter {
  resolve: (raw: ConfirmChannel) => void;
  reject: (error: unknown) => void;
}

class AmqplibChannel implements AmqpChannel {
  /** @internal 当前存活的 amqplib channel；断开时为 null */
  _rawChannel: ConfirmChannel | null = null;
  private rawGeneration = 0;
  private closeRequested = false;
  private restoring = false;
  private lastError: Error | null = null;
  private restoreError: Error | null = null;
  private restoreFailureReported = false;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private waiters: Waiter[] = [];
  private readonly exchanges = new Map<string, ExchangeDeclaration>();
  private readonly queues = new Map<string, Set<AmqplibQueue>>();

  constructor(private readonly connection: AmqplibConnectionBase) {}

  /** 当前不可用：已关闭、已断开，或正在（重新）恢复拓扑。robust 连接上的后两种情况会自动恢复。 */
  get isClosed(): boolean {
    return this.closeRequested || this._rawChannel === null || this.restoring;
  }

  /** 是否已被显式 `close()`（之后不再自动恢复）。 */
  get closeCalled(): boolean {
    return this.closeRequested;
  }

  /** 最近一次自动恢复失败的原因；恢复成功后复位为 null。 */
  get lastRestoreError(): Error | null {
    return this.restoreError;
  }

  /** @internal 永久不可用：显式关闭、连接已关闭 / 放弃重连，或普通连接上的 channel 已断开 */
  get _permanentlyClosed(): boolean {
    return (
      this.closeRequested ||
      this.connection.isClosed ||
      (!this.connection.robust && this._rawChannel === null)
    );
  }

  /** @internal 在指定连接模型上打开底层 confirm channel */
  async _open(model: ChannelModel, generation: number): Promise<ConfirmChannel> {
    const raw = await model.createConfirmChannel();
    raw.on("error", (error: Error) => {
      this.lastError = error;
      logger.warn(`event=amqp_channel_error error=${error.message}`);
    });
    raw.on("close", () => this.onRawClose(raw));
    raw.on("return", (returned: ConsumeMessage) => {
      logger.warn(
        `event=amqp_message_returned exchange=${returned.fields.exchange} routing_key=${returned.fields.routingKey}`,
      );
    });
    this._rawChannel = raw;
    this.rawGeneration = generation;
    this.lastError = null;
    return raw;
  }

  private onRawClose(raw: ConfirmChannel): void {
    if (this._rawChannel !== raw) return;
    this._rawChannel = null;
    if (this.closeRequested) return;
    if (!this.connection.robust) {
      this.rejectWaiters(new ChannelInvalidStateError("Channel closed"));
      return;
    }
    if (this.restoring) return;
    const generation = this.rawGeneration;
    setImmediate(() => {
      const model = this.connection._liveModel(generation);
      // 连接级断开：等重连后由 _setup 统一恢复
      if (!model || this.closeRequested || this._rawChannel || this.restoring) return;
      this.restoreOrRetry(model, generation);
    });
  }

  /** 恢复；拓扑错误时进入重试，连接断开时等待下一次重连。 */
  private restoreOrRetry(model: ChannelModel, generation: number): void {
    this._restore(model, generation).catch((error: unknown) => {
      if (this.closeRequested) return;
      if (this.connection._liveModel(generation) !== model) return;
      this._restoreFailed(error, generation);
    });
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }

  /**
   * @internal 重连成功后、逐个恢复 channel 之前调用：先把 channel 标记为“恢复中”，
   * 让此期间的操作排队等待恢复结果（而不是因 `_rawChannel === null` 立即失败）。
   */
  _beginRestore(): void {
    if (this.closeRequested) return;
    this.restoring = true;
    this.clearRetry();
  }

  /**
   * @internal 重新打开并恢复拓扑（对应 RobustChannel.restore）。
   *
   * 失败时保持“恢复中”（`isClosed === true`，发布被拒绝，声明类操作继续排队）并抛出；
   * 拓扑错误由调用方转入 {@link _restoreFailed} 定时重试，连接级失败则等待下一次重连。
   */
  async _restore(model: ChannelModel, generation: number): Promise<void> {
    this.clearRetry();
    if (this.closeRequested) {
      this.restoring = false;
      return;
    }
    this.restoring = true;
    try {
      const raw = await this._open(model, generation);
      for (const declaration of this.exchanges.values()) {
        await assertExchangeOn(raw, declaration);
      }
      for (const queues of this.queues.values()) {
        for (const queue of queues) {
          await queue._restore(raw);
        }
      }
    } catch (error) {
      this.restoring = !this.closeRequested;
      throw error;
    }
    this.restoring = false;
    if (this.restoreError !== null) {
      logger.info(`event=amqp_channel_restored_after_failure`);
    }
    this.restoreError = null;
    this.restoreFailureReported = false;
    const raw = this._rawChannel;
    const waiters = this.waiters.splice(0);
    if (raw) for (const waiter of waiters) waiter.resolve(raw);
  }

  /**
   * @internal 拓扑恢复失败（channel 级，例如群组 Exchange 暂时不存在导致 404）：
   * 关闭只恢复了一半的底层 channel，保持“恢复中”，并在连接仍存活时每隔
   * `restoreRetryIntervalMs`（默认 5 秒，同 aio-pika `reconnect_interval`）重试，
   * 直到成功、连接断开（交给重连）或 `close()`。首次失败时通知应用。
   */
  _restoreFailed(error: unknown, generation: number): void {
    if (this.closeRequested) return;
    this.restoreError = toError(error);
    const raw = this._rawChannel;
    if (raw !== null) {
      this._rawChannel = null;
      raw.close().catch(() => undefined);
    }
    this.restoring = true;
    if (!this.restoreFailureReported) {
      this.restoreFailureReported = true;
      this.connection._reportRestoreFailure(this, error);
    } else {
      logger.warn(`event=amqp_channel_restore_retry_failed error=${errorMessage(error)}`);
    }
    this.clearRetry();
    const timer = setTimeout(() => {
      this.retryTimer = null;
      if (this.closeRequested) return;
      const model = this.connection._liveModel(generation);
      if (!model) return; // 连接已断开：下一次 _setup 会恢复
      this.restoreOrRetry(model, generation);
    }, this.connection._restoreRetryMs);
    timer.unref?.();
    this.retryTimer = timer;
  }

  private rejectWaiters(error: unknown): void {
    const waiters = this.waiters.splice(0);
    for (const waiter of waiters) waiter.reject(error);
  }

  /**
   * @internal 等待 channel 可用（对应 RobustChannel.ready）。
   *
   * 声明类操作在断线 / 恢复期间排队等待；`teardown`（取消消费、删除队列 / Exchange）在连接仍在、
   * 但拓扑恢复正在失败重试时立即报错，避免 leave / dissolve 在无法恢复的 channel 上永久等待。
   */
  async _ready({ teardown = false }: { teardown?: boolean } = {}): Promise<ConfirmChannel> {
    if (this.closeRequested) throw new ChannelInvalidStateError("Channel closed");
    if (this._rawChannel && !this.restoring) return this._rawChannel;
    if (teardown && this.restoreError !== null && this.connection._isUp()) {
      throw new ChannelInvalidStateError(`Channel restore failed: ${this.restoreError.message}`, {
        cause: this.restoreError,
      });
    }
    if (
      this.connection.robust &&
      !this.connection.isClosed &&
      (this.restoring || !this.connection._isUp())
    ) {
      return new Promise<ConfirmChannel>((resolve, reject) =>
        this.waiters.push({ resolve, reject }),
      );
    }
    throw new ChannelInvalidStateError(
      this.lastError ? `Channel closed: ${this.lastError.message}` : "Channel closed",
    );
  }

  /** @internal */
  _forgetQueue(name: string, queue: AmqplibQueue): void {
    const set = this.queues.get(name);
    if (!set) return;
    set.delete(queue);
    if (set.size === 0) this.queues.delete(name);
  }

  /** @internal 由连接在关闭 / 放弃重连时调用 */
  _abandon(error: unknown): void {
    this.clearRetry();
    this.rejectWaiters(error);
  }

  /** @internal */
  _publish(
    exchange: string,
    routingKey: string,
    message: AmqpMessage,
    mandatory: boolean,
  ): Promise<void> {
    const raw = this._rawChannel;
    if (this.closeRequested || raw === null || this.restoring) {
      // 恢复中的 channel 只恢复了部分拓扑：不允许发布（同 aio-pika，发布前不等待恢复，直接报错）
      return Promise.reject(
        new ChannelInvalidStateError(
          this.restoring && !this.closeRequested ? "Channel is being restored" : "Channel closed",
        ),
      );
    }
    const options: Options.Publish = {
      mandatory,
      deliveryMode: message.deliveryMode,
      priority: message.priority,
      headers: message.headers,
      // aiormq 在缺省时生成 UUID4 的十六进制形式
      messageId: message.messageId ?? randomUUID().replace(/-/g, ""),
    };
    if (message.contentType !== null) options.contentType = message.contentType;
    if (message.contentEncoding !== null) options.contentEncoding = message.contentEncoding;
    if (message.correlationId !== null) options.correlationId = message.correlationId;
    if (message.replyTo !== null) options.replyTo = message.replyTo;
    if (message.type !== null) options.type = message.type;
    if (message.userId !== null) options.userId = message.userId;
    if (message.appId !== null) options.appId = message.appId;
    return new Promise<void>((resolve, reject) => {
      try {
        raw.publish(exchange, routingKey, message.body, options, (error: unknown) => {
          if (!error) {
            resolve();
          } else if (errorMessage(error) === "message nacked") {
            reject(
              new DeliveryError(
                `Message nacked by broker: exchange=${exchange} routing_key=${routingKey}`,
              ),
            );
          } else {
            reject(
              new ChannelClosedError(
                this.lastError ? this.lastError.message : errorMessage(error),
                {
                  cause: error,
                },
              ),
            );
          }
        });
      } catch (error) {
        reject(new ChannelInvalidStateError(errorMessage(error), { cause: error }));
      }
    });
  }

  async declareExchange(name: string, options: DeclareExchangeOptions = {}): Promise<AmqpExchange> {
    const raw = await this._ready();
    if (options.passive && this.exchanges.has(name)) {
      return new AmqplibExchange(this, name);
    }
    const declaration: ExchangeDeclaration = {
      name,
      type: options.type ?? ExchangeType.DIRECT,
      options,
    };
    await assertExchangeOn(raw, declaration);
    if (!options.internal && options.robust !== false) {
      this.exchanges.set(name, declaration);
    }
    return new AmqplibExchange(this, name);
  }

  async getExchange(
    name: string,
    { ensure = true }: { ensure?: boolean } = {},
  ): Promise<AmqpExchange> {
    if (ensure) {
      return this.declareExchange(name, { passive: true });
    }
    return new AmqplibExchange(this, name);
  }

  async declareQueue(name: string, options: DeclareQueueOptions = {}): Promise<AmqpQueue> {
    const raw = await this._ready();
    if (options.passive && name) {
      const existing = this.queues.get(name);
      const first = existing ? [...existing][0] : undefined;
      if (first) return first;
    }
    await assertQueueOn(raw, name, options);
    const queue = new AmqplibQueue(this, name, options);
    if (options.robust !== false) {
      const set = this.queues.get(name) ?? new Set<AmqplibQueue>();
      set.add(queue);
      this.queues.set(name, set);
    }
    return queue;
  }

  async queueDelete(
    queueName: string,
    { ifUnused = false, ifEmpty = false }: { ifUnused?: boolean; ifEmpty?: boolean } = {},
  ): Promise<void> {
    const raw = await this._ready({ teardown: true });
    await raw.deleteQueue(queueName, { ifUnused, ifEmpty });
    for (const queue of this.queues.get(queueName) ?? []) queue._markDeleted();
    this.queues.delete(queueName);
  }

  async exchangeDelete(
    exchangeName: string,
    { ifUnused = false }: { ifUnused?: boolean } = {},
  ): Promise<void> {
    const raw = await this._ready({ teardown: true });
    await raw.deleteExchange(exchangeName, { ifUnused });
    this.exchanges.delete(exchangeName);
  }

  async close(): Promise<void> {
    if (this.closeRequested) return;
    this.closeRequested = true;
    this.restoring = false;
    this.clearRetry();
    this.connection._forgetChannel(this);
    this.rejectWaiters(new ChannelInvalidStateError("Channel closed"));
    const raw = this._rawChannel;
    if (!raw) {
      logger.warn("Transport is not ready");
      return;
    }
    try {
      await raw.close();
    } catch (error) {
      logger.debug(`event=amqp_channel_close_ignored error=${errorMessage(error)}`);
    }
    this._rawChannel = null;
  }
}

/** 与 aio-pika `reconnect_interval` 默认值一致（秒）：重连间隔与 channel 拓扑恢复重试间隔。 */
const RECONNECT_INTERVAL_SECONDS = 5;

abstract class AmqplibConnectionBase implements AmqpConnection {
  protected readonly channels = new Set<AmqplibChannel>();
  abstract readonly robust: boolean;

  constructor(protected readonly hooks: AmqplibConnectorOptions = {}) {}

  /** @internal channel 拓扑恢复失败后的重试间隔（毫秒） */
  get _restoreRetryMs(): number {
    return this.hooks.restoreRetryIntervalMs ?? RECONNECT_INTERVAL_SECONDS * 1000;
  }

  /** @internal channel 自动恢复失败：记录错误日志并通知应用（{@link AmqplibConnectorOptions.onChannelRestoreFailed}） */
  _reportRestoreFailure(channel: AmqpChannel, error: unknown): void {
    logger.error(`event=amqp_channel_restore_failed error=${errorMessage(error)}`);
    const callback = this.hooks.onChannelRestoreFailed;
    if (!callback) return;
    try {
      callback(error instanceof Error ? error : new Error(String(error)), channel);
    } catch (callbackError) {
      logger.warn(
        `event=amqp_channel_restore_callback_failed error=${errorMessage(callbackError)}`,
      );
    }
  }
  abstract get isClosed(): boolean;
  /** @internal 该代连接仍存活时返回其模型 */
  abstract _liveModel(generation: number): ChannelModel | null;
  /** @internal */
  abstract _isUp(): boolean;
  protected abstract waitForModel(): Promise<{ model: ChannelModel; generation: number }>;
  abstract close(): Promise<void>;

  async channel(): Promise<AmqpChannel> {
    if (this.isClosed) {
      throw new ChannelInvalidStateError("Connection closed");
    }
    const { model, generation } = await this.waitForModel();
    const channel = new AmqplibChannel(this);
    await channel._open(model, generation);
    this.channels.add(channel);
    return channel;
  }

  /** @internal */
  _forgetChannel(channel: AmqplibChannel): void {
    this.channels.delete(channel);
  }
}

/** 对应 `aio_pika.connect` 返回的普通连接：断开后不重连。 */
class AmqplibConnection extends AmqplibConnectionBase {
  readonly robust = false;
  private closed = false;

  constructor(
    private readonly model: ChannelModel,
    hooks: AmqplibConnectorOptions = {},
  ) {
    super(hooks);
    model.on("error", (error: Error) =>
      logger.warn(`event=amqp_connection_error error=${error.message}`),
    );
    model.on("close", () => {
      this.closed = true;
      for (const channel of this.channels)
        channel._abandon(new ChannelInvalidStateError("Connection closed"));
    });
  }

  get isClosed(): boolean {
    return this.closed;
  }

  _liveModel(): ChannelModel | null {
    return this.closed ? null : this.model;
  }

  _isUp(): boolean {
    return !this.closed;
  }

  protected async waitForModel(): Promise<{ model: ChannelModel; generation: number }> {
    if (this.closed) throw new ChannelInvalidStateError("Connection closed");
    return { model: this.model, generation: 1 };
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    try {
      await this.model.close();
    } catch (error) {
      logger.debug(`event=amqp_connection_close_ignored error=${errorMessage(error)}`);
    }
  }
}

/** 对应 `aio_pika.connect_robust` 返回的 RobustConnection。 */
class RobustAmqplibConnection extends AmqplibConnectionBase {
  readonly robust = true;
  /** 与 aio-pika `reconnect_interval` 默认值一致（秒） */
  static readonly RECONNECT_INTERVAL_SECONDS = RECONNECT_INTERVAL_SECONDS;
  private closeCalled = false;
  private abandoned = false;
  private up = false;
  private model: ChannelModel | null = null;
  private generation = 0;
  private connectedOnce = false;
  private modelWaiters: Array<{
    resolve: (value: { model: ChannelModel; generation: number }) => void;
    reject: (error: unknown) => void;
  }> = [];
  private recovering: RecoveringChannelModel | null = null;

  /**
   * @internal amqplib recovery 的 setup 钩子：每次（重）连成功后调用，负责恢复所有 channel。
   *
   * amqplib 只在 setup 成功返回之后才给新连接挂 `close` 监听（recovery.js `_connect`：
   * runSetup → _bindModel），所以恢复期间连接再次断开时必须由这里发现：一旦这条连接在 setup
   * 期间关闭，或恢复失败属于连接级错误，就复位状态并抛出，让 amqplib 关闭该连接并安排下一次重连；
   * 只有 channel 级的拓扑错误（如 404 / 406）才在记录后吞掉，不拖垮整条连接。
   */
  async _setup(model: ChannelModel): Promise<void> {
    let modelClosed: Error | null = null;
    const onClose = (error?: Error): void => {
      modelClosed = error ?? new Error("Connection closed");
      if (this.model === model) {
        this.up = false;
        this.model = null;
      }
    };
    model.on("close", onClose);
    model.on("error", (error: Error) =>
      logger.warn(`event=amqp_connection_error error=${error.message}`),
    );
    const connectionLost = (error: unknown): boolean =>
      modelClosed !== null || isConnectionLevelError(error);

    this.generation += 1;
    const generation = this.generation;
    this.model = model;
    if (this.connectedOnce) {
      for (const channel of this.channels) channel._beginRestore();
    }
    this.up = true;
    try {
      if (this.connectedOnce) {
        logger.info(`event=amqp_connection_restored channels=${this.channels.size}`);
        for (const channel of [...this.channels]) {
          if (channel.closeCalled) continue;
          try {
            await channel._restore(model, generation);
          } catch (error) {
            if (connectionLost(error)) throw error;
            // 恢复期间被调用方关闭的 channel：不再报告、不再重试
            if (channel.closeCalled) continue;
            // 拓扑错误：只让这个 channel 定时重试（aio-pika 会关掉整条连接重连）
            channel._restoreFailed(error, generation);
          }
        }
      }
      if (modelClosed !== null) throw modelClosed;
    } catch (error) {
      // 连接在恢复期间断开：不要停留在一条已死的连接上；排队的操作继续等待下一次重连
      if (this.model === model) {
        this.up = false;
        this.model = null;
      }
      for (const channel of this.channels) channel._beginRestore();
      logger.warn(
        `event=amqp_connection_lost_during_restore error=${errorMessage(error)}. Reconnecting after ${RobustAmqplibConnection.RECONNECT_INTERVAL_SECONDS} seconds.`,
      );
      throw error;
    }
    this.connectedOnce = true;
    const waiters = this.modelWaiters.splice(0);
    for (const waiter of waiters) waiter.resolve({ model, generation });
  }

  /** @internal */
  _attach(recovering: RecoveringChannelModel): void {
    this.recovering = recovering;
    recovering.on("disconnect", (error: Error) => {
      this.up = false;
      this.model = null;
      logger.info(
        `Connection closed: ${error.message}. Reconnecting after ${RobustAmqplibConnection.RECONNECT_INTERVAL_SECONDS} seconds.`,
      );
    });
    recovering.on("reconnect-failed", (error: Error) => {
      this.abandoned = true;
      this.up = false;
      this.model = null;
      const waiters = this.modelWaiters.splice(0);
      for (const waiter of waiters) waiter.reject(error);
      for (const channel of this.channels) channel._abandon(error);
    });
    recovering.on("connect-failed", (error: Error) => {
      if (this.connectedOnce) {
        logger.warn(
          `Connection attempt failed: ${error.message}. Reconnecting after ${RobustAmqplibConnection.RECONNECT_INTERVAL_SECONDS} seconds.`,
        );
      }
    });
    recovering.on("error", (error: Error) =>
      logger.warn(`event=amqp_connection_error error=${error.message}`),
    );
  }

  get isClosed(): boolean {
    return this.closeCalled || this.abandoned;
  }

  _liveModel(generation: number): ChannelModel | null {
    return this.up && this.generation === generation ? this.model : null;
  }

  _isUp(): boolean {
    return this.up;
  }

  protected waitForModel(): Promise<{ model: ChannelModel; generation: number }> {
    if (this.isClosed) return Promise.reject(new ChannelInvalidStateError("Connection closed"));
    if (this.up && this.model)
      return Promise.resolve({ model: this.model, generation: this.generation });
    return new Promise((resolve, reject) => this.modelWaiters.push({ resolve, reject }));
  }

  async close(): Promise<void> {
    if (this.closeCalled) return;
    this.closeCalled = true;
    const error = new ChannelInvalidStateError("Connection closed");
    const waiters = this.modelWaiters.splice(0);
    for (const waiter of waiters) waiter.reject(error);
    for (const channel of this.channels) channel._abandon(error);
    try {
      await this.recovering?.close();
    } catch (closeError) {
      logger.debug(`event=amqp_connection_close_ignored error=${errorMessage(closeError)}`);
    }
    this.up = false;
    this.model = null;
  }
}

/** {@link createAmqplibConnector} 的选项。 */
export interface AmqplibConnectorOptions {
  /**
   * 自动重连后某个 channel 的拓扑恢复失败（例如群组 Exchange 暂时不存在导致 404）时调用，
   * 每轮连续失败只调用一次（恢复成功后复位）。期间该 channel `isClosed === true`、
   * `lastRestoreError` 记录原因、发布被拒绝，并每隔 `restoreRetryIntervalMs` 自动重试，
   * 直到成功、连接断开（交给重连）或被 `close()`。
   */
  onChannelRestoreFailed?: (error: Error, channel: AmqpChannel) => void;
  /** 拓扑恢复失败后的重试间隔（毫秒），默认 5000（同 aio-pika `reconnect_interval`）。 */
  restoreRetryIntervalMs?: number;
}

/**
 * 基于 amqplib 的连接器。`connectFn` 仅供测试注入（默认 `amqplib.connect`）。
 */
export function createAmqplibConnector(
  connectFn: AmqplibConnectFn = amqplibConnect as unknown as AmqplibConnectFn,
  hooks: AmqplibConnectorOptions = {},
): AmqpConnector {
  return {
    async connect(url: string, options: AmqpConnectOptions = {}): Promise<AmqpConnection> {
      const params = parseAmqpConnectionUrl(url, options);
      const model = (await connectFn(params.url, params.socketOptions)) as ChannelModel;
      return new AmqplibConnection(model, hooks);
    },
    async connectRobust(url: string, options: AmqpConnectOptions = {}): Promise<AmqpConnection> {
      const params = parseAmqpConnectionUrl(url, options);
      const connection = new RobustAmqplibConnection(hooks);
      const intervalMs = RobustAmqplibConnection.RECONNECT_INTERVAL_SECONDS * 1000;
      const recovering = (await connectFn(params.url, {
        ...params.socketOptions,
        recovery: {
          initialDelay: intervalMs,
          maxDelay: intervalMs,
          factor: 1,
          jitter: 0,
          // fail_fast：首次连接失败直接报错；连上之后无限重连
          initialMaxRetries: 0,
          maxRetries: Infinity,
          // 先拿到句柄挂好监听器，再等待首次连接
          waitForConnect: false,
          setup: (model: ChannelModel) => connection._setup(model),
        },
      })) as RecoveringChannelModel;
      connection._attach(recovering);
      try {
        await recovering.waitForConnect();
      } catch (error) {
        await connection.close();
        throw error;
      }
      return connection;
    },
  };
}

/** 默认连接器（amqplib）。 */
export const defaultAmqpConnector: AmqpConnector = createAmqplibConnector();

/**
 * 清理时是否仍需调用 `channel.close()`：只要调用方还没关闭过就要关（断线 / 恢复中的 robust channel
 * 也一样，否则重连后会恢复已不需要的队列与消费者）。不提供 `closeCalled` 的实现回退到 `!isClosed`。
 * @internal
 */
export function channelNeedsClose(channel: AmqpChannel | null | undefined): channel is AmqpChannel {
  if (!channel) return false;
  return channel.closeCalled !== undefined ? !channel.closeCalled : !channel.isClosed;
}
