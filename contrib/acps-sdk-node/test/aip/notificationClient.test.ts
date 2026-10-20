/**
 * 对应 Python `tests/test_notification_client.py`（N3：AipNotificationClient + NotificationReceiver）。
 */

import { afterEach, describe, expect, it } from "vitest";

import { type TaskResult, TaskState } from "../../src/aip/baseModel.js";
import {
  AipNotificationClient,
  NotificationReceiver,
  constantTimeEquals,
} from "../../src/aip/notificationClient.js";
import { NOTIFICATION_TOKEN_HEADER } from "../../src/aip/notificationModel.js";
import {
  NotificationService,
  handleNotificationRequest,
} from "../../src/aip/notificationServer.js";
import type { HttpRequestOptions, HttpResponseData } from "../../src/internal/http.js";
import { utcNowIso } from "../../src/internal/time.js";
import { type TestHttpServer, flushAsync, memoryTransport, startHttpServer } from "./_helpers.js";

const NOW = utcNowIso();

function taskResult(taskId = "t-1", state: TaskState = TaskState.Completed): TaskResult {
  return {
    type: "task-result",
    id: "tr-1",
    sentAt: NOW,
    senderRole: "partner",
    senderId: "agent",
    taskId,
    status: { state, stateChangedAt: NOW },
  };
}

/** 模拟 Partner 通知服务，回显请求 ID。 */
function notifServerTransport(responseBody: Record<string, unknown>) {
  return memoryTransport((req) => ({
    body: { ...responseBody, id: (req.json as { id?: unknown }).id },
  }));
}

function client(transport: ReturnType<typeof memoryTransport>): AipNotificationClient {
  return new AipNotificationClient("http://partner/", "l", {
    transport,
    identityBindingEnabled: false,
  });
}

const servers: TestHttpServer[] = [];

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => server.close()));
});

async function mountReceiver(receiver: NotificationReceiver): Promise<string> {
  const server = await startHttpServer(receiver.createRequestListener());
  servers.push(server);
  return `${server.url}/callback`;
}

describe("test_notification_client: AipNotificationClient", () => {
  it("test_set_notification", async () => {
    const transport = notifServerTransport({
      jsonrpc: "2.0",
      result: { id: "nc-1", url: "http://cb/n", token: "tok", taskId: "t-1" },
    });
    const result = await client(transport).setNotification("t-1", "http://cb/n", "tok");
    expect(result.id).toBe("nc-1");
    expect(result.taskId).toBe("t-1");
    // partner_url 末尾的 / 被去掉
    expect(transport.requests[0]?.url).toBe("http://partner/notification/set");
  });

  it("test_delete_notification", async () => {
    const success = await client(
      notifServerTransport({ jsonrpc: "2.0", result: { success: true } }),
    ).deleteNotification("t-1", "nc-1");
    expect(success).toBe(true);
  });

  it("test_get_notifications", async () => {
    const configs = await client(
      notifServerTransport({
        jsonrpc: "2.0",
        result: [{ id: "nc-1", url: "http://cb/n", token: "tok", taskId: "t-1" }],
      }),
    ).getNotifications("t-1");
    expect(configs).toHaveLength(1);
    expect(configs[0]?.id).toBe("nc-1");
  });

  it("test_start_notification", async () => {
    const ok = await client(
      notifServerTransport({ jsonrpc: "2.0", result: true }),
    ).startNotification("t-1", "nc-1", "sess-1");
    expect(ok).toBe(true);
  });

  it("set_config raises when the partner returns no result", async () => {
    await expect(
      client(notifServerTransport({ jsonrpc: "2.0" })).setConfig("t-1", "http://cb", "tok"),
    ).rejects.toThrow("notification/set returned no result");
  });

  it("non-2xx responses raise HttpStatusError", async () => {
    const transport = memoryTransport(() => ({ status: 404, rawBody: "missing" }));
    await expect(client(transport).deleteConfig("t-1")).rejects.toThrow(
      "HTTP Error: 404 - missing",
    );
  });
});

