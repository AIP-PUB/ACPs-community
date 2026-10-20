/**
 * 全量审查发现的 AIP 核心问题的回归测试。
 */

import { type AddressInfo } from "node:net";
import { type IncomingMessage, type ServerResponse, createServer, request } from "node:http";

import { afterEach, describe, expect, it } from "vitest";

import { type TaskResult, TaskState } from "../../src/aip/baseModel.js";
import { InvalidPeerCertificateError } from "../../src/aip/identity.js";
import {
  NotificationConfigStore,
  NotificationDispatcher,
  NotificationRegistry,
  NotificationService,
  createAipNotificationRequestListener,
  handleNotificationRequest,
} from "../../src/aip/notificationServer.js";
import { handleRpcRequest } from "../../src/aip/rpcServer.js";
import { AipStreamClient } from "../../src/aip/streamClient.js";
import {
  StreamHub,
  createAipStreamRequestListener,
  handleStreamRequest,
  writeStreamOutcome,
} from "../../src/aip/streamServer.js";
import {
  type HttpStreamTransport,
  type HttpTransport,
  ResponseTooLargeError,
  createNodeTransport,
} from "../../src/internal/http.js";
import { utcNowIso } from "../../src/internal/time.js";

const LOCAL_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF547.0JU5";
const servers: Array<ReturnType<typeof createServer>> = [];

afterEach(async () => {
  await Promise.all(
    servers.splice(0).map(
      (server) =>
        new Promise<void>((resolve) => {
          server.closeAllConnections();
          server.close(() => resolve());
        }),
    ),
  );
});

async function listen(
  handler: (req: IncomingMessage, res: ServerResponse) => void,
): Promise<number> {
  const server = createServer(handler);
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return (server.address() as AddressInfo).port;
}

function command(kind: string, taskId: string, extra: Record<string, unknown> = {}) {
  return {
    type: "task-command",
    id: `cmd-${taskId}`,
    sentAt: utcNowIso(),
    senderRole: "leader",
    senderId: "leader",
    command: kind,
    taskId,
    sessionId: "sess-1",
    ...extra,
  };
}

function working(taskId: string): TaskResult {
  const now = utcNowIso();
  return {
    type: "task-result",
    id: `r-${taskId}`,
    sentAt: now,
    senderRole: "partner",
    senderId: "partner",
    taskId,
    status: { state: TaskState.Working, stateChangedAt: now },
  };
}

describe("JSON-RPC response id", () => {
  it("is null (not omitted) when the request carries no id", async () => {
    const response = await handleRpcRequest(
      {
        body: { jsonrpc: "2.0", method: "rpc", params: { command: command("get", "") } },
        getPeerAic: () => null,
      },
      {},
      { identityBindingEnabled: false },
    );
    expect(JSON.parse(JSON.stringify(response))).toMatchObject({
      id: null,
      error: { code: -32602 },
    });
  });

  it("is null on parse errors", async () => {
    const response = await handleRpcRequest(
      { body: { nonsense: true }, getPeerAic: () => null },
      {},
      { identityBindingEnabled: false },
    );
    expect(JSON.stringify(response)).toContain('"id":null');
  });
});

describe("notification request listener", () => {
  it("does not throw on an unparsable absolute-form request target", async () => {
    const service = new NotificationService({ identityBindingEnabled: false });
    const listener = createAipNotificationRequestListener(service);
    const fakeReq = { method: "POST", url: "http://[", headers: {} } as unknown as IncomingMessage;
    await expect(listener(fakeReq, {} as ServerResponse)).resolves.toBe(false);
    const getReq = {
      method: "GET",
      url: "/notification/set",
      headers: {},
    } as unknown as IncomingMessage;
    await expect(listener(getReq, {} as ServerResponse)).resolves.toBe(false);
  });

  it("does not read the peer certificate when identity binding is disabled", async () => {
    const service = new NotificationService({ identityBindingEnabled: false });
    const result = await handleNotificationRequest(service, "set", {
      body: {
        jsonrpc: "2.0",
        method: "notification/set",
        id: "1",
        params: { url: "https://leader/cb", token: "t", taskId: "task-1" },
      },
      getPeerAic: () => {
        throw new InvalidPeerCertificateError("peer certificate is not trusted: test");
      },
    });
    expect(result.status).toBe(200);
  });
});

describe("NotificationDispatcher", () => {
  it("validates localAic format at construction", () => {
    expect(
      () =>
        new NotificationDispatcher(new NotificationConfigStore(), new NotificationRegistry(), {
          localAic: "not-an-aic",
          transport: async () => ({
            status: 200,
            headers: {},
            body: Buffer.alloc(0),
            peerCertificate: null,
          }),
        }),
    ).toThrow("local_aic is invalid");
  });

  it("close() aborts in-flight retries", async () => {
    const service = new NotificationService({
      localAic: LOCAL_AIC,
      transport: async () => {
        throw new Error("connection refused");
      },
      maxRetries: 5,
      backoffS: 60,
    });
    const config = service.store.set({ url: "https://leader/cb", token: "t", taskId: "task-1" });
    service.registry.add({ taskId: "task-1", configId: config.id as string });
    const dispatched = service.dispatch({ ...working("task-1"), senderId: LOCAL_AIC });
    await new Promise((resolve) => setTimeout(resolve, 20));
    const started = Date.now();
    await service.close();
    await dispatched;
    expect(Date.now() - started).toBeLessThan(1000);
  });
});

