/** 对应 tests/aip/test_group_partner_message.py — GroupPartnerMqClient opt-in message 埋点测试。 */

import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  type TaskCommand,
  type TaskResult,
  TaskCommandSchema,
  TaskResultSchema,
} from "../../src/aip/baseModel.js";
import { GroupPartnerMqClient, activeCmdTrace } from "../../src/aip/groupPartner.js";
import { MessageEmitter } from "../../src/amp/messageEmitter.js";
import { TRACEPARENT_HEADER } from "../../src/amp/traceContext.js";
import { FakeExchange, FakeQueue, Recorder, makeIncoming } from "./groupFakes.js";

let tmpPath: string;

beforeEach(() => {
  tmpPath = mkdtempSync(join(tmpdir(), "group-partner-message-"));
});

afterEach(() => {
  rmSync(tmpPath, { recursive: true, force: true });
});

function taskCommand({ cmdId = "cmd-001", senderId = "leader-aic" } = {}): TaskCommand {
  return TaskCommandSchema.parse({
    id: cmdId,
    sentAt: "2026-01-01T00:00:00Z",
    senderRole: "leader",
    senderId,
    sessionId: "sess-1",
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

function setupClient({ withEmitter = true } = {}) {
  const logFile = join(tmpPath, "message.jsonl");
  const emitter = withEmitter ? new MessageEmitter(logFile, "partner-aic") : null;
  const client = new GroupPartnerMqClient("partner-aic", {
    messageEmitter: emitter,
    messageSystem: "rabbitmq",
    identityBindingEnabled: false,
  });
  const recorder = new Recorder();
  const exchange = new FakeExchange(recorder, "group.test");
  const queue = new FakeQueue(recorder, "partner.queue");
  client._exchange = exchange;
  client._exchangeName = "group.test";
  client._groupId = "group-1";
  client.rabbitmqVhost = "acps";
  client._queue = queue;
  client._queueName = "partner.queue";
  return { client, emitter, logFile, exchange, queue };
}

async function captureOnMessage(client: GroupPartnerMqClient, queue: FakeQueue) {
  await client._startConsuming();
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

describe("test_group_partner_message", () => {
  it("test_consume_task_command_emits_receive_and_ack", async () => {
    const { client, logFile, queue } = setupClient();
    const sendSpan = "c".repeat(16);
    client._commandHandler = async () => undefined;
    const onMessage = await captureOnMessage(client, queue);
    await onMessage(
      makeIncoming(taskCommand(), {
        headers: { [TRACEPARENT_HEADER]: `00-${"d".repeat(32)}-${sendSpan}-01` },
      }),
    );
    const lines = readLines(logFile);
    expect(lines).toHaveLength(2);
    const [receive, ack] = lines;
    expect(receive?.body.eventType).toBe("receive");
    expect(receive?.parent_span_id).toBe(sendSpan);
    expect(ack?.body.eventType).toBe("ack");
  });

  it("test_handler_error_emits_nack_clears_contextvar", async () => {
    const { client, logFile, queue } = setupClient();
    const seenInHandler: unknown[] = [];
    client._commandHandler = async () => {
      seenInHandler.push(activeCmdTrace.getStore());
      throw new Error("handler failed");
    };
    const onMessage = await captureOnMessage(client, queue);
    await expect(onMessage(makeIncoming(taskCommand()))).rejects.toThrow("handler failed");
    // 处理命令期间 trace 上下文已设置；处理结束（含异常）后不再泄漏到调用方
    expect(seenInHandler[0]).toMatchObject({ traceId: expect.stringMatching(/^[0-9a-f]{32}$/) });
    expect(activeCmdTrace.getStore()).toBeUndefined();
    const record = readLines(logFile).at(-1);
    expect(record?.body.eventType).toBe("nack");
  });

  it("test_self_task_result_not_emitted", async () => {
    const { client, logFile, queue } = setupClient();
    const onMessage = await captureOnMessage(client, queue);
    await onMessage(makeIncoming(taskResult({ senderId: "partner-aic" })));
    expect(existsSync(logFile)).toBe(false);
  });

  it("test_other_task_result_not_emitted", async () => {
    const { client, logFile, queue } = setupClient();
    const onMessage = await captureOnMessage(client, queue);
    await onMessage(makeIncoming(taskResult({ senderId: "other-partner" })));
    expect(existsSync(logFile)).toBe(false);
  });

  it("test_publish_task_result_emits_send", async () => {
    const { client, logFile } = setupClient();
    await client._publishMessage(taskResult());
    const [record] = readLines(logFile);
    expect(record?.body.eventType).toBe("send");
  });

  it("test_o_m6_in_chain_trace_inheritance", async () => {
    const { client, logFile, queue } = setupClient();
    client._commandHandler = async () => {
      await client._publishMessage(taskResult({ resultId: "res-chain" }));
    };
    const onMessage = await captureOnMessage(client, queue);
    const sendSpan = "e".repeat(16);
    await onMessage(
      makeIncoming(taskCommand(), {
        headers: { [TRACEPARENT_HEADER]: `00-${"f".repeat(32)}-${sendSpan}-01` },
      }),
    );
    const lines = readLines(logFile);
    const receive = lines[0];
    const send = lines[1];
    expect(receive?.trace_id).toBe(send?.trace_id);
    expect(send?.parent_span_id).toBe(receive?.span_id);
  });

  it("test_o_m6_out_of_chain_new_trace", async () => {
    const { client, logFile } = setupClient();
    await client._publishMessage(taskResult({ resultId: "res-standalone" }));
    const [record] = readLines(logFile);
    expect(record?.parent_span_id).toBe("");
  });

  it("test_without_emitter_unchanged", async () => {
    const { client, logFile } = setupClient({ withEmitter: false });
    await client._publishMessage(taskResult());
    expect(existsSync(logFile)).toBe(false);
  });
});
