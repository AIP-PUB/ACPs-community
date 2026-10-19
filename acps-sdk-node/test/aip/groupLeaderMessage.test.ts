/** 对应 tests/aip/test_group_leader_message.py — GroupLeaderMqClient opt-in message 埋点测试。 */

import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  type TaskCommand,
  type TaskResult,
  TaskCommandSchema,
  TaskResultSchema,
} from "../../src/aip/baseModel.js";
import { GroupLeaderMqClient } from "../../src/aip/groupLeader.js";
import { GroupMgmtCommandSchema, GroupMgmtCommandType } from "../../src/aip/groupModel.js";
import { MessageEmitter } from "../../src/amp/messageEmitter.js";
import { TRACEPARENT_HEADER } from "../../src/amp/traceContext.js";
import { FakeExchange, FakeQueue, Recorder, makeIncoming } from "./groupFakes.js";

let tmpPath: string;

beforeEach(() => {
  tmpPath = mkdtempSync(join(tmpdir(), "group-leader-message-"));
});

afterEach(() => {
  rmSync(tmpPath, { recursive: true, force: true });
});

function taskCommand({ cmdId = "cmd-001", sessionId = "sess-1" } = {}): TaskCommand {
  return TaskCommandSchema.parse({
    id: cmdId,
    sentAt: "2026-01-01T00:00:00Z",
    senderRole: "leader",
    senderId: "leader-aic",
    sessionId,
    command: "get",
    taskId: "task-1",
    groupId: "group-1",
  });
}

function taskResult({ resultId = "res-001", senderId = "partner-aic" } = {}): TaskResult {
  return TaskResultSchema.parse({
    id: resultId,
    sentAt: "2026-01-01T00:00:00Z",
    senderRole: "partner",
    senderId,
    taskId: "task-1",
    sessionId: "sess-1",
    groupId: "group-1",
    status: { state: "working", stateChangedAt: "2026-01-01T00:00:00Z" },
  });
}

function setupClient({ withEmitter = true } = {}): {
  client: GroupLeaderMqClient;
  emitter: MessageEmitter | null;
  logFile: string;
  exchange: FakeExchange;
  queue: FakeQueue;
} {
  const logFile = join(tmpPath, "message.jsonl");
  const emitter = withEmitter ? new MessageEmitter(logFile, "leader-aic") : null;
  const client = new GroupLeaderMqClient("leader-aic", {
    messageEmitter: emitter,
    messageSystem: "rabbitmq",
    traceContextProvider: () => ({ traceId: "a".repeat(32), spanId: "b".repeat(16) }),
    identityBindingEnabled: false,
  });
  const recorder = new Recorder();
  const exchange = new FakeExchange(recorder, "group.test");
  const queue = new FakeQueue(recorder, "leader.queue");
  client._exchange = exchange;
  client._exchangeName = "group.test";
  client._groupId = "group-1";
  client.rabbitmqVhost = "acps";
  client._leaderQueue = queue;
  return { client, emitter, logFile, exchange, queue };
}

async function captureOnMessage(client: GroupLeaderMqClient, queue: FakeQueue) {
  await client.startConsuming();
  const handler = queue.consumers[0];
  if (!handler) throw new Error("consumer not registered");
  return handler;
}

interface LogLine {
  log_type?: string;
  trace_id?: string;
  span_id?: string;
  parent_span_id?: string;
  body: {
    eventType: string;
    messageId?: string;
    destination: { name: string };
    settlement?: { reason?: string };
  };
}

function readLines(logFile: string): LogLine[] {
  return readFileSync(logFile, "utf-8")
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line) as LogLine);
}

describe("test_group_leader_message", () => {
  it("test_publish_task_command_emits_send_and_traceparent", async () => {
    const { client, logFile, exchange } = setupClient();

    await client.publishMessage(taskCommand());

    const published = exchange.published[0];
    expect(published?.message.headers).toHaveProperty(TRACEPARENT_HEADER);
    expect(published?.routingKey).toBe("");
    const [record] = readLines(logFile);
    expect(record?.log_type).toBe("message");
    expect(record?.body.eventType).toBe("send");
    expect(record?.body.messageId).toBe("cmd-001");
    expect(record?.body.destination.name).toBe("group.test");
    expect(record?.trace_id).toBe("a".repeat(32));
  });

  it("test_on_message_task_result_emits_receive_and_ack", async () => {
    const { client, logFile, queue } = setupClient();
    const sendSpan = "c".repeat(16);
    const handler = vi.fn(async () => undefined);
    client.setMessageHandler(handler);

    const onMessage = await captureOnMessage(client, queue);
    await onMessage(
      makeIncoming(taskResult(), {
        headers: { [TRACEPARENT_HEADER]: `00-${"d".repeat(32)}-${sendSpan}-01` },
      }),
    );

    expect(handler).toHaveBeenCalledTimes(1);
    const lines = readLines(logFile);
    expect(lines).toHaveLength(2);
    const [receive, ack] = lines;
    expect(receive?.body.eventType).toBe("receive");
    expect(receive?.parent_span_id).toBe(sendSpan);
    expect(ack?.body.eventType).toBe("ack");
  });

  it("test_on_message_handler_error_emits_nack_and_reraises", async () => {
    const { client, logFile, queue } = setupClient();
    client.setMessageHandler(async () => {
      throw new Error("handler failed");
    });
    const onMessage = await captureOnMessage(client, queue);

    await expect(onMessage(makeIncoming(taskResult()))).rejects.toThrow("handler failed");

    const record = readLines(logFile).at(-1);
    expect(record?.body.eventType).toBe("nack");
    expect(record?.body.settlement?.reason).toBe("handler failed");
  });

  it("test_self_sent_message_not_emitted", async () => {
    const { client, logFile, queue } = setupClient();
    client.setMessageHandler(vi.fn(async () => undefined));
    const onMessage = await captureOnMessage(client, queue);
    await onMessage(makeIncoming(taskResult({ senderId: "leader-aic" })));
    expect(existsSync(logFile)).toBe(false);
  });

  it("test_mgmt_message_not_emitted_and_no_traceparent", async () => {
    const { client, logFile, exchange } = setupClient();

    const mgmt = GroupMgmtCommandSchema.parse({
      id: "mgmt-1",
      sentAt: "2026-01-01T00:00:00Z",
      senderRole: "leader",
      senderId: "leader-aic",
      sessionId: "sess-1",
      groupId: "group-1",
      command: GroupMgmtCommandType.MUTE,
    });
    await client.publishMessage(mgmt);

    expect(exchange.published[0]?.message.headers).not.toHaveProperty(TRACEPARENT_HEADER);
    expect(existsSync(logFile)).toBe(false);
  });

  it("test_without_emitter_behavior_unchanged", async () => {
    const { client, logFile, exchange } = setupClient({ withEmitter: false });
    await client.publishMessage(taskCommand());
    expect(exchange.published[0]?.message.headers).not.toHaveProperty(TRACEPARENT_HEADER);
    expect(existsSync(logFile)).toBe(false);
  });

  it("test_emit_failure_does_not_break_publish", async () => {
    const { client, emitter, exchange } = setupClient();
    vi.spyOn(emitter as MessageEmitter, "emit").mockRejectedValue(new Error("emit failed"));

    await client.publishMessage(taskCommand());
    expect(exchange.published).toHaveLength(1);
  });
});
