import { afterEach, describe, expect, it, vi } from "vitest";

import { GroupAclClient, type GroupAclClientLike, GroupAclError } from "../../src/aip/groupAuth.js";
import {
  GroupLeader,
  GroupLeaderMqClient,
  GroupLeaderSession,
  PartnerInviteError,
  type PartnerConnectionInfo,
  PartnerNetworkError,
  PartnerResponseError,
} from "../../src/aip/groupLeader.js";
import {
  type GroupInfo,
  type GroupInvitationError,
  type InboxGroupInvitation,
  type AMQPConfig,
  InboxGroupInvitationErrorSchema,
  InboxGroupInvitationSchema,
  RabbitMQRequestSchema,
} from "../../src/aip/groupModel.js";
import { GroupPartnerMqClient, PartnerGroupState } from "../../src/aip/groupPartner.js";
import {
  CancelledError,
  Deferred,
  calculateInvitationExpiry,
  createTask,
  sleep,
} from "../../src/aip/groupRuntime.js";
import type { HttpTransport } from "../../src/internal/http.js";
import { setLoggerFactory } from "../../src/internal/logger.js";
import { FakeChannel, FakeConnector, FakeQueue, Recorder } from "./groupFakes.js";

const LEADER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4";
const PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JUE";
const MQ_CONFIG = { host: "mq.acps.example.com", port: 5671, vhost: "acps" };
/** 测试用 TLS 选项（只需非空，对应 Python `ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)`） */
const TLS = { rejectUnauthorized: true };

function buildGroupInfo(): GroupInfo {
  return {
    groupId: "group-session-1",
    leader: { aic: LEADER_AIC },
    partners: [{ aic: PARTNER_AIC }],
  };
}

function buildAmqpConfig(): AMQPConfig {
  return {
    exchange: `group_${LEADER_AIC}_group-session-1`,
    exchangeType: "fanout",
    routingKey: "",
  };
}

function fakeAcl(): GroupAclClientLike & { removeMember: ReturnType<typeof vi.fn> } {
  return {
    addMember: vi.fn(async () => undefined),
    removeMember: vi.fn(async () => undefined),
    deleteGroup: vi.fn(async () => undefined),
    closeMemberConnection: vi.fn(async () => undefined),
    aclose: vi.fn(async () => undefined),
  } as never;
}

const ENDPOINTS = (amqpHost: string) => ({
  endPoints: [
    { transport: "AMQP", url: `amqps://${amqpHost}:5671/acps?inbox=inbox_{AIC}` },
    { transport: "JSONRPC", url: "https://partner.example.com/group/rpc" },
  ],
});

