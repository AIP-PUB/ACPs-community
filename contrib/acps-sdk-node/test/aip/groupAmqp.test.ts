/**
 * groupAmqp 适配层（amqplib v2 → aio-pika 形状）的单元测试。
 *
 * 用内存假 amqplib（ChannelModel / ConfirmChannel / RecoveringChannelModel）验证：
 * URL 翻译、发布属性与 publisher confirm、消费与 ack/reject、以及 connect_robust 的拓扑恢复。
 */

import { EventEmitter } from "node:events";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AmqpMessage,
  ChannelClosedError,
  ChannelInvalidStateError,
  DeliveryError,
  DeliveryMode,
  ExchangeType,
  createAmqplibConnector,
  parseAmqpConnectionUrl,
  type AmqpChannel,
  type AmqpConnection,
  type AmqpIncomingMessage,
  type AmqplibConnectFn,
} from "../../src/aip/groupAmqp.js";
import { buildExternalConnectionUrl, buildPlainConnectionUrl } from "../../src/aip/groupRuntime.js";
import { LEADER_AIC } from "./groupFakes.js";

type Call = [string, ...unknown[]];

/** amqplib 自带的 recovery 核心（包的 exports 未暴露，按文件路径加载） */
const requireCjs = createRequire(import.meta.url);
const amqplibRecovery = requireCjs(
  resolve(dirname(requireCjs.resolve("amqplib")), "lib/recovery.js"),
) as {
  connectWithRecoveryPromise(
    openModel: () => Promise<unknown>,
    recovery: Record<string, unknown>,
  ): Promise<unknown>;
};

async function waitUntil(predicate: () => boolean, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("timed out waiting for condition");
    await new Promise((resolveWait) => setTimeout(resolveWait, 5));
  }
}

let consumerSeq = 0;

/**
 * 模拟 broker 拒绝某个操作：像真实 amqplib 一样，失败的 RPC 以带 `code` 的错误结束，
 * 同时 broker 关闭整个 channel（emit error + close）。
 */
interface FailureRule {
  op: string;
  arg?: string;
  code: number;
  text: string;
  /** 失败次数；缺省为一直失败 */
  times?: number;
  /** 失败前的延迟（毫秒） */
  delayMs?: number;
}

const brokerFailures: FailureRule[] = [];

class FakeRawChannel extends EventEmitter {
  calls: Call[] = [];
  consumers = new Map<string, (msg: unknown) => void>();
  publishMode: "ack" | "nack" | "close" = "ack";
  closed = false;

  private record(...call: Call): void {
    this.calls.push(call);
  }

  private ensureOpen(): void {
    if (this.closed) throw new Error("Channel closed");
  }

  private async maybeFail(op: string, arg: string): Promise<void> {
    const index = brokerFailures.findIndex(
      (rule) => rule.op === op && (rule.arg === undefined || rule.arg === arg),
    );
    if (index < 0) return;
    const rule = brokerFailures[index] as FailureRule;
    if (rule.times !== undefined) {
      rule.times -= 1;
      if (rule.times <= 0) brokerFailures.splice(index, 1);
    }
    if (rule.delayMs) await new Promise((resolveDelay) => setTimeout(resolveDelay, rule.delayMs));
    const error = Object.assign(
      new Error(`Operation failed: ${op}; ${rule.code} (${rule.text}) with message "${rule.text}"`),
      { code: rule.code, classId: 50, methodId: 10 },
    );
    this.serverClose(`${rule.code} (${rule.text})`, rule.code);
    throw error;
  }

  async assertExchange(name: string, type: string, options: unknown) {
    this.ensureOpen();
    await this.maybeFail("assertExchange", name);
    this.record("assertExchange", name, type, options);
    return { exchange: name };
  }

  async checkExchange(name: string) {
    this.ensureOpen();
    await this.maybeFail("checkExchange", name);
    this.record("checkExchange", name);
    return {};
  }

  async assertQueue(name: string, options: unknown) {
    this.ensureOpen();
    await this.maybeFail("assertQueue", name);
    this.record("assertQueue", name, options);
    return { queue: name, messageCount: 0, consumerCount: 0 };
  }

  async checkQueue(name: string) {
    this.record("checkQueue", name);
    return { queue: name, messageCount: 0, consumerCount: 0 };
  }

  async bindQueue(queue: string, source: string, pattern: string, args?: unknown) {
    this.ensureOpen();
    await this.maybeFail("bindQueue", queue);
    this.record("bindQueue", queue, source, pattern, args);
    return {};
  }

  async consume(
    queue: string,
    callback: (msg: unknown) => void,
    options: { consumerTag?: string },
  ) {
    this.ensureOpen();
    const consumerTag = options.consumerTag || `amq.ctag-${++consumerSeq}`;
    this.record("consume", queue, options);
    this.consumers.set(consumerTag, callback);
    return { consumerTag };
  }

