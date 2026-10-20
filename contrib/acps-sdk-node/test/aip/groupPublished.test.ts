/**
 * 跨语言对照：用与 scripts/fixtures/gen_aip_group.py 相同的常量（uuid4 / now / token / trace id）
 * 重放同一组操作，逐项比对 AMQP 拓扑声明、发布的消息体字节与属性、连接 URL、RPC / ACL 请求。
 */

import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { TaskCommandType } from "../../src/aip/baseModel.js";
import { GroupAclClient, GroupAclError } from "../../src/aip/groupAuth.js";
import { GroupLeader, GroupLeaderMqClient } from "../../src/aip/groupLeader.js";
import {
  GroupMgmtCommandType,
  InboxGroupInvitationSchema,
  RabbitMQRequestSchema,
  dumpGroupMessage,
} from "../../src/aip/groupModel.js";
import { GroupPartnerMqClient } from "../../src/aip/groupPartner.js";
import type {
  HttpRequestOptions,
  HttpResponseData,
  HttpTransport,
} from "../../src/internal/http.js";
import { loadPythonFixture } from "../fixtures.js";
import {
  type EmitCall,
  FakeConnection,
  FakeConnector,
  FakeEmitter,
  FakeExchange,
  LEADER_AIC,
  OTHER_AIC,
  PARTNER_AIC,
  Recorder,
  makeIncoming,
  plain,
} from "./groupFakes.js";

const FIXED_UUID = "12345678-1234-4678-9234-567812345678";
const FIXED_NOW = "2026-10-19T08:01:12.160Z";
const FIXED_TRACE_ID = "0af7651916cd43dd8448eb211c80319c";
const FIXED_SPAN_ID = "b7ad6b7169203331";
const FIXED_PID = 4242;

vi.mock("node:crypto", async (importOriginal) => {
  const actual = (await importOriginal()) as Record<string, unknown>;
  return { ...actual, randomUUID: () => "12345678-1234-4678-9234-567812345678" };
});

vi.mock("../../src/aip/groupRuntime.js", async (importOriginal) => {
  const actual = (await importOriginal()) as Record<string, unknown>;
  return { ...actual, createInvitationToken: () => "fixed-invitation-token" };
});

vi.mock("../../src/amp/traceContext.js", async (importOriginal) => {
  const actual = (await importOriginal()) as Record<string, unknown>;
  return {
    ...actual,
    newTraceId: () => "0af7651916cd43dd8448eb211c80319c",
    newSpanId: () => "b7ad6b7169203331",
  };
});

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- 未建模的 fixture JSON
type Json = any;

const published = loadPythonFixture<Json>("aip_group", "published");
const http = loadPythonFixture<Json>("aip_group", "http");

const TLS = { rejectUnauthorized: true };

function groupInfo(): Json {
  return {
    groupId: "group-session-1",
    leader: { aic: LEADER_AIC },
    partners: [{ aic: PARTNER_AIC }],
  };
}

function amqpConfig(): Json {
  return {
    exchange: `group_${LEADER_AIC}_group-session-1`,
    exchangeType: "fanout",
    routingKey: "",
  };
}

/**
 * 响应模型保留 `"id": null`（JSON-RPC 要求响应带 id），Python 样例按 exclude_none 省略了它；
 * 这是有意差异（同 test/aip/pythonParity.test.ts），比对时去掉值为 null 的 id。
 */
function dropNullResponseId(response: Json): Json {
  if (response && typeof response === "object" && response.id === null) {
    const { id: _id, ...rest } = response;
    return rest;
  }
  return response;
}

/** Python 记录中 processId 用的是固定 pid。 */
function withFixedPid(value: unknown): unknown {
  return JSON.parse(
    JSON.stringify(value).replaceAll(`pid-${process.pid}`, `pid-${FIXED_PID}`),
  ) as unknown;
}

function publishes(recorder: Recorder): Json[] {
  return recorder.ops.filter((op) => op.op === "publish");
}

