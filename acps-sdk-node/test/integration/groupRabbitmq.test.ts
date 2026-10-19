/**
 * 群组模式真实 RabbitMQ 集成测试（默认跳过）。
 *
 * 运行：ACPS_TEST_RABBITMQ_URL=amqp://guest:guest@localhost:5672/%2F npx vitest run --project integration
 *
 * 用 PLAIN 认证（guest）验证：
 * - Leader 建群 → Partner 直连 RPC 加入 → 双向收发任务消息 → Partner 退出 → Leader 解散；
 * - RabbitMQ 对 AMQP `user_id` 的校验：`user_id` 与认证用户名不一致时 broker 关闭 channel，发布失败；
 * - `user_id` 与认证用户名一致时，消费者能读到该属性（身份绑定所依赖的 broker 行为）。
 */

import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";

import { describe, expect, it } from "vitest";

import type { TaskCommand, TaskResult } from "../../src/aip/baseModel.js";
import {
  AmqpMessage,
  ChannelClosedError,
  ChannelInvalidStateError,
  defaultAmqpConnector,
} from "../../src/aip/groupAmqp.js";
import { type GroupMessage, GroupLeaderMqClient } from "../../src/aip/groupLeader.js";
import { RabbitMQRequestSchema } from "../../src/aip/groupModel.js";
import { GroupPartnerMqClient } from "../../src/aip/groupPartner.js";
import { pyUnquote, pyUrlparse } from "../../src/aip/groupRuntime.js";

const RABBITMQ_URL = process.env.ACPS_TEST_RABBITMQ_URL;
/** 运行 broker 的 docker 容器名；用于 `rabbitmqctl close_connection` 模拟断线 */
const RABBITMQ_CONTAINER = process.env.ACPS_TEST_RABBITMQ_CONTAINER ?? "acps-sdk-node-rmq";

function dockerBrokerAvailable(): boolean {
  if (!RABBITMQ_URL) return false;
  try {
    const running = execFileSync(
      "docker",
      ["inspect", "-f", "{{.State.Running}}", RABBITMQ_CONTAINER],
      { encoding: "utf-8", stdio: ["ignore", "pipe", "ignore"], timeout: 10_000 },
    );
    return running.trim() === "true";
  } catch {
    return false;
  }
}

/** 让 broker 强制关闭 connection_name 为 `name` 的连接（320 CONNECTION_FORCED），返回关闭的数量。 */
function forceCloseConnections(name: string): number {
  const listing = execFileSync(
    "docker",
    [
      "exec",
      RABBITMQ_CONTAINER,
      "rabbitmqctl",
      "-q",
      "list_connections",
      "--no-table-headers",
      "pid",
      "client_properties",
    ],
    { encoding: "utf-8", timeout: 30_000 },
  );
  const pids = listing
    .split("\n")
    .filter((line) => line.includes(`"${name}"`))
    .map((line) => line.split("\t")[0]?.trim() ?? "")
    .filter((pid) => pid.length > 0);
  for (const pid of pids) {
    execFileSync(
      "docker",
      [
        "exec",
        RABBITMQ_CONTAINER,
        "rabbitmqctl",
        "close_connection",
        pid,
        "integration test: forced close",
      ],
      { stdio: "ignore", timeout: 30_000 },
    );
  }
  return pids.length;
}

const LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4";
const PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5";

function brokerConfig() {
  const parts = pyUrlparse(RABBITMQ_URL ?? "");
  return {
    host: parts.hostname ?? "localhost",
    port: parts.port ?? 5672,
    vhost: parts.path && parts.path !== "/" ? pyUnquote(parts.path.slice(1)) : "/",
    user: parts.username ? pyUnquote(parts.username) : "guest",
    password: parts.password ? pyUnquote(parts.password) : "guest",
  };
}

async function waitFor<T>(predicate: () => T | undefined, timeoutMs = 10_000): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = predicate();
    if (value !== undefined) return value;
    if (Date.now() > deadline) throw new Error("timed out waiting for condition");
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