  async cancel(consumerTag: string) {
    this.record("cancel", consumerTag);
    this.consumers.delete(consumerTag);
    return {};
  }

  async deleteQueue(name: string, options: unknown) {
    this.record("deleteQueue", name, options);
    return { messageCount: 0 };
  }

  async deleteExchange(name: string, options: unknown) {
    this.record("deleteExchange", name, options);
    return {};
  }

  publish(
    exchange: string,
    routingKey: string,
    content: Buffer,
    options: unknown,
    cb: (err: unknown) => void,
  ) {
    this.ensureOpen();
    this.record("publish", exchange, routingKey, content.toString("utf-8"), options);
    const mode = this.publishMode;
    setImmediate(() => {
      if (mode === "ack") cb(null);
      else if (mode === "nack") cb(new Error("message nacked"));
      else {
        this.serverClose('404 (NOT-FOUND) with message "NOT_FOUND - no exchange"');
        cb(new Error("channel closed"));
      }
    });
    return true;
  }

  ack(message: unknown) {
    if (this.closed) throw new Error("Channel closed");
    this.record("ack", message);
  }

  reject(message: unknown, requeue?: boolean) {
    this.record("reject", message, requeue);
  }

  nack(message: unknown, allUpTo?: boolean, requeue?: boolean) {
    this.record("nack", message, allUpTo, requeue);
  }

  async close() {
    this.record("close");
    this.closed = true;
    this.emit("close");
  }

  serverClose(reason: string, code = 406): void {
    if (this.closed) return;
    this.closed = true;
    this.emit("error", Object.assign(new Error(`Channel closed by server: ${reason}`), { code }));
    this.emit("close");
  }

  deliver(
    consumerTag: string,
    body: unknown,
    properties: Record<string, unknown> = {},
    redelivered = false,
  ): void {
    const callback = this.consumers.get(consumerTag);
    if (!callback) throw new Error(`no consumer ${consumerTag}`);
    callback({
      content: Buffer.from(JSON.stringify(body), "utf-8"),
      fields: { deliveryTag: 1, redelivered, exchange: "ex", routingKey: "rk", consumerTag },
      properties: {
        headers: undefined,
        userId: undefined,
        messageId: undefined,
        contentType: undefined,
        ...properties,
      },
    });
  }
}

class FakeModel extends EventEmitter {
  channels: FakeRawChannel[] = [];
  closed = false;

  async createConfirmChannel() {
    if (this.closed) throw new Error("Connection closed");
    const channel = new FakeRawChannel();
    this.channels.push(channel);
    return channel;
  }

  async close() {
    this.drop();
  }

  drop(): void {
    if (this.closed) return;
    this.closed = true;
    for (const channel of this.channels) {
      if (!channel.closed) {
        channel.closed = true;
        channel.emit("close");
      }
    }
    this.emit("close");
  }
}

interface RecoveryOptions {
  setup: (model: FakeModel) => Promise<void>;
  initialMaxRetries?: number;
}

class FakeRecovering extends EventEmitter {
  models: FakeModel[] = [];
  closed = false;
  private ready: Promise<void>;

  constructor(
    private readonly recovery: RecoveryOptions,
    failInitial: boolean,
  ) {
    super();
    this.ready = new Promise<void>((resolve, reject) => {
      setImmediate(() => {
        if (failInitial) {
          const error = new Error("connect ECONNREFUSED");
          this.emit("connect-failed", error);
          this.emit("reconnect-failed", error);
          reject(error);
          return;
        }
        void this.openModel().then(resolve, reject);
      });
    });
    this.ready.catch(() => undefined);
  }

  private async openModel(): Promise<void> {
    const model = new FakeModel();
    this.models.push(model);
    await this.recovery.setup(model);
    this.emit("connect", model);
  }

  async waitForConnect() {
    await this.ready;
    return this;
  }

  /** 模拟断线后自动重连成功。 */
  async reconnect(): Promise<void> {
    const current = this.models.at(-1);
    current?.drop();
    this.emit("disconnect", new Error("Connection closed: 320 (CONNECTION-FORCED)"));
    await this.openModel();
  }

  async close() {
    this.closed = true;
    this.models.at(-1)?.drop();
  }
}

function makeConnectFn({ failInitial = false } = {}) {
  const calls: Array<{ url: Record<string, unknown>; socketOptions: Record<string, unknown> }> = [];
  const created: Array<FakeModel | FakeRecovering> = [];
  const connectFn = vi.fn(async (url: unknown, socketOptions: Record<string, unknown>) => {
    calls.push({ url: url as Record<string, unknown>, socketOptions });
    if (socketOptions.recovery) {
      const recovering = new FakeRecovering(socketOptions.recovery as RecoveryOptions, failInitial);
      created.push(recovering);
      return recovering;
    }
    if (failInitial) throw new Error("connect ECONNREFUSED");
    const model = new FakeModel();
    created.push(model);
    return model;
  });
  return { connectFn: connectFn as unknown as AmqplibConnectFn, calls, created };
}