/** EmitCall → Python FakeEmitter 记录形状（kwargs 为 snake_case）。 */
function emitRecord(call: EmitCall, { dropSettlement = false } = {}): Json {
  const body = plain(call.body) as Json;
  if (dropSettlement) delete body.settlement;
  const options = call.options as Json;
  const pythonOptions: Json = {
    trace_id: options.traceId,
    span_id: options.spanId,
    parent_span_id: options.parentSpanId,
    correlation_id: options.correlationId ?? null,
  };
  return { body, options: pythonOptions };
}

beforeAll(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: new Date(FIXED_NOW) });
});

afterAll(() => {
  vi.useRealTimers();
});

describe("Python fixtures: aip_group/published (leader)", () => {
  async function createLeader(): Promise<{
    leader: GroupLeaderMqClient;
    recorder: Recorder;
    groupId: string;
  }> {
    const recorder = new Recorder();
    const leader = new GroupLeaderMqClient(LEADER_AIC, {
      rabbitmqHost: "mq.acps.example.com",
      rabbitmqPort: 5671,
      rabbitmqVhost: "acps",
      rabbitmqUser: null,
      rabbitmqPassword: null,
      connection: new FakeConnection(recorder),
      connectionOwner: false,
    });
    await leader.connect();
    const groupId = await leader.createGroup({
      groupId: "group-session-1",
      partnerAcsList: [{ aic: OTHER_AIC }],
    });
    return { leader, recorder, groupId };
  }

  it("declares the same group topology", async () => {
    const { recorder, groupId } = await createLeader();
    expect({ group_id: groupId, ops: recorder.ops }).toEqual(published.leader_create_group);
  });

  it("publishes byte-identical task / management messages with the same AMQP properties", async () => {
    const { leader, recorder } = await createLeader();
    recorder.ops = [];
    await leader.sendTaskCommand(TaskCommandType.Start, "task-1", "session-1", {
      textContent: "请处理：Ünïcödé ✓",
      dataItems: [{ type: "data", data: { k: [1, 2.5, null, true], nested: { x: null } } }],
      mentions: [PARTNER_AIC],
    });
    await leader.startTask("session-1", "start text", { mentions: "all" });
    await leader.continueTask("task-1", "session-1", "more");
    await leader.completeTask("task-1", "session-1");
    await leader.cancelTask("task-1", "session-1", { reason: "stop" });
    await leader.sendMgmtCommand(GroupMgmtCommandType.GET_STATUS, {
      mentions: "all",
      sessionId: "session-1",
    });
    await leader.getPartnerStatus({ partnerAic: PARTNER_AIC });
    await leader.mutePartner(PARTNER_AIC, { sessionId: "session-1" });
    await leader.unmutePartner(PARTNER_AIC);
    await leader.requestPartnerLeave(PARTNER_AIC, { sessionId: "session-1" });

    const actual = publishes(recorder);
    expect(actual).toHaveLength(published.leader_publishes.length);
    actual.forEach((record, index) => {
      expect(record, `publish #${index}`).toEqual(published.leader_publishes[index]);
    });
  });

  it("sends the same inbox invitations", async () => {
    const { leader, recorder } = await createLeader();
    recorder.ops = [];
    const invitation = await leader.inviteViaInbox(
      { aic: PARTNER_AIC },
      {
        partnerInbox: `inbox_${PARTNER_AIC}`,
        invitationToken: "token-abc",
        expiresAt: "2026-10-19T08:06:12.160000+00:00",
      },
    );
    await leader.inviteViaInbox({ aic: PARTNER_AIC }, { partnerInbox: `inbox_${PARTNER_AIC}` });
    expect(plain(dumpGroupMessage(invitation))).toEqual(
      published.leader_invite_via_inbox.invitation,
    );
    expect(recorder.ops).toEqual(published.leader_invite_via_inbox.ops);
    // 解析得到的邀请可再次通过模型校验（Partner 侧会这样做）
    expect(
      InboxGroupInvitationSchema.parse(JSON.parse(String(publishes(recorder)[0]?.body))),
    ).toEqual(invitation);
  });

  it("cleans up queues and exchanges the same way", async () => {
    const { leader, recorder } = await createLeader();
    recorder.ops = [];
    await leader.deletePartnerQueue(PARTNER_AIC);
    await leader.cleanupGroupResources();
    expect(recorder.ops).toEqual(published.leader_cleanup);
  });

  it("builds the same connection URLs", async () => {
    const connector = new FakeConnector();
    await new GroupLeaderMqClient(LEADER_AIC, {
      rabbitmqHost: "mq.example.com",
      rabbitmqPort: 5672,
      rabbitmqVhost: "/",
      connector,
    }).connect();
    await new GroupLeaderMqClient(LEADER_AIC, {
      rabbitmqHost: "mq.acps.example.com",
      rabbitmqPort: 5671,
      rabbitmqVhost: "acps",
      rabbitmqUser: null,
      rabbitmqPassword: null,
      sslContext: TLS,
      connector,
    }).connect();
    expect(connector.calls.map(({ url, ssl_context }) => ({ url, ssl_context }))).toEqual(
      published.leader_connect_urls,
    );
    expect(connector.calls.every((call) => call.robust)).toBe(true);

    await expect(
      new GroupLeaderMqClient(LEADER_AIC, {
        rabbitmqUser: "",
        rabbitmqPassword: null,
        connector,
      }).connect(),
    ).rejects.toThrow(published.leader_external_without_ssl);
  });

  it("GroupLeader.createGroupSession shares one connection and declares the inbox", async () => {
    const recorder = new Recorder();
    const connector = new FakeConnector(recorder);
    const groupLeader = new GroupLeader(
      LEADER_AIC,
      {
        host: "mq.acps.example.com",
        port: 5671,
        vhost: "acps",
        user: "u",
        password: "p",
      },
      { connector },
    );
    const session = await groupLeader.createGroupSession("sess_b50a1a6a", [{ aic: PARTNER_AIC }]);
    const expected = published.group_leader_create_session;

    expect(connector.calls.map(({ url, ssl_context }) => ({ url, ssl_context }))).toEqual(
      expected.connect_calls,
    );
    expect(session.groupId).toBe(expected.group_id);
    // Python 的 start_consuming 用 create_task 异步注册消费者，记录时尚未执行；Node 同步调用 consume
    const ops = [...recorder.ops];
    expect(ops.at(-1)).toEqual({
      op: "consume",
      queue: `group_${LEADER_AIC}_group-sess-b50a1a6a_${LEADER_AIC}`,
    });
    expect(ops.slice(0, -1)).toEqual(expected.ops);
    expect(plain(groupLeader.getGroupRuntime({ sessionId: "sess_b50a1a6a" }))).toEqual(
      expected.runtime,
    );
    groupLeader._cleanupTask?.cancel();
  });
});

