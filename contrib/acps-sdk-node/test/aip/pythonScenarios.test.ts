/**
 * 与 Python 版逐步比对的行为样例（scripts/fixtures/gen_aip_core.py 生成）：
 * RPC 服务端（DefaultHandlers / TaskManager）、流式服务端、通知路由 / 分发 / 接收、各客户端发出的请求体。
 *
 * 两边都把 UUID 与时间戳固定为同样的递增序列（Python patch `uuid.uuid4` / `datetime`，
 * 这里 mock `node:crypto.randomUUID` 与 `utcNowIso`），所以输出可以逐字节比较（含键顺序）。
 */

import type * as NodeCrypto from "node:crypto";

import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as TimeModule from "../../src/internal/time.js";

const seq = vi.hoisted(() => ({ uuid: 0, time: 0 }));

vi.mock("node:crypto", async (importOriginal) => {
  const actual = await importOriginal<typeof NodeCrypto>();
  return {
    ...actual,
    randomUUID: () => {
      seq.uuid += 1;
      return `00000000-0000-0000-0000-${seq.uuid.toString(16).padStart(12, "0")}`;
    },
  };
});

vi.mock("../../src/internal/time.js", async (importOriginal) => {
  const actual = await importOriginal<typeof TimeModule>();
  const pad = (value: number, width = 2): string => String(value).padStart(width, "0");
  return {
    ...actual,
    utcNowIso: () => {
      seq.time += 1;
      const n = seq.time;
      return `2026-01-01T00:${pad(Math.floor(n / 60))}:${pad(n % 60)}.${pad(n, 6)}+00:00`;
    },
  };
});

import {
  type TaskCommand,
  ProductSchema,
  TaskCommandSchema,
  TaskResultSchema,
  type TaskState,
} from "../../src/aip/baseModel.js";
import { AipIdentityError } from "../../src/aip/identity.js";
import { AipNotificationClient, NotificationReceiver } from "../../src/aip/notificationClient.js";
import {
  type NotificationMethod,
  type NotificationSubscription,
  NotificationService,
  handleNotificationRequest,
} from "../../src/aip/notificationServer.js";
import { AipRpcClient } from "../../src/aip/rpcClient.js";
import { type CommandHandlers, TaskManager, handleRpcRequest } from "../../src/aip/rpcServer.js";
import { AipStreamClient } from "../../src/aip/streamClient.js";
import {
  ProductChunkEventSchema,
  SSE_MEDIA_TYPE,
  TaskStatusUpdateEventSchema,
} from "../../src/aip/streamModel.js";
import {
  StreamHub,
  TaskStreamChannel,
  buildStreamResponse,
  formatSse,
  handleStreamRequest,
} from "../../src/aip/streamServer.js";
import type {
  HttpRequestOptions,
  HttpResponseData,
  HttpStreamResponse,
} from "../../src/internal/http.js";
import { loadPythonFixture } from "../fixtures.js";
import { LEADER_AIC, OTHER_AIC, PARTNER_AIC, flushAsync, pythonEnumToValue } from "./_helpers.js";

const NOW = "2026-06-24T00:00:00Z";

function resetSequences(): void {
  seq.uuid = 0;
  seq.time = 0;
}

beforeEach(() => {
  resetSequences();
  TaskManager.clear();
});

function resultFixture(
  taskId: string,
  state: string,
  senderId: string,
  id = "res",
): Record<string, unknown> {
  return {
    type: "task-result",
    id,
    sentAt: NOW,
    senderRole: "partner",
    senderId,
    taskId,
    status: { state, stateChangedAt: NOW },
  };
}

/** Python 在 -32700 响应里删掉 error.data 后追加的标记（pydantic 与 zod 的错误文本本来就不同）。 */
/**
 * Python 样例是 `model_dump(exclude_none=True)`，会省略 `"id": null`；Node 的 RPC 响应按 JSON-RPC / AIP §5.1
 * 在无法确定请求 id 时输出 `"id": null`（FastAPI 线上响应同样如此，见 docs/porting-notes.md）。比对前去掉它。
 */
