/**
 * 对应 Python `tests/test_notification_service.py`（N2：NotificationService + 四个 JSON-RPC 端点）。
 *
 * Python 经 ASGITransport 调用 FastAPI 路由；Node 在 127.0.0.1 上起 HTTP 服务挂
 * `createAipNotificationRequestListener`，用 fetch 发请求。
 */

import { afterEach, describe, expect, it } from "vitest";

import { type TaskResult, TaskState } from "../../src/aip/baseModel.js";
import { NOTIFICATION_TOKEN_HEADER } from "../../src/aip/notificationModel.js";
import {
  NotificationService,
  createAipNotificationRequestListener,
} from "../../src/aip/notificationServer.js";
import type { HttpTransport } from "../../src/internal/http.js";
import { utcNowIso } from "../../src/internal/time.js";
import { type TestHttpServer, memoryTransport, startHttpServer } from "./_helpers.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- 测试里按路径读取任意 JSON 响应
type Json = any;

const NOW = utcNowIso();

function fixedStatusTransport(status: number) {
  return memoryTransport(() => ({ status, rawBody: "" }));
}

const servers: TestHttpServer[] = [];

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => server.close()));
});

async function makeApp(
  transport?: HttpTransport,
): Promise<{ base: string; service: NotificationService }> {
  const service = new NotificationService({ transport, identityBindingEnabled: false });
  const server = await startHttpServer(createAipNotificationRequestListener(service));
  servers.push(server);
  return { base: server.url, service };
}

