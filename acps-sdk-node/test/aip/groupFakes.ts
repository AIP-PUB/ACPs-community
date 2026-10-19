/**
 * 群组模式测试共用的内存假对象（对应 Python 测试里用 MagicMock / AsyncMock 伪造的 aio-pika 对象，
 * 以及 scripts/fixtures/gen_aip_group.py 中记录调用参数的 Fake* 类）。
 */

import type {
  AmqpChannel,
  AmqpConnectOptions,
  AmqpConnection,
  AmqpConnector,
  AmqpConsumeCallback,
  AmqpExchange,
  AmqpIncomingMessage,
  AmqpMessage,
  AmqpQueue,
  DeclareExchangeOptions,
  DeclareQueueOptions,
} from "../../src/aip/groupAmqp.js";
import type { MessageEmitterLike } from "../../src/aip/groupLeader.js";
import type { MessageBody } from "../../src/amp/models.js";

export const LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4";
export const PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5";
export const OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF548.0JU6";

export type Op = Record<string, unknown> & { op: string };

export class Recorder {
  ops: Op[] = [];

  add(op: string, fields: Record<string, unknown> = {}): void {
    this.ops.push({ op, ...fields });
  }
}

/** 与 Python 生成脚本 `_message_record` 相同的记录格式。 */
export function messageRecord(
  message: AmqpMessage,
  exchange: string,
  routingKey: string,
): Record<string, unknown> {
  return {
    exchange,
    routing_key: routingKey,
    body: message.body.toString("utf-8"),
    body_size: message.body.length,
    content_type: message.contentType,
    delivery_mode: message.deliveryMode,
    headers: { ...message.headers },
    user_id: message.userId,
    priority: message.priority,
    message_id: message.messageId,
  };
}

export class FakeExchange implements AmqpExchange {
  published: Array<{ message: AmqpMessage; routingKey: string }> = [];
  publishError: Error | null = null;

  constructor(
    readonly recorder: Recorder,
    readonly name: string,
  ) {}

  async publish(message: AmqpMessage, routingKey: string): Promise<void> {
    if (this.publishError) throw this.publishError;
    this.published.push({ message, routingKey });
    this.recorder.add("publish", messageRecord(message, this.name, routingKey));
  }
}

export class FakeQueue implements AmqpQueue {
  consumers: AmqpConsumeCallback[] = [];
  isClosed?: boolean;

  constructor(
    readonly recorder: Recorder,
    readonly name: string,
  ) {}

  async bind(
    exchange: AmqpExchange | string,
    options: { routingKey?: string | null } = {},
  ): Promise<void> {
    this.recorder.add("bind", {
      queue: this.name,
      exchange: typeof exchange === "string" ? exchange : exchange.name,
      routing_key: options.routingKey ?? null,
    });
  }

  async consume(callback: AmqpConsumeCallback): Promise<string> {
    this.consumers.push(callback);
    this.recorder.add("consume", { queue: this.name });
    return `ctag-${this.name}`;
  }

  async cancel(consumerTag: string): Promise<void> {
    this.recorder.add("cancel", { queue: this.name, consumer_tag: consumerTag });
  }

  async delete({
    ifUnused = true,
    ifEmpty = true,
  }: { ifUnused?: boolean; ifEmpty?: boolean } = {}): Promise<void> {
    this.recorder.add("queue_object_delete", {
      queue: this.name,
      if_unused: ifUnused,
      if_empty: ifEmpty,
    });
  }
}

export class FakeChannel implements AmqpChannel {
  isClosed = false;
  exchanges = new Map<string, FakeExchange>();
  queues = new Map<string, FakeQueue>();

  constructor(readonly recorder: Recorder) {}

  async declareExchange(name: string, options: DeclareExchangeOptions = {}): Promise<FakeExchange> {
    this.recorder.add("declare_exchange", {
      name,
      type: options.type ?? "direct",
      durable: options.durable ?? false,
      auto_delete: options.autoDelete ?? false,
      passive: options.passive ?? false,
    });
    const exchange = this.exchanges.get(name) ?? new FakeExchange(this.recorder, name);
    this.exchanges.set(name, exchange);
    return exchange;
  }