describe("stream server", () => {
  it("releases the subscription when the client is gone before the listener runs", async () => {
    const hub = new StreamHub();
    hub.getOrCreateChannel("t-gap");
    await hub.publishTaskResult("t-gap", working("t-gap"));
    const listener = createAipStreamRequestListener(hub, async () => undefined, {
      identityBindingEnabled: false,
    });
    let listenerDone: Promise<void> | null = null;
    const port = await listen((req, res) => {
      // 模拟耗时的前置中间件：读完请求体后延迟再交给监听函数
      const chunks: Buffer[] = [];
      req.on("data", (chunk: Buffer) => chunks.push(chunk));
      req.on("end", () => {
        (req as IncomingMessage & { body?: unknown }).body = JSON.parse(
          Buffer.concat(chunks).toString("utf-8"),
        );
        setTimeout(() => {
          listenerDone = listener(req, res);
        }, 150);
      });
    });
    const body = JSON.stringify({
      jsonrpc: "2.0",
      id: "r1",
      method: "stream",
      params: { message: command("re-stream", "t-gap") },
    });
    await new Promise<void>((resolve) => {
      const req = request({
        host: "127.0.0.1",
        port,
        path: "/stream",
        method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
      });
      req.on("error", () => resolve());
      req.end(body, () => {
        setTimeout(() => {
          req.destroy();
          resolve();
        }, 30);
      });
    });
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(listenerDone).not.toBeNull();
    await expect(
      Promise.race([
        listenerDone,
        new Promise((_, reject) => setTimeout(() => reject(new Error("listener hung")), 1000)),
      ]),
    ).resolves.toBeUndefined();
    expect(hub.getChannel("t-gap")?.subscriberCount).toBe(0);
  });

  it("writeStreamOutcome finishes when the response is already destroyed", async () => {
    const hub = new StreamHub();
    hub.getOrCreateChannel("t-dead");
    await hub.publishTaskResult("t-dead", working("t-dead"));
    const outcome = await handleStreamRequest(
      {
        body: {
          jsonrpc: "2.0",
          id: "r",
          method: "stream",
          params: { message: command("re-stream", "t-dead") },
        },
        getPeerAic: () => null,
      },
      hub,
      async () => undefined,
      { identityBindingEnabled: false },
    );
    const port = await listen((_req, res) => {
      res.destroy();
      void writeStreamOutcome(res, outcome).then(() => res.emit("written"));
    });
    await new Promise<void>((resolve) => {
      const req = request({ host: "127.0.0.1", port, method: "GET", path: "/" });
      req.on("error", () => resolve());
      req.end();
    });
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(hub.getChannel("t-dead")?.subscriberCount).toBe(0);
  });

  it("rejects a non-integer lastEventSeq with 400", async () => {
    const hub = new StreamHub();
    hub.getOrCreateChannel("t-seq");
    const outcome = await handleStreamRequest(
      {
        body: {
          jsonrpc: "2.0",
          id: "r",
          method: "stream",
          params: {
            message: command("re-stream", "t-seq", { commandParams: { lastEventSeq: "5" } }),
          },
        },
        getPeerAic: () => null,
      },
      hub,
      async () => undefined,
      { identityBindingEnabled: false },
    );
    expect(outcome).toMatchObject({ kind: "error", status: 400, detail: { code: -32602 } });
  });
});

describe("stream client", () => {
  function sseTransport(frames: string[]): HttpStreamTransport {
    return async () => ({
      status: 200,
      headers: {},
      peerCertificate: null,
      body: (async function* () {
        for (const frame of frames) yield Buffer.from(frame);
      })(),
      close: () => undefined,
    });
  }

  function frame(seq: number, taskId: string): string {
    return `data: ${JSON.stringify({ jsonrpc: "2.0", id: "x", result: { eventSeq: seq, eventData: working(taskId) } })}\n\n`;
  }

  it("startStream resets lastEventSeq from a previous task", async () => {
    let frames = [frame(1, "t1"), frame(2, "t1"), frame(3, "t1")];
    const client = new AipStreamClient("http://p/stream", "http://p/rpc", "leader", {
      identityBindingEnabled: false,
      streamTransport: (options) => sseTransport(frames)(options),
    });
    for await (const _event of client.startStream("s", { taskId: "t1" })) {
      // drain
    }
    expect(client.lastEventSeq).toBe(3);
    frames = [];
    for await (const _event of client.startStream("s", { taskId: "t2" })) {
      // no events
    }
    expect(client.lastEventSeq).toBeNull();
  });

  it("aborts on an oversized SSE event", async () => {
    const client = new AipStreamClient("http://p/stream", "http://p/rpc", "leader", {
      identityBindingEnabled: false,
      maxEventChars: 1024,
      streamTransport: sseTransport([`data: ${"x".repeat(4096)}`, "\n\n"]),
    });
    await expect(
      (async () => {
        for await (const _event of client.startStream("s", { taskId: "t" })) {
          // drain
        }
      })(),
    ).rejects.toThrow(/exceeds 1024 characters/);
  });
});

describe("http transport", () => {
  it("rejects responses larger than maxResponseBytes", async () => {
    const port = await listen((_req, res) => {
      res.end(Buffer.alloc(64 * 1024, 0x61));
    });
    const transport: HttpTransport = createNodeTransport(undefined, { maxResponseBytes: 1024 });
    await expect(
      transport({ url: `http://127.0.0.1:${port}/`, method: "GET" }),
    ).rejects.toBeInstanceOf(ResponseTooLargeError);
  });
});
