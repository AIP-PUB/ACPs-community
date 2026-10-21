/**
 * 群组 MQ 收发 → MessageBody 组装的纯函数（供 GroupLeaderMqClient / GroupPartnerMqClient 复用）。
 *
 * 对应 Python `acps_sdk.amp._message_tap`（模块私有，不从 `acps-sdk/amp` 入口导出）。
 */

import { type ErrorInfo, type MessageBody, MessageBodySchema } from "./models.js";
import { reprStr } from "../internal/pyCompat.js";

const SETTLEMENT_EVENT_TYPES: ReadonlySet<string> = new Set([
  "ack",
  "nack",
  "reject",
  "timeout",
  "dead_letter",
]);

export interface BuildSendBodyOptions {
  system: string;
  exchange: string;
  virtualHost: string;
  routingKey: string;
  messageId: string;
  payloadSizeBytes: number;
  attributes?: Record<string, unknown> | null;
  error?: ErrorInfo | null;
}

/** producer 侧 send 事件 body 组装。 */
export function buildSendBody(options: BuildSendBodyOptions): MessageBody {
  return MessageBodySchema.parse({
    eventType: "send",
    operationName: "publish",
    system: options.system,
    destination: { name: options.exchange, kind: "exchange", virtualHost: options.virtualHost },
    routing: { key: options.routingKey },
    messageId: options.messageId,
    payloadSizeBytes: options.payloadSizeBytes,
    attributes: options.attributes,
    error: options.error,
  });
}

export interface BuildReceiveBodyOptions {
  system: string;
  exchange: string;
  virtualHost: string;
  routingKey: string;
  queueName: string;
  messageId: string;
  payloadSizeBytes: number;
  deliveryAttempt: number;
  attributes?: Record<string, unknown> | null;
}

/** consumer 侧 receive 事件 body 组装（destination.name 取 exchange，queue 记入 subscriptionName）。 */
export function buildReceiveBody(options: BuildReceiveBodyOptions): MessageBody {
  return MessageBodySchema.parse({
    eventType: "receive",
    operationName: "deliver",
    system: options.system,
    destination: { name: options.exchange, kind: "exchange", virtualHost: options.virtualHost },
    subscriptionName: options.queueName,
    routing: { key: options.routingKey },
    messageId: options.messageId,
    payloadSizeBytes: options.payloadSizeBytes,
    deliveryAttempt: options.deliveryAttempt,
    attributes: options.attributes,
  });
}

export interface BuildSettlementBodyOptions {
  system: string;
  exchange: string;
  virtualHost: string;
  queueName: string;
  messageId: string;
  deliveryAttempt: number;
  latencyMs: number;
  reason?: string | null;
  error?: ErrorInfo | null;
}

/**
 * 结算类事件（ack/nack/reject/timeout/dead_letter）body 组装。
 *
 * @throws Error eventType 不是结算类事件
 */
export function buildSettlementBody(
  eventType: string,
  options: BuildSettlementBodyOptions,
): MessageBody {
  if (!SETTLEMENT_EVENT_TYPES.has(eventType)) {
    throw new Error(`invalid settlement event_type: ${reprStr(eventType)}`);
  }

  const settlement =
    eventType === "nack" && options.reason !== undefined && options.reason !== null
      ? { latencyMs: options.latencyMs, reason: options.reason }
      : { latencyMs: options.latencyMs };

  return MessageBodySchema.parse({
    eventType,
    operationName: `basic.${eventType}`,
    system: options.system,
    destination: { name: options.exchange, kind: "exchange", virtualHost: options.virtualHost },
    subscriptionName: options.queueName,
    messageId: options.messageId,
    deliveryAttempt: options.deliveryAttempt,
    settlement,
    error: options.error,
  });
}
