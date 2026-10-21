/**
 * 互通测试的 Node 端代理进程（由 run_interop.py 启动，不单独使用）。
 *
 * 协议：stdin 每行一个 JSON 请求 `{"id", "op", "params"}`；stdout 每行一个 JSON：
 * - 应答 `{"id", "ok": true, "result"}` / `{"id", "ok": false, "error": {"name", "message", ...}}`
 * - 事件 `{"event", "data"}`（收到的回调、群组消息等）
 * stdout 只写协议行，其余输出一律走 stderr。
 *
 * 运行时加载构建产物 dist/（需先 `npm run build`）；类型取自 src/，因此 `tsc --noEmit` 不依赖 dist。
 * 用 Node 自带的类型剥离直接运行（Node >= 22.18 / 23.6），不需要 tsx 等额外依赖。
 */

import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { type IncomingMessage, type ServerResponse, request as httpRequest } from "node:http";
import { type Server, createServer, request as httpsRequest } from "node:https";
import type { AddressInfo } from "node:net";
import { createInterface } from "node:readline";

import type {
  AipRpcClient,
  DataItem,
  GroupLeaderMqClient,
  GroupMgmtCommand,
  GroupPartnerMqClient,
  InboxGroupInvitation,
  NotificationService,
  Product,
  StreamHub,
  TaskCommand,
  TaskResult,
  TaskState,
} from "../../src/aip/index.js";
import type * as AipModule from "../../src/aip/index.js";
import type * as RootModule from "../../src/index.js";
import type { TlsClientOptions } from "../../src/internal/http.js";

const distUrl = (path: string): string => new URL(`../../dist/${path}`, import.meta.url).href;
const sdk: typeof RootModule = await import(distUrl("index.js"));
const aip: typeof AipModule = await import(distUrl("aip/index.js"));

if (process.env.ACPS_INTEROP_DEBUG) {
  const write =
    (level: string, name: string) =>
    (message: string, ...args: unknown[]): void => {
      process.stderr.write(
        `[node ${level}] ${name}: ${message}${args.length ? " " + args.map(String).join(" ") : ""}\n`,
      );
    };
  sdk.setLoggerFactory((name) => ({
    debug: write("debug", name),
    info: write("info", name),
    warn: write("warn", name),
    error: write("error", name),
  }));
}

// ---------------------------------------------------------------------------
// 协议 I/O
// ---------------------------------------------------------------------------

function send(message: unknown): void {
  process.stdout.write(JSON.stringify(message) + "\n");
}

function emit(event: string, data: unknown): void {
  send({ event, data });
}

function describeError(error: unknown): Record<string, unknown> {
  if (error instanceof Error) {
    const described: Record<string, unknown> = { name: error.name, message: error.message };
    const extra = error as Error & { status?: unknown; code?: unknown; text?: unknown };
    if (typeof extra.status === "number") described["status"] = extra.status;
    if (extra.code !== undefined) described["code"] = String(extra.code);
    if (typeof extra.text === "string") described["body"] = extra.text;
    if (error.cause instanceof Error) described["cause"] = describeError(error.cause);
    return described;
  }
  return { name: typeof error, message: String(error) };
}

/** 把 SDK 对象转成纯 JSON（去掉 undefined，与线上序列化一致）。 */
function toJson<T>(value: T): unknown {
  return value === undefined ? null : JSON.parse(JSON.stringify(value));
}

// ---------------------------------------------------------------------------
// 共用工具
// ---------------------------------------------------------------------------

interface TlsSpec {
  cert?: string;
  key?: string;
  ca: string;
}

function loadTls(spec: TlsSpec): TlsClientOptions {
  return {
    cert: spec.cert ? readFileSync(spec.cert) : undefined,
    key: spec.key ? readFileSync(spec.key) : undefined,
    ca: readFileSync(spec.ca),
  };
}

const NEED_INPUT_MARK = "[need-input]";
const INPUT_PROMPT = "请补充信息";
const FILE_BYTES_B64 = Buffer.from("hello, 世界\n", "utf-8").toString("base64");

