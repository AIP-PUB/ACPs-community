/** 对应 Python tests/amp/test_message_tap.py：messageTap 纯函数。 */

import { describe, expect, it } from "vitest";

import * as tap from "../../src/amp/messageTap.js";

const common = { system: "rabbitmq", exchange: "group.abc", virtualHost: "acps" };

describe("messageTap", () => {
  it("test_build_send_body_exchange_fanout", () => {
    const body = tap.buildSendBody({
      ...common,
      routingKey: "",
      messageId: "cmd-001",
      payloadSizeBytes: 256,
    });

    expect(body.eventType).toBe("send");
    expect(body.destination.kind).toBe("exchange");
    expect(body.destination.name).toBe("group.abc");
    expect(body.destination.virtualHost).toBe("acps");
    expect(body.routing).toBeDefined();
    expect(body.routing?.key).toBe("");
    expect(body.deliveryAttempt).toBeUndefined();
    expect(body.subscriptionName).toBeUndefined();
  });

  it("test_build_receive_body_uses_exchange_not_queue", () => {
    const body = tap.buildReceiveBody({
      ...common,
      routingKey: "",
      queueName: "partner.queue.1",
      messageId: "cmd-001",
      payloadSizeBytes: 256,
      deliveryAttempt: 1,
    });

    expect(body.eventType).toBe("receive");
    expect(body.destination.name).toBe("group.abc");
    expect(body.destination.kind).toBe("exchange");
    expect(body.subscriptionName).toBe("partner.queue.1");
    expect(body.deliveryAttempt).toBe(1);
  });

  it("test_build_send_and_receive_share_destination_name", () => {
    const sendBody = tap.buildSendBody({
      ...common,
      routingKey: "",
      messageId: "cmd-001",
      payloadSizeBytes: 100,
    });
    const recvBody = tap.buildReceiveBody({
      ...common,
      routingKey: "",
      queueName: "partner.queue.1",
      messageId: "cmd-001",
      payloadSizeBytes: 100,
      deliveryAttempt: 1,
    });
    expect(sendBody.destination).toEqual(recvBody.destination);
  });

  it("test_build_settlement_body_ack", () => {
    const body = tap.buildSettlementBody("ack", {
      ...common,
      queueName: "partner.queue.1",
      messageId: "cmd-001",
      deliveryAttempt: 1,
      latencyMs: 12.5,
    });

    expect(body.eventType).toBe("ack");
    expect(body.settlement).toBeDefined();
    expect(body.settlement?.latencyMs).toBe(12.5);
    expect(body.settlement?.reason).toBeUndefined();
  });

  it("test_build_settlement_body_nack_with_reason", () => {
    const body = tap.buildSettlementBody("nack", {
      ...common,
      queueName: "partner.queue.1",
      messageId: "cmd-001",
      deliveryAttempt: 2,
      latencyMs: 5.0,
      reason: "handler failed",
    });

    expect(body.eventType).toBe("nack");
    expect(body.settlement?.reason).toBe("handler failed");
  });

  it("reason is ignored for non-nack settlements (same as Python)", () => {
    const body = tap.buildSettlementBody("reject", {
      ...common,
      queueName: "q",
      messageId: "m",
      deliveryAttempt: 1,
      latencyMs: 1,
      reason: "ignored",
    });
    expect(body.settlement).toEqual({ latencyMs: 1 });
  });

  it("test_build_settlement_body_invalid_event_type", () => {
    expect(() =>
      tap.buildSettlementBody("send", {
        ...common,
        queueName: "q",
        messageId: "cmd-001",
        deliveryAttempt: 1,
        latencyMs: 1.0,
      }),
    ).toThrow(/invalid settlement event_type: 'send'/);
  });
});
