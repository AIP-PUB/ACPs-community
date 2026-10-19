import { afterEach, describe, expect, it } from "vitest";

import {
  type TaskCommand,
  type TaskResult,
  TaskCommandSchema,
  TaskResultSchema,
} from "../../src/aip/baseModel.js";
import {
  assertDirectGroupRequestIdentity,
  assertIncomingGroupMessageIdentity,
  buildOutgoingAmqpUserId,
  extractAmqpUserId,
} from "../../src/aip/groupIdentity.js";
import { GroupLeaderMqClient } from "../../src/aip/groupLeader.js";
import { type RabbitMQRequest, RabbitMQRequestSchema } from "../../src/aip/groupModel.js";
import { GroupPartnerMqClient } from "../../src/aip/groupPartner.js";
import { PeerAicMissingError, SenderIdentityMismatchError } from "../../src/aip/identity.js";
import { setLoggerFactory } from "../../src/internal/logger.js";
import { FakeExchange, FakeQueue, Recorder, makeIncoming } from "./groupFakes.js";

const LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4";
const PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5";
const OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF548.0JU6";

function command({ senderId = LEADER_AIC, groupId = "group-1" } = {}): TaskCommand {
  return TaskCommandSchema.parse({
    id: "cmd-1",
    sentAt: "2026-06-24T00:00:00Z",
    senderRole: "leader",
    senderId,
    sessionId: "session-1",
    command: "start",
    taskId: "task-1",
    groupId,
  });
}

function result({ senderId = PARTNER_AIC, groupId = "group-1" } = {}): TaskResult {
  return TaskResultSchema.parse({
    id: "res-1",
    sentAt: "2026-06-24T00:00:01Z",
    senderRole: "partner",
    senderId,
    sessionId: "session-1",
    taskId: "task-1",
    groupId,
    status: { state: "working", stateChangedAt: "2026-06-24T00:00:01Z" },
  });
}

function groupRequest(leaderAic = LEADER_AIC): RabbitMQRequest {
  return RabbitMQRequestSchema.parse({
    id: "rpc-group-1",
    params: {
      protocol: "rabbitmq:4.0",
      group: { groupId: "group-1", leader: { aic: leaderAic }, partners: [] },
      server: { host: "localhost", port: 5671, vhost: "acps" },
      amqp: { exchange: "group-1", exchangeType: "fanout", routingKey: "" },
    },
  });
}

afterEach(() => {
  setLoggerFactory(undefined);
});

