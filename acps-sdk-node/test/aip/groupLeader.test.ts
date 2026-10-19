import { describe, expect, it, vi } from "vitest";

import { DeliveryError } from "../../src/aip/groupAmqp.js";
import type { GroupAclClientLike } from "../../src/aip/groupAuth.js";
import {
  GroupLeader,
  GroupLeaderMqClient,
  GroupLeaderSession,
  PartnerConnectionInfo,
} from "../../src/aip/groupLeader.js";
import {
  type GroupMgmtCommand,
  GroupMgmtCommandType,
  GroupMgmtResultSchema,
} from "../../src/aip/groupModel.js";
import { FakeChannel, Recorder } from "./groupFakes.js";

const VALID_LEADER_AIC = "1.2.156.3088.1.1.CIQJUQ.HELDGD.1.03TO";
const VALID_PARTNER_AIC = "1.2.156.3088.1.1.8UDX9U.NNVB61.1.13WT";

const MQ_CONFIG = { host: "mq.acps.example.com", port: 5671, vhost: "acps" };

function fakeAcl(overrides: Partial<GroupAclClientLike> = {}): GroupAclClientLike & {
  addMember: ReturnType<typeof vi.fn>;
  removeMember: ReturnType<typeof vi.fn>;
  deleteGroup: ReturnType<typeof vi.fn>;
  closeMemberConnection: ReturnType<typeof vi.fn>;
  aclose: ReturnType<typeof vi.fn>;
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

// 对应 tests/test_group_leader.py
describe("test_group_leader", () => {
  it("test_send_mgmt_command_with_explicit_session_id", async () => {
    // 管理命令应使用显式传入的 sessionId，而非从 groupId 推导
    const client = new GroupLeaderMqClient("leader-aic");
    client._groupId = "group-session-123";
    const published: GroupMgmtCommand[] = [];
    client.publishMessage = async (message) => {
      published.push(message as GroupMgmtCommand);
    };

    const message = await client.sendMgmtCommand(GroupMgmtCommandType.GET_STATUS, {
      mentions: "all",
      sessionId: "session-91011",
    });

    expect(message.groupId).toBe("group-session-123");
    expect(message.sessionId).toBe("session-91011");
    expect(published[0]?.sessionId).toBe("session-91011");
  });

  it("test_send_mgmt_command_without_session_id", async () => {
    // 不传 sessionId 时，sessionId 应为空
    const client = new GroupLeaderMqClient("leader-aic");
    client._groupId = "group123";
    const published: GroupMgmtCommand[] = [];
    client.publishMessage = async (message) => {
      published.push(message as GroupMgmtCommand);
    };

    const message = await client.sendMgmtCommand(GroupMgmtCommandType.GET_STATUS, {
      mentions: "all",
    });

    expect(message.groupId).toBe("group123");
    expect(message.sessionId).toBeUndefined();
    expect(published[0]?.sessionId).toBeUndefined();
  });

  it("test_request_partner_leave_retries_on_delivery_error", async () => {
    const client = new GroupLeaderMqClient("leader-aic");
    client._groupId = "group123";

    const expected = { marker: true } as unknown as GroupMgmtCommand;
    const sendMgmtCommand = vi
      .fn<GroupLeaderMqClient["sendMgmtCommand"]>()
      .mockRejectedValueOnce(new DeliveryError("nacked"))
      .mockResolvedValueOnce(expected);
    client.sendMgmtCommand = sendMgmtCommand;

    const result = await client.requestPartnerLeave("partner-aic", { sessionId: "session-1" });

    expect(result).toBe(expected);
    expect(sendMgmtCommand).toHaveBeenCalledTimes(2);
  });

  it("test_request_partner_leave_becomes_noop_when_partner_already_disconnected", async () => {
    const client = new GroupLeaderMqClient("leader-aic");
    client._groupId = "group123";
    const partner = new PartnerConnectionInfo(
      "partner-aic",
      "conn-1",
      "acps",
      "rabbit@node",
      "group_leader-aic_group123_partner-aic",
    );
    partner.connected = false;
    client._partners.set("partner-aic", partner);
    const sendMgmtCommand = vi.fn<GroupLeaderMqClient["sendMgmtCommand"]>();
    client.sendMgmtCommand = sendMgmtCommand;

    const result = await client.requestPartnerLeave("partner-aic", { sessionId: "session-1" });

    expect(sendMgmtCommand).not.toHaveBeenCalled();
    expect(result.command).toBe(GroupMgmtCommandType.LEAVE_GROUP);
    expect(result.mentions).toEqual(["partner-aic"]);
    expect(result.sessionId).toBe("session-1");
  });

  it("test_request_partner_leave_becomes_noop_when_partner_disconnects_after_delivery_error", async () => {
    const client = new GroupLeaderMqClient("leader-aic");
    client._groupId = "group123";
    const partner = new PartnerConnectionInfo(
      "partner-aic",
      "conn-1",
      "acps",
      "rabbit@node",
      "group_leader-aic_group123_partner-aic",
    );
    client._partners.set("partner-aic", partner);

    client.sendMgmtCommand = async () => {
      partner.connected = false;
      throw new DeliveryError("nacked");
    };

    const result = await client.requestPartnerLeave("partner-aic", { sessionId: "session-1" });

    expect(result.command).toBe(GroupMgmtCommandType.LEAVE_GROUP);
    expect(result.mentions).toEqual(["partner-aic"]);
    expect(result.sessionId).toBe("session-1");
  });

  it("request_partner_leave re-raises the last DeliveryError after all retries", async () => {
    const client = new GroupLeaderMqClient("leader-aic");
    client._groupId = "group123";
    const error = new DeliveryError("nacked");
    const sendMgmtCommand = vi
      .fn<GroupLeaderMqClient["sendMgmtCommand"]>()
      .mockRejectedValue(error);
    client.sendMgmtCommand = sendMgmtCommand;

    await expect(client.requestPartnerLeave("partner-aic")).rejects.toBe(error);
    expect(sendMgmtCommand).toHaveBeenCalledTimes(3);
  });

  it("test_delete_partner_queue_uses_channel_queue_delete", async () => {
    const client = new GroupLeaderMqClient(VALID_LEADER_AIC);
    client._groupId = "group123";
    const recorder = new Recorder();
    client._channel = new FakeChannel(recorder);

    const result = await client.deletePartnerQueue(VALID_PARTNER_AIC);

    expect(recorder.ops).toEqual([
      {
        op: "queue_delete",
        name: "group_1.2.156.3088.1.1.CIQJUQ.HELDGD.1.03TO_group123_1.2.156.3088.1.1.8UDX9U.NNVB61.1.13WT",
        if_unused: false,
        if_empty: false,
      },
    ]);
    expect(result).toBe(true);
  });

  it("test_cleanup_group_resources_uses_channel_delete_and_clears_refs", async () => {
    const client = new GroupLeaderMqClient("leader-aic");
    client._groupId = "group123";
    client._exchangeName = "exchange-1";
    client._leaderQueue = { name: "queue-1" } as never;
    const exchangeDelete = vi.fn();
    client._exchange = { name: "exchange-1", publish: vi.fn(), delete: exchangeDelete } as never;
    const recorder = new Recorder();
    client._channel = new FakeChannel(recorder);

    await client.cleanupGroupResources();

    expect(recorder.ops).toEqual([
      { op: "queue_delete", name: "queue-1", if_unused: false, if_empty: false },
      { op: "exchange_delete", name: "exchange-1", if_unused: false },
    ]);
    expect(exchangeDelete).not.toHaveBeenCalled();
    expect(client._leaderQueue).toBeNull();
    expect(client._exchange).toBeNull();
    expect(client._exchangeName).toBeNull();
  });

  it("test_group_runtime_exposes_invitation_route", () => {
    const leader = new GroupLeader("leader-aic", MQ_CONFIG);
    const session = new GroupLeaderSession(
      "session-1",
      "group-session-1",
      new GroupLeaderMqClient("leader-aic"),
    );
    session.recordInvitationRoute("partner-aic", "inbox");
    session.leaderMqClient._partners.set(
      "partner-aic",
      new PartnerConnectionInfo(
        "partner-aic",
        "conn-1",
        "acps",
        "rabbit@node",
        "group_leader-aic_group-session-1_partner-aic",
      ),
    );
    leader.groupSessions.set("session-1", session);

    const runtime = leader.getGroupRuntime({ sessionId: "session-1" });

    expect(runtime.group_id).toBe("group-session-1");
    expect(runtime.connected_members).toBe(1);
    expect(runtime.members[0]?.partner_aic).toBe("partner-aic");
    expect(runtime.members[0]?.invitation_route).toBe("inbox");
  });

  it("test_force_remove_partner_cleans_acl_queue_and_runtime", async () => {
    const leader = new GroupLeader("leader-aic", MQ_CONFIG);
    const callOrder: string[] = [];
    const acl = fakeAcl({
      removeMember: vi.fn(async () => {
        callOrder.push("remove");
      }),
      closeMemberConnection: vi.fn(async () => {
        callOrder.push("close");
      }),
    });
    leader._groupAclClient = acl;
    const waitForDisconnect = vi.fn(async () => false);
    leader._waitForPartnerDisconnect = waitForDisconnect;

    const mqClient = new GroupLeaderMqClient("leader-aic");
    const requestPartnerLeave = vi.fn(async () => {
      callOrder.push("leave");
      return {} as GroupMgmtCommand;
    });
    const deletePartnerQueue = vi.fn(async (partnerAic: string) => {
      callOrder.push(`delete:${partnerAic}`);
      return true;
    });
    const removePartnerTracking = vi.fn((partnerAic: string) => {
      callOrder.push(`track:${partnerAic}`);
    });
    mqClient.requestPartnerLeave = requestPartnerLeave;
    mqClient.deletePartnerQueue = deletePartnerQueue;
    mqClient.removePartnerTracking = removePartnerTracking;

    const session = new GroupLeaderSession("session-1", "group-session-1", mqClient);
    leader.groupSessions.set("session-1", session);

    const result = await leader.forceRemovePartner("partner-aic", { sessionId: "session-1" });

    expect(acl.removeMember).toHaveBeenCalledTimes(1);
    expect(acl.removeMember).toHaveBeenCalledWith({
      leaderAic: "leader-aic",
      groupId: "group-session-1",
      memberAic: "partner-aic",
    });
    expect(deletePartnerQueue).toHaveBeenCalledWith("partner-aic");
    expect(acl.closeMemberConnection).toHaveBeenCalledWith({
      leaderAic: "leader-aic",
      groupId: "group-session-1",
      memberAic: "partner-aic",
    });
    expect(requestPartnerLeave).toHaveBeenCalledWith("partner-aic", { sessionId: "session-1" });
    expect(removePartnerTracking).toHaveBeenCalledWith("partner-aic");
    expect(callOrder).toEqual([
      "leave",
      "remove",
      "delete:partner-aic",
      "close",
      "track:partner-aic",
    ]);
    expect(result).toEqual({
      session_id: "session-1",
      group_id: "group-session-1",
      partner_aic: "partner-aic",
      queue_deleted: true,
    });
  });

  it("test_force_remove_partner_prefers_graceful_leave_cleanup", async () => {
    const leader = new GroupLeader("leader-aic", MQ_CONFIG);
    const acl = fakeAcl();
    leader._groupAclClient = acl;
    const waitForDisconnect = vi.fn(async () => true);
    leader._waitForPartnerDisconnect = waitForDisconnect;

    const mqClient = new GroupLeaderMqClient("leader-aic");
    const deletePartnerQueue = vi.fn(async () => true);
    const requestPartnerLeave = vi.fn(async () => ({}) as GroupMgmtCommand);
    const removePartnerTracking = vi.fn();
    mqClient.deletePartnerQueue = deletePartnerQueue;
    mqClient.requestPartnerLeave = requestPartnerLeave;
    mqClient.removePartnerTracking = removePartnerTracking;

    const session = new GroupLeaderSession("session-1", "group-session-1", mqClient);
    leader.groupSessions.set("session-1", session);

    const result = await leader.forceRemovePartner("partner-aic", { sessionId: "session-1" });

    expect(requestPartnerLeave).toHaveBeenCalledWith("partner-aic", { sessionId: "session-1" });
    expect(waitForDisconnect).toHaveBeenCalledWith(session, "partner-aic");
    expect(acl.removeMember).toHaveBeenCalledWith({
      leaderAic: "leader-aic",
      groupId: "group-session-1",
      memberAic: "partner-aic",
    });
    expect(deletePartnerQueue).not.toHaveBeenCalled();
    expect(acl.closeMemberConnection).not.toHaveBeenCalled();
    expect(removePartnerTracking).toHaveBeenCalledWith("partner-aic");
    expect(result).toEqual({
      session_id: "session-1",
      group_id: "group-session-1",
      partner_aic: "partner-aic",
      queue_deleted: false,
    });
  });

  it("test_group_mgmt_result_disconnected_cleans_acl", async () => {
    const leader = new GroupLeader("leader-aic", MQ_CONFIG);
    const acl = fakeAcl();
    leader._groupAclClient = acl;

    const session = new GroupLeaderSession(
      "session-1",
      "group-session-1",
      new GroupLeaderMqClient("leader-aic"),
    );
    leader.groupSessions.set("session-1", session);

    const message = GroupMgmtResultSchema.parse({
      id: "mgmt-result-1",
      sentAt: "2026-04-15T00:00:00+00:00",
      senderRole: "partner",
      senderId: "partner-aic",
      dataItems: [],
      status: { connected: false, muted: false },
      groupId: "group-session-1",
      sessionId: "session-1",
    });

    await leader._handleGroupMessage(message);

    expect(acl.removeMember).toHaveBeenCalledTimes(1);
    expect(acl.removeMember).toHaveBeenCalledWith({
      leaderAic: "leader-aic",
      groupId: "group-session-1",
      memberAic: "partner-aic",
    });
  });
});

describe("GroupLeaderSession / GroupLeader extras", () => {
  it("records task state, products and prompts and fires the update event", async () => {
    const session = new GroupLeaderSession("s", "g", new GroupLeaderMqClient("leader-aic"));
    const waited = session.stateUpdateEvent.wait();
    session.recordTaskState("task-1", "p1", "awaiting-input", { awaitingPrompt: "need more" });
    await expect(waited).resolves.toBe(true);
    expect(session.getPartnerTaskSnapshot("task-1", "p1")).toEqual({
      task_id: "task-1",
      state: "awaiting-input",
      product_text: undefined,
      awaiting_prompt: "need more",
    });
    session.recordTaskState("task-1", "p1", "completed", { productText: "done" });
    session.recordTaskState("task-1", "p2", "working");
    expect(session.getPartnerTaskSnapshot("task-1", "p1")?.awaiting_prompt).toBeUndefined();
    expect(session.getTaskSummary("task-1")).toEqual({
      task_id: "task-1",
      states: { p1: "completed", p2: "working" },
      products: { p1: "done" },
      completed_count: 1,
      working_count: 1,
    });
    expect(session.getPartnerTaskSnapshot("task-x", "p1")).toBeNull();
  });

  it("routes TaskResult messages into the session and extracts texts", async () => {
    const leader = new GroupLeader("leader-aic", MQ_CONFIG);
    const session = new GroupLeaderSession(
      "session-1",
      "group-1",
      new GroupLeaderMqClient("leader-aic"),
    );
    leader.groupSessions.set("session-1", session);

    await leader._handleGroupMessage({
      type: "task-result",
      id: "r1",
      sentAt: "t",
      senderRole: "partner",
      senderId: "partner-aic",
      groupId: "group-1",
      taskId: "task-1",
      status: {
        state: "awaiting-completion",
        stateChangedAt: "t",
        dataItems: [
          { type: "text", text: "" },
          { type: "data", data: {} },
        ],
      },
      products: [
        {
          id: "p",
          dataItems: [
            { type: "text", text: "line 1" },
            { type: "text", text: "line 2" },
          ],
        },
      ],
    });

    expect(session.messageHistory).toHaveLength(1);
    expect(session.taskStates["task-1"]).toEqual({ "partner-aic": "awaiting-completion" });
    expect(session.taskProducts["task-1"]).toEqual({ "partner-aic": "line 1\nline 2" });
    // Python：空字符串也是 str，所以提示为 ""（而不是 None）
    expect(session.taskPrompts["task-1"]).toEqual({ "partner-aic": "" });
  });

  it("resolves the pending inbox invitation when the partner reports connected", async () => {
    const leader = new GroupLeader(VALID_LEADER_AIC, MQ_CONFIG);
    const session = new GroupLeaderSession(
      "session-1",
      "group-1",
      new GroupLeaderMqClient(VALID_LEADER_AIC),
    );
    session.leaderMqClient._groupId = "group-1";
    leader.groupSessions.set("session-1", session);
    const { Deferred, createTask, sleep } = await import("../../src/aip/groupRuntime.js");
    const future = new Deferred<PartnerConnectionInfo>();
    const timeoutTask = createTask((signal) => sleep(3_600_000, signal));
    leader._pendingInvitations.set(VALID_PARTNER_AIC, {
      sessionId: "session-1",
      partnerAic: VALID_PARTNER_AIC,
      invitationToken: "t",
      future,
      timeoutTask,
    });

    await leader._handleGroupMessage(
      GroupMgmtResultSchema.parse({
        id: "r",
        sentAt: "t",
        senderRole: "partner",
        senderId: VALID_PARTNER_AIC,
        groupId: "group-1",
        status: { connected: true, muted: false },
      }),
    );

    const info = await future.promise;
    expect(info.aic).toBe(VALID_PARTNER_AIC);
    expect(info.queueName).toBe(`group_${VALID_LEADER_AIC}_group-1_${VALID_PARTNER_AIC}`);
    expect(leader._pendingInvitations.size).toBe(0);
    await expect(timeoutTask.promise).rejects.toThrow("cancelled");
    expect(session.leaderMqClient._partnerAcs).toEqual([{ aic: VALID_PARTNER_AIC }]);
  });

  it("_getSession requires a session id when several sessions are active", () => {
    const leader = new GroupLeader("leader-aic", MQ_CONFIG);
    expect(() => leader._getSession(null)).toThrow(
      "Multiple sessions active; please specify session_id",
    );
    leader.groupSessions.set(
      "a",
      new GroupLeaderSession("a", "g", new GroupLeaderMqClient("leader-aic")),
    );
    expect(leader._getSession(null).sessionId).toBe("a");
    leader.groupSessions.set(
      "b",
      new GroupLeaderSession("b", "g", new GroupLeaderMqClient("leader-aic")),
    );
    expect(() => leader._getSession(undefined)).toThrow("Multiple sessions active");
    expect(() => leader._getSession("c")).toThrow("Group session not found: c");
  });

  it("PartnerConnectionInfo.toDict uses Python keys", () => {
    const info = new PartnerConnectionInfo("a", "c", "v", "n", "q", "pid-1");
    expect(Object.keys(info.toDict())).toEqual([
      "aic",
      "connection_name",
      "vhost",
      "node_name",
      "queue_name",
      "process_id",
      "connected",
      "muted",
      "joined_at",
    ]);
  });
});