describe.skipIf(!RABBITMQ_URL)("group mode over a real RabbitMQ", () => {
  it("leader and partner exchange task messages through the group exchange", async () => {
    const cfg = brokerConfig();
    // guest 不是 AIC：PLAIN 认证下关闭身份绑定（生产环境用 mTLS + EXTERNAL，user_id 即证书 CN）
    const leader = new GroupLeaderMqClient(LEADER_AIC, {
      rabbitmqHost: cfg.host,
      rabbitmqPort: cfg.port,
      rabbitmqVhost: cfg.vhost,
      rabbitmqUser: cfg.user,
      rabbitmqPassword: cfg.password,
      identityBindingEnabled: false,
    });
    const leaderInbox: GroupMessage[] = [];
    await leader.connect();
    const groupId = await leader.createGroup({ groupId: `it-${randomUUID()}` });
    leader.setMessageHandler(async (message) => {
      leaderInbox.push(message);
    });
    await leader.startConsuming();

    const partner = new GroupPartnerMqClient(PARTNER_AIC, { identityBindingEnabled: false });
    const commands: Array<[TaskCommand, boolean]> = [];
    partner.setCommandHandler(async (command, isMentioned) => {
      commands.push([command, isMentioned]);
      await partner.startWorking(command.taskId ?? "", command.sessionId ?? "");
    });
    const response = await partner.joinGroup(
      RabbitMQRequestSchema.parse({
        id: "it-1",
        params: {
          protocol: "rabbitmq:4.0",
          group: { groupId, leader: { aic: LEADER_AIC }, partners: [{ aic: PARTNER_AIC }] },
          server: {
            host: cfg.host,
            port: cfg.port,
            vhost: cfg.vhost,
            accessToken: `${cfg.user}:${cfg.password}`,
          },
          amqp: { exchange: leader._exchangeName, exchangeType: "fanout", routingKey: "" },
        },
      }),
    );
    expect(response.error).toBeUndefined();
    expect(response.result?.queueName).toBe(`group_${LEADER_AIC}_${groupId}_${PARTNER_AIC}`);

    // Partner 加入通知（GroupMgmtResult connected=true）
    await waitFor(() =>
      leaderInbox.find((m) => m.type === "group-mgmt-result" && m.status.connected),
    );

    const taskId = await leader.startTask("session-1", "hello ✓", { mentions: [PARTNER_AIC] });
    const [command, isMentioned] = await waitFor(() => commands[0]);
    expect(command.taskId).toBe(taskId);
    expect(isMentioned).toBe(true);

    const result = (await waitFor(() =>
      leaderInbox.find((m) => m.type === "task-result" && m.taskId === taskId),
    )) as TaskResult;
    expect(result.status.state).toBe("working");
    expect(result.senderId).toBe(PARTNER_AIC);

    await partner.leaveGroup({ sessionId: "session-1" });
    await waitFor(() =>
      leaderInbox.find((m) => m.type === "group-mgmt-result" && !m.status.connected),
    );
    await leader.close();
    await partner.close();
  });

  it("RabbitMQ rejects a user_id that differs from the authenticated user", async () => {
    const connection = await defaultAmqpConnector.connect(RABBITMQ_URL ?? "");
    const channel = await connection.channel();
    const exchange = await channel.declareExchange(`it-x-${randomUUID()}`, {
      type: "fanout",
      autoDelete: true,
    });
    const failure = await exchange
      .publish(new AmqpMessage("x", { userId: LEADER_AIC }), "")
      .catch((error: unknown) => error);
    expect(
      failure instanceof ChannelClosedError || failure instanceof ChannelInvalidStateError,
    ).toBe(true);
    expect(String((failure as Error).message)).toMatch(/PRECONDITION|user_id|closed/i);
    await connection.close();
  });

  it("delivers the broker-validated user_id to consumers", async () => {
    const cfg = brokerConfig();
    const connection = await defaultAmqpConnector.connect(RABBITMQ_URL ?? "");
    const channel = await connection.channel();
    const exchange = await channel.declareExchange(`it-u-${randomUUID()}`, {
      type: "fanout",
      autoDelete: true,
    });
    const queue = await channel.declareQueue(`it-u-${randomUUID()}`, {
      autoDelete: true,
      exclusive: true,
    });
    await queue.bind(exchange);
    const userIds: unknown[] = [];
    await queue.consume(async (message) => {
      await message.process(async () => {
        userIds.push(message.userId);
      });
    });
    await exchange.publish(new AmqpMessage("x", { userId: cfg.user }), "");
    expect(await waitFor(() => userIds[0])).toBe(cfg.user);
    await connection.close();
  });
});