  async getExchange(
    name: string,
    { ensure = true }: { ensure?: boolean } = {},
  ): Promise<FakeExchange> {
    this.recorder.add("get_exchange", { name, ensure });
    const exchange = this.exchanges.get(name) ?? new FakeExchange(this.recorder, name);
    this.exchanges.set(name, exchange);
    return exchange;
  }

  async declareQueue(name: string, options: DeclareQueueOptions = {}): Promise<FakeQueue> {
    this.recorder.add("declare_queue", {
      name,
      durable: options.durable ?? false,
      exclusive: options.exclusive ?? false,
      auto_delete: options.autoDelete ?? false,
      arguments: options.arguments ?? null,
    });
    const queue = new FakeQueue(this.recorder, name);
    this.queues.set(name, queue);
    return queue;
  }

  async queueDelete(name: string, { ifUnused = false, ifEmpty = false } = {}): Promise<void> {
    this.recorder.add("queue_delete", { name, if_unused: ifUnused, if_empty: ifEmpty });
  }

  async exchangeDelete(name: string, { ifUnused = false } = {}): Promise<void> {
    this.recorder.add("exchange_delete", { name, if_unused: ifUnused });
  }

  async close(): Promise<void> {
    this.isClosed = true;
    this.recorder.add("channel_close");
  }
}

export class FakeConnection implements AmqpConnection {
  isClosed = false;
  channels: FakeChannel[] = [];

  constructor(readonly recorder: Recorder = new Recorder()) {}

  async channel(): Promise<FakeChannel> {
    const channel = new FakeChannel(this.recorder);
    this.channels.push(channel);
    this.recorder.add("open_channel");
    return channel;
  }

  async close(): Promise<void> {
    this.isClosed = true;
    this.recorder.add("connection_close");
  }
}

/** 记录连接 URL 的连接器（对应 monkeypatch `aio_pika.connect(_robust)`）。 */
export class FakeConnector implements AmqpConnector {
  calls: Array<{ url: string; ssl_context: boolean; robust: boolean }> = [];
  connections: FakeConnection[] = [];

  constructor(readonly recorder: Recorder = new Recorder()) {}

  private make(
    url: string,
    options: AmqpConnectOptions | undefined,
    robust: boolean,
  ): FakeConnection {
    this.calls.push({ url, ssl_context: Boolean(options?.sslContext), robust });
    const connection = new FakeConnection(this.recorder);
    this.connections.push(connection);
    return connection;
  }

  async connect(url: string, options?: AmqpConnectOptions): Promise<AmqpConnection> {
    return this.make(url, options, false);
  }

  async connectRobust(url: string, options?: AmqpConnectOptions): Promise<AmqpConnection> {
    return this.make(url, options, true);
  }
}

export interface EmitCall {
  body: MessageBody;
  options: Record<string, unknown>;
}

/** 捕获 `emit(body, options)` 的发射器。 */
export class FakeEmitter implements MessageEmitterLike {
  calls: EmitCall[] = [];
  error: Error | null = null;

  async emit(body: MessageBody, options: Record<string, unknown> = {}): Promise<string> {
    if (this.error) throw this.error;
    this.calls.push({ body, options });
    return "log-id";
  }
}

/** 对应 Python 测试里的 `MagicMock()` 入站消息：`process()` 只是执行回调。 */
export function makeIncoming(
  payload: unknown,
  {
    userId = null,
    headers = null,
    redelivered = false,
  }: {
    userId?: string | Buffer | null;
    headers?: Record<string, unknown> | null;
    redelivered?: boolean;
  } = {},
): AmqpIncomingMessage & { acked: number; rejected: number } {
  const body = Buffer.isBuffer(payload) ? payload : Buffer.from(JSON.stringify(payload), "utf-8");
  const message = {
    body,
    headers,
    routingKey: "",
    redelivered,
    userId,
    acked: 0,
    rejected: 0,
    async process<T>(callback: () => Promise<T>): Promise<T> {
      try {
        const result = await callback();
        message.acked += 1;
        return result;
      } catch (error) {
        message.rejected += 1;
        throw error;
      }
    },
  };
  return message;
}

/** JSON 往返：去掉 undefined，便于与 Python fixture 比较。 */
export function plain<T>(value: T): unknown {
  return JSON.parse(JSON.stringify(value)) as unknown;
}