describe("Python fixtures: aip_group/published (partner)", () => {
  function joinRequest(accessToken?: string, id: string | number = "req-1") {
    return RabbitMQRequestSchema.parse({
      id,
      params: {
        protocol: "rabbitmq:4.0",
        group: groupInfo(),
        server: { host: "mq.acps.example.com", port: 5672, vhost: "acps", accessToken },
        amqp: amqpConfig(),
      },
    });
  }

  async function joinedPartner(): Promise<{
    partner: GroupPartnerMqClient;
    recorder: Recorder;
    connector: FakeConnector;
    response: unknown;
  }> {
    const recorder = new Recorder();
    const connector = new FakeConnector(recorder);
    const partner = new GroupPartnerMqClient(PARTNER_AIC, { connector });
    const response = await partner.joinGroup(joinRequest("user:pa:ss"));
    return { partner, recorder, connector, response };
  }

  it("joins via direct RPC with the same topology, response and join notification", async () => {
    const { partner, recorder, connector, response } = await joinedPartner();
    const expected = published.partner_join_group;
    expect(withFixedPid(plain(response))).toEqual(expected.response);
    expect(connector.calls).toEqual(expected.connect_calls);
    expect(recorder.ops).toEqual(expected.ops);
    expect(partner.rabbitmqUser).toBe(expected.user);
    expect(partner.rabbitmqPassword).toBe(expected.password);
  });

  it("publishes byte-identical status and task results", async () => {
    const { partner, recorder } = await joinedPartner();
    recorder.ops = [];
    await partner.sendStatusResult(true, true, { sessionId: "session-1" });
    await partner.acceptTask("task-1", "session-1");
    await partner.requestInput("task-1", "session-1", "need more ✓");
    await partner.submitForCompletion("task-1", "session-1", [
      {
        id: "p-1",
        name: "report",
        dataItems: [
          { type: "text", text: "done" },
          { type: "data", data: { score: 0.5 } },
        ],
      },
    ]);
    await partner.failTask("task-1", "session-1", "boom");
    const actual = publishes(recorder);
    expect(actual).toHaveLength(published.partner_publishes.length);
    actual.forEach((record, index) => {
      expect(record, `publish #${index}`).toEqual(published.partner_publishes[index]);
    });
  });

  it("leaves the group the same way", async () => {
    const { partner, recorder } = await joinedPartner();
    recorder.ops = [];
    await partner.leaveGroup({ sessionId: "session-1" });
    expect(recorder.ops).toEqual(published.partner_leave_group);
  });

  it("returns the same failure / rejection responses", async () => {
    const failRequest = RabbitMQRequestSchema.parse({
      id: 99,
      params: {
        protocol: "rabbitmq:4.0",
        group: groupInfo(),
        server: { host: "mq", port: 5671, vhost: "acps" },
        amqp: amqpConfig(),
      },
    });
    const failing = new GroupPartnerMqClient(PARTNER_AIC, { connector: new FakeConnector() });
    expect(plain(await failing.joinGroup(failRequest))).toEqual(published.partner_join_failed);

    const rejecting = new GroupPartnerMqClient(PARTNER_AIC, { connector: new FakeConnector() });
    rejecting.setInvitationHandler(async () => false);
    expect(plain(await rejecting.joinGroup(failRequest))).toEqual(published.partner_join_rejected);

    // 请求没有 id：JSON-RPC 响应仍带 "id": null（Python 的 exclude_none 会省略它，比对时去掉）
    const noId = plain(
      await rejecting.joinGroup(RabbitMQRequestSchema.parse({ params: failRequest.params })),
    ) as Json;
    expect(noId.id).toBeNull();
    expect(dropNullResponseId(noId)).toEqual(published.partner_join_rejected_no_id);
  });

  it("declares the inbox and sends invitation errors the same way", async () => {
    const recorder = new Recorder();
    const connector = new FakeConnector(recorder);
    const partner = new GroupPartnerMqClient(PARTNER_AIC, {
      rabbitmqHost: "mq.acps.example.com",
      rabbitmqPort: 5671,
      rabbitmqVhost: "acps",
      rabbitmqUser: null,
      rabbitmqPassword: null,
      sslContext: TLS,
      robustConnection: false,
      connector,
    });
    await partner.ensureInboxQueue();
    const invitation = InboxGroupInvitationSchema.parse({
      id: "invite-1",
      sentAt: "2026-10-19T08:01:12.160000+00:00",
      senderRole: "leader",
      senderId: LEADER_AIC,
      groupId: "group-session-1",
      protocol: "rabbitmq:4.2",
      expiresAt: "2026-10-19T08:06:12.160000+00:00",
      invitationToken: "token-abc",
      group: groupInfo(),
      amqp: amqpConfig(),
    });
    await partner.sendInvitationError(invitation, {
      code: -32021,
      message: "Connection failed",
      data: { errorType: "CONNECTION_FAILED", details: "reason ✓" },
    });
    expect({ connect_calls: connector.calls, ops: recorder.ops }).toEqual(published.partner_inbox);
  });
});