describe.skipIf(!dockerBrokerAvailable())("group mode survives broker-side connection loss", () => {
  /** 建一个 Leader + Partner 群组（PLAIN 认证、关闭身份绑定），返回可重试的往返检查。 */
  async function setUpGroup(LEADER: string, PARTNER: string) {
    const cfg = brokerConfig();
    const leader = new GroupLeaderMqClient(LEADER, {
      rabbitmqHost: cfg.host,
      rabbitmqPort: cfg.port,
      rabbitmqVhost: cfg.vhost,
      rabbitmqUser: cfg.user,
      rabbitmqPassword: cfg.password,
      identityBindingEnabled: false,
    });
    const leaderInbox: GroupMessage[] = [];
    await leader.connect();
    const groupId = await leader.createGroup({ groupId: `it-rc-${randomUUID()}` });
    leader.setMessageHandler(async (message) => {
      leaderInbox.push(message);
    });
    await leader.startConsuming();

    const partner = new GroupPartnerMqClient(PARTNER, { identityBindingEnabled: false });
    const commands: TaskCommand[] = [];
    partner.setCommandHandler(async (command) => {
      commands.push(command);
      await partner.startWorking(command.taskId ?? "", command.sessionId ?? "");
    });
    const response = await partner.joinGroup(
      RabbitMQRequestSchema.parse({
        id: "it-rc",
        params: {
          protocol: "rabbitmq:4.0",
          group: { groupId, leader: { aic: LEADER }, partners: [{ aic: PARTNER }] },
          server: {
            host: cfg.host,
            port: cfg.port,
            vhost: cfg.vhost,
            accessToken: `${cfg.user}:${cfg.password}`,
          },
          amqp: { exchange: leader._exchangeName, exchangeType: "fanout", routingKey: "" },
        },
      }),
    );
    expect(response.error).toBeUndefined();

    const publishFailures: unknown[] = [];
    const roundTrip = async (label: string, timeoutMs = 40_000): Promise<void> => {
      // 重连需要 ~5s（aio-pika 默认 reconnect_interval）；期间发出的消息可能因队列尚未恢复而丢失，所以重发直到收到
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        if (Date.now() > deadline) throw new Error(`round trip "${label}" did not recover`);
        let taskId: string;
        try {
          taskId = await leader.startTask("session-rc", label, { mentions: [PARTNER] });
        } catch (error) {
          // 与 aio-pika 相同：连接恢复期间发布会失败，由调用方重试
          publishFailures.push(error);
          await new Promise((resolve) => setTimeout(resolve, 500));
          continue;
        }
        const received = await waitFor(
          () =>
            leaderInbox.find(
              (m) => m.type === "task-result" && m.taskId === taskId && m.senderId === PARTNER,
            ),
          1000,
        ).catch(() => undefined);
        if (received) return;
      }
    };
    const finish = async (): Promise<void> => {
      for (const failure of publishFailures) {
        expect(
          failure instanceof ChannelInvalidStateError || failure instanceof ChannelClosedError,
        ).toBe(true);
      }
      expect(leader.isConnected).toBe(true);
      expect(partner.isJoined).toBe(true);
      await partner.leaveGroup({ sessionId: "session-rc" });
      await leader.close();
      await partner.close();
    };
    return { leader, partner, commands, roundTrip, finish };
  }

  it(
    "leader and partner reconnect (connect_robust) and keep consuming",
    { timeout: 90_000 },
    async () => {
      // 与上面的用例使用不同 AIC，避免按连接名关闭时误伤
      const LEADER = "1.2.156.3088.1.1.34C2.478BDF.3GF549.0JU7";
      const PARTNER = "1.2.156.3088.1.1.34C2.478BDF.3GF54A.0JU8";
      const { commands, roundTrip, finish } = await setUpGroup(LEADER, PARTNER);

      await roundTrip("before");
      const commandsBefore = commands.length;

      // 1) broker 强制关闭 Partner 的连接：Partner 自动重连，恢复队列 / 绑定 / 消费者
      expect(forceCloseConnections(`agent-${PARTNER}`)).toBeGreaterThan(0);
      await roundTrip("after partner reconnect");
      expect(commands.length).toBeGreaterThan(commandsBefore);

      // 2) broker 强制关闭 Leader 的连接：Leader 自动重连，恢复 Exchange / 队列 / 消费者
      expect(forceCloseConnections(`agent-${LEADER}`)).toBeGreaterThan(0);
      await roundTrip("after leader reconnect");
      await finish();
    },
  );

  it(
    "both connections lost: the partner retries its group channel until the leader re-creates the exchange",
    { timeout: 90_000 },
    async () => {
      const LEADER = "1.2.156.3088.1.1.34C2.478BDF.3GF54B.0JU9";
      const PARTNER = "1.2.156.3088.1.1.34C2.478BDF.3GF54C.0JUA";
      const { partner, roundTrip, finish } = await setUpGroup(LEADER, PARTNER);
      await roundTrip("before");

      // 先断 Partner、1 秒后再断 Leader：两个 auto-delete 队列都消失后 fanout Exchange 被自动删除；
      // Partner 先重连，绑定得到 404 → 群组 channel 进入“恢复失败、定时重试”；
      // Leader 随后重连并重建 Exchange，Partner 的下一次重试成功。
      expect(forceCloseConnections(`agent-${PARTNER}`)).toBeGreaterThan(0);
      await new Promise((resolve) => setTimeout(resolve, 1000));
      expect(forceCloseConnections(`agent-${LEADER}`)).toBeGreaterThan(0);

      const restoreError = await waitFor(() => partner.groupChannelError ?? undefined, 20_000);
      expect(restoreError.message).toMatch(/404|NOT[-_]FOUND/);
      expect(partner.isJoined).toBe(false);

      await roundTrip("after both reconnected", 60_000);
      expect(partner.groupChannelError).toBeNull();
      await finish();
    },
  );
});
