/**
 * NotificationReceiver 挂在 Express `express.json()` 之后时，请求流已被读走，必须使用 `req.body`。
 */

import { type AddressInfo } from "node:net";
import { type IncomingMessage, createServer, request } from "node:http";

import { afterEach, describe, expect, it } from "vitest";

import type { TaskResult } from "../../src/aip/baseModel.js";
import { NotificationReceiver } from "../../src/aip/notificationClient.js";
import { NOTIFICATION_TOKEN_HEADER } from "../../src/aip/notificationModel.js";
import { readRequestBody } from "../../src/aip/rpcServer.js";
import { utcNowIso } from "../../src/internal/time.js";

const servers: Array<ReturnType<typeof createServer>> = [];

afterEach(async () => {
  await Promise.all(
    servers
      .splice(0)
      .map((server) => new Promise<void>((resolve) => server.close(() => resolve()))),
  );
});

function post(
  port: number,
  body: string,
  token: string,
): Promise<{ status: number; text: string }> {
  return new Promise((resolve, reject) => {
    const req = request(
      {
        host: "127.0.0.1",
        port,
        path: "/callback",
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
          [NOTIFICATION_TOKEN_HEADER]: token,
        },
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (chunk: Buffer) => chunks.push(chunk));
        res.on("end", () =>
          resolve({ status: res.statusCode ?? 0, text: Buffer.concat(chunks).toString("utf-8") }),
        );
      },
    );
    req.on("error", reject);
    req.end(body);
  });
}

describe("NotificationReceiver with a pre-parsed body", () => {
  it("uses req.body when a JSON body parser already consumed the stream", async () => {
    const received: TaskResult[] = [];
    const receiver = new NotificationReceiver(
      "secret-token",
      async (taskResult) => {
        received.push(taskResult);
      },
      { identityBindingEnabled: false },
    );
    const listener = receiver.createRequestListener();
    const server = createServer((req, res) => {
      // 模拟 express.json()：读走请求流并把解析结果放到 req.body
      void readRequestBody(req).then((raw) => {
        (req as IncomingMessage & { body?: unknown }).body = JSON.parse(raw.toString("utf-8"));
        void listener(req, res);
      });
    });
    servers.push(server);
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const { port } = server.address() as AddressInfo;

    const now = utcNowIso();
    const taskResult = {
      type: "task-result",
      id: "result-1",
      sentAt: now,
      senderRole: "partner",
      senderId: "partner",
      taskId: "task-1",
      status: { state: "completed", stateChangedAt: now },
    };
    const response = await post(port, JSON.stringify(taskResult), "secret-token");
    expect(response.status).toBe(200);
    expect(JSON.parse(response.text)).toEqual({ ok: true });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(received.map((item) => item.taskId)).toEqual(["task-1"]);
  });
});