function withoutNullId(value: Record<string, unknown>): Record<string, unknown> {
  if (value["id"] !== null) return value;
  const { id: _id, ...rest } = value;
  return rest;
}

function stripParseError(
  value: Record<string, unknown>,
  container: "error" | "detail" = "error",
): Record<string, unknown> {
  const inner = value[container] as Record<string, unknown> | undefined;
  if (inner && typeof inner === "object" && inner["code"] === -32700) {
    delete inner["data"];
    return { ...value, parseErrorDataOmitted: true };
  }
  return value;
}

// ===========================================================================
// RPC
// ===========================================================================

interface RpcStep {
  rpc?: unknown;
  peerAic?: string | null;
  setState?: [string, TaskState];
  setDefaultTask?: string;
  setProducts?: [string, unknown[], number | null];
  snapshot?: string;
}

interface RpcScenario {
  name: string;
  options: {
    identityBindingEnabled?: boolean;
    localAic?: string | null;
    peerAic?: string | null;
    handlers?: Record<string, string>;
  };
  steps: RpcStep[];
  outputs: Array<{
    response?: string;
    exception?: string;
    message?: string;
    snapshot?: string | null;
  }>;
}

const namedHandlers: Record<
  string,
  (command: TaskCommand) => Promise<ReturnType<typeof TaskResultSchema.parse>>
> = {
  raise: async () => {
    throw new Error("boom");
  },
  echo: async (command) =>
    TaskResultSchema.parse({
      ...resultFixture(command.taskId ?? "none", "working", PARTNER_AIC, "res-1"),
    }),
  forged: async (command) =>
    TaskResultSchema.parse({
      ...resultFixture(command.taskId ?? "none", "working", OTHER_AIC, "res-1"),
    }),
};

function buildHandlers(spec: Record<string, string> | undefined): CommandHandlers {
  const handlers: CommandHandlers = {};
  for (const [key, name] of Object.entries(spec ?? {})) {
    const fn = namedHandlers[name];
    if (!fn) throw new Error(name);
    (handlers as Record<string, unknown>)[key] = (command: TaskCommand) => fn(command);
  }
  return handlers;
}

describe("python fixtures: RPC server scenarios", () => {
  const scenarios = loadPythonFixture<RpcScenario[]>("aip", "rpc_scenarios");

  it.each(scenarios.map((s) => [s.name, s] as const))("%s", async (_name, scenario) => {
    const handlers = buildHandlers(scenario.options.handlers);
    for (const [index, step] of scenario.steps.entries()) {
      const expected = scenario.outputs[index];
      const where = `${scenario.name} step ${index}`;
      if (step.rpc !== undefined) {
        const peer = "peerAic" in step ? step.peerAic : scenario.options.peerAic;
        try {
          const response = await handleRpcRequest(
            { body: structuredClone(step.rpc), getPeerAic: () => peer ?? null },
            handlers,
            {
              localAic: scenario.options.localAic ?? null,
              identityBindingEnabled: scenario.options.identityBindingEnabled ?? true,
            },
          );
          expect(expected?.response, where).toBeDefined();
          const actual = JSON.stringify(
            stripParseError(
              withoutNullId(JSON.parse(JSON.stringify(response)) as Record<string, unknown>),
            ),
          );
          expect(actual, where).toBe(pythonEnumToValue(expected?.response ?? ""));
        } catch (error) {
          if (expected?.exception === undefined) throw error;
          expect(error, where).toBeInstanceOf(Error);
          // Python 的 task_id 为 None 时消息是 "Task with id None not found."；Node 的 taskId 为 undefined
          // （只影响 500 异常文本，不上线路）
          if (!expected.message?.includes("id None")) {
            expect((error as Error).message, where).toBe(expected.message);
          }
        }
      } else if (step.setState) {
        TaskManager.updateTaskStatus(step.setState[0], step.setState[1]);
      } else if (step.setDefaultTask) {
        TaskManager.createTask(
          TaskCommandSchema.parse({
            type: "task-command",
            id: "seed",
            sentAt: NOW,
            senderRole: "leader",
            senderId: LEADER_AIC,
            command: "start",
            sessionId: "sess-1",
            taskId: step.setDefaultTask,
          }),
        );
      } else if (step.setProducts) {
        const [taskId, products, maxBytes] = step.setProducts;
        if (TaskManager.getTask(taskId) && maxBytes !== null) {
          TaskManager.setMaxProductsBytes(taskId, maxBytes);
        }
        TaskManager.setProducts(
          taskId,
          products.map((p) => ProductSchema.parse(p)),
        );
      } else if (step.snapshot) {
        const task = TaskManager.getTask(step.snapshot);
        expect(task === null ? null : JSON.stringify(task), where).toBe(expected?.snapshot);
      }
    }
  });
});