// 对应 tests/test_group_invitation.py
describe("test_group_invitation", () => {
  it("test_group_leader_prefers_amqp_endpoint", async () => {
    const leader = new GroupLeader(LEADER_AIC, MQ_CONFIG);
    const session = new GroupLeaderSession(
      "session-1",
      "group-session-1",
      new GroupLeaderMqClient(LEADER_AIC),
    );
    leader.groupSessions.set("session-1", session);
    const called: Record<string, string> = {};

    leader._invitePartnerViaInbox = async ({ partnerInbox }) => {
      called.route = "inbox";
      called.inbox = partnerInbox;
    };
    leader._invitePartnerViaRpc = async () => {
      called.route = "rpc";
    };

    await leader.invitePartner(
      "session-1",
      { aic: PARTNER_AIC },
      { partnerAcsData: ENDPOINTS("mq.acps.example.com") },
    );

    expect(called).toEqual({ route: "inbox", inbox: `inbox_${PARTNER_AIC}` });
    expect(session.invitationRoutes.get(PARTNER_AIC)).toBe("inbox");
  });

  it("test_group_leader_falls_back_to_rpc_when_amqp_server_mismatch", async () => {
    const leader = new GroupLeader(LEADER_AIC, MQ_CONFIG);
    const session = new GroupLeaderSession(
      "session-1",
      "group-session-1",
      new GroupLeaderMqClient(LEADER_AIC),
    );
    leader.groupSessions.set("session-1", session);
    const called: Record<string, string> = {};

    leader._invitePartnerViaInbox = async () => {
      called.route = "inbox";
    };
    leader._invitePartnerViaRpc = async ({ rpcUrl }) => {
      called.route = "rpc";
      called.rpc_url = rpcUrl;
    };

    await leader.invitePartner(
      "session-1",
      { aic: PARTNER_AIC },
      { partnerAcsData: ENDPOINTS("other-mq.example.com") },
    );

    expect(called).toEqual({ route: "rpc", rpc_url: "https://partner.example.com/group/rpc" });
  });

  it("invite_partner without any endpoint raises PartnerInviteError", async () => {
    const leader = new GroupLeader(LEADER_AIC, MQ_CONFIG);
    leader.groupSessions.set(
      "session-1",
      new GroupLeaderSession("session-1", "group-session-1", new GroupLeaderMqClient(LEADER_AIC)),
    );
    await expect(leader.invitePartner("session-1", { aic: PARTNER_AIC })).rejects.toThrow(
      `No AMQP or JSONRPC invitation endpoint for partner ${PARTNER_AIC}`,
    );
    await expect(leader.invitePartner("missing", { aic: PARTNER_AIC })).rejects.toThrow(
      "Group session not found: missing",
    );
  });

  it("test_partner_direct_rpc_rejection_uses_invitation_error_code", async () => {
    const client = new GroupPartnerMqClient(PARTNER_AIC);
    client.setInvitationHandler(async () => false);

    const request = RabbitMQRequestSchema.parse({
      id: "req-1",
      params: {
        protocol: "rabbitmq:4.2",
        group: buildGroupInfo(),
        server: { host: "mq.acps.example.com", port: 5671, vhost: "acps" },
        amqp: buildAmqpConfig(),
      },
    });

    const response = await client.joinGroup(request);

    expect(response.error).toBeDefined();
    expect(response.error?.code).toBe(-32020);
    expect(response.error?.data?.errorType).toBe("INVITATION_REJECTED");
  });

  it("test_partner_invitation_handler_rejection_sends_inbox_error", async () => {
    const client = new GroupPartnerMqClient(PARTNER_AIC, { sslContext: TLS });
    client.setInvitationHandler(async () => false);
    const sent: { invitation?: InboxGroupInvitation; error?: GroupInvitationError } = {};
    client.sendInvitationError = async (invitation, error) => {
      sent.invitation = invitation;
      sent.error = error as GroupInvitationError;
    };

    const invitation = InboxGroupInvitationSchema.parse({
      protocol: "rabbitmq:4.2",
      expiresAt: calculateInvitationExpiry(300),
      invitationToken: "token-1",
      group: buildGroupInfo(),
      amqp: buildAmqpConfig(),
    });

    const joined = await client.joinGroupFromInvitation(invitation);

    expect(joined).toBe(false);
    expect(sent.invitation).toEqual(invitation);
    expect(sent.error?.code).toBe(-32020);
    expect(sent.error?.data?.errorType).toBe("INVITATION_REJECTED");
  });

  it("join_group_from_invitation ignores expired invitations", async () => {
    const client = new GroupPartnerMqClient(PARTNER_AIC, { sslContext: TLS });
    const handler = vi.fn(async () => true);
    client.setInvitationHandler(handler);
    const invitation = InboxGroupInvitationSchema.parse({
      protocol: "rabbitmq:4.2",
      expiresAt: "2020-01-01T00:00:00Z",
      invitationToken: "token-1",
      group: buildGroupInfo(),
      amqp: buildAmqpConfig(),
    });
    expect(await client.joinGroupFromInvitation(invitation)).toBe(false);
    expect(handler).not.toHaveBeenCalled();
  });

  it("test_partner_connect_uses_robust_connection_by_default", async () => {
    const connector = new FakeConnector();
    const client = new GroupPartnerMqClient(PARTNER_AIC, { connector });

    await client.connect();

    expect(connector.calls).toHaveLength(1);
    expect(connector.calls[0]?.robust).toBe(true);
    expect(client.connection).toBe(connector.connections[0]);
    expect(client._channel).toBe(connector.connections[0]?.channels[0]);
  });

  it("test_partner_connect_can_disable_robust_reconnect", async () => {
    const connector = new FakeConnector();
    const client = new GroupPartnerMqClient(PARTNER_AIC, { robustConnection: false, connector });

    await client.connect();

    expect(connector.calls).toHaveLength(1);
    expect(connector.calls[0]?.robust).toBe(false);
    expect(client.connection).toBe(connector.connections[0]);
    expect(client._channel).toBe(connector.connections[0]?.channels[0]);
  });

  it("test_partner_leave_group_announces_exit_before_cleanup", async () => {
    const client = new GroupPartnerMqClient(PARTNER_AIC);
    client._state = PartnerGroupState.JOINED;
    client._groupId = "group-session-1";
    client._exchange = { name: "x", publish: vi.fn() };
    client._muted = false;

    const published: Array<Record<string, unknown>> = [];
    const statusArgs: Record<string, unknown> = {};
    client._publishMessage = async (message) => {
      published.push(message as unknown as Record<string, unknown>);
    };
    client.sendStatusResult = async (connected, muted, { sessionId = null } = {}) => {
      statusArgs.connected = connected;
      statusArgs.muted = muted;
      statusArgs.session_id = sessionId;
    };
    const cleanup = vi.fn(async () => undefined);
    client._cleanupGroupResources = cleanup;

    await client.leaveGroup({ sessionId: "session-1" });

    const message = published[0];
    expect(message?.senderRole).toBe("partner");
    expect(message?.senderId).toBe(PARTNER_AIC);
    expect(message?.command).toBe("leave-group");
    expect(message?.mentions).toEqual([PARTNER_AIC]);
    expect(message?.groupId).toBe("group-session-1");
    expect(message?.sessionId).toBe("session-1");
    expect(statusArgs).toEqual({ connected: false, muted: false, session_id: "session-1" });
    expect(cleanup).toHaveBeenCalledTimes(1);
  });

  it("test_partner_cleanup_cancels_registered_consumer_before_queue_delete", async () => {
    const client = new GroupPartnerMqClient(PARTNER_AIC);
    client._state = PartnerGroupState.JOINED;
    client._groupId = "group-session-1";

    const recorder = new Recorder();
    const queue = new FakeQueue(recorder, "queue-1");
    client._queue = queue;

    await client._startConsuming();

    expect(client._consumerTag).toBe("ctag-queue-1");
    expect(queue.consumers).toHaveLength(1);

    await client._cleanupGroupResources();

    expect(recorder.ops.filter((op) => op.op !== "consume")).toEqual([
      { op: "cancel", queue: "queue-1", consumer_tag: "ctag-queue-1" },
      { op: "queue_object_delete", queue: "queue-1", if_unused: false, if_empty: false },
    ]);
    expect(client._consumerTag).toBeNull();
    expect(client.state).toBe(PartnerGroupState.DISCONNECTED);
  });

  it("test_partner_cleanup_resets_group_state_and_notifies_disconnect_handler", async () => {
    const client = new GroupPartnerMqClient(PARTNER_AIC);
    client._state = PartnerGroupState.JOINED;
    client._groupId = "group-session-1";
    client._groupInfo = buildGroupInfo();
    client._exchangeName = "exchange-1";
    client._queueName = "queue-1";
    client._muted = true;
    client._pendingLeave = true;
    client._pendingLeaveSessionId = "session-1";
    client._taskStates.set("task-1", "working");

    const recorder = new Recorder();
    const channel = new FakeChannel(recorder);
    client._channel = channel;
    const connectionClose = vi.fn(async () => undefined);
    const connection = { isClosed: false, channel: vi.fn(), close: connectionClose };
    client._connection = connection;
    client._connectionOwner = true;

    const disconnectHandler = vi.fn(async () => undefined);
    client.setDisconnectHandler(disconnectHandler);

    await client._cleanupGroupResources();

    expect(channel.isClosed).toBe(true);
    expect(connectionClose).toHaveBeenCalledTimes(1);
    expect(disconnectHandler).toHaveBeenCalledTimes(1);
    expect(disconnectHandler).toHaveBeenCalledWith(client, "group-session-1");
    expect(client.state).toBe(PartnerGroupState.DISCONNECTED);
    expect(client.groupId).toBeNull();
    expect(client.queueName).toBeNull();
    expect(client.connection).toBeNull();
    expect(client._groupInfo).toBeNull();
    expect(client._exchangeName).toBeNull();
    expect(client._connectionName).toBeNull();
    expect(client._vhost).toBeNull();
    expect(client._nodeName).toBeNull();
    expect(client._pendingLeave).toBe(false);
    expect(client._pendingLeaveSessionId).toBeNull();
    expect(client.isMuted).toBe(false);
    expect(client._taskStates.size).toBe(0);
  });

  it("test_partner_handle_leave_request_continues_cleanup_when_status_publish_fails", async () => {
    const client = new GroupPartnerMqClient(PARTNER_AIC);
    client._state = PartnerGroupState.JOINED;
    client._groupId = "group-session-1";
    client._pendingLeaveSessionId = "session-1";
    const cleanup = vi.fn(async () => undefined);
    client._cleanupGroupResources = cleanup;
    client.sendStatusResult = async () => {
      throw new Error("delivery nack");
    };

    await client._handleLeaveRequest();

    expect(cleanup).toHaveBeenCalledTimes(1);
    expect(client._pendingLeaveSessionId).toBeNull();
  });

  it("test_partner_access_token_four_state_logic", () => {
    const client = new GroupPartnerMqClient(PARTNER_AIC, { sslContext: TLS });

    client._prepareConnectionFromServer({ host: "mq.acps.example.com", port: 5671, vhost: "acps" });
    expect(client.rabbitmqUser).toBeNull();
    expect(client.rabbitmqPassword).toBeNull();

    client._prepareConnectionFromServer({
      host: "mq.acps.example.com",
      port: 5671,
      vhost: "acps",
      accessToken: undefined,
    });
    expect(client.rabbitmqUser).toBeNull();
    expect(client.rabbitmqPassword).toBeNull();

    client._prepareConnectionFromServer({
      host: "mq.acps.example.com",
      port: 5672,
      vhost: "/",
      accessToken: "guest:guest",
    });
    expect(client.rabbitmqUser).toBe("guest");
    expect(client.rabbitmqPassword).toBe("guest");

    expect(() =>
      client._prepareConnectionFromServer({
        host: "mq.acps.example.com",
        port: 5671,
        vhost: "acps",
        accessToken: "",
      }),
    ).toThrow(/Malformed accessToken/);
    expect(() =>
      client._prepareConnectionFromServer({
        host: "mq",
        port: 5671,
        vhost: "acps",
        accessToken: "no-colon",
      }),
    ).toThrow("Malformed accessToken: expected user:password");
    const noTls = new GroupPartnerMqClient(PARTNER_AIC);
    expect(() =>
      noTls._prepareConnectionFromServer({ host: "mq", port: 5671, vhost: "acps" }),
    ).toThrow("ssl_context is required when accessToken is missing or null");
  });

  it("test_leader_invitation_timeout_cleans_acl_and_fails_pending_future", async () => {
    const leader = new GroupLeader(LEADER_AIC, MQ_CONFIG, { invitationTimeoutSeconds: 0 });
    const acl = fakeAcl();
    leader._groupAclClient = acl;

    const future = new Deferred<PartnerConnectionInfo>();
    const timeoutTask = createTask((signal) => sleep(3_600_000, signal));
    leader._pendingInvitations.set(PARTNER_AIC, {
      sessionId: "session-1",
      partnerAic: PARTNER_AIC,
      invitationToken: "token-1",
      future,
      timeoutTask,
    });

    await leader._watchInvitationTimeout({
      sessionId: "session-1",
      groupId: "group-session-1",
      partnerAic: PARTNER_AIC,
      invitationToken: "token-1",
    });

    expect(acl.removeMember).toHaveBeenCalledTimes(1);
    expect(acl.removeMember).toHaveBeenCalledWith({
      leaderAic: LEADER_AIC,
      groupId: "group-session-1",
      memberAic: PARTNER_AIC,
    });
    expect(future.done()).toBe(true);
    expect(future.exception()).toBeInstanceOf(PartnerInviteError);
    expect(leader._pendingInvitations.size).toBe(0);
    await expect(timeoutTask.promise).rejects.toBeInstanceOf(CancelledError);
  });

  it("test_leader_invitation_error_token_mismatch_preserves_pending_state", async () => {
    const leader = new GroupLeader(LEADER_AIC, MQ_CONFIG);
    const acl = fakeAcl();
    leader._groupAclClient = acl;

    const future = new Deferred<PartnerConnectionInfo>();
    const timeoutTask = createTask((signal) => sleep(3_600_000, signal));
    leader._pendingInvitations.set(PARTNER_AIC, {
      sessionId: "session-1",
      partnerAic: PARTNER_AIC,
      invitationToken: "token-expected",
      future,
      timeoutTask,
    });

    await leader._handleInvitationError(
      InboxGroupInvitationErrorSchema.parse({
        groupId: "group-session-1",
        partnerAic: PARTNER_AIC,
        invitationToken: "token-actual",
        error: { code: -32020, message: "rejected", data: { errorType: "INVITATION_REJECTED" } },
      }),
    );

    expect(acl.removeMember).not.toHaveBeenCalled();
    expect(future.done()).toBe(false);
    expect(leader._pendingInvitations.has(PARTNER_AIC)).toBe(true);

    timeoutTask.cancel();
    await expect(timeoutTask.promise).rejects.toBeInstanceOf(CancelledError);
  });
});