const flush = () => new Promise<void>((resolve) => setImmediate(resolve));

afterEach(() => {
  vi.restoreAllMocks();
  brokerFailures.length = 0;
});

describe("parseAmqpConnectionUrl", () => {
  it("translates aio-pika style PLAIN URLs", () => {
    const url = buildPlainConnectionUrl({
      host: "MQ.Example.com",
      port: 5673,
      vhost: "/team/a b",
      username: "u@x",
      password: "p:w/d!",
      connectionName: `agent-${LEADER_AIC}`,
    });
    const params = parseAmqpConnectionUrl(url);
    expect(params.url).toEqual({
      protocol: "amqp",
      hostname: "mq.example.com",
      port: 5673,
      vhost: encodeURIComponent("team/a b"),
      heartbeat: 60,
      username: "u@x",
      password: "p:w/d!",
    });
    expect(params.socketOptions.credentials?.mechanism).toBe("PLAIN");
    expect(params.socketOptions.clientProperties).toEqual({
      connection_name: `agent-${LEADER_AIC}`,
    });
    expect(params.socketOptions.timeout).toBe(60_000);
    expect(params.socketOptions.cert).toBeUndefined();
  });

  it("uses the default vhost and guest credentials", () => {
    const params = parseAmqpConnectionUrl("amqp://localhost:5672/%2F");
    expect(params.url.vhost).toBe(encodeURIComponent("/"));
    expect(params.url.username).toBe("guest");
    expect(params.socketOptions.clientProperties).toEqual({});
  });

  it("translates auth=external to SASL EXTERNAL over TLS with the client certificate", () => {
    const url = buildExternalConnectionUrl({
      host: "mq.acps.example.com",
      port: 5671,
      vhost: "acps",
      connectionName: `agent-${LEADER_AIC}`,
    });
    const tls = { cert: "CERT", key: "KEY", ca: "CA" };
    const params = parseAmqpConnectionUrl(url, { sslContext: tls });
    expect(params.url.protocol).toBe("amqps");
    expect(params.url.vhost).toBe("acps");
    expect(params.socketOptions.credentials?.mechanism).toBe("EXTERNAL");
    expect(params.socketOptions.credentials?.response().length).toBe(0);
    expect(params.socketOptions).toMatchObject({
      cert: "CERT",
      key: "KEY",
      ca: "CA",
      servername: "mq.acps.example.com",
    });
    expect(params.socketOptions.clientProperties).toEqual({
      connection_name: `agent-${LEADER_AIC}`,
    });
  });

  it("parses heartbeat / timeout like aiormq (invalid → 0, fractional seconds kept)", () => {
    const parse = (query: string) => parseAmqpConnectionUrl(`amqp://localhost/%2F?${query}`);
    expect(parse("heartbeat=30").url.heartbeat).toBe(30);
    expect(parse("heartbeat=+1_0").url.heartbeat).toBe(10);
    expect(parse("heartbeat=abc").url.heartbeat).toBe(0);
    expect(parse("heartbeat=70000").url.heartbeat).toBe(0);
    expect(parse("heartbeat=-1").url.heartbeat).toBe(0);
    expect(parse("timeout=1.5").socketOptions.timeout).toBe(1500);
    expect(parse("timeout=0.0004").socketOptions.timeout).toBe(0);
    expect(parse("timeout=abc").socketOptions.timeout).toBe(0);
    expect(parse("timeout=10").socketOptions.timeout).toBe(10_000);
    expect(parseAmqpConnectionUrl("amqp://localhost/%2F").socketOptions.timeout).toBe(60_000);
  });

  it("rejects non-AMQP schemes", () => {
    expect(() => parseAmqpConnectionUrl("http://x")).toThrow(
      "Expected amqp: or amqps: as the protocol",
    );
  });
});