// ===========================================================================
// Stream
// ===========================================================================

interface StreamScenario {
  name: string;
  options: { identityBindingEnabled?: boolean; localAic?: string | null; peerAic?: string | null };
  maxBuffer?: number;
  reStreamChannel?: boolean;
  publish?: Array<[string, Record<string, unknown>]>;
  onStart?: Array<[string, Record<string, unknown>]>;
  request: unknown;
  outcome: {
    status?: number;
    detail?: Record<string, unknown>;
    parseErrorDataOmitted?: boolean;
    mediaType?: string;
    frames?: string[];
    streamError?: string;
    streamErrorMessage?: string;
    exception?: string;
    message?: string;
  };
}

function parsePayload(payload: Record<string, unknown>) {
  switch (payload["type"]) {
    case "task-result":
      return TaskResultSchema.parse(payload);
    case "task-command":
      return TaskCommandSchema.parse(payload);
    case "task-status-update":
      return TaskStatusUpdateEventSchema.parse(payload);
    case "product-chunk":
      return ProductChunkEventSchema.parse(payload);
    default:
      throw new Error(String(payload["type"]));
  }
}

describe("python fixtures: stream server scenarios", () => {
  const scenarios = loadPythonFixture<StreamScenario[]>("aip", "stream_scenarios");

  it.each(scenarios.map((s) => [s.name, s] as const))("%s", async (_name, scenario) => {
    const hub = new StreamHub(scenario.maxBuffer ?? 200);
    for (const [taskId, payload] of scenario.publish ?? []) {
      const model = parsePayload(payload);
      if (model.type === "task-result") {
        await hub.publishTaskResult(taskId, model);
      } else {
        await hub.getOrCreateChannel(taskId).publish(model);
      }
    }
    const onStreamStart = async (): Promise<void> => {
      for (const [taskId, payload] of scenario.onStart ?? []) {
        await hub.publishTaskResult(taskId, TaskResultSchema.parse(payload));
      }
    };
    const expected = scenario.outcome;
    let outcome;
    try {
      outcome = await handleStreamRequest(
        {
          body: structuredClone(scenario.request),
          getPeerAic: () => scenario.options.peerAic ?? null,
        },
        hub,
        {
          onStreamStart,
          onReStream: async () => (scenario.reStreamChannel ? new TaskStreamChannel() : null),
        },
        {
          localAic: scenario.options.localAic ?? null,
          identityBindingEnabled: scenario.options.identityBindingEnabled ?? true,
        },
      );
    } catch (error) {
      expect(expected.exception).toBeDefined();
      expect((error as Error).message).toBe(expected.message);
      return;
    }
    if (outcome.kind === "error") {
      expect(outcome.status).toBe(expected.status);
      const detail = JSON.parse(JSON.stringify(outcome.detail)) as Record<string, unknown>;
      if (expected.parseErrorDataOmitted) delete detail["data"];
      expect(pythonEnumToValue(JSON.stringify(detail))).toBe(
        pythonEnumToValue(JSON.stringify(expected.detail)),
      );
      return;
    }
    expect(expected.status).toBe(200);
    expect(expected.mediaType).toBe(SSE_MEDIA_TYPE);
    const frames: string[] = [];
    try {
      for await (const frame of outcome.frames) frames.push(frame);
    } catch (error) {
      expect(error).toBeInstanceOf(AipIdentityError);
      expect((error as Error).name).toBe(expected.streamError);
      expect((error as Error).message).toBe(expected.streamErrorMessage);
      expect(frames).toEqual(expected.frames);
      return;
    }
    expect(expected.streamError).toBeUndefined();
    expect(frames).toEqual(expected.frames);
  });
});