// 对应 tests/test_aip_group_identity.py
describe("test_aip_group_identity", () => {
  it("test_extract_amqp_user_id_accepts_str_and_bytes", () => {
    expect(extractAmqpUserId({ userId: LEADER_AIC })).toBe(LEADER_AIC);
    expect(extractAmqpUserId({ userId: Buffer.from(LEADER_AIC) })).toBe(LEADER_AIC);
    // amqplib 原生 ConsumeMessage 形状
    expect(extractAmqpUserId({ properties: { userId: LEADER_AIC.toLowerCase() } })).toBe(
      LEADER_AIC,
    );
    expect(extractAmqpUserId({ userId: null })).toBeNull();
    expect(() => extractAmqpUserId({ userId: "guest" })).toThrow(SenderIdentityMismatchError);
  });

  it("test_build_outgoing_amqp_user_id_returns_sender_when_enabled", () => {
    expect(
      buildOutgoingAmqpUserId(command(), { localAic: LEADER_AIC, identityBindingEnabled: true }),
    ).toBe(LEADER_AIC);
  });

  it("test_build_outgoing_amqp_user_id_returns_none_when_disabled", () => {
    expect(
      buildOutgoingAmqpUserId(command({ senderId: OTHER_AIC }), {
        localAic: LEADER_AIC,
        identityBindingEnabled: false,
      }),
    ).toBeNull();
  });

  it("test_assert_incoming_group_message_identity_passes", () => {
    expect(
      assertIncomingGroupMessageIdentity(command(), {
        amqpUserId: LEADER_AIC,
        expectedGroupId: "group-1",
      }),
    ).toBe(LEADER_AIC);
  });

  it("test_assert_incoming_group_message_identity_rejects_missing_user_id", () => {
    expect(() =>
      assertIncomingGroupMessageIdentity(command(), {
        amqpUserId: null,
        expectedGroupId: "group-1",
      }),
    ).toThrow(PeerAicMissingError);
  });

  it("test_assert_incoming_group_message_identity_rejects_sender_mismatch", () => {
    expect(() =>
      assertIncomingGroupMessageIdentity(command({ senderId: OTHER_AIC }), {
        amqpUserId: LEADER_AIC,
        expectedGroupId: "group-1",
      }),
    ).toThrow(SenderIdentityMismatchError);
  });

  it("test_assert_incoming_group_message_identity_rejects_group_mismatch", () => {
    expect(() =>
      assertIncomingGroupMessageIdentity(command({ groupId: "group-x" }), {
        amqpUserId: LEADER_AIC,
        expectedGroupId: "group-1",
      }),
    ).toThrow(SenderIdentityMismatchError);
    expect(() =>
      assertIncomingGroupMessageIdentity(
        { senderId: LEADER_AIC, groupId: "  " },
        { amqpUserId: LEADER_AIC },
      ),
    ).toThrow("groupId is required for group identity binding");
  });

  it("test_assert_direct_group_request_identity_passes", () => {
    expect(assertDirectGroupRequestIdentity(groupRequest(), { peerAic: LEADER_AIC })).toBe(
      LEADER_AIC,
    );
  });

  it("test_assert_direct_group_request_identity_rejects_missing_peer", () => {
    expect(() => assertDirectGroupRequestIdentity(groupRequest(), { peerAic: null })).toThrow(
      PeerAicMissingError,
    );
  });

  it("test_assert_direct_group_request_identity_rejects_leader_mismatch", () => {
    expect(() =>
      assertDirectGroupRequestIdentity(groupRequest(OTHER_AIC), { peerAic: LEADER_AIC }),
    ).toThrow(SenderIdentityMismatchError);
  });

  it("test_group_leader_publish_sets_amqp_user_id_when_enabled", async () => {
    const client = new GroupLeaderMqClient(LEADER_AIC, { identityBindingEnabled: true });
    const exchange = new FakeExchange(new Recorder(), "group-1");
    client._exchange = exchange;
    client._exchangeName = "group-1";
    client._groupId = "group-1";

    await client.publishMessage(command());
    expect(exchange.published[0]?.message.userId).toBe(LEADER_AIC);
  });

  it("test_group_partner_publish_sets_amqp_user_id_when_enabled", async () => {
    const client = new GroupPartnerMqClient(PARTNER_AIC, { identityBindingEnabled: true });
    const exchange = new FakeExchange(new Recorder(), "group-1");
    client._exchange = exchange;
    client._groupId = "group-1";

    await client._publishMessage(result());
    expect(exchange.published[0]?.message.userId).toBe(PARTNER_AIC);
  });

  it("test_group_partner_consume_rejects_missing_amqp_user_id_when_enabled", async () => {
    const client = new GroupPartnerMqClient(PARTNER_AIC, { identityBindingEnabled: true });
    const queue = new FakeQueue(new Recorder(), "partner.queue");
    client._queue = queue;
    client._queueName = "partner.queue";
    client._groupId = "group-1";
    await client._startConsuming();

    const message = makeIncoming(command(), { userId: null });
    const handler = queue.consumers[0];
    expect(handler).toBeDefined();
    await expect(handler!(message)).rejects.toBeInstanceOf(PeerAicMissingError);
    expect(message.rejected).toBe(1);
  });

  it("test_group_leader_consume_rejects_user_id_sender_mismatch_when_enabled", async () => {
    const client = new GroupLeaderMqClient(LEADER_AIC, { identityBindingEnabled: true });
    client._groupId = "group-1";
    client._exchangeName = "group-1";
    const queue = new FakeQueue(new Recorder(), "leader.queue");
    client._leaderQueue = queue;
    await client.startConsuming();

    const message = makeIncoming(result(), { userId: OTHER_AIC });
    await expect(queue.consumers[0]!(message)).rejects.toBeInstanceOf(SenderIdentityMismatchError);
  });

  it("test_group_partner_client_logs_warning_when_identity_binding_disabled", () => {
    const warnings: string[] = [];
    setLoggerFactory(() => ({
      debug: () => undefined,
      info: () => undefined,
      warn: (message: string) => warnings.push(message),
      error: () => undefined,
    }));
    new GroupPartnerMqClient(PARTNER_AIC, { identityBindingEnabled: false });
    expect(warnings.join("\n").toLowerCase()).toContain(
      "identity binding disabled for grouppartnermqclient",
    );
  });

  it("test_group_leader_client_logs_warning_when_identity_binding_disabled", () => {
    const warnings: string[] = [];
    setLoggerFactory(() => ({
      debug: () => undefined,
      info: () => undefined,
      warn: (message: string) => warnings.push(message),
      error: () => undefined,
    }));
    new GroupLeaderMqClient(LEADER_AIC, { identityBindingEnabled: false });
    expect(warnings.join("\n").toLowerCase()).toContain(
      "identity binding disabled for groupleadermqclient",
    );
  });
});