describe("invitation error mapping", () => {
  async function handle(error: Record<string, unknown>): Promise<unknown> {
    const leader = new GroupLeader(LEADER_AIC, MQ_CONFIG);
    leader._groupAclClient = fakeAcl();
    const future = new Deferred<PartnerConnectionInfo>();
    leader._pendingInvitations.set(PARTNER_AIC, {
      sessionId: "s",
      partnerAic: PARTNER_AIC,
      invitationToken: "t",
      future,
      timeoutTask: createTask(async () => undefined),
    });
    await leader._handleInvitationError(
      InboxGroupInvitationErrorSchema.parse({
        groupId: "g",
        partnerAic: PARTNER_AIC,
        invitationToken: "t",
        error,
      }),
    );
    expect(leader._pendingInvitations.size).toBe(0);
    return future.exception();
  }

  it("maps rejection to PartnerResponseError and other errors to PartnerNetworkError", async () => {
    const rejected = await handle({ code: -32020, message: "rejected" });
    expect(rejected).toBeInstanceOf(PartnerResponseError);
    expect((rejected as Error).message).toBe("rejected");

    const failed = await handle({
      code: -32021,
      message: "Connection failed",
      data: { errorType: "CONNECTION_FAILED", details: { host: "mq", port: 5671 } },
    });
    expect(failed).toBeInstanceOf(PartnerNetworkError);
    // Python: f": {details}" 使用 dict 的 str()
    expect((failed as Error).message).toBe("Connection failed: {'host': 'mq', 'port': 5671}");

    const emptyDetails = await handle({
      code: -32021,
      message: "x",
      data: { errorType: "E", details: {} },
    });
    expect((emptyDetails as Error).message).toBe("x");
  });
});