describe("test_notification_client: NotificationReceiver", () => {
  async function postCallback(url: string, body: string, headers: Record<string, string>) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body,
    });
  }

  it("test_receiver_valid_token_calls_handler", async () => {
    const token = "my-secret-token";
    const received: TaskResult[] = [];
    const url = await mountReceiver(
      new NotificationReceiver(token, async (result) => void received.push(result), {
        identityBindingEnabled: false,
      }),
    );
    const resp = await postCallback(url, JSON.stringify(taskResult()), {
      [NOTIFICATION_TOKEN_HEADER]: token,
    });
    expect(resp.status).toBe(200);
    expect(await resp.json()).toEqual({ ok: true });
    await flushAsync();
    expect(received).toHaveLength(1);
    expect(received[0]?.taskId).toBe("t-1");
  });

  it("test_receiver_legacy_token_header_calls_handler", async () => {
    const token = "legacy-token";
    const received: TaskResult[] = [];
    const url = await mountReceiver(
      new NotificationReceiver(token, async (result) => void received.push(result), {
        identityBindingEnabled: false,
      }),
    );
    const resp = await postCallback(url, JSON.stringify(taskResult()), {
      "X-ACPS-AIP-Notification-Token": token,
    });
    expect(resp.status).toBe(200);
    await flushAsync();
    expect(received).toHaveLength(1);
    expect(received[0]?.taskId).toBe("t-1");
  });

  it("test_receiver_invalid_token_401", async () => {
    const received: TaskResult[] = [];
    const url = await mountReceiver(
      new NotificationReceiver("correct-token", async (result) => void received.push(result), {
        identityBindingEnabled: false,
      }),
    );
    const resp = await postCallback(url, JSON.stringify(taskResult()), {
      [NOTIFICATION_TOKEN_HEADER]: "wrong-token",
    });
    expect(resp.status).toBe(401);
    await flushAsync();
    expect(received).toEqual([]);
  });

  it("test_receiver_missing_token_401", async () => {
    const url = await mountReceiver(
      new NotificationReceiver("secret", async () => undefined, { identityBindingEnabled: false }),
    );
    expect((await postCallback(url, JSON.stringify(taskResult()), {})).status).toBe(401);
  });

  it("test_receiver_malformed_body_returns_400", async () => {
    const token = "tok-malform";
    const url = await mountReceiver(
      new NotificationReceiver(token, async () => undefined, { identityBindingEnabled: false }),
    );
    const resp = await postCallback(url, '{"not": "a task result"}', {
      [NOTIFICATION_TOKEN_HEADER]: token,
    });
    expect(resp.status).toBe(400);
  });

  it("delegates to tokenValidator with the parsed TaskResult (multi-task scenario)", async () => {
    const seen: Array<[string, string]> = [];
    const receiver = new NotificationReceiver("", async () => undefined, {
      identityBindingEnabled: false,
      tokenValidator: (provided, result) => {
        seen.push([provided, result.taskId]);
        return provided === `token-for-${result.taskId}`;
      },
    });
    const url = await mountReceiver(receiver);
    expect(
      (
        await postCallback(url, JSON.stringify(taskResult("t-9")), {
          [NOTIFICATION_TOKEN_HEADER]: "token-for-t-9",
        })
      ).status,
    ).toBe(200);
    expect(
      (
        await postCallback(url, JSON.stringify(taskResult("t-9")), {
          [NOTIFICATION_TOKEN_HEADER]: "nope",
        })
      ).status,
    ).toBe(401);
    expect(seen).toEqual([
      ["token-for-t-9", "t-9"],
      ["nope", "t-9"],
    ]);
  });

  it("constantTimeEquals compares like secrets.compare_digest", () => {
    expect(constantTimeEquals("abc", "abc")).toBe(true);
    expect(constantTimeEquals("abc", "abd")).toBe(false);
    expect(constantTimeEquals("abc", "abcd")).toBe(false);
    expect(constantTimeEquals("", "")).toBe(true);
    expect(constantTimeEquals("中文", "中文")).toBe(true);
  });

  it("test_set_start_roundtrip_via_asgi", async () => {
    const service = new NotificationService({ identityBindingEnabled: false });
    // 对应 Python 的 ASGITransport：直接把客户端请求交给框架无关的路由处理函数
    const asgiTransport = async (request: HttpRequestOptions): Promise<HttpResponseData> => {
      const method = new URL(request.url).pathname.split("/").pop() as
        "set" | "get" | "delete" | "start";
      const result = await handleNotificationRequest(service, method, {
        body: JSON.parse(String(request.body)) as unknown,
        getPeerAic: () => null,
      });
      return {
        status: result.status,
        headers: {},
        body: Buffer.from(JSON.stringify(result.body)),
        peerCertificate: null,
      };
    };
    const roundTrip = new AipNotificationClient("http://test", "leader-1", {
      transport: asgiTransport,
      identityBindingEnabled: false,
    });
    const taskId = "t-roundtrip";
    const cfg = await roundTrip.setNotification(taskId, "http://leader/callback", "mytoken");
    expect(cfg.id).toBeDefined();
    expect(await roundTrip.startNotification(taskId, cfg.id as string, "sess-rt")).toBe(true);
    expect(service.registry.matches(taskId, TaskState.Completed).length).toBeGreaterThanOrEqual(1);
    await roundTrip.close();
  });
});