// ===========================================================================
// Notification
// ===========================================================================

interface NotificationScenario {
  name: string;
  options: { identityBindingEnabled: boolean; localAic?: string | null; peerAic?: string | null };
  steps: Array<{
    method?: NotificationMethod;
    body?: unknown;
    peerAic?: string | null;
    matches?: [string, TaskState];
  }>;
  outputs: Array<{
    status?: number;
    body?: unknown;
    parseErrorDataOmitted?: boolean;
    matches?: unknown[];
  }>;
}

const matchView = (subs: NotificationSubscription[]) =>
  subs.map((sub) => ({
    taskId: sub.taskId,
    configId: sub.configId,
    ownerAic: sub.ownerAic ?? null,
    notifyOnStates: sub.notifyOnStates ?? null,
  }));

describe("python fixtures: notification router scenarios", () => {
  const scenarios = loadPythonFixture<NotificationScenario[]>("aip", "notification_scenarios");

  it.each(scenarios.map((s) => [s.name, s] as const))("%s", async (_name, scenario) => {
    const service = new NotificationService({
      localAic: scenario.options.localAic ?? null,
      identityBindingEnabled: scenario.options.identityBindingEnabled,
      transport: async () => ({
        status: 200,
        headers: {},
        body: Buffer.alloc(0),
        peerCertificate: null,
      }),
    });
    for (const [index, step] of scenario.steps.entries()) {
      const expected = scenario.outputs[index];
      const where = `${scenario.name} step ${index}`;
      if (step.matches) {
        expect(
          matchView(service.registry.matches(step.matches[0], step.matches[1])),
          where,
        ).toEqual(expected?.matches);
        continue;
      }
      const peer = "peerAic" in step ? step.peerAic : scenario.options.peerAic;
      const result = await handleNotificationRequest(service, step.method as NotificationMethod, {
        body: structuredClone(step.body),
        getPeerAic: () => peer ?? null,
      });
      expect(result.status, where).toBe(expected?.status);
      const body = JSON.parse(JSON.stringify(result.body)) as Record<string, unknown>;
      const normalized = expected?.parseErrorDataOmitted ? stripParseError(body, "detail") : body;
      const { parseErrorDataOmitted: _flag, ...normalizedBody } = normalized;
      expect(JSON.stringify(normalizedBody), where).toBe(JSON.stringify(expected?.body));
    }
  });
});

describe("python fixtures: notification dispatch", () => {
  const cases = loadPythonFixture<
    Array<{
      name: string;
      states: Array<TaskState[] | null>;
      result: Record<string, unknown>;
      requests: Array<{ url: string; token: string; contentType: string; body: string }>;
    }>
  >("aip", "notification_dispatch");

  it.each(cases.map((c) => [c.name, c] as const))("%s", async (_name, c) => {
    const captured: Array<{
      url: string;
      token: string | undefined;
      contentType: string | undefined;
      body: string;
    }> = [];
    const service = new NotificationService({
      identityBindingEnabled: false,
      transport: async (request: HttpRequestOptions): Promise<HttpResponseData> => {
        captured.push({
          url: request.url,
          token: request.headers?.["X-ACPs-AIP-Notification-Token"],
          contentType: request.headers?.["Content-Type"],
          body: String(request.body),
        });
        return { status: 204, headers: {}, body: Buffer.alloc(0), peerCertificate: null };
      },
    });
    c.states.forEach((states, index) => {
      service.store.set({
        id: `cfg-${index}`,
        url: `https://leader/cb/${index}`,
        token: `tok-${index}`,
        taskId: "task-1",
      });
      service.registry.add({ taskId: "task-1", configId: `cfg-${index}`, notifyOnStates: states });
    });
    await service.dispatch(TaskResultSchema.parse(c.result));
    expect(captured).toEqual(c.requests);
  });
});

