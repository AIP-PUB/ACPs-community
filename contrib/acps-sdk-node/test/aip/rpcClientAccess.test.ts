/**
 * 对应 Python `tests/aip/test_aip_rpc_client_access.py`（AipRpcClient opt-in access 埋点）。
 *
 * Python 替换 `http_client.post` 并 patch uuid；Node 注入 `transport`，由它回显请求 id 并记录请求头。
 */

import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type TaskCommand, TaskCommandType, TaskState } from "../../src/aip/baseModel.js";
import { AipRpcClient } from "../../src/aip/rpcClient.js";
import { AccessEmitter } from "../../src/amp/accessEmitter.js";
import { TRACEPARENT_HEADER } from "../../src/amp/traceContext.js";
import type { HttpRequestOptions, HttpResponseData } from "../../src/internal/http.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- 测试里按路径读取任意 JSON 响应
type Json = any;

function makeCommand(): TaskCommand {
  return {
    type: "task-command",
    id: "cmd-1",
    sentAt: "2026-01-01T00:00:00Z",
    senderRole: "leader",
    senderId: "leader-aic",
    sessionId: "sess-1",
    command: TaskCommandType.Get,
    taskId: "task-1",
  };
}

function successResponse(requestId: unknown): Record<string, unknown> {
  return {
    jsonrpc: "2.0",
    id: requestId,
    result: {
      type: "task-result",
      id: "msg-1",
      sentAt: "2026-01-01T00:00:00Z",
      senderRole: "partner",
      senderId: "partner-aic",
      taskId: "task-1",
      sessionId: "sess-1",
      status: { state: TaskState.Working, stateChangedAt: "2026-01-01T00:00:00Z" },
    },
  };
}

/** 回显请求 id 的成功传输层，并记录请求头。 */
function capturingTransport() {
  const captured: Array<Record<string, string>> = [];
  const transport = async (request: HttpRequestOptions): Promise<HttpResponseData> => {
    captured.push({ ...(request.headers ?? {}) });
    const id = (JSON.parse(String(request.body)) as { id: string }).id;
    return {
      status: 200,
      headers: {},
      body: Buffer.from(JSON.stringify(successResponse(id))),
      peerCertificate: null,
    };
  };
  return { transport, captured };
}

let tmpPath: string;

beforeEach(() => {
  tmpPath = mkdtempSync(join(tmpdir(), "acps-access-"));
});

afterEach(() => {
  rmSync(tmpPath, { recursive: true, force: true });
});

function readRecord(logFile: string): Record<string, Json> {
  return JSON.parse(readFileSync(logFile, "utf-8").trim()) as Record<string, Json>;
}

