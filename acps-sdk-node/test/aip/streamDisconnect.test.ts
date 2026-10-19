/**
 * 客户端断开后必须释放订阅（Python 中由 Starlette 取消 SSE 生成器完成）。
 */

import { type AddressInfo } from "node:net";
import { createServer, request } from "node:http";

import { afterEach, describe, expect, it } from "vitest";

import { StreamHub, createAipStreamRequestListener } from "../../src/aip/streamServer.js";
import { utcNowIso } from "../../src/internal/time.js";

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

function startBody(taskId: string): string {
  return JSON.stringify({
    jsonrpc: "2.0",
    id: `req-${taskId}`,
    method: "stream",
    params: {
      message: {
        type: "task-command",
        id: `cmd-${taskId}`,
        sentAt: utcNowIso(),
        senderRole: "leader",
        senderId: "leader",
        command: "start",
        taskId,
        sessionId: "sess-1",
      },
    },
  });
}

/** 发起 stream/start，收到响应头后立刻断开。 */
function startAndDisconnect(port: number, taskId: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const body = startBody(taskId);
    const req = request(
      {
        host: "127.0.0.1",
        port,
        path: "/stream",
        method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
      },
      (res) => {
        res.destroy();
        req.destroy();
        resolve();
      },
    );
    req.on("error", (error) =>
      (error as NodeJS.ErrnoException).code === "ECONNRESET" ? resolve() : reject(error),
    );
    req.end(body);
  });
}

describe("stream subscriptions are released when the client disconnects", () => {
  it("disconnected subscribers do not accumulate on idle channels", async () => {
    const hub = new StreamHub();
    const listener = createAipStreamRequestListener(hub, async () => undefined, {
      identityBindingEnabled: false,
    });
    const server = createServer((req, res) => void listener(req, res));
    servers.push(server);
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const { port } = server.address() as AddressInfo;

    const taskIds = Array.from({ length: 20 }, (_, index) => `task-${index}`);
    for (const taskId of taskIds) {
      await startAndDisconnect(port, taskId);
    }
    await new Promise((resolve) => setTimeout(resolve, 100));

    const remaining = taskIds.reduce(
      (total, taskId) => total + (hub.getChannel(taskId)?.subscriberCount ?? 0),
      0,
    );
    expect(remaining).toBe(0);
  });
});