describe("python fixtures: notification receiver", () => {
  const cases = loadPythonFixture<
    Array<{
      name: string;
      binding: boolean;
      body: string;
      token?: string;
      tokenHeader?: string;
      peerAic?: string;
      status: number;
      parseError?: boolean;
      responseBody?: unknown;
      handled: number;
    }>
  >("aip", "notification_receiver");

  it.each(cases.map((c) => [c.name, c] as const))("%s", async (_name, c) => {
    let handled = 0;
    const receiver = new NotificationReceiver(
      "tok",
      async () => {
        handled += 1;
      },
      { identityBindingEnabled: c.binding },
    );
    const headers: Record<string, string> = {};
    if (c.token !== undefined)
      headers[(c.tokenHeader ?? "X-ACPs-AIP-Notification-Token").toLowerCase()] = c.token;
    const result = await receiver.handle({
      rawBody: Buffer.from(c.body, "utf-8"),
      getPeerAic: () => c.peerAic ?? null,
      getHeader: (name) => headers[name.toLowerCase()],
    });
    await flushAsync();
    expect(result.status).toBe(c.status);
    if (c.parseError) {
      expect((result.body as { detail: string }).detail.startsWith("Parse error: ")).toBe(true);
    } else {
      expect(JSON.stringify(result.body)).toBe(JSON.stringify(c.responseBody));
    }
    expect(handled).toBe(c.handled);
  });
});

// ===========================================================================
// 客户端请求体
// ===========================================================================

interface RecordedClientRequest {
  url: string;
  method: string;
  contentType: string | null;
  accept: string | null;
  body: string;
}

interface ClientRequestsFixture {
  rpc: { requests: RecordedClientRequest[]; results: string[] };
  stream: { requests: RecordedClientRequest[]; results: Array<string | number | null> };
  notification: { requests: RecordedClientRequest[]; results: unknown[] };
}

const clientFixture = loadPythonFixture<ClientRequestsFixture>("aip", "client_requests");

type Recorded = {
  url: string;
  method: string;
  contentType: string | undefined;
  accept: string | undefined;
  body: string;
};

function record(log: Recorded[], request: HttpRequestOptions): Record<string, unknown> {
  const body = String(request.body);
  log.push({
    url: request.url,
    method: request.method,
    contentType: request.headers?.["Content-Type"],
    accept: request.headers?.["Accept"],
    body,
  });
  return JSON.parse(body) as Record<string, unknown>;
}

function rpcReply(body: Record<string, unknown>): HttpResponseData {
  const command = (body["params"] as { command: { taskId: string } }).command;
  const payload = {
    jsonrpc: "2.0",
    id: body["id"],
    result: resultFixture(command.taskId, "working", "agent"),
  };
  return {
    status: 200,
    headers: {},
    body: Buffer.from(JSON.stringify(payload)),
    peerCertificate: null,
  };
}

/** Python 记录的是 httpx 发出的头；Node 不发默认 Accept，因此只在 Python 显式设置时比较。 */
function compareRequests(actual: Recorded[], expected: RecordedClientRequest[]): void {
  expect(actual.length).toBe(expected.length);
  actual.forEach((request, index) => {
    const want = expected[index];
    expect(request.url).toBe(want?.url);
    expect(request.method).toBe(want?.method);
    expect(request.contentType).toBe(want?.contentType);
    if (want?.accept && want.accept !== "*/*") expect(request.accept).toBe(want.accept);
    expect(request.body).toBe(want?.body);
  });
}