/** 与 interop_common.py 的 expected_products 完全一致。 */
function expectedProducts(taskId: string, text: string): Product[] {
  return [
    {
      id: `product-${taskId}`,
      name: "echo",
      dataItems: [
        { type: "text", text: `已处理：${text}` },
        {
          type: "data",
          data: {
            taskId,
            values: [1, 2.5, null, true, "✓"],
            nested: { empty: null, list: [] },
          },
        },
        { type: "file", name: "hello.txt", mimeType: "text/plain", bytes: FILE_BYTES_B64 },
      ],
    },
  ];
}

function commandText(command: TaskCommand): string {
  return (command.dataItems ?? [])
    .map((item: DataItem) => (item.type === "text" ? item.text : ""))
    .filter((text) => text)
    .join("\n");
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

type Listener = (req: IncomingMessage, res: ServerResponse) => Promise<boolean>;

async function listen(
  tls: TlsSpec,
  { strict }: { strict: boolean },
  listener: Listener,
): Promise<{ server: Server; baseUrl: string }> {
  const server = createServer(
    {
      cert: readFileSync(tls.cert ?? ""),
      key: readFileSync(tls.key ?? ""),
      ca: readFileSync(tls.ca),
      requestCert: true,
      // strict=false：不强制客户端证书（用来验证缺少对端身份时的 -32008）
      rejectUnauthorized: strict,
    },
    (req, res) => {
      void listener(req, res)
        .then((handled) => {
          if (!handled) {
            res.statusCode = 404;
            res.end();
          }
        })
        .catch((error: unknown) => {
          process.stderr.write(`[agent] listener error: ${String(error)}\n`);
          if (!res.headersSent) {
            res.statusCode = 500;
          }
          res.end();
        });
    },
  );
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return { server, baseUrl: `https://127.0.0.1:${port}` };
}

const servers: Server[] = [];

// ---------------------------------------------------------------------------
// Partner（Direct RPC / Stream / Notification 服务端）
// ---------------------------------------------------------------------------

interface PartnerState {
  aic: string;
  hub: StreamHub;
  notifications: NotificationService;
}

let partner: PartnerState | null = null;

function newTaskResult(
  aic: string,
  taskId: string,
  sessionId: string | undefined,
  state: TaskState,
  extra: Partial<TaskResult> = {},
): TaskResult {
  return aip.TaskResultSchema.parse({
    id: `result-${randomUUID()}`,
    sentAt: new Date().toISOString(),
    senderRole: "partner",
    senderId: aic,
    sessionId,
    taskId,
    status: { state, stateChangedAt: new Date().toISOString() },
    ...extra,
  });
}

async function runStreamTask(state: PartnerState, command: TaskCommand): Promise<void> {
  const taskId = command.taskId ?? "";
  const sessionId = command.sessionId;
  const text = commandText(command);
  const task = aip.TaskManager.createTask(command, aip.TaskState.Accepted);
  task.senderId = state.aic;
  const base = {
    sentAt: new Date().toISOString(),
    senderRole: "partner" as const,
    senderId: state.aic,
    taskId,
    sessionId,
  };
  await state.hub.publishTaskResult(taskId, structuredClone(task));
  await state.hub.publishStatusUpdate(
    taskId,
    aip.TaskStatusUpdateEventSchema.parse({
      ...base,
      id: `evt-${taskId}-2`,
      status: { state: aip.TaskState.Working, stateChangedAt: new Date().toISOString() },
    }),
  );
  await sleep(400);
  await state.hub.publishProductChunk(
    taskId,
    aip.ProductChunkEventSchema.parse({
      ...base,
      id: `evt-${taskId}-3`,
      product: {
        id: `product-${taskId}`,
        name: "stream",
        dataItems: [{ type: "text", text: "已处理：" }],
      },
      append: false,
      lastChunk: false,
    }),
  );
  await state.hub.publishProductChunk(
    taskId,
    aip.ProductChunkEventSchema.parse({
      ...base,
      id: `evt-${taskId}-4`,
      product: { id: `product-${taskId}`, dataItems: [{ type: "text", text }] },
      append: true,
      lastChunk: true,
    }),
  );
  aip.TaskManager.setProducts(taskId, expectedProducts(taskId, text));
  const awaiting = aip.TaskManager.updateTaskStatus(taskId, aip.TaskState.AwaitingCompletion);
  await state.hub.publishTaskResult(taskId, structuredClone(awaiting));
}

async function partnerServe(params: {
  aic: string;
  tls: TlsSpec;
  callbackTls: TlsSpec;
}): Promise<Record<string, string>> {
  const hub = new aip.StreamHub();
  const notifications = new aip.NotificationService({
    localAic: params.aic,
    callbackTls: loadTls(params.callbackTls),
    maxRetries: 0,
  });
  const state: PartnerState = { aic: params.aic, hub, notifications };
  partner = state;

  const rpc = aip.createAipRpcRequestListener(
    {
      async onStart(command, task) {
        if (task) {
          aip.TaskManager.addCommandToHistory(task.taskId, command);
          return task;
        }
        const text = commandText(command);
        let created: TaskResult;
        if (text.includes(NEED_INPUT_MARK)) {
          created = aip.TaskManager.createTask(command, aip.TaskState.AwaitingInput, [
            { type: "text", text: INPUT_PROMPT },
          ]);
        } else {
          created = aip.TaskManager.createTask(command, aip.TaskState.AwaitingCompletion);
          aip.TaskManager.setProducts(created.taskId, expectedProducts(created.taskId, text));
        }
        created.senderId = params.aic;
        return created;
      },
      async onContinue(command, task) {
        aip.TaskManager.addCommandToHistory(task.taskId, command);
        const text = commandText(command);
        if (task.status.state !== aip.TaskState.AwaitingInput || !text.trim()) {
          return task;
        }
        aip.TaskManager.setProducts(task.taskId, expectedProducts(task.taskId, text));
        return aip.TaskManager.updateTaskStatus(task.taskId, aip.TaskState.AwaitingCompletion);
      },
      async onComplete(command, task) {
        const result = await aip.DefaultHandlers.complete(command, task);
        if (result.status.state === aip.TaskState.Completed && hub.getChannel(task.taskId)) {
          await hub.publishTaskResult(task.taskId, structuredClone(result));
        }
        return result;
      },
    },
    { localAic: params.aic },
  );
  const stream = aip.createAipStreamRequestListener(
    hub,
    {
      onStreamStart: (command) => runStreamTask(state, command),
    },
    { localAic: params.aic },
  );
  const notification = aip.createAipNotificationRequestListener(notifications);

  const router: Listener = async (req, res) => {
    const path = new URL(req.url ?? "/", "http://localhost").pathname;
    if (req.method === "POST" && path === "/rpc") {
      await rpc(req, res);
      return true;
    }
    if (req.method === "POST" && path === "/stream") {
      await stream(req, res);
      return true;
    }
    return notification(req, res);
  };

  const strict = await listen(params.tls, { strict: true }, router);
  const permissive = await listen(params.tls, { strict: false }, router);
  servers.push(strict.server, permissive.server);
  return { baseUrl: strict.baseUrl, permissiveBaseUrl: permissive.baseUrl };
}

async function partnerNotify(params: {
  taskId: string;
  sessionId: string;
  state: TaskState;
}): Promise<unknown> {
  if (!partner) throw new Error("partner not started");
  const result = newTaskResult(partner.aic, params.taskId, params.sessionId, params.state);
  await partner.notifications.dispatch(result);
  return toJson(result);
}

// ---------------------------------------------------------------------------
// Leader（客户端）
// ---------------------------------------------------------------------------

interface ClientParams {
  leaderId: string;
  expectedPartnerAic: string;
  tls: TlsSpec;
}

function rpcClient(url: string, params: ClientParams): AipRpcClient {
  return new aip.AipRpcClient(url, params.leaderId, {
    tls: loadTls(params.tls),
    expectedPartnerAic: params.expectedPartnerAic,
  });
}

async function rpcCall(
  params: ClientParams & {
    url: string;
    method: "start" | "get" | "continue" | "complete" | "cancel";
    taskId: string;
    sessionId: string;
    text?: string;
  },
): Promise<unknown> {
  const client = rpcClient(params.url, params);
  try {
    switch (params.method) {
      case "start":
        return toJson(await client.startTask(params.sessionId, params.text ?? "", params.taskId));
      case "get":
        return toJson(await client.getTask(params.taskId, params.sessionId));
      case "continue":
        return toJson(
          await client.continueTask(params.taskId, params.sessionId, params.text ?? ""),
        );
      case "complete":
        return toJson(await client.completeTask(params.taskId, params.sessionId));
      case "cancel":
        return toJson(await client.cancelTask(params.taskId, params.sessionId));
    }
  } finally {
    await client.close();
  }
}

async function streamCollect(
  params: ClientParams & {
    streamUrl: string;
    rpcUrl: string;
    mode: "start" | "restream";
    taskId: string;
    sessionId: string;
    text?: string;
    lastEventSeq?: number | null;
    stopAfter?: number | null;
    completeOnAwaitingCompletion?: boolean;
  },
): Promise<unknown> {
  const client = new aip.AipStreamClient(params.streamUrl, params.rpcUrl, params.leaderId, {
    tls: loadTls(params.tls),
    expectedPartnerAic: params.expectedPartnerAic,
  });
  const generator =
    params.mode === "start"
      ? client.startStream(params.sessionId, { taskId: params.taskId, textContent: params.text })
      : client.reStream(params.taskId, params.sessionId, params.lastEventSeq ?? null);
  const events: unknown[] = [];
  let completeResult: unknown = null;
  try {
    for await (const event of generator) {
      events.push(toJson(event));
      if (params.stopAfter && events.length >= params.stopAfter) {
        break;
      }
      const data = event.result?.eventData;
      if (
        params.completeOnAwaitingCompletion &&
        data?.type === "task-result" &&
        data.status.state === aip.TaskState.AwaitingCompletion
      ) {
        completeResult = toJson(await client.complete(params.taskId, params.sessionId));
      }
    }
  } finally {
    await client.close();
  }
  return { events, lastEventSeq: client.lastEventSeq, completeResult };
}

const receivedCallbacks: unknown[] = [];

async function receiverStart(params: { tls: TlsSpec; token: string }): Promise<unknown> {
  const receiver = new aip.NotificationReceiver(params.token, async (taskResult) => {
    receivedCallbacks.push(toJson(taskResult));
    emit("notif.received", toJson(taskResult));
  });
  const listener = aip.createAipNotificationReceiverListener(receiver);
  const { server, baseUrl } = await listen(params.tls, { strict: true }, async (req, res) => {
    const path = new URL(req.url ?? "/", "http://localhost").pathname;
    if (req.method !== "POST" || !path.startsWith("/callbacks/")) return false;
    await listener(req, res);
    return true;
  });
  servers.push(server);
  return { baseUrl };
}

async function notifCall(
  params: ClientParams & {
    partnerUrl: string;
    method: "set" | "get" | "start" | "delete";
    taskId: string;
    callbackUrl?: string;
    token?: string;
    configId?: string | null;
    sessionId?: string;
    notifyOnStates?: string[] | null;
  },
): Promise<unknown> {
  const client = new aip.AipNotificationClient(params.partnerUrl, params.leaderId, {
    tls: loadTls(params.tls),
    expectedPartnerAic: params.expectedPartnerAic,
  });
  switch (params.method) {
    case "set":
      return toJson(
        await client.setConfig(
          params.taskId,
          params.callbackUrl ?? "",
          params.token ?? "",
          params.configId,
        ),
      );
    case "get":
      return toJson(await client.getConfigs(params.taskId, params.configId));
    case "start":
      return client.startNotification(
        params.taskId,
        params.configId ?? "",
        params.sessionId ?? "",
        params.notifyOnStates,
      );
    case "delete":
      return client.deleteConfig(params.taskId, params.configId);
  }
}

/** 原样发送一个 HTTPS POST（用于构造伪造请求、检查错误响应的线上形状）。 */
async function rawPost(params: {
  url: string;
  body: unknown;
  headers?: Record<string, string>;
  tls: TlsSpec;
}): Promise<unknown> {
  const tls = loadTls(params.tls);
  const url = new URL(params.url);
  const payload = typeof params.body === "string" ? params.body : JSON.stringify(params.body);
  const send = url.protocol === "https:" ? httpsRequest : httpRequest;
  return new Promise((resolve, reject) => {
    const req = send(
      url,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": String(Buffer.byteLength(payload)),
          ...(params.headers ?? {}),
        },
        agent: false,
        cert: tls.cert,
        key: tls.key,
        ca: tls.ca,
        timeout: 15_000,
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (chunk: Buffer) => chunks.push(chunk));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf-8");
          let body: unknown = text;
          try {
            body = JSON.parse(text);
          } catch {
            // 非 JSON（例如 SSE），原样返回
          }
          resolve({ status: res.statusCode, contentType: res.headers["content-type"], body });
        });
        res.on("error", reject);
      },
    );
    req.on("timeout", () => req.destroy(new Error("raw post timed out")));
    req.on("error", reject);
    req.end(payload);
  });
}