/**
 * 修复（与 Python 行为不同）：ACL 服务不可达时，inbox 邀请不能永远挂起。
 * Python 版里 httpx 的传输层异常不是 GroupAclError，会在设置 future 之前逃出超时任务，
 * 调用方永远等待，挂起记录也不会被移除。
 */
describe("inbox invitation settlement when the ACL service is unreachable", () => {
  afterEach(() => {
    setLoggerFactory(undefined);
  });

  /** PUT 成功、DELETE 抛出传输层错误的 ACL 服务 */
  function flakyAclTransport(): { transport: HttpTransport; methods: string[] } {
    const methods: string[] = [];
    const transport: HttpTransport = async (request) => {
      methods.push(request.method);
      if (request.method === "DELETE") {
        throw new Error("connect ECONNREFUSED 127.0.0.1:9");
      }
      return { status: 204, headers: {}, body: Buffer.alloc(0), peerCertificate: null };
    };
    return { transport, methods };
  }

  function leaderWithSession(transport: HttpTransport): GroupLeader {
    const leader = new GroupLeader(
      LEADER_AIC,
      { ...MQ_CONFIG, auth_service_url: "https://auth.example.com" },
      { invitationTimeoutSeconds: 0, aclTransport: transport },
    );
    const client = new GroupLeaderMqClient(LEADER_AIC);
    client.inviteViaInbox = vi.fn(async () => ({}) as InboxGroupInvitation);
    leader.groupSessions.set(
      "session-1",
      new GroupLeaderSession("session-1", "group-session-1", client),
    );
    return leader;
  }

  it("GroupAclClient wraps transport failures in GroupAclError (with cause)", async () => {
    const { transport } = flakyAclTransport();
    const acl = new GroupAclClient({ baseUrl: "https://auth.example.com", transport });
    const error = await acl
      .removeMember({ leaderAic: LEADER_AIC, groupId: "g", memberAic: PARTNER_AIC })
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(GroupAclError);
    expect((error as Error).message).toBe(
      `DELETE /groups/${LEADER_AIC}/g/members/${PARTNER_AIC} failed: connect ECONNREFUSED 127.0.0.1:9`,
    );
    expect(((error as Error).cause as Error).message).toContain("ECONNREFUSED");
  });

  it("times out, settles the invitation and clears the pending entry even if ACL cleanup fails", async () => {
    const { transport, methods } = flakyAclTransport();
    const leader = leaderWithSession(transport);
    const invite = () =>
      leader.invitePartner(
        "session-1",
        { aic: PARTNER_AIC },
        { partnerAcsData: ENDPOINTS("mq.acps.example.com") },
      );

    await expect(invite()).rejects.toThrow(
      `Invitation timed out for partner ${PARTNER_AIC} in group group-session-1`,
    );
    expect(leader._pendingInvitations.size).toBe(0);
    // 再次邀请不会因为残留的挂起记录而报 "already has a pending invitation"
    await expect(invite()).rejects.toBeInstanceOf(PartnerInviteError);
    await expect(invite()).rejects.not.toThrow("already has a pending invitation");
    expect(methods.filter((method) => method === "PUT")).toHaveLength(3);
  });

  it("_watchInvitationTimeout settles the future even when ACL cleanup throws a non-ACL error", async () => {
    const leader = new GroupLeader(LEADER_AIC, MQ_CONFIG, { invitationTimeoutSeconds: 0 });
    const acl = fakeAcl();
    acl.removeMember.mockRejectedValue(new TypeError("unexpected"));
    leader._groupAclClient = acl;
    const future = new Deferred<PartnerConnectionInfo>();
    leader._pendingInvitations.set(PARTNER_AIC, {
      sessionId: "session-1",
      partnerAic: PARTNER_AIC,
      invitationToken: "token-1",
      future,
      timeoutTask: createTask(async () => undefined),
    });

    await leader._watchInvitationTimeout({
      sessionId: "session-1",
      groupId: "group-session-1",
      partnerAic: PARTNER_AIC,
      invitationToken: "token-1",
    });

    expect(future.exception()).toBeInstanceOf(PartnerInviteError);
    expect(leader._pendingInvitations.size).toBe(0);
  });

  it("_handleInvitationError settles the future even when ACL cleanup throws", async () => {
    const leader = new GroupLeader(LEADER_AIC, MQ_CONFIG);
    const acl = fakeAcl();
    acl.removeMember.mockRejectedValue(new Error("socket hang up"));
    leader._groupAclClient = acl;
    const future = new Deferred<PartnerConnectionInfo>();
    leader._pendingInvitations.set(PARTNER_AIC, {
      sessionId: "session-1",
      partnerAic: PARTNER_AIC,
      invitationToken: "token-1",
      future,
      timeoutTask: createTask(async () => undefined),
    });

    await leader._handleInvitationError(
      InboxGroupInvitationErrorSchema.parse({
        groupId: "group-session-1",
        partnerAic: PARTNER_AIC,
        invitationToken: "token-1",
        error: { code: -32020, message: "rejected" },
      }),
    );

    expect(future.exception()).toBeInstanceOf(PartnerResponseError);
    expect(leader._pendingInvitations.size).toBe(0);
  });

  it("a failing ACL rollback does not replace the original invitation error", async () => {
    const leader = new GroupLeader(LEADER_AIC, MQ_CONFIG);
    const acl = fakeAcl();
    acl.removeMember.mockRejectedValue(new TypeError("rollback exploded"));
    leader._groupAclClient = acl;
    const client = new GroupLeaderMqClient(LEADER_AIC);
    const original = new PartnerNetworkError("Network error: refused");
    client.invitePartner = vi.fn(async () => {
      throw original;
    });
    const session = new GroupLeaderSession("session-1", "group-session-1", client);

    await expect(
      leader._invitePartnerViaRpc({
        session,
        partnerAcs: { aic: PARTNER_AIC },
        rpcUrl: "https://p/rpc",
      }),
    ).rejects.toBe(original);
  });

  it("does not write invitation tokens to the logs", async () => {
    const lines: string[] = [];
    const capture = (message: string) => lines.push(message);
    setLoggerFactory(() => ({ debug: capture, info: capture, warn: capture, error: capture }));
    const leader = new GroupLeader(LEADER_AIC, MQ_CONFIG);
    leader._pendingInvitations.set(PARTNER_AIC, {
      sessionId: "session-1",
      partnerAic: PARTNER_AIC,
      invitationToken: "super-secret-expected",
      future: new Deferred<PartnerConnectionInfo>(),
      timeoutTask: createTask(async () => undefined),
    });

    await leader._handleInvitationError(
      InboxGroupInvitationErrorSchema.parse({
        groupId: "group-session-1",
        partnerAic: PARTNER_AIC,
        invitationToken: "super-secret-actual",
        error: { code: -32020, message: "rejected" },
      }),
    );

    const text = lines.join("\n");
    expect(text).toContain("event=invitation_error_token_mismatch");
    expect(text).toContain("<redacted len=");
    expect(text).not.toContain("super-secret");
  });
});