describe("Python fixtures: aip_group/published (message emitter)", () => {
  it("leader send: traceparent header and send event body", async () => {
    const emitter = new FakeEmitter();
    const recorder = new Recorder();
    const leader = new GroupLeaderMqClient(LEADER_AIC, {
      rabbitmqVhost: "acps",
      connection: new FakeConnection(recorder),
      connectionOwner: false,
      messageEmitter: emitter,
      traceContextProvider: () => ({ traceId: "a".repeat(32), spanId: "c".repeat(16) }),
    });
    await leader.connect();
    await leader.createGroup({ groupId: "group-session-1" });
    await leader.sendTaskCommand(TaskCommandType.Get, "task-1", "session-1", {
      textContent: "状态？",
    });

    const expected = published.emitter.leader_send;
    expect(publishes(recorder).at(-1)).toEqual(expected.publish);
    expect(emitter.calls.map((call) => emitRecord(call))).toEqual(expected.emits);
  });

  it("leader receive: receive / ack event bodies", async () => {
    const emitter = new FakeEmitter();
    const recorder = new Recorder();
    const connection = new FakeConnection(recorder);
    const leader = new GroupLeaderMqClient(LEADER_AIC, {
      rabbitmqVhost: "acps",
      connection,
      connectionOwner: false,
      messageEmitter: emitter,
    });
    await leader.connect();
    await leader.createGroup({ groupId: "group-session-1" });
    leader.setMessageHandler(async () => undefined);
    await leader.startConsuming();
    const queue = connection.channels[0]?.queues.get(
      `group_${LEADER_AIC}_group-session-1_${LEADER_AIC}`,
    );
    const expected = published.emitter.leader_receive;
    await queue!.consumers[0]!(
      makeIncoming(Buffer.from(expected.incoming_body, "utf-8"), {
        userId: PARTNER_AIC,
        headers: { traceparent: `00-${"d".repeat(32)}-${"e".repeat(16)}-01` },
      }),
    );
    expect(emitter.calls.map((call) => emitRecord(call, { dropSettlement: true }))).toEqual(
      expected.emits,
    );
    expect(typeof emitter.calls[1]?.body.settlement?.latencyMs).toBe("number");
  });

  it("partner send: new trace when not inside a command", async () => {
    const emitter = new FakeEmitter();
    const recorder = new Recorder();
    const partner = new GroupPartnerMqClient(PARTNER_AIC, {
      messageEmitter: emitter,
      identityBindingEnabled: true,
    });
    partner._exchange = new FakeExchange(recorder, "group.test");
    partner._exchangeName = "group.test";
    partner._groupId = "group-session-1";
    partner.rabbitmqVhost = "acps";
    await partner.completeTask("task-1", "session-1");

    const expected = published.emitter.partner_send;
    expect(recorder.ops.at(-1)).toEqual(expected.publish);
    expect(emitter.calls.map((call) => emitRecord(call))).toEqual(expected.emits);
    // 不在命令处理链内：新 trace（固定的 newTraceId / newSpanId），父 span 为空
    expect((recorder.ops.at(-1) as Json).headers).toEqual({
      traceparent: `00-${FIXED_TRACE_ID}-${FIXED_SPAN_ID}-01`,
    });
    expect(emitter.calls[0]?.options).toMatchObject({
      traceId: FIXED_TRACE_ID,
      spanId: FIXED_SPAN_ID,
      parentSpanId: "",
    });
    expect(String(expected.publish.body)).toContain(`result-${FIXED_UUID}`);
  });
});