// ---------------------------------------------------------------------------
// 群组模式
// ---------------------------------------------------------------------------

interface BrokerParams {
  host: string;
  port: number;
  vhost: string;
  password: string;
}

let groupLeader: GroupLeaderMqClient | null = null;

async function groupLeaderOpen(
  params: BrokerParams & { aic: string; groupId: string },
): Promise<unknown> {
  const client = new aip.GroupLeaderMqClient(params.aic, {
    rabbitmqHost: params.host,
    rabbitmqPort: params.port,
    rabbitmqVhost: params.vhost,
    rabbitmqUser: params.aic,
    rabbitmqPassword: params.password,
    identityBindingEnabled: true,
  });
  await client.connect();
  const groupId = await client.createGroup({ groupId: params.groupId });
  client.setMessageHandler(async (message) => {
    emit("group.leader.message", toJson(message));
  });
  await client.startConsuming();
  groupLeader = client;
  return { groupId, exchange: client._exchangeName };
}

type Mentions = "all" | string[] | null;

async function groupLeaderCall(params: {
  method: string;
  partnerAic?: string;
  sessionId?: string | null;
  taskId?: string;
  text?: string | null;
  mentions?: Mentions;
}): Promise<unknown> {
  const client = groupLeader;
  if (!client) throw new Error("group leader not open");
  const mentions = params.mentions ?? null;
  const sessionId = params.sessionId ?? null;
  switch (params.method) {
    case "inviteViaInbox":
      return toJson(
        await client.inviteViaInbox(
          { aic: params.partnerAic ?? "" },
          { partnerInbox: aip.buildInboxQueueName(params.partnerAic ?? "") },
        ),
      );
    case "startTask":
      return client.startTask(sessionId ?? "", params.text ?? "", {
        taskId: params.taskId,
        mentions,
      });
    case "continueTask":
      await client.continueTask(params.taskId ?? "", sessionId ?? "", params.text ?? "", {
        mentions,
      });
      return null;
    case "completeTask":
      await client.completeTask(params.taskId ?? "", sessionId ?? "", { mentions });
      return null;
    case "cancelTask":
      await client.cancelTask(params.taskId ?? "", sessionId ?? "", { mentions });
      return null;
    case "getPartnerStatus":
      return toJson(await client.getPartnerStatus({ partnerAic: params.partnerAic, sessionId }));
    case "mutePartner":
      return toJson(await client.mutePartner(params.partnerAic ?? "", { sessionId }));
    case "unmutePartner":
      return toJson(await client.unmutePartner(params.partnerAic ?? "", { sessionId }));
    case "requestPartnerLeave":
      return toJson(await client.requestPartnerLeave(params.partnerAic ?? "", { sessionId }));
    case "dissolveGroup":
      await client.dissolveGroup();
      return { state: client.state };
    case "close":
      await client.close();
      groupLeader = null;
      return null;
    default:
      throw new Error(`unknown group leader method: ${params.method}`);
  }
}