describe("amqplib connector (non-robust)", () => {
  async function openChannel() {
    const { connectFn, calls, created } = makeConnectFn();
    const connector = createAmqplibConnector(connectFn);
    const connection = await connector.connect(
      "amqp://guest:guest@localhost:5672/%2F?name=agent-x",
    );
    const channel = await connection.channel();
    const model = created[0] as FakeModel;
    const raw = model.channels[0] as FakeRawChannel;
    return { connection, channel, model, raw, calls };
  }

  it("declares exchanges / queues with aio-pika semantics", async () => {
    const { channel, raw, calls } = await openChannel();
    expect(calls[0]?.socketOptions.recovery).toBeUndefined();

    const exchange = await channel.declareExchange("group_x", {
      type: ExchangeType.FANOUT,
      durable: true,
      autoDelete: true,
    });
    const queue = await channel.declareQueue("inbox_x", {
      durable: true,
      exclusive: false,
      autoDelete: false,
      arguments: { "x-expires": 5_184_000_000, "x-message-ttl": 604_800_000 },
    });
    await queue.bind(exchange);
    await queue.bind("inbox.topic", { routingKey: "inbox_x" });
    const handle = await channel.getExchange("group_y", { ensure: false });
    expect(handle.name).toBe("group_y");
    await channel.getExchange("group_z");
    await channel.queueDelete("inbox_x");
    await channel.exchangeDelete("group_x");
    await queue.delete({ ifUnused: false, ifEmpty: false });

    expect(raw.calls).toEqual([
      ["assertExchange", "group_x", "fanout", { durable: true, autoDelete: true, internal: false }],
      [
        "assertQueue",
        "inbox_x",
        {
          durable: true,
          exclusive: false,
          autoDelete: false,
          arguments: { "x-expires": 5_184_000_000, "x-message-ttl": 604_800_000 },
        },
      ],
      // aio-pika：bind 未给 routing key 时使用队列名
      ["bindQueue", "inbox_x", "group_x", "inbox_x", undefined],
      ["bindQueue", "inbox_x", "inbox.topic", "inbox_x", undefined],
      ["checkExchange", "group_z"],
      ["deleteQueue", "inbox_x", { ifUnused: false, ifEmpty: false }],
      ["deleteExchange", "group_x", { ifUnused: false }],
      ["deleteQueue", "inbox_x", { ifUnused: false, ifEmpty: false }],
    ]);
  });

  it("publishes with aio-pika / aiormq default properties and publisher confirms", async () => {
    const { channel, raw } = await openChannel();
    const exchange = await channel.declareExchange("group_x", { type: "fanout" });
    await exchange.publish(
      new AmqpMessage(Buffer.from('{"a": 1}'), {
        contentType: "application/json",
        deliveryMode: DeliveryMode.PERSISTENT,
        userId: LEADER_AIC,
        headers: { traceparent: "00-x" },
      }),
      "",
    );
    await exchange.publish(new AmqpMessage("plain"), "rk", { mandatory: false });

    const [first, second] = raw.calls.filter(([name]) => name === "publish");
    expect(first?.slice(0, 4)).toEqual(["publish", "group_x", "", '{"a": 1}']);
    const options = first?.[4] as Record<string, unknown>;
    expect(options).toMatchObject({
      mandatory: true,
      deliveryMode: 2,
      priority: 0,
      contentType: "application/json",
      userId: LEADER_AIC,
      headers: { traceparent: "00-x" },
    });
    expect(options.messageId).toMatch(/^[0-9a-f]{32}$/);
    expect(second?.[4]).toMatchObject({
      mandatory: false,
      deliveryMode: 1,
      priority: 0,
      headers: {},
    });
    expect(second?.[4]).not.toHaveProperty("userId");
    expect(second?.[4]).not.toHaveProperty("contentType");
  });

  it("maps broker nacks to DeliveryError and channel closure to ChannelClosedError", async () => {
    const { channel, raw } = await openChannel();
    const exchange = await channel.declareExchange("group_x", { type: "fanout" });

    raw.publishMode = "nack";
    await expect(exchange.publish(new AmqpMessage("x"), "")).rejects.toBeInstanceOf(DeliveryError);

    raw.publishMode = "close";
    const closed = await exchange
      .publish(new AmqpMessage("x"), "")
      .catch((error: unknown) => error);
    expect(closed).toBeInstanceOf(ChannelClosedError);
    expect((closed as Error).message).toContain("NOT_FOUND");
    expect(channel.isClosed).toBe(true);
    await expect(exchange.publish(new AmqpMessage("x"), "")).rejects.toBeInstanceOf(
      ChannelInvalidStateError,
    );
    await expect(channel.declareQueue("q")).rejects.toBeInstanceOf(ChannelInvalidStateError);
  });

  it("delivers messages to consumers; process() acks on success and rejects (requeue=false) on error", async () => {
    const { channel, raw } = await openChannel();
    const queue = await channel.declareQueue("q");
    const received: AmqpIncomingMessage[] = [];
    let fail = false;
    const tag = await queue.consume(async (message) => {
      received.push(message);
      await message.process(async () => {
        if (fail) throw new Error("handler failed");
      });
    });
    expect(raw.calls.find(([name]) => name === "consume")?.[2]).toEqual({
      noAck: false,
      exclusive: false,
    });

    raw.deliver(
      tag,
      { hello: "world" },
      { userId: LEADER_AIC, headers: { traceparent: "tp" } },
      true,
    );
    await flush();
    expect(received[0]?.userId).toBe(LEADER_AIC);
    expect(received[0]?.headers).toEqual({ traceparent: "tp" });
    expect(received[0]?.routingKey).toBe("rk");
    expect(received[0]?.redelivered).toBe(true);
    expect(JSON.parse(received[0]?.body.toString("utf-8") ?? "")).toEqual({ hello: "world" });
    expect(raw.calls.filter(([name]) => name === "ack")).toHaveLength(1);

    fail = true;
    const unhandled = vi.fn();
    process.on("unhandledRejection", unhandled);
    raw.deliver(tag, { n: 2 });
    await flush();
    await flush();
    process.off("unhandledRejection", unhandled);
    expect(raw.calls.filter(([name]) => name === "reject")).toEqual([
      ["reject", expect.anything(), false],
    ]);
    expect(unhandled).not.toHaveBeenCalled();

    // broker 取消消费者（例如队列被删除）时回调收到 null，不应抛错
    const callback = raw.consumers.get(tag);
    expect(() => callback?.(null)).not.toThrow();

    await queue.cancel(tag);
    expect(raw.consumers.has(tag)).toBe(false);
  });

  it("process() refuses to settle no_ack deliveries (like aio-pika) instead of acking them", async () => {
    const { channel, raw } = await openChannel();
    const queue = await channel.declareQueue("q");
    const outcomes: unknown[] = [];
    let mode: "ok" | "fail" | "ignore" = "ok";
    const tag = await queue.consume(
      async (message) => {
        const run = message.process(
          async () => {
            if (mode === "fail") throw new Error("handler failed");
            return "done";
          },
          { ignoreProcessed: mode === "ignore" },
        );
        outcomes.push(await run.catch((error: unknown) => error));
      },
      { noAck: true },
    );
    expect(raw.calls.find(([name]) => name === "consume")?.[2]).toMatchObject({ noAck: true });

    raw.deliver(tag, { n: 1 });
    await flush();
    expect(outcomes[0]).toBeInstanceOf(TypeError);
    expect((outcomes[0] as Error).message).toBe('Can\'t ack message with "no_ack" flag');

    mode = "fail";
    raw.deliver(tag, { n: 2 });
    await flush();
    expect(outcomes[1]).toBeInstanceOf(TypeError);
    expect((outcomes[1] as Error).message).toBe('This message has "no_ack" flag.');
    expect(((outcomes[1] as Error).cause as Error).message).toBe("handler failed");

    mode = "ignore";
    raw.deliver(tag, { n: 3 });
    await flush();
    expect(outcomes[2]).toBe("done");

    // 绝不向 broker 发送 ack / reject（否则 broker 以 406 PRECONDITION_FAILED 关闭 channel）
    expect(raw.calls.filter(([name]) => name === "ack" || name === "reject")).toEqual([]);
    expect(channel.isClosed).toBe(false);
  });

  it("process({ ignoreProcessed }) decides on settlement after the callback, like aio-pika", async () => {
    const { channel, raw } = await openChannel();
    const queue = await channel.declareQueue("q");
    const outcomes: unknown[] = [];
    const tag = await queue.consume(async (message) => {
      const run = message.process(
        async () => {
          // 回调里手动 ack：退出 process() 时不应再次 ack
          await (message as unknown as { ack(): Promise<void> }).ack();
          return "done";
        },
        { ignoreProcessed: true },
      );
      outcomes.push(await run.catch((error: unknown) => error));
    });
    raw.deliver(tag, { n: 1 });
    await waitUntil(() => outcomes.length === 1);
    expect(outcomes[0]).toBe("done");
    expect(raw.calls.filter(([name]) => name === "ack")).toHaveLength(1);
  });

  it("marks the connection closed when the broker drops it", async () => {
    const { connection, channel, model } = await openChannel();
    model.drop();
    expect(connection.isClosed).toBe(true);
    expect(channel.isClosed).toBe(true);
    await expect(connection.channel()).rejects.toBeInstanceOf(ChannelInvalidStateError);
    await connection.close();
  });

  it("propagates initial connection failures", async () => {
    const { connectFn } = makeConnectFn({ failInitial: true });
    await expect(createAmqplibConnector(connectFn).connect("amqp://localhost")).rejects.toThrow(
      "ECONNREFUSED",
    );
  });
});

