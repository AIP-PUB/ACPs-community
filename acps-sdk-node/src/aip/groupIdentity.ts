/**
 * AIP 群组模式身份绑定（对应 Python `acps_sdk.aip.aip_group_identity`）
 *
 * 群组模式下，RabbitMQ 通过 mTLS + EXTERNAL 认证得到连接用户名（即证书 CN = AIC），
 * 并校验 AMQP `user_id` 属性等于该用户名。发送方把 `user_id` 设为自己的 AIC，
 * 接收方再校验消息体里的 `senderId` 与 `user_id` 一致，从而把 payload 身份绑定到 broker 认证身份。
 */

import {
  PeerAicMissingError,
  SenderIdentityMismatchError,
  assertSenderMatchesExpected,
  assertSenderMatchesPeer,
  normalizeAic,
  normalizeAndValidateAic,
} from "./identity.js";
import { pyStrip } from "../internal/pyCompat.js";
import { isRecord } from "./groupRuntime.js";

type SenderSource = Parameters<typeof assertSenderMatchesPeer>[0];
/** 带 groupId 的消息（Python 侧接受任意对象 / dict）。 */
type GroupMessageSource = string | { senderId?: unknown; groupId?: unknown } | null | undefined;

/**
 * 入站消息的最小形状：兼容本 SDK 的 `AmqpIncomingMessage`（`userId`）
 * 以及 amqplib 原生 `ConsumeMessage`（`properties.userId`）。
 */
export interface AmqpUserIdCarrier {
  userId?: unknown;
  properties?: { userId?: unknown } | null;
}

/** Extract a normalized AMQP user_id property from an incoming message. */
export function extractAmqpUserId(
  incomingMessage: AmqpUserIdCarrier | null | undefined,
): string | null {
  let userId: unknown = incomingMessage?.userId;
  if ((userId === undefined || userId === null) && isRecord(incomingMessage?.properties)) {
    userId = incomingMessage.properties.userId;
  }
  if (userId instanceof Uint8Array) {
    userId = Buffer.from(userId).toString("utf-8");
  }
  if (userId === null || userId === undefined) {
    return null;
  }
  return normalizeAndValidateAic(String(userId), { fieldName: "user_id" });
}

/** Build the outgoing AMQP user_id for a message. */
export function buildOutgoingAmqpUserId(
  messageOrSender: SenderSource,
  {
    localAic,
    identityBindingEnabled,
  }: { localAic: string | null | undefined; identityBindingEnabled: boolean },
): string | null {
  if (!identityBindingEnabled) {
    return null;
  }
  return assertSenderMatchesExpected(messageOrSender, localAic);
}

/** Assert AMQP-authenticated sender identity and group context. */
export function assertIncomingGroupMessageIdentity(
  messageOrSender: GroupMessageSource,
  {
    amqpUserId,
    expectedGroupId = null,
  }: { amqpUserId: string | null | undefined; expectedGroupId?: string | null },
): string {
  if (amqpUserId === null || amqpUserId === undefined) {
    throw new PeerAicMissingError("AMQP user_id is required for group identity binding");
  }
  const senderId = assertSenderMatchesPeer(messageOrSender, amqpUserId);

  const groupId = isRecord(messageOrSender)
    ? (messageOrSender as { groupId?: unknown }).groupId
    : undefined;
  const normalizedGroupId =
    groupId !== null && groupId !== undefined ? pyStrip(String(groupId)) : "";
  if (!normalizedGroupId) {
    throw new SenderIdentityMismatchError("groupId is required for group identity binding");
  }
  if (
    expectedGroupId !== null &&
    expectedGroupId !== undefined &&
    normalizedGroupId !== expectedGroupId
  ) {
    throw new SenderIdentityMismatchError(
      `groupId ${normalizedGroupId} does not match expected groupId ${expectedGroupId}`,
    );
  }
  return senderId;
}

function extractGroupLeaderAic(requestOrParams: unknown): string | null {
  if (!isRecord(requestOrParams)) {
    return null;
  }
  if (Object.hasOwn(requestOrParams, "params")) {
    return extractGroupLeaderAic(requestOrParams.params);
  }
  const group = requestOrParams.group;
  if (!isRecord(group)) {
    return null;
  }
  const leader = group.leader;
  if (!isRecord(leader)) {
    return null;
  }
  const aic = leader.aic;
  return aic !== null && aic !== undefined ? String(aic) : null;
}

/** Assert that a direct group invitation request is sent by the mTLS peer. */
export function assertDirectGroupRequestIdentity(
  requestOrParams: unknown,
  { peerAic }: { peerAic: string | null | undefined },
): string {
  const normalizedPeer = normalizeAic(peerAic);
  if (normalizedPeer === null) {
    throw new PeerAicMissingError(
      "peer AIC is required for direct group invitation identity binding",
    );
  }

  const leaderAic = normalizeAndValidateAic(extractGroupLeaderAic(requestOrParams), {
    fieldName: "group.leader.aic",
  });
  if (leaderAic !== normalizedPeer) {
    throw new SenderIdentityMismatchError(
      `group.leader.aic ${leaderAic} does not match peer AIC ${normalizedPeer}`,
    );
  }
  return leaderAic;
}