interface PartnerReport {
  commands: Array<Record<string, unknown>>;
  mgmt: Array<Record<string, unknown>>;
  disconnects: Array<string | null>;
}

let groupPartner: GroupPartnerMqClient | null = null;
let groupPartnerReport: PartnerReport = { commands: [], mgmt: [], disconnects: [] };

async function groupPartnerOpen(params: BrokerParams & { aic: string }): Promise<unknown> {
  const client = new aip.GroupPartnerMqClient(params.aic, {
    rabbitmqHost: params.host,
    rabbitmqPort: params.port,
    rabbitmqVhost: params.vhost,
    rabbitmqUser: params.aic,
    rabbitmqPassword: params.password,
    identityBindingEnabled: true,
  });
  const report: PartnerReport = { commands: [], mgmt: [], disconnects: [] };
  groupPartnerReport = report;

  client.setCommandHandler(async (command, isMentioned) => {
    const text = commandText(command);
    report.commands.push({
      id: command.id,
      command: command.command,
      taskId: command.taskId,
      sessionId: command.sessionId,
      senderId: command.senderId,
      groupId: command.groupId,
      mentions: command.mentions,
      isMentioned,
      text,
    });
    if (!isMentioned) return;
    const taskId = command.taskId ?? "";
    const sessionId = command.sessionId ?? "";
    switch (command.command) {
      case aip.TaskCommandType.Start:
        await client.acceptTask(taskId, sessionId);
        await client.startWorking(taskId, sessionId);
        if (text.includes(NEED_INPUT_MARK)) {
          await client.requestInput(taskId, sessionId, INPUT_PROMPT);
        } else {
          await client.submitForCompletion(taskId, sessionId, expectedProducts(taskId, text));
        }
        break;
      case aip.TaskCommandType.Continue:
        await client.submitForCompletion(taskId, sessionId, expectedProducts(taskId, text));
        break;
      case aip.TaskCommandType.Complete:
        await client.completeTask(taskId, sessionId);
        break;
      case aip.TaskCommandType.Cancel:
        await client.cancelTask(taskId, sessionId);
        break;
      default:
        break;
    }
  });
  client.setMgmtCommandHandler(async (command: GroupMgmtCommand) => {
    const entry = {
      id: command.id,
      command: command.command,
      senderId: command.senderId,
      sessionId: command.sessionId,
      mentions: command.mentions,
    };
    report.mgmt.push(entry);
    emit("group.partner.mgmt", entry);
  });
  client.setDisconnectHandler((_client, groupId) => {
    report.disconnects.push(groupId);
    emit("group.partner.disconnected", { groupId });
  });
  await client.startInboxConsuming(async (invitation: InboxGroupInvitation) => {
    emit("group.partner.invitation", toJson(invitation));
    const ok = await client.joinGroupFromInvitation(invitation);
    emit("group.partner.joined", {
      ok,
      groupId: client.groupId,
      queueName: client.queueName,
      state: client.state,
    });
  });
  groupPartner = client;
  return { inbox: aip.buildInboxQueueName(params.aic) };
}