describe("amqplib connector (robust, connect_robust)", () => {
  it("configures fail-fast recovery with a fixed 5 second interval", async () => {
    const { connectFn, calls } = makeConnectFn();
    const connection =
      await createAmqplibConnector(connectFn).connectRobust("amqp://localhost/%2F");
    const recovery = calls[0]?.socketOptions.recovery as Record<string, unknown>;
    expect(recovery).toMatchObject({
      initialDelay: 5000,
      maxDelay: 5000,
      factor: 1,
      jitter: 0,
      initialMaxRetries: 0,
      maxRetries: Infinity,
      waitForConnect: false,
    });
    expect(connection.isClosed).toBe(false);
    await connection.close();
    expect(connection.isClosed).toBe(true);
  });

  it("fails fast when the first connection attempt fails", async () => {
    const { connectFn } = makeConnectFn({ failInitial: true });
    await expect(
      createAmqplibConnector(connectFn).connectRobust("amqp://localhost"),
    ).rejects.toThrow("ECONNREFUSED");
  });

  it("restores exchanges, queues, bindings and consumers after reconnecting", async () => {
    const { connectFn, created } = makeConnectFn();
    const connection =
      await createAmqplibConnector(connectFn).connectRobust("amqp://localhost/%2F");
    const recovering = created[0] as FakeRecovering;
    const channel = await connection.channel();
    const exchange = await channel.declareExchange("group_x", {
      type: "fanout",
      durable: true,
      autoDelete: true,
    });
    const queue = await channel.declareQueue("group_x_q", { durable: true, autoDelete: true });
    await queue.bind(exchange);
    const bodies: unknown[] = [];
    const tag = await queue.consume(async (message) => {
      await message.process(async () => {
        bodies.push(JSON.parse(message.body.toString("utf-8")));
      });
    });

    await recovering.reconnect();
    expect(channel.isClosed).toBe(false);
    const restored = recovering.models[1]?.channels[0] as FakeRawChannel;
    expect(restored.calls.map(([name, ...args]) => [name, args[0]])).toEqual([
      ["assertExchange", "group_x"],
      ["assertQueue", "group_x_q"],
      ["bindQueue", "group_x_q"],
      ["consume", "group_x_q"],
    ]);
    expect(restored.calls[3]?.[2]).toMatchObject({ consumerTag: tag });

    restored.deliver(tag, { after: "reconnect" });
    await flush();
    expect(bodies).toEqual([{ after: "reconnect" }]);
    expect(restored.calls.at(-1)?.[0]).toBe("ack");

    await exchange.publish(new AmqpMessage("x"), "");
    expect(restored.calls.at(-1)?.[0]).toBe("publish");
    await connection.close();
  });

  it("reopens a channel closed by a channel-level error while the connection is up", async () => {
    const { connectFn, created } = makeConnectFn();
    const connection =
      await createAmqplibConnector(connectFn).connectRobust("amqp://localhost/%2F");
    const recovering = created[0] as FakeRecovering;
    const channel = await connection.channel();
    await channel.declareQueue("q", { durable: true });
    const first = recovering.models[0]?.channels[0] as FakeRawChannel;

    first.serverClose("406 (PRECONDITION-FAILED)");
    expect(channel.isClosed).toBe(true);
    await waitUntil(() => !channel.isClosed);
    const second = recovering.models[0]?.channels[1] as FakeRawChannel;
    expect(second.calls[0]?.slice(0, 2)).toEqual(["assertQueue", "q"]);

    // 显式关闭后不再恢复
    await channel.close();
    await flush();
    expect(channel.isClosed).toBe(true);
    expect(recovering.models[0]?.channels).toHaveLength(2);
    await connection.close();
  });

  it("retries a channel whose topology restore fails (e.g. 404) until it recovers, like aio-pika", async () => {
    const { connectFn, created } = makeConnectFn();
    const failures: Array<{ error: Error; channel: AmqpChannel }> = [];
    const connection = await createAmqplibConnector(connectFn, {
      onChannelRestoreFailed: (error, channel) => failures.push({ error, channel }),
      restoreRetryIntervalMs: 20,
    }).connectRobust("amqp://localhost/%2F");
    const recovering = created[0] as FakeRecovering;

    // Partner 的群组 channel：队列绑定到一个不是本 channel 声明的群组 Exchange
    const broken = await connection.channel();
    const queue = await broken.declareQueue("partner-q", { durable: true, autoDelete: true });
    await queue.bind("group-x");
    const bodies: unknown[] = [];
    const tag = await queue.consume(async (message) => {
      await message.process(async () => {
        bodies.push(JSON.parse(message.body.toString("utf-8")));
      });
    });
    const groupExchange = await broken.getExchange("group-x", { ensure: false });
    const healthy = await connection.channel();
    await healthy.declareQueue("fine", { durable: true });

    // 双方都断线后群组 Exchange 被自动删除：Partner 先重连时绑定得到 404，之后 Leader 重建 Exchange
    brokerFailures.push({
      op: "bindQueue",
      arg: "partner-q",
      code: 404,
      text: "NOT-FOUND",
      times: 3,
    });
    recovering.models[0]?.drop();
    recovering.emit("disconnect", new Error("down"));
    const queued = broken.declareQueue("queued-while-broken");
    await flush();
    await (recovering as unknown as { openModel(): Promise<void> }).openModel();

    expect(failures).toHaveLength(1);
    expect(failures[0]?.channel).toBe(broken);
    expect(failures[0]?.error.message).toContain("404");
    expect(broken.isClosed).toBe(true);
    expect(broken.lastRestoreError?.message).toContain("404");
    expect(healthy.isClosed).toBe(false);
    expect(connection.isClosed).toBe(false);
    // 恢复中的 channel 拒绝发布（不会往只恢复了一半的 channel 上发消息）
    await expect(groupExchange.publish(new AmqpMessage("x"), "")).rejects.toThrow(
      "Channel is being restored",
    );
    // 拆除类操作在恢复失败重试期间立即报错，而不是永久等待
    await expect(queue.cancel(tag)).rejects.toBeInstanceOf(ChannelInvalidStateError);

    await waitUntil(() => !broken.isClosed);
    expect(failures).toHaveLength(1); // 每轮连续失败只通知一次
    expect(broken.lastRestoreError).toBeNull();
    await expect(queued).resolves.toBeDefined();

    const model = recovering.models[1] as FakeModel;
    const live = model.channels.find(
      (raw) =>
        !raw.closed && raw.calls.some(([name, arg]) => name === "consume" && arg === "partner-q"),
    );
    expect(live?.calls.map(([name, arg]) => [name, arg])).toEqual([
      ["assertQueue", "partner-q"],
      ["bindQueue", "partner-q"],
      ["consume", "partner-q"],
      ["assertQueue", "queued-while-broken"],
    ]);
    live?.deliver(tag, { after: "retry" });
    await waitUntil(() => bodies.length === 1);
    await groupExchange.publish(new AmqpMessage("y"), "");
    await connection.close();
  });

  it("stops retrying once the channel is closed, and does not report channels closed during restore", async () => {
    const { connectFn, created } = makeConnectFn();
    const failures: AmqpChannel[] = [];
    const connection = await createAmqplibConnector(connectFn, {
      onChannelRestoreFailed: (_error, channel) => failures.push(channel),
      restoreRetryIntervalMs: 10,
    }).connectRobust("amqp://localhost/%2F");
    const recovering = created[0] as FakeRecovering;
    const retrying = await connection.channel();
    await retrying.declareQueue("doomed");
    const closedMidRestore = await connection.channel();
    await closedMidRestore.declareQueue("slow");

    brokerFailures.push({ op: "assertQueue", arg: "doomed", code: 404, text: "NOT-FOUND" });
    brokerFailures.push({
      op: "assertQueue",
      arg: "slow",
      code: 404,
      text: "NOT-FOUND",
      delayMs: 30,
    });
    recovering.models[0]?.drop();
    recovering.emit("disconnect", new Error("down"));
    const reconnect = (recovering as unknown as { openModel(): Promise<void> }).openModel();
    await waitUntil(() => (recovering.models[1]?.channels.length ?? 0) >= 2);
    await closedMidRestore.close();
    await reconnect;

    expect(failures).toEqual([retrying]);
    await waitUntil(() => (recovering.models[1]?.channels.length ?? 0) >= 4);
    await retrying.close();
    const opened = recovering.models[1]?.channels.length ?? 0;
    await new Promise((resolveWait) => setTimeout(resolveWait, 60));
    expect(recovering.models[1]?.channels.length).toBe(opened);
    expect(retrying.isClosed).toBe(true);
    await connection.close();
  });

  it("classifies restore failures by AMQP reply code: hard errors reconnect, soft errors retry the channel", async () => {
    const { connectFn, created } = makeConnectFn();
    const connection = await createAmqplibConnector(connectFn, {
      restoreRetryIntervalMs: 10,
    }).connectRobust("amqp://localhost/%2F");
    const recovering = created[0] as FakeRecovering;
    const channel = await connection.channel();
    await channel.declareQueue("q");

    brokerFailures.push({
      op: "assertQueue",
      arg: "q",
      code: 320,
      text: "CONNECTION-FORCED",
      times: 1,
    });
    await expect(recovering.reconnect()).rejects.toMatchObject({ code: 320 });
    expect((connection as unknown as { _isUp(): boolean })._isUp()).toBe(false);

    brokerFailures.push({ op: "assertQueue", arg: "q", code: 404, text: "NOT-FOUND", times: 1 });
    await (recovering as unknown as { openModel(): Promise<void> }).openModel();
    expect((connection as unknown as { _isUp(): boolean })._isUp()).toBe(true);
    expect(channel.lastRestoreError?.message).toContain("404");
    await waitUntil(() => !channel.isClosed);
    expect(channel.lastRestoreError).toBeNull();
    await connection.close();
  });

  it("queue handles stay valid while their robust channel is restoring", async () => {
    const { connectFn, created } = makeConnectFn();
    const connection =
      await createAmqplibConnector(connectFn).connectRobust("amqp://localhost/%2F");
    const recovering = created[0] as FakeRecovering;
    const channel = await connection.channel();
    const queue = await channel.declareQueue("q");
    const other = await channel.declareQueue("other");

    recovering.models[0]?.drop();
    recovering.emit("disconnect", new Error("down"));
    expect(channel.isClosed).toBe(true);
    expect(channel.closeCalled).toBe(false);
    expect(queue.isClosed).toBe(false);
    await (recovering as unknown as { openModel(): Promise<void> }).openModel();
    expect(queue.isClosed).toBe(false);

    await other.delete({ ifUnused: false, ifEmpty: false });
    expect(other.isClosed).toBe(true);
    await channel.close();
    expect(channel.closeCalled).toBe(true);
    expect(queue.isClosed).toBe(true);
    await connection.close();
  });

  it("operations wait for the reconnect instead of failing while the connection is down", async () => {
    const { connectFn, created } = makeConnectFn();
    const connection =
      await createAmqplibConnector(connectFn).connectRobust("amqp://localhost/%2F");
    const recovering = created[0] as FakeRecovering;
    const channel = await connection.channel();

    recovering.models[0]?.drop();
    recovering.emit("disconnect", new Error("down"));
    let settled = false;
    const pending = channel.declareQueue("later").finally(() => {
      settled = true;
    });
    await new Promise((resolveWait) => setTimeout(resolveWait, 20));
    expect(settled).toBe(false); // 断线期间排队等待
    await (recovering as unknown as { openModel(): Promise<void> }).openModel();
    await pending;
    const restored = recovering.models[1]?.channels[0] as FakeRawChannel;
    expect(restored.calls.map(([name, arg]) => [name, arg])).toEqual([["assertQueue", "later"]]);
    await connection.close();
  });
});