describe("test_aip_rpc_client_access", () => {
  it("test_injected_emitter_emits_client_span_and_traceparent", async () => {
    const logFile = join(tmpPath, "access.jsonl");
    const { transport, captured } = capturingTransport();
    const client = new AipRpcClient("http://partner.local/rpc", "leader-aic", {
      accessEmitter: new AccessEmitter(logFile, "leader-aic"),
      calleeAic: "partner-aic",
      callerService: "demo-leader",
      calleeService: "demo-partner-x",
      traceContextProvider: () => ({ traceId: "a".repeat(32), spanId: "b".repeat(16) }),
      identityBindingEnabled: false,
      transport,
    });
    await client.sendRequest(makeCommand());

    expect(captured[0]).toHaveProperty(TRACEPARENT_HEADER);
    expect(captured[0]?.[TRACEPARENT_HEADER]).toMatch(
      new RegExp(`^00-${"a".repeat(32)}-[0-9a-f]{16}-01$`),
    );
    expect(existsSync(logFile)).toBe(true);
    const record = readRecord(logFile);
    expect(record["log_type"]).toBe("access");
    expect(record["trace_id"]).toBe("a".repeat(32));
    expect(record["span_id"]).toBe(captured[0]?.[TRACEPARENT_HEADER]?.split("-")[2]);
    expect(record["parent_span_id"]).toBe("");
    expect(record["correlation_id"]).toBe("sess-1");
    expect(record["body"]["caller"]).toEqual({ aic: "leader-aic", serviceName: "demo-leader" });
    expect(record["body"]["callee"]).toEqual({ aic: "partner-aic", serviceName: "demo-partner-x" });
    expect(record["body"]["response"]["statusCode"]).toBe(200);
    expect(record["body"]["request"]).toMatchObject({
      method: "get",
      url: "http://partner.local/rpc",
      route: "/rpc",
      headers: { "content-type": "application/json" },
    });
    expect(record["body"]["request"]["headers"][TRACEPARENT_HEADER]).toBe(
      captured[0]?.[TRACEPARENT_HEADER],
    );
  });

  it("test_http_error_still_emits_access_log", async () => {
    const logFile = join(tmpPath, "access.jsonl");
    const client = new AipRpcClient("http://partner.local/rpc", "leader-aic", {
      accessEmitter: new AccessEmitter(logFile, "leader-aic"),
      calleeAic: "partner-aic",
      identityBindingEnabled: false,
      transport: async () => ({
        status: 500,
        headers: {},
        body: Buffer.from("boom"),
        peerCertificate: null,
      }),
    });
    await expect(client.sendRequest(makeCommand())).rejects.toThrow("HTTP Error: 500 - boom");
    const record = readRecord(logFile);
    expect(record["body"]["response"]["statusCode"]).toBe(500);
    expect(record["body"]["error"]).toBeDefined();
    expect(record["body"]["error"]).toMatchObject({ code: 500, message: "boom" });
  });

  it("emits error info for JSON-RPC errors and failed / rejected tasks", async () => {
    const cases: Array<[Record<string, unknown>, number, unknown]> = [
      [
        { error: { code: -32001, message: "Task not found" } },
        404,
        { code: 404, message: "Task not found" },
      ],
      [
        {
          result: {
            ...(successResponse("x")["result"] as object),
            status: { state: "failed", stateChangedAt: "n" },
          },
        },
        500,
        { code: 500, message: "task failed" },
      ],
      [
        {
          result: {
            ...(successResponse("x")["result"] as object),
            status: { state: "rejected", stateChangedAt: "n" },
          },
        },
        200,
        { code: "REJECTED", message: "task rejected" },
      ],
    ];
    for (const [index, [reply, status, errorInfo]] of cases.entries()) {
      const logFile = join(tmpPath, `access-${index}.jsonl`);
      const client = new AipRpcClient("http://partner.local/rpc", "leader-aic", {
        accessEmitter: new AccessEmitter(logFile, "leader-aic"),
        identityBindingEnabled: false,
        transport: async (request) => {
          const id = (JSON.parse(String(request.body)) as { id: string }).id;
          const body = { jsonrpc: "2.0", id, ...reply };
          return {
            status: 200,
            headers: {},
            body: Buffer.from(JSON.stringify(body)),
            peerCertificate: null,
          };
        },
      });
      await client.sendRequest(makeCommand()).catch(() => undefined);
      const record = readRecord(logFile);
      expect(record["body"]["response"]["statusCode"]).toBe(status);
      expect(record["body"]["error"]).toEqual(errorInfo);
    }
  });

  it("test_without_emitter_no_header_and_no_file", async () => {
    const logFile = join(tmpPath, "access.jsonl");
    const { transport, captured } = capturingTransport();
    const client = new AipRpcClient("http://partner.local/rpc", "leader-aic", {
      identityBindingEnabled: false,
      transport,
    });
    const resp = await client.sendRequest(makeCommand());
    expect(resp.result).toBeDefined();
    expect(captured[0]).not.toHaveProperty(TRACEPARENT_HEADER);
    expect(existsSync(logFile)).toBe(false);
  });

  it("test_emit_failure_does_not_break_rpc", async () => {
    const emitter = new AccessEmitter(join(tmpPath, "access.jsonl"), "leader-aic");
    const emit = vi.spyOn(emitter, "emit").mockRejectedValue(new Error("emit failed"));
    const { transport } = capturingTransport();
    const client = new AipRpcClient("http://partner.local/rpc", "leader-aic", {
      accessEmitter: emitter,
      identityBindingEnabled: false,
      transport,
    });
    const resp = await client.sendRequest(makeCommand());
    expect(resp.result).toBeDefined();
    expect(emit).toHaveBeenCalledTimes(1);
  });

  it("generates a new trace id when no trace context provider is given", async () => {
    const logFile = join(tmpPath, "access.jsonl");
    const { transport, captured } = capturingTransport();
    const client = new AipRpcClient("http://partner.local/rpc", "leader-aic", {
      accessEmitter: new AccessEmitter(logFile, "leader-aic"),
      identityBindingEnabled: false,
      transport,
    });
    await client.sendRequest(makeCommand());
    const record = readRecord(logFile);
    expect(record["trace_id"]).toMatch(/^[0-9a-f]{32}$/);
    expect(captured[0]?.[TRACEPARENT_HEADER]).toBe(
      `00-${record["trace_id"]}-${record["span_id"]}-01`,
    );
  });
});