async function groupPartnerCall(params: {
  method: string;
  sessionId?: string | null;
}): Promise<unknown> {
  const client = groupPartner;
  switch (params.method) {
    case "report":
      return {
        ...groupPartnerReport,
        state: client?.state ?? null,
        groupId: client?.groupId ?? null,
        isMuted: client?.isMuted ?? null,
      };
    case "leave":
      if (!client) throw new Error("group partner not open");
      await client.leaveGroup({ sessionId: params.sessionId ?? null });
      return { state: client.state };
    case "close":
      if (client) await client.close();
      groupPartner = null;
      return null;
    default:
      throw new Error(`unknown group partner method: ${params.method}`);
  }
}

// ---------------------------------------------------------------------------
// 分发
// ---------------------------------------------------------------------------

type Handler = (params: never) => Promise<unknown>;

const OPS: Record<string, Handler> = {
  ping: async () => ({ node: process.version }),
  "partner.serve": partnerServe,
  "partner.notify": partnerNotify,
  "rpc.call": rpcCall,
  "stream.collect": streamCollect,
  "notif.receiver.start": receiverStart,
  "notif.received": async () => receivedCallbacks,
  "notif.call": notifCall,
  "raw.post": rawPost,
  "group.leader.open": groupLeaderOpen,
  "group.leader.call": groupLeaderCall,
  "group.partner.open": groupPartnerOpen,
  "group.partner.call": groupPartnerCall,
};

async function shutdown(): Promise<void> {
  for (const server of servers) {
    server.closeAllConnections();
    server.close();
  }
  await groupPartner?.close().catch(() => undefined);
  await groupLeader?.close().catch(() => undefined);
}

const lines = createInterface({ input: process.stdin });
lines.on("line", (line) => {
  if (!line.trim()) return;
  let request: { id: number; op: string; params?: unknown };
  try {
    request = JSON.parse(line) as typeof request;
  } catch (error) {
    emit("agent.error", describeError(error));
    return;
  }
  const handler = OPS[request.op];
  if (!handler) {
    send({ id: request.id, ok: false, error: { name: "UnknownOp", message: request.op } });
    return;
  }
  void handler((request.params ?? {}) as never).then(
    (result) => send({ id: request.id, ok: true, result: result ?? null }),
    (error: unknown) => send({ id: request.id, ok: false, error: describeError(error) }),
  );
});
lines.on("close", () => {
  void shutdown().finally(() => process.exit(0));
});

emit("agent.ready", { node: process.version, pid: process.pid });