describe("Python fixtures: aip_group/http", () => {
  function scriptedTransport(responses: Array<{ status: number; body: unknown }>): {
    transport: HttpTransport;
    requests: HttpRequestOptions[];
  } {
    const requests: HttpRequestOptions[] = [];
    const transport: HttpTransport = async (request) => {
      requests.push(request);
      const next = responses.shift() ?? { status: 204, body: "" };
      const body = typeof next.body === "string" ? next.body : JSON.stringify(next.body);
      const response: HttpResponseData = {
        status: next.status,
        headers: {},
        body: Buffer.from(body, "utf-8"),
        peerCertificate: null,
      };
      return response;
    };
    return { transport, requests };
  }

  it("direct RPC invitation sends the same JSON-RPC body and maps responses like Python", async () => {
    const rpc = http.rpc;
    const { transport, requests } = scriptedTransport([
      { status: 200, body: rpc.ok.response },
      { status: 200, body: { jsonrpc: "2.0", id: "x", error: { code: -32020, message: "nope" } } },
      { status: 200, body: { jsonrpc: "2.0", id: "x" } },
      { status: 404, body: "missing" },
      { status: 200, body: "not json" },
    ]);
    const client = new GroupLeaderMqClient(LEADER_AIC, {
      rabbitmqHost: "mq.acps.example.com",
      rabbitmqPort: 5672,
      rabbitmqVhost: "acps",
      rabbitmqUser: "guest",
      rabbitmqPassword: "guest",
      connection: new FakeConnection(),
      connectionOwner: false,
      transport,
    });
    await client.connect();
    await client.createGroup({ groupId: "group-session-1", partnerAcsList: [{ aic: OTHER_AIC }] });

    const info = await client.invitePartner(
      { aic: PARTNER_AIC },
      "https://partner.example.com/group/rpc",
    );
    const request = requests[0];
    expect({
      method: request?.method,
      url: request?.url,
      content_type: request?.headers?.["Content-Type"],
      body: request?.body,
    }).toEqual(rpc.ok.request);
    const { joined_at: _joinedAt, ...partner } = info.toDict();
    expect(partner).toEqual(rpc.ok.partner);

    const url = "https://partner.example.com/group/rpc";
    await expect(client.invitePartner({ aic: OTHER_AIC }, url)).rejects.toThrow(rpc.refused);
    await expect(client.invitePartner({ aic: OTHER_AIC }, url)).rejects.toThrow(rpc.empty);
    await expect(client.invitePartner({ aic: OTHER_AIC }, url)).rejects.toThrow(rpc.not_found);
    const notJson = await client
      .invitePartner({ aic: OTHER_AIC }, url)
      .catch((error: Error) => error);
    expect(String((notJson as Error).message).split(":")[0]).toBe(rpc.not_json_prefix);
    expect(requests).toHaveLength(rpc.captured_count);
  });

  it("ACL client issues the same requests and error messages", async () => {
    const acl = http.acl;
    const { transport, requests } = scriptedTransport([
      { status: 204, body: "" },
      { status: 204, body: "" },
      { status: 204, body: "" },
      { status: 204, body: "" },
      { status: 204, body: "" },
      { status: 404, body: "  group not found \n" },
      { status: 503, body: "" },
    ]);
    const client = new GroupAclClient({ baseUrl: "https://auth.example.com/api/", transport });
    expect(client.baseUrl).toBe(acl.base_url);
    await client.addMember({ leaderAic: LEADER_AIC, groupId: "group-1", memberAic: PARTNER_AIC });
    await client.removeMember({
      leaderAic: LEADER_AIC,
      groupId: "group-1",
      memberAic: PARTNER_AIC,
    });
    await client.deleteGroup({ leaderAic: LEADER_AIC, groupId: "group-1" });
    await client.closeMemberConnection({
      leaderAic: LEADER_AIC,
      groupId: "group-1",
      memberAic: PARTNER_AIC,
    });
    await client.addMember({ leaderAic: "a b/c!*'()~", groupId: "中", memberAic: "x" });
    const withBody = await client
      .deleteGroup({ leaderAic: LEADER_AIC, groupId: "group-1" })
      .catch((e: Error) => e);
    const withoutBody = await client
      .removeMember({ leaderAic: LEADER_AIC, groupId: "group-1", memberAic: PARTNER_AIC })
      .catch((e: Error) => e);
    await client.aclose();

    expect(requests.map(({ method, url }) => ({ method, url }))).toEqual(acl.requests);
    expect(withBody).toBeInstanceOf(GroupAclError);
    expect((withBody as Error).message).toBe(acl.errors.with_body);
    expect((withoutBody as Error).message).toBe(acl.errors.without_body);
    expect(requests[0]?.timeoutMs).toBe(10_000);
  });
});