describe("robust reconnect with amqplib's real recovering core", () => {
  it("schedules another reconnect when the connection drops while channels are being restored", async () => {
    const models: FakeModel[] = [];
    const plan = ["healthy", "dies-during-restore", "healthy"] as const;
    let connection: AmqpConnection | null = null;
    const upWhenAttempting: boolean[] = [];

    const openModel = async (): Promise<FakeModel> => {
      if (connection) {
        upWhenAttempting.push((connection as unknown as { _isUp(): boolean })._isUp());
      }
      const model = new FakeModel();
      if (plan[models.length] === "dies-during-restore") {
        // 新连接刚建立就被 broker 关掉：恢复 channel 时发现连接已死
        model.createConfirmChannel = async () => {
          model.drop();
          throw new Error("Connection closed: 320 (CONNECTION-FORCED)");
        };
      }
      models.push(model);
      return model;
    };
    const connectFn = (async (_url: unknown, socketOptions?: { recovery?: unknown }) =>
      amqplibRecovery.connectWithRecoveryPromise(openModel, {
        ...(socketOptions?.recovery as Record<string, unknown>),
        // 用真实的 recovery 状态机，只把重连间隔缩短到毫秒级
        initialDelay: 5,
        maxDelay: 5,
      })) as unknown as AmqplibConnectFn;

    connection = await createAmqplibConnector(connectFn).connectRobust("amqp://localhost/%2F");
    const channel = await connection.channel();
    const queue = await channel.declareQueue("q", { durable: true });
    const received: unknown[] = [];
    const tag = await queue.consume(async (message) => {
      await message.process(async () => {
        received.push(JSON.parse(message.body.toString("utf-8")));
      });
    });

    models[0]?.drop();
    await waitUntil(() => models.length === 3 && !channel.isClosed);

    // 第二条连接在恢复中死掉：SDK 没有停留在它上面，而是触发了第三次连接
    expect(models[1]?.closed).toBe(true);
    expect(upWhenAttempting).toEqual([false, false]);
    expect((connection as unknown as { _isUp(): boolean })._isUp()).toBe(true);
    expect(connection.isClosed).toBe(false);

    const restored = models[2]?.channels[0] as FakeRawChannel;
    expect(restored.calls.map(([name, arg]) => [name, arg])).toEqual([
      ["assertQueue", "q"],
      ["consume", "q"],
    ]);
    restored.deliver(tag, { after: "second reconnect" });
    await flush();
    expect(received).toEqual([{ after: "second reconnect" }]);

    // 恢复后可以继续开新 channel
    const another = await connection.channel();
    expect(another.isClosed).toBe(false);
    await connection.close();
    expect(connection.isClosed).toBe(true);
  });
});