async function post(base: string, path: string, body: unknown) {
  const response = await fetch(`${base}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return { status: response.status, json: (await response.json()) as Record<string, Json> };
}

function setBody(taskId = "t-1", url = "http://cb/notify") {
  return {
    jsonrpc: "2.0",
    method: "notification/set",
    id: "1",
    params: { url, token: "tok123", taskId },
  };
}

function deleteBody(taskId = "t-1", configId?: string) {
  const body: Record<string, Json> = {
    jsonrpc: "2.0",
    method: "notification/delete",
    id: "2",
    params: { taskId },
  };
  // 与 Python 测试一致：故意使用 configId（不是 notificationConfigId），未知键被忽略 → 删除该任务全部配置
  if (configId) body["params"]["configId"] = configId;
  return body;
}

function getBody(taskId = "t-1", configId?: string) {
  const body: Record<string, Json> = {
    jsonrpc: "2.0",
    method: "notification/get",
    id: "3",
    params: { taskId },
  };
  if (configId) body["params"]["configId"] = configId;
  return body;
}

function startBody(
  taskId: string,
  configId: string,
  extra: Record<string, unknown> = {},
  id = "4",
) {
  return {
    jsonrpc: "2.0",
    method: "notification/start",
    id,
    params: {
      message: {
        type: "task-command",
        id: "cmd-1",
        sentAt: NOW,
        senderRole: "leader",
        senderId: "leader-1",
        command: "start",
        taskId,
        commandParams: { notificationConfigId: configId, ...extra },
      },
    },
  };
}

function taskResult(taskId: string, state: TaskState): TaskResult {
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

describe("test_notification_service", () => {
  it("test_set_creates_config", async () => {
    const { base } = await makeApp();
    const resp = await post(base, "/notification/set", setBody("t-set"));
    expect(resp.status).toBe(200);
    expect(resp.json).toHaveProperty("result");
    expect(resp.json["result"]["id"]).toBeTruthy();
    expect(resp.json["result"]["taskId"]).toBe("t-set");
  });

  it("test_set_invalid_body_400", async () => {
    const { base } = await makeApp();
    expect((await post(base, "/notification/set", { bad: "data" })).status).toBe(400);
  });

  it("invalid JSON body is a 400 parse error", async () => {
    const { base } = await makeApp();
    const response = await fetch(`${base}/notification/set`, { method: "POST", body: "{" });
    expect(response.status).toBe(400);
    expect(((await response.json()) as { detail: { code: number } }).detail.code).toBe(-32700);
  });

  it("test_delete_existing", async () => {
    const { base } = await makeApp();
    const configId = (await post(base, "/notification/set", setBody("t-del"))).json["result"][
      "id"
    ] as string;
    const del = await post(base, "/notification/delete", deleteBody("t-del", configId));
    expect(del.status).toBe(200);
    expect(del.json["result"]["success"]).toBe(true);
  });

  it("test_delete_nonexistent_404", async () => {
    const { base } = await makeApp();
    expect(
      (await post(base, "/notification/delete", deleteBody("t-noexist", "nc-bad"))).status,
    ).toBe(404);
  });

  it("test_get_all", async () => {
    const { base } = await makeApp();
    await post(base, "/notification/set", setBody("t-get", "http://a/cb"));
    await post(base, "/notification/set", setBody("t-get", "http://b/cb"));
    const resp = await post(base, "/notification/get", getBody("t-get"));
    expect(resp.status).toBe(200);
    expect(Array.isArray(resp.json["result"])).toBe(true);
    expect(resp.json["result"]).toHaveLength(2);
  });

  it("test_get_specific", async () => {
    const { base } = await makeApp();
    const configId = (await post(base, "/notification/set", setBody("t-getone"))).json["result"][
      "id"
    ] as string;
    // 与 Python 测试一致：configId 不是 NotificationIdParams 的字段，被忽略 → 返回全部（此处只有一条）
    const resp = await post(base, "/notification/get", getBody("t-getone", configId));
    expect(resp.status).toBe(200);
    expect(resp.json["result"]).toHaveLength(1);
    expect(resp.json["result"][0]["id"]).toBe(configId);
  });

  it("test_start_registers_subscription", async () => {
    const { base, service } = await makeApp();
    const configId = (await post(base, "/notification/set", setBody("t-start"))).json["result"][
      "id"
    ] as string;
    const resp = await post(base, "/notification/start", startBody("t-start", configId));
    expect(resp.status).toBe(200);
    expect(resp.json).toEqual({ jsonrpc: "2.0", id: "4", result: true });
    expect(service.registry.matches("t-start", TaskState.Completed).length).toBeGreaterThanOrEqual(
      1,
    );
  });

  it("unknown paths and non-POST methods are left to the caller", async () => {
    const { base } = await makeApp();
    expect((await fetch(`${base}/notification/other`, { method: "POST", body: "{}" })).status).toBe(
      404,
    );
    expect((await fetch(`${base}/notification/set`)).status).toBe(404);
  });

  it("test_service_dispatch_calls_callback", async () => {
    const transport = fixedStatusTransport(200);
    const { base, service } = await makeApp(transport);
    const configId = (
      await post(base, "/notification/set", setBody("t-dispatch", "http://cb/notify"))
    ).json["result"]["id"] as string;
    await post(base, "/notification/start", startBody("t-dispatch", configId, {}, "5"));
    await service.dispatch(taskResult("t-dispatch", TaskState.Completed));
    expect(transport.requests).toHaveLength(1);
    expect(transport.requests[0]?.headers?.[NOTIFICATION_TOKEN_HEADER]).toBe("tok123");
  });

  it("test_notify_working_state_filtered_when_only_completed_subscribed", async () => {
    const transport = fixedStatusTransport(200);
    const { base, service } = await makeApp(transport);
    const configId = (
      await post(base, "/notification/set", setBody("t-filter", "http://cb/filter"))
    ).json["result"]["id"] as string;
    const start = await post(
      base,
      "/notification/start",
      startBody("t-filter", configId, { notifyOnStates: ["completed"] }, "6"),
    );
    expect(start.status).toBe(200);
    await service.dispatch(taskResult("t-filter", TaskState.Working));
    expect(transport.requests, "working 不应触发 callback").toHaveLength(0);
    await service.notify(taskResult("t-filter", TaskState.Completed));
    expect(transport.requests, "completed 应触发一次 callback").toHaveLength(1);
  });

  it("test_notify_all_states_when_no_filter", async () => {
    const transport = fixedStatusTransport(200);
    const { base, service } = await makeApp(transport);
    const configId = (await post(base, "/notification/set", setBody("t-nofilter", "http://cb/all")))
      .json["result"]["id"] as string;
    await post(base, "/notification/start", startBody("t-nofilter", configId, {}, "7"));
    await service.dispatch(taskResult("t-nofilter", TaskState.Working));
    await service.dispatch(taskResult("t-nofilter", TaskState.Completed));
    expect(transport.requests, "两次 dispatch 均应触发 callback").toHaveLength(2);
  });

  it("calls onNotificationStart fire-and-forget (single-stage design)", async () => {
    const started: string[] = [];
    const service = new NotificationService({
      identityBindingEnabled: false,
      handlers: { onNotificationStart: async (command) => void started.push(command.taskId ?? "") },
    });
    const server = await startHttpServer(
      createAipNotificationRequestListener(service, { basePath: "/aip/" }),
    );
    servers.push(server);
    const configId = (await post(server.url, "/aip/notification/set", setBody("t-1"))).json[
      "result"
    ]["id"] as string;
    expect(
      (await post(server.url, "/aip/notification/start", startBody("t-1", configId))).status,
    ).toBe(200);
    await new Promise((resolve) => setImmediate(resolve));
    expect(started).toEqual(["t-1"]);
  });
});
