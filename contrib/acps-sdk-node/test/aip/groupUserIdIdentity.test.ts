/**
 * 对应 tests/integration/test_aip_group_user_id_identity.py。
 *
 * Python 版放在 integration 目录，但只用 MagicMock 伪造 aio-pika 对象，不需要真实 broker，
 * 因此 Node 版作为单元测试。
 */

import { describe, expect, it, vi } from "vitest";

import { GroupLeader } from "../../src/aip/groupLeader.js";
import type { InboxGroupInvitation, InboxGroupInvitationError } from "../../src/aip/groupModel.js";
import { GroupPartnerMqClient } from "../../src/aip/groupPartner.js";
import { SenderIdentityMismatchError } from "../../src/aip/identity.js";
import { FakeConnection, FakeQueue, Recorder, makeIncoming } from "./groupFakes.js";

const LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4";
const PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5";
const OTHER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF548.0JU6";

function legacyInvitationBody(): Record<string, unknown> {
  return {
    type: "group-invitation",
    id: "invite-1",
    sentAt: "2026-06-24T00:00:00Z",
    senderRole: "leader",
    groupId: "group-1",
    protocol: "rabbitmq:4.2",
    expiresAt: "2026-06-24T00:05:00Z",
    invitationToken: "token-1",
    group: {
      groupId: "group-1",
      leader: { aic: LEADER_AIC },
      partners: [{ aic: PARTNER_AIC }],
    },
    amqp: { exchange: "group_foo", exchangeType: "fanout", routingKey: "" },
  };
}

function legacyInvitationErrorBody(partnerAic = PARTNER_AIC): Record<string, unknown> {
  return {
    type: "group-invitation-error",
    id: "invite-err-1",
    sentAt: "2026-06-24T00:00:01Z",
    senderRole: "partner",
    groupId: "group-1",
    partnerAic,
    invitationToken: "token-1",
    error: {
      code: -32020,
      message: "Invitation rejected by partner policy",
      data: { errorType: "INVITATION_REJECTED" },
    },
  };
}

async function leaderWithInbox(): Promise<{
  leader: GroupLeader;
  queue: FakeQueue;
  handled: InboxGroupInvitationError[];
}> {
  const leader = new GroupLeader(
    LEADER_AIC,
    { host: "mq.local", port: 5671, vhost: "acps" },
    {
      identityBindingEnabled: true,
    },
  );
  const connection = new FakeConnection(new Recorder());
  leader._connection = connection;
  const handled: InboxGroupInvitationError[] = [];
  leader._handleInvitationError = async (message) => {
    handled.push(message);
  };
  await leader._ensureInbox();
  const queue = connection.channels[0]?.queues.get(`inbox_${LEADER_AIC}`);
  if (!queue) throw new Error("inbox queue not declared");
  return { leader, queue, handled };
}

describe("test_aip_group_user_id_identity", () => {
  it("test_partner_inbox_consumer_normalizes_legacy_invitation_from_amqp_user_id", async () => {
    const client = new GroupPartnerMqClient(PARTNER_AIC, { identityBindingEnabled: true });
    const inboxQueue = new FakeQueue(new Recorder(), `inbox_${PARTNER_AIC}`);
    client.ensureInboxQueue = vi.fn(async () => inboxQueue);
    const received: InboxGroupInvitation[] = [];

    await client.startInboxConsuming(async (invitation) => {
      received.push(invitation);
    });
    await inboxQueue.consumers[0]!(makeIncoming(legacyInvitationBody(), { userId: LEADER_AIC }));

    const invitation = received[0];
    expect(invitation?.senderId).toBe(LEADER_AIC);
    expect(invitation?.groupId).toBe("group-1");
    expect(invitation?.group.leader.aic).toBe(LEADER_AIC);
  });

  it("partner inbox consumer drops (and acks) invitations whose user_id mismatches", async () => {
    const client = new GroupPartnerMqClient(PARTNER_AIC, { identityBindingEnabled: true });
    const inboxQueue = new FakeQueue(new Recorder(), `inbox_${PARTNER_AIC}`);
    client.ensureInboxQueue = vi.fn(async () => inboxQueue);
    const handler = vi.fn(async () => undefined);

    await client.startInboxConsuming(handler);
    const message = makeIncoming(legacyInvitationBody(), { userId: OTHER_AIC });
    await inboxQueue.consumers[0]!(message);

    expect(handler).not.toHaveBeenCalled();
    expect(message.acked).toBe(1);
  });

  it("test_leader_inbox_consumer_normalizes_legacy_invitation_error_from_amqp_user_id", async () => {
    const { queue, handled } = await leaderWithInbox();

    await queue.consumers[0]!(makeIncoming(legacyInvitationErrorBody(), { userId: PARTNER_AIC }));

    const errorMessage = handled[0];
    expect(errorMessage?.senderId).toBe(PARTNER_AIC);
    expect(errorMessage?.partnerAic).toBe(PARTNER_AIC);
  });

  it("test_leader_inbox_consumer_rejects_legacy_invitation_error_when_user_id_mismatches", async () => {
    const { queue, handled } = await leaderWithInbox();

    const message = makeIncoming(legacyInvitationErrorBody(), { userId: OTHER_AIC });
    await expect(queue.consumers[0]!(message)).rejects.toBeInstanceOf(SenderIdentityMismatchError);

    expect(handled).toHaveLength(0);
    expect(message.rejected).toBe(1);
  });

  it("leader inbox consumer ignores invalid JSON and other message types", async () => {
    const { queue, handled } = await leaderWithInbox();
    const invalid = makeIncoming(Buffer.from("not json"), { userId: PARTNER_AIC });
    await queue.consumers[0]!(invalid);
    const other = makeIncoming({ type: "group-invitation" }, { userId: PARTNER_AIC });
    await queue.consumers[0]!(other);
    expect(handled).toHaveLength(0);
    expect(invalid.acked).toBe(1);
    expect(other.acked).toBe(1);
  });
});
