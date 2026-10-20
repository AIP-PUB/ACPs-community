/**
 * 群组模式加固（Node 版相对 Python 的修复，见各用例说明）：并发邀请、只接受 Leader 的命令、
 * 按会话路由、迟到的加入结果、清理与 inbox 生命周期、重入、日志脱敏等。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { TaskCommandSchema, type TaskResult, TaskResultSchema } from "../../src/aip/baseModel.js";
import type { AmqpChannel, AmqpQueue } from "../../src/aip/groupAmqp.js";
import { GroupAclClient, type GroupAclClientLike } from "../../src/aip/groupAuth.js";
import {
  GroupLeader,
  GroupLeaderMqClient,
  GroupLeaderSession,
  type PartnerConnectionInfo,
  PartnerInviteError,
} from "../../src/aip/groupLeader.js";
import {
  type GroupInfo,
  GroupMgmtCommandSchema,
  GroupMgmtResultSchema,
  type InboxGroupInvitation,
  InboxGroupInvitationSchema,
  RabbitMQRequestSchema,
} from "../../src/aip/groupModel.js";
import { GroupPartnerMqClient, PartnerGroupState } from "../../src/aip/groupPartner.js";
import { Deferred, createTask, sleep } from "../../src/aip/groupRuntime.js";
import type { HttpTransport } from "../../src/internal/http.js";
import { setLoggerFactory } from "../../src/internal/logger.js";
import {
  FakeChannel,
  FakeConnection,
  FakeConnector,
  FakeExchange,
  FakeQueue,
  LEADER_AIC,
  OTHER_AIC,
  PARTNER_AIC,
  Recorder,
  makeIncoming,
} from "./groupFakes.js";

const MQ_CONFIG = { host: "mq.acps.example.com", port: 5671, vhost: "acps" };
const ENDPOINTS = {
  endPoints: [
    { transport: "AMQP", url: "amqps://mq.acps.example.com:5671/acps?inbox=inbox_{AIC}" },
  ],
};

afterEach(() => {
  setLoggerFactory(undefined);
});

function captureLogs(): string[] {
  const lines: string[] = [];
  const push = (message: string) => lines.push(message);
  setLoggerFactory(() => ({ debug: push, info: push, warn: push, error: push }));
  return lines;
}

function fakeAcl(overrides: Partial<GroupAclClientLike> = {}): GroupAclClientLike & {
  removeMember: ReturnType<typeof vi.fn>;
  deleteGroup: ReturnType<typeof vi.fn>;
} {
  return {
    addMember: vi.fn(async () => undefined),
    removeMember: vi.fn(async () => undefined),
    deleteGroup: vi.fn(async () => undefined),
    closeMemberConnection: vi.fn(async () => undefined),
    aclose: vi.fn(async () => undefined),
    ...overrides,
  } as never;
}

function groupInfo(groupId = "group-1"): GroupInfo {
  return { groupId, leader: { aic: LEADER_AIC }, partners: [{ aic: PARTNER_AIC }] };
}

function connectedResult(senderId: string, groupId: string, sessionId?: string) {
  return GroupMgmtResultSchema.parse({
    id: `r-${senderId}`,
    sentAt: "t",
    senderRole: "partner",
    senderId,
    groupId,
    sessionId,
    status: { connected: true, muted: false },
  });
}

function taskResult(groupId: string, sessionId: string): TaskResult {
  return TaskResultSchema.parse({
    id: "res-1",
    sentAt: "t",
    senderRole: "partner",
    senderId: PARTNER_AIC,
    groupId,
    sessionId,
    taskId: "task-1",
    status: { state: "working", stateChangedAt: "t" },
  });
}

function leaderWithInboxSession(aclTransport: HttpTransport): {
  leader: GroupLeader;
  session: GroupLeaderSession;
} {
  const leader = new GroupLeader(
    LEADER_AIC,
    { ...MQ_CONFIG, auth_service_url: "https://auth.example.com" },
    { aclTransport, invitationTimeoutSeconds: 3600 },
  );
  const client = new GroupLeaderMqClient(LEADER_AIC);
  client._groupId = "group-1";
  client.inviteViaInbox = vi.fn(async () => ({}) as InboxGroupInvitation);
  const session = new GroupLeaderSession("session-1", "group-1", client);
  leader.groupSessions.set("session-1", session);
  return { leader, session };
}

describe("concurrent inbox invitations (Node fix)", () => {
  it("reserves the pending slot before the ACL call, so a concurrent invite is rejected instead of overwriting", async () => {
    const transport: HttpTransport = async () => {
      await sleep(20);
      return { status: 204, headers: {}, body: Buffer.alloc(0), peerCertificate: null };
    };
    const { leader, session } = leaderWithInboxSession(transport);
    const invite = () =>
      leader.invitePartner("session-1", { aic: PARTNER_AIC }, { partnerAcsData: ENDPOINTS });

    const first = invite();
    const second = invite();
    await expect(second).rejects.toThrow(`Partner ${PARTNER_AIC} already has a pending invitation`);
    expect(leader._pendingInvitations.size).toBe(1);

    await vi.waitFor(() => expect(session.leaderMqClient.inviteViaInbox).toHaveBeenCalledTimes(1));
    await leader._handleGroupMessage(connectedResult(PARTNER_AIC, "group-1"), session);
    await expect(first).resolves.toBe(true);
    expect(leader._pendingInvitations.size).toBe(0);
  });

  it("drops the reservation when the ACL call fails", async () => {
    let calls = 0;
    const transport: HttpTransport = async () => {
      calls += 1;
      if (calls === 1) throw new Error("connect ECONNREFUSED");
      return { status: 204, headers: {}, body: Buffer.alloc(0), peerCertificate: null };
    };
    const { leader } = leaderWithInboxSession(transport);
    await expect(
      leader.invitePartner("session-1", { aic: PARTNER_AIC }, { partnerAcsData: ENDPOINTS }),
    ).rejects.toThrow("ECONNREFUSED");
    expect(leader._pendingInvitations.size).toBe(0);
  });
});

describe("partners only obey the Leader (Node fix, AIP §4.4 / §7.2.2)", () => {
  async function joinedPartner({ identityBindingEnabled = true } = {}) {
    const partner = new GroupPartnerMqClient(PARTNER_AIC, { identityBindingEnabled });
    const recorder = new Recorder();
    const queue = new FakeQueue(recorder, "partner.queue");
    partner._queue = queue;
    partner._queueName = "partner.queue";
    partner._groupId = "group-1";
    partner._groupInfo = groupInfo();
    partner._exchange = new FakeExchange(recorder, "group-x");
    partner._state = PartnerGroupState.JOINED;
    const commands: unknown[] = [];
    const mgmt: unknown[] = [];
    partner.setCommandHandler(async (command) => {
      commands.push(command);
    });
    partner.setMgmtCommandHandler(async (command) => {
      mgmt.push(command);
    });
    await partner._startConsuming();
    const deliver = queue.consumers[0]!;
    return { partner, recorder, deliver, commands, mgmt };
  }

  function mgmtCommand(senderId: string, command: string) {
    return GroupMgmtCommandSchema.parse({
      id: `m-${command}`,
      sentAt: "t",
      senderRole: senderId === LEADER_AIC ? "leader" : "partner",
      senderId,
      groupId: "group-1",
      command,
      mentions: [PARTNER_AIC],
    });
  }

  function taskCommand(senderId: string) {
    return TaskCommandSchema.parse({
      id: "c-1",
      sentAt: "t",
      senderRole: "leader",
      senderId,
      groupId: "group-1",
      sessionId: "s",
      taskId: "task-1",
      command: "start",
    });
  }

  it("ignores leave / mute / status / task commands from other members", async () => {
    const { partner, recorder, deliver, commands, mgmt } = await joinedPartner();
    for (const command of ["leave-group", "disband", "mute", "get-status"]) {
      await deliver(makeIncoming(mgmtCommand(OTHER_AIC, command), { userId: OTHER_AIC }));
    }
    await deliver(makeIncoming(taskCommand(OTHER_AIC), { userId: OTHER_AIC }));

    expect(partner._pendingLeave).toBe(false);
    expect(partner.isMuted).toBe(false);
    expect(partner.state).toBe(PartnerGroupState.JOINED);
    expect(commands).toEqual([]);
    expect(mgmt).toEqual([]);
    expect(recorder.ops.filter((op) => op.op === "publish")).toEqual([]);
  });

  it("still obeys the Leader", async () => {
    const { partner, recorder, deliver, commands, mgmt } = await joinedPartner();
    await deliver(makeIncoming(mgmtCommand(LEADER_AIC, "mute"), { userId: LEADER_AIC }));
    await deliver(makeIncoming(taskCommand(LEADER_AIC), { userId: LEADER_AIC }));
    expect(partner.isMuted).toBe(true);
    expect(mgmt).toHaveLength(1);
    expect(commands).toHaveLength(1);
    expect(recorder.ops.filter((op) => op.op === "publish")).toHaveLength(1); // 静音后的状态响应
  });

  it("keeps the Python behaviour when identity binding is disabled (senderId is not authenticated)", async () => {
    const { partner, deliver } = await joinedPartner({ identityBindingEnabled: false });
    await deliver(makeIncoming(mgmtCommand(OTHER_AIC, "mute")));
    expect(partner.isMuted).toBe(true);
  });
});

describe("leader routes group messages by the receiving session, not the payload sessionId (Node fix)", () => {
  function twoSessions() {
    const leader = new GroupLeader(LEADER_AIC, MQ_CONFIG);
    const clientA = new GroupLeaderMqClient(LEADER_AIC);
    clientA._groupId = "group-a";
    const clientB = new GroupLeaderMqClient(LEADER_AIC);
    clientB._groupId = "group-b";
    const sessionA = new GroupLeaderSession("A", "group-a", clientA);
    const sessionB = new GroupLeaderSession("B", "group-b", clientB);
    leader.groupSessions.set("A", sessionA);
    leader.groupSessions.set("B", sessionB);
    return { leader, sessionA, sessionB };
  }

  it("a member of group A cannot inject into session B via sessionId", async () => {
    const { leader, sessionA, sessionB } = twoSessions();
    await leader._handleGroupMessage(taskResult("group-a", "B"), sessionA);
    expect(sessionA.messageHistory).toHaveLength(1);
    expect(sessionB.messageHistory).toHaveLength(0);

    // 直接调用（无接收会话）时：sessionId 指向的会话 groupId 不一致 → 按 groupId 路由
    await leader._handleGroupMessage(taskResult("group-a", "B"));
    expect(sessionA.messageHistory).toHaveLength(2);
    expect(sessionB.messageHistory).toHaveLength(0);

    // groupId 与接收会话不一致的消息直接丢弃
    await leader._handleGroupMessage(taskResult("group-b", "B"), sessionA);
    expect(sessionA.messageHistory).toHaveLength(2);
    expect(sessionB.messageHistory).toHaveLength(0);
  });

  it("cannot resolve a pending invitation that belongs to another session", async () => {
    const { leader, sessionA } = twoSessions();
    const future = new Deferred<PartnerConnectionInfo>();
    leader._pendingInvitations.set(PARTNER_AIC, {
      sessionId: "B",
      partnerAic: PARTNER_AIC,
      invitationToken: "t",
      future,
      timeoutTask: createTask(async () => undefined),
    });
    await leader._handleGroupMessage(connectedResult(PARTNER_AIC, "group-a", "B"), sessionA);
    expect(future.done()).toBe(false);
    expect(leader._pendingInvitations.has(PARTNER_AIC)).toBe(true);
  });
});

describe("late 'connected' results racing the invitation timeout (Node fix)", () => {
  it("settles the invitation as timed out before the ACL cleanup, so a late join cannot resolve it", async () => {
    const leader = new GroupLeader(LEADER_AIC, MQ_CONFIG, { invitationTimeoutSeconds: 0 });
    const aclGate = new Deferred<void>();
    leader._groupAclClient = fakeAcl({ removeMember: vi.fn(() => aclGate.promise) });
    const client = new GroupLeaderMqClient(LEADER_AIC);
    client._groupId = "group-1";
    const session = new GroupLeaderSession("session-1", "group-1", client);
    leader.groupSessions.set("session-1", session);
    const future = new Deferred<PartnerConnectionInfo>();
    leader._pendingInvitations.set(PARTNER_AIC, {
      sessionId: "session-1",
      partnerAic: PARTNER_AIC,
      invitationToken: "t",
      future,
      timeoutTask: createTask(async () => undefined),
    });

    const watching = leader._watchInvitationTimeout({
      sessionId: "session-1",
      groupId: "group-1",
      partnerAic: PARTNER_AIC,
      invitationToken: "t",
    });
    await vi.waitFor(() => expect(future.done()).toBe(true));
    // ACL 清理还没完成时迟到的加入结果到达：邀请已经结束，不会被当成成功
    await leader._handleGroupMessage(connectedResult(PARTNER_AIC, "group-1"), session);
    expect(future.exception()).toBeInstanceOf(PartnerInviteError);
    expect(leader._pendingInvitations.size).toBe(0);
    aclGate.setResult();
    await watching;
  });
});

describe("cleanup closes channels the SDK has not closed yet (Node fix)", () => {
  function reconnectingChannel(): AmqpChannel & { close: ReturnType<typeof vi.fn> } {
    return {
      isClosed: true, // 断线重连 / 恢复中
      closeCalled: false,
      close: vi.fn(async () => undefined),
    } as never;
  }

  it("leader dissolveGroup closes a reconnecting channel (otherwise its consumer would come back)", async () => {
    const client = new GroupLeaderMqClient(LEADER_AIC);
    client._groupId = "group-1";
    client._state = "READY";
    const channel = reconnectingChannel();
    client._channel = channel;
    await client.dissolveGroup();
    expect(channel.close).toHaveBeenCalledTimes(1);
  });

  it("partner cleanup closes a reconnecting channel", async () => {
    const partner = new GroupPartnerMqClient(PARTNER_AIC);
    partner._state = PartnerGroupState.JOINED;
    partner._groupId = "group-1";
    const channel = reconnectingChannel();
    partner._channel = channel;
    await partner._cleanupGroupResources();
    expect(channel.close).toHaveBeenCalledTimes(1);
  });
});

describe("partner inbox survives leaving a group (Node fix)", () => {
  it("does not close the connection the inbox depends on, and close() still releases everything", async () => {
    const recorder = new Recorder();
    const connection = new FakeConnection(recorder);
    const partner = new GroupPartnerMqClient(PARTNER_AIC, { connection });
    const inbox = await partner.ensureInboxQueue();
    partner._state = PartnerGroupState.JOINED;
    partner._groupId = "group-1";
    // ensureInboxQueue() 已经通过 connect() 打开了群组 channel（partner._channel）
    expect(partner._channel).not.toBeNull();
    partner._queue = new FakeQueue(recorder, "group-q");
    partner._consumerTag = "tag";

    await partner.leaveGroup();
    expect(partner.state).toBe(PartnerGroupState.DISCONNECTED);
    expect(connection.isClosed).toBe(false);
    expect(partner.connection).toBe(connection);
    expect(partner._inboxQueue).toBe(inbox);
    expect(await partner.ensureInboxQueue()).toBe(inbox);

    await partner.close();
    expect(recorder.ops.filter((op) => op.op === "connection_close")).toHaveLength(1);
    expect(connection.channels.every((channel) => channel.isClosed)).toBe(true);
  });

  it("a failed inbox join keeps the inbox and reports the error through it", async () => {
    const recorder = new Recorder();
    const connection = new FakeConnection(recorder);
    const partner = new GroupPartnerMqClient(PARTNER_AIC, { connection });
    await partner.ensureInboxQueue();
    partner._joinGroupCommon = vi.fn(async () => {
      throw new Error("bind refused");
    });
    const invitation = InboxGroupInvitationSchema.parse({
      protocol: "rabbitmq:4.2",
      expiresAt: "2999-01-01T00:00:00Z",
      invitationToken: "token-1",
      group: groupInfo(),
      amqp: { exchange: "x", exchangeType: "fanout", routingKey: "" },
    });
    expect(await partner.joinGroupFromInvitation(invitation)).toBe(false);
    expect(connection.isClosed).toBe(false);
    const errorPublish = recorder.ops.find((op) => op.op === "publish");
    expect(errorPublish?.routing_key).toBe(`inbox_${LEADER_AIC}`);
    expect(String(errorPublish?.body)).toContain("bind refused");
  });
});

describe("half-initialised inbox is not kept (Node fix)", () => {
  class FlakyConnection extends FakeConnection {
    failNextBind = true;
    override async channel(): Promise<FakeChannel> {
      const channel = await super.channel();
      const declareQueue = channel.declareQueue.bind(channel);
      channel.declareQueue = async (name, options) => {
        const queue = await declareQueue(name, options);
        if (this.failNextBind) {
          this.failNextBind = false;
          queue.bind = async () => {
            throw new Error("403 ACCESS_REFUSED");
          };
        }
        return queue;
      };
      return channel;
    }
  }

  it("leader _ensureInbox retries after a failed setup", async () => {
    const connection = new FlakyConnection();
    const leader = new GroupLeader(LEADER_AIC, MQ_CONFIG);
    leader._connection = connection;
    await expect(leader._ensureInbox()).rejects.toThrow("ACCESS_REFUSED");
    expect(leader._inboxQueue).toBeNull();
    expect(connection.channels[0]?.isClosed).toBe(true);
    await leader._ensureInbox();
    expect(leader._inboxQueue).not.toBeNull();
    expect(connection.channels).toHaveLength(2);
  });

  it("partner ensureInboxQueue retries after a failed setup", async () => {
    const connection = new FlakyConnection();
    const partner = new GroupPartnerMqClient(PARTNER_AIC, { connection });
    await expect(partner.ensureInboxQueue()).rejects.toThrow("ACCESS_REFUSED");
    expect(partner._inboxQueue).toBeNull();
    const queue: AmqpQueue = await partner.ensureInboxQueue();
    expect(partner._inboxQueue).toBe(queue);
  });
});

describe("consume task handling in cleanupGroupResources", () => {
  it("does not wait for a consume that never completes (Python cancels the task)", async () => {
    const client = new GroupLeaderMqClient(LEADER_AIC);
    client._groupId = "group-1";
    const queue = new FakeQueue(new Recorder(), "q");
    queue.consume = () => new Promise<string>(() => undefined);
    client._leaderQueue = queue;
    client._channel = new FakeChannel(new Recorder());
    await client.startConsuming();
    await expect(client.cleanupGroupResources()).resolves.toBeUndefined();
  });

  it("re-raises a consume that failed, like awaiting a failed asyncio task", async () => {
    const client = new GroupLeaderMqClient(LEADER_AIC);
    client._groupId = "group-1";
    const queue = new FakeQueue(new Recorder(), "q");
    queue.consume = async () => {
      throw new Error("ACCESS_REFUSED");
    };
    client._leaderQueue = queue;
    client._channel = new FakeChannel(new Recorder());
    await client.startConsuming();
    await Promise.resolve();
    await expect(client.cleanupGroupResources()).rejects.toThrow("ACCESS_REFUSED");
  });
});

describe("rollback / cleanup never masks the original flow (Node fix)", () => {
  it("dissolveGroupSession completes even if the ACL service throws", async () => {
    const leader = new GroupLeader(LEADER_AIC, MQ_CONFIG);
    leader._groupAclClient = fakeAcl({
      deleteGroup: vi.fn(async () => {
        throw new TypeError("boom");
      }),
    });
    const client = new GroupLeaderMqClient(LEADER_AIC);
    client._groupId = "group-1";
    leader.groupSessions.set("s", new GroupLeaderSession("s", "group-1", client));
    await leader.dissolveGroupSession("s");
    expect(client.state).toBe("DISCONNECTED");
    expect(leader.groupSessions.size).toBe(0);
  });

  it("a disconnected result is handled even if ACL removal throws", async () => {
    const leader = new GroupLeader(LEADER_AIC, MQ_CONFIG);
    leader._groupAclClient = fakeAcl({
      removeMember: vi.fn(async () => {
        throw new TypeError("boom");
      }),
    });
    const client = new GroupLeaderMqClient(LEADER_AIC);
    client._groupId = "group-1";
    const session = new GroupLeaderSession("s", "group-1", client);
    leader.groupSessions.set("s", session);
    await expect(
      leader._handleGroupMessage(
        GroupMgmtResultSchema.parse({
          id: "r",
          sentAt: "t",
          senderRole: "partner",
          senderId: PARTNER_AIC,
          groupId: "group-1",
          status: { connected: false, muted: false },
        }),
        session,
      ),
    ).resolves.toBeUndefined();
    expect(client.partners.get(PARTNER_AIC)?.connected).toBe(false);
  });
});

describe("re-entrancy (Node fix)", () => {
  it("createGroupSession rejects an existing or in-flight sessionId", async () => {
    const leader = new GroupLeader(
      LEADER_AIC,
      { ...MQ_CONFIG, user: "u", password: "p" },
      { connector: new FakeConnector() },
    );
    const first = leader.createGroupSession("dup", []);
    await expect(leader.createGroupSession("dup", [])).rejects.toThrow(
      "Group session already exists: dup",
    );
    await first;
    await expect(leader.createGroupSession("dup", [])).rejects.toThrow(
      "Group session already exists: dup",
    );
    leader._cleanupTask?.cancel();
  });

  it("a joined partner rejects another invitation instead of leaking its group resources", async () => {
    const partner = new GroupPartnerMqClient(PARTNER_AIC, { connector: new FakeConnector() });
    partner._state = PartnerGroupState.JOINED;
    partner._groupId = "group-1";
    const response = await partner.joinGroup(
      RabbitMQRequestSchema.parse({
        params: {
          protocol: "rabbitmq:4.0",
          group: groupInfo("group-2"),
          server: { host: "mq", port: 5672, vhost: "acps", accessToken: "u:p" },
          amqp: { exchange: "x", exchangeType: "fanout", routingKey: "" },
        },
      }),
    );
    // JSON-RPC 响应总是带 id；请求没有 id 时为 null
    expect(JSON.parse(JSON.stringify(response))).toMatchObject({
      jsonrpc: "2.0",
      id: null,
      error: { code: -32020, data: { errorType: "INVITATION_REJECTED" } },
    });
    expect(partner.groupId).toBe("group-1");
    expect(partner.state).toBe(PartnerGroupState.JOINED);

    const sent: unknown[] = [];
    partner.sendInvitationError = async (_invitation, error) => {
      sent.push(error);
    };
    const invitation = InboxGroupInvitationSchema.parse({
      protocol: "rabbitmq:4.2",
      expiresAt: "2999-01-01T00:00:00Z",
      invitationToken: "token-2",
      group: groupInfo("group-2"),
      amqp: { exchange: "x", exchangeType: "fanout", routingKey: "" },
    });
    expect(await partner.joinGroupFromInvitation(invitation)).toBe(false);
    expect(sent).toMatchObject([{ code: -32020 }]);
    expect(partner.groupId).toBe("group-1");
  });
});

describe("partner join falls back to a passive declare when getExchange fails", () => {
  it("declares the group exchange passively", async () => {
    const recorder = new Recorder();
    const connection = new FakeConnection(recorder);
    const partner = new GroupPartnerMqClient(PARTNER_AIC, { connection });
    await partner.connect();
    const channel = partner._channel as FakeChannel;
    channel.getExchange = async () => {
      throw new Error("not found");
    };
    await partner._joinGroupCommon({
      group: groupInfo("group-1"),
      amqp: { exchange: `group_${LEADER_AIC}_group-1`, exchangeType: "fanout", routingKey: "" },
    });
    expect(recorder.ops.find((op) => op.op === "declare_exchange")).toEqual({
      op: "declare_exchange",
      name: `group_${LEADER_AIC}_group-1`,
      type: "fanout",
      durable: true,
      auto_delete: true,
      passive: true,
    });
  });
});

describe("credentials in URLs are not logged (Node fix)", () => {
  it("redacts userinfo of the partner RPC URL and the ACL base URL", async () => {
    const lines = captureLogs();
    const client = new GroupLeaderMqClient(LEADER_AIC, {
      transport: async () => {
        throw new Error("connect ECONNREFUSED");
      },
    });
    client._state = "READY";
    client._groupId = "group-1";
    client._exchangeName = `group_${LEADER_AIC}_group-1`;
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    const invite = client
      .invitePartner({ aic: PARTNER_AIC }, "https://user:s3cret@partner.example.com/rpc")
      .catch((error: unknown) => error);
    await vi.runAllTimersAsync();
    vi.useRealTimers();
    expect(await invite).toBeInstanceOf(PartnerInviteError);

    const acl = new GroupAclClient({
      baseUrl: "https://acl-user:acl-s3cret@auth.example.com",
      transport: async () => ({
        status: 204,
        headers: {},
        body: Buffer.alloc(0),
        peerCertificate: null,
      }),
    });
    await acl.deleteGroup({ leaderAic: LEADER_AIC, groupId: "g" });

    const text = lines.join("\n");
    expect(text).toContain("https://***@partner.example.com/rpc");
    expect(text).toContain("https://***@auth.example.com");
    expect(text).not.toContain("s3cret");
  });
});