describe("python fixtures: client request bodies", () => {
  it("AipRpcClient", async () => {
    const log: Recorded[] = [];
    const client = new AipRpcClient("http://partner/rpc", "leader-1", {
      transport: async (request) => rpcReply(record(log, request)),
      identityBindingEnabled: false,
    });
    const results = [
      await client.startTask("sess-1", "hello"),
      await client.startTask("sess-1", "", "t-fixed"),
      await client.continueTask("t-fixed", "sess-1", "more"),
      await client.completeTask("t-fixed", "sess-1"),
      await client.cancelTask("t-fixed", "sess-1"),
      await client.getTask("t-fixed", "sess-1"),
    ];
    await client.close();
    compareRequests(log, clientFixture.rpc.requests);
    expect(results.map((r) => JSON.stringify(r))).toEqual(clientFixture.rpc.results);
  });

  it("AipStreamClient", async () => {
    const log: Recorded[] = [];
    const sse = [1, 2]
      .map((eventSeq) =>
        formatSse(
          buildStreamResponse("s", {
            eventSeq,
            payload: TaskResultSchema.parse(
              resultFixture("t-s", eventSeq === 1 ? "working" : "completed", "agent"),
            ),
            isTerminal: false,
          }),
        ),
      )
      .join("");
    const client = new AipStreamClient("http://partner/stream", "http://partner/rpc", "leader-1", {
      identityBindingEnabled: false,
      transport: async (request) => rpcReply(record(log, request)),
      streamTransport: async (request): Promise<HttpStreamResponse> => {
        record(log, request);
        return {
          status: 200,
          headers: {},
          peerCertificate: null,
          body: (async function* () {
            yield Buffer.from(sse);
          })(),
          close: () => undefined,
        };
      },
    });
    const collected: Array<string | number | null> = [];
    for await (const event of client.startStream("sess-1", { textContent: "hi" }))
      collected.push(JSON.stringify(event));
    for await (const event of client.startStream("sess-1", { taskId: "t-s" }))
      collected.push(JSON.stringify(event));
    for await (const event of client.reStream("t-s", "sess-1", 1))
      collected.push(JSON.stringify(event));
    for await (const event of client.reStream("t-s", "sess-1"))
      collected.push(JSON.stringify(event));
    collected.push(JSON.stringify(await client.complete("t-s", "sess-1")));
    collected.push(JSON.stringify(await client.cancel("t-s", "sess-1")));
    collected.push(client.lastEventSeq);
    await client.close();
    compareRequests(log, clientFixture.stream.requests);
    expect(collected).toEqual(clientFixture.stream.results);
  });

  it("AipNotificationClient", async () => {
    const log: Recorded[] = [];
    const reply = (body: Record<string, unknown>): unknown => {
      const id = body["id"];
      switch (body["method"]) {
        case "notification/set": {
          const { id: configId, ...rest } = body["params"] as Record<string, unknown>;
          return { jsonrpc: "2.0", id, result: { id: configId || "cfg-new", ...rest } };
        }
        case "notification/get":
          return {
            jsonrpc: "2.0",
            id,
            result: [{ taskId: "task-1", token: "tok", url: "https://leader/cb", id: "cfg-1" }],
          };
        case "notification/delete":
          return { jsonrpc: "2.0", id, result: { success: true } };
        default:
          return { jsonrpc: "2.0", id, result: true };
      }
    };
    const client = new AipNotificationClient("http://partner/", "leader-1", {
      identityBindingEnabled: false,
      transport: async (request) => ({
        status: 200,
        headers: {},
        body: Buffer.from(JSON.stringify(reply(record(log, request)))),
        peerCertificate: null,
      }),
    });
    const results: unknown[] = [
      JSON.stringify(await client.setConfig("task-1", "https://leader/cb", "tok")),
      JSON.stringify(await client.setConfig("task-1", "https://leader/cb", "tok", "cfg-9")),
      await client.deleteConfig("task-1"),
      await client.deleteConfig("task-1", "cfg-9"),
      (await client.getConfigs("task-1")).map((c) => JSON.stringify(c)),
      (await client.getConfigs("task-1", "cfg-1")).map((c) => JSON.stringify(c)),
      await client.startNotification("task-1", "cfg-1", "sess-1"),
      await client.startNotification("task-1", "cfg-1", "sess-1", ["completed", "failed"]),
      await client.startNotification("task-1", "cfg-1", "sess-1", []),
    ];
    await client.close();
    compareRequests(log, clientFixture.notification.requests);
    expect(results).toEqual(clientFixture.notification.results);
  });
});
