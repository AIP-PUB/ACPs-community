/**
 * 对应 Python `tests/test_stream_client.py`（S3：AipStreamClient）。
 *
 * - Python 用一个 httpx transport 同时服务 SSE 与 RPC；Node 的客户端分别接受 `streamTransport`（SSE）
 *   与 `transport`（complete / cancel 走的 RPC）。
 * - Python 的私有 `_parse_sse_line` 在 Node 中没有对应方法（Python 版自身也未在流解析中使用它），
 *   这里改为通过完整的 SSE 字节流验证同样的行为。
 * - httpx 的 `RemoteProtocolError` 对应 Node 的 `ECONNRESET` 等网络错误（见 `isReconnectableStreamError`）。
 */

import { Readable } from "node:stream";

import { describe, expect, it } from "vitest";

import { type TaskResult, TaskCommandType, TaskState } from "../../src/aip/baseModel.js";
import {
  AipStreamClient,
  StreamProtocolError,
  isReconnectableStreamError,
} from "../../src/aip/streamClient.js";
import type { StreamResponse } from "../../src/aip/streamModel.js";
import { formatSse } from "../../src/aip/streamServer.js";
import { HttpStatusError, type HttpStreamTransport } from "../../src/internal/http.js";
import { utcNowIso } from "../../src/internal/time.js";
import { memoryTransport, sseTransport } from "./_helpers.js";

const NOW = utcNowIso();

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

function taskResultResp(seq: number, taskId: string, state: TaskState): StreamResponse {
  return {
    jsonrpc: "2.0",
    id: "rpc-1",
    result: { eventSeq: seq, eventData: taskResult(taskId, state) },
  };
}

function errorResp(code: number, message: string): StreamResponse {
  return { jsonrpc: "2.0", id: "rpc-err", error: { code, message } };
}

const makeSse = (...events: StreamResponse[]): string =>
  events.map((event) => formatSse(event)).join("");

/** 回显请求 ID 的 RPC 传输层（确保 ID 匹配校验通过）。 */
function rpcTransport(result: TaskResult) {
  return memoryTransport((req) => ({
    body: { jsonrpc: "2.0", id: (req.json as { id: string }).id, result },
  }));
}

function client(options: ConstructorParameters<typeof AipStreamClient>[3] = {}): AipStreamClient {
  return new AipStreamClient("http://p/stream", "http://p/rpc", "l", {
    identityBindingEnabled: false,
    ...options,
  });
}

async function drain<T>(iterable: AsyncIterable<T>): Promise<T[]> {
  const items: T[] = [];
  for await (const item of iterable) items.push(item);
  return items;
}

/** 按给定分块返回原始字节的流式传输层。 */
function chunkedTransport(chunks: string[]): HttpStreamTransport {
  return async () => ({
    status: 200,
    headers: {},
    peerCertificate: null,
    body: Readable.from(chunks.map((chunk) => Buffer.from(chunk, "utf-8"))),
    close: () => undefined,
  });
}

describe("test_stream_client: makeCommand", () => {
  it("test_make_command_start", async () => {
    const c = client();
    const cmd = c.makeCommand(TaskCommandType.Start, "sess-1");
    expect(cmd.taskId).toBeTruthy();
    expect(cmd.taskId?.startsWith("task-")).toBe(true);
    expect(cmd.command).toBe("start");
    expect(cmd.senderId).toBe("l");
    await c.close();
  });

  it("test_make_command_with_task_id", async () => {
    const c = client();
    expect(c.makeCommand(TaskCommandType.Cancel, "sess-1", { taskId: "fixed-task" }).taskId).toBe(
      "fixed-task",
    );
    await c.close();
  });
});

describe("test_stream_client: SSE parsing", () => {
  it("test_parse_sse_line_valid", async () => {
    // 单个 data 行、没有结尾空行：流结束时仍会解析并产出
    const line = `data: ${JSON.stringify(taskResultResp(1, "t-1", TaskState.Working))}`;
    const events = await drain(
      client({ streamTransport: chunkedTransport([line]) }).startStream("s"),
    );
    expect(events).toHaveLength(1);
    expect(events[0]?.result?.eventSeq).toBe(1);
  });

  it("test_parse_sse_line_non_data", async () => {
    const body = [": heartbeat", "event: ping", "id: 7", "retry: 1000", "", ""].join("\n");
    expect(
      await drain(client({ streamTransport: chunkedTransport([body]) }).startStream("s")),
    ).toEqual([]);
  });

  it("skips frames whose JSON does not parse as a StreamResponse", async () => {
    const body = `data: {not json\n\ndata: {"jsonrpc":"2.0","result":{"eventSeq":"x"}}\n\n${makeSse(taskResultResp(3, "t", TaskState.Working))}`;
    const events = await drain(
      client({ streamTransport: chunkedTransport([body]) }).startStream("s"),
    );
    expect(events.map((e) => e.result?.eventSeq)).toEqual([3]);
  });

  it("handles CRLF / CR line endings and frames split across chunks", async () => {
    const frame = `data: ${JSON.stringify(taskResultResp(1, "t", TaskState.Working))}`;
    const second = `data: ${JSON.stringify(taskResultResp(2, "t", TaskState.Completed))}`;
    const raw = `${frame}\r\n\r\n${second}\r\r`;
    const chunks = [
      raw.slice(0, 17),
      raw.slice(17, frame.length + 1),
      raw.slice(frame.length + 1, -1),
      raw.slice(-1),
    ];
    const events = await drain(
      client({ streamTransport: chunkedTransport(chunks) }).startStream("s"),
    );
    expect(events.map((e) => e.result?.eventSeq)).toEqual([1, 2]);
  });

  it("decodes multi-byte UTF-8 characters split across chunks", async () => {
    const result = { ...taskResult("t", TaskState.Working), id: "中文😀" };
    const bytes = Buffer.from(
      formatSse({ jsonrpc: "2.0", result: { eventSeq: 1, eventData: result } }),
      "utf-8",
    );
    const split = bytes.indexOf(Buffer.from("😀")) + 2;
    const transport: HttpStreamTransport = async () => ({
      status: 200,
      headers: {},
      peerCertificate: null,
      body: Readable.from([bytes.subarray(0, split), bytes.subarray(split)]),
      close: () => undefined,
    });
    const events = await drain(client({ streamTransport: transport }).startStream("s"));
    expect(events[0]?.result?.eventData.id).toBe("中文😀");
  });

  it("keeps events whose strings contain raw U+2028 / U+0085 (only CR/LF split lines)", async () => {
    // 有意差异：Python 版经 httpx aiter_lines（str.splitlines 语义）会在 U+2028/U+0085 处断行，
    // 导致这类事件被静默丢弃；SSE 规范只以 CR/LF 断行，Node 版按规范处理。
    for (const ch of [" ", " ", "\u0085"]) {
      const result = { ...taskResult("t", TaskState.Working), id: `x${ch}y` };
      const body = formatSse({ jsonrpc: "2.0", result: { eventSeq: 1, eventData: result } });
      const events = await drain(client({ streamTransport: sseTransport(body) }).startStream("s"));
      expect(events[0]?.result?.eventData.id).toBe(`x${ch}y`);
    }
  });

  it("raises HttpStatusError on non-2xx stream responses", async () => {
    const transport = sseTransport("boom", { status: 503 });
    await expect(
      drain(client({ streamTransport: transport }).startStream("s")),
    ).rejects.toBeInstanceOf(HttpStatusError);
  });
});

describe("test_stream_client: streaming", () => {
  it("test_start_stream_yields_events", async () => {
    const taskId = "t-stream";
    const transport = sseTransport(
      makeSse(
        taskResultResp(1, taskId, TaskState.Working),
        taskResultResp(2, taskId, TaskState.Completed),
      ),
    );
    const c = client({ streamTransport: transport });
    const events = await drain(c.startStream("sess-1", { taskId }));
    expect(events).toHaveLength(2);
    expect(events[0]?.result?.eventSeq).toBe(1);
    expect(events[1]?.result?.eventSeq).toBe(2);
    const request = transport.requests[0];
    expect(request?.headers).toMatchObject({
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    });
    expect(request?.json).toMatchObject({
      jsonrpc: "2.0",
      method: "stream",
      params: { message: { command: "start", taskId } },
    });
    await c.close();
  });

  it("test_complete_calls_rpc", async () => {
    const c = client({ transport: rpcTransport(taskResult("t-complete", TaskState.Completed)) });
    expect((await c.complete("t-complete", "sess-1")).status.state).toBe(TaskState.Completed);
    await c.close();
  });

  it("test_cancel_calls_rpc", async () => {
    const c = client({ transport: rpcTransport(taskResult("t-cancel", TaskState.Canceled)) });
    expect((await c.cancel("t-cancel", "sess-1")).status.state).toBe(TaskState.Canceled);
    await c.close();
  });

  it("test_multiline_sse_data_concatenated", async () => {
    const response = taskResultResp(1, "t-ml", TaskState.Completed);
    const pretty = JSON.stringify(response, null, 2).split("\n");
    expect(pretty.length).toBeGreaterThan(1);
    const body = `${pretty.map((line) => `data: ${line}`).join("\n")}\n\n`;
    const events = await drain(
      client({ streamTransport: sseTransport(body) }).startStream("s", { taskId: "t-ml" }),
    );
    expect(events).toHaveLength(1);
    expect(events[0]?.result?.eventSeq).toBe(1);
  });

  it("test_last_event_seq_updated_during_stream", async () => {
    const taskId = "t-seq";
    const c = client({
      streamTransport: sseTransport(
        makeSse(
          taskResultResp(10, taskId, TaskState.Working),
          taskResultResp(20, taskId, TaskState.Completed),
        ),
      ),
    });
    expect(c.lastEventSeq).toBeNull();
    await drain(c.startStream("s", { taskId }));
    expect(c.lastEventSeq).toBe(20);
    await c.close();
  });

  it("reStream sends lastEventSeq in commandParams", async () => {
    const transport = sseTransport("");
    await drain(client({ streamTransport: transport }).reStream("t-1", "s", 4));
    await drain(client({ streamTransport: transport }).reStream("t-1", "s"));
    const [withSeq, withoutSeq] = transport.requests.map(
      (r) => (r.json as { params: { message: Record<string, unknown> } }).params.message,
    );
    expect(withSeq?.["command"]).toBe("re-stream");
    expect(withSeq?.["commandParams"]).toEqual({ lastEventSeq: 4 });
    expect(withoutSeq).not.toHaveProperty("commandParams");
  });
});

describe("test_stream_client: streamWithReconnect", () => {
  it("test_stream_with_reconnect_happy_path", async () => {
    const taskId = "t-reconnect-ok";
    const c = client({
      streamTransport: sseTransport(
        makeSse(
          taskResultResp(1, taskId, TaskState.Working),
          taskResultResp(2, taskId, TaskState.Completed),
        ),
      ),
    });
    const events = await drain(c.streamWithReconnect("s", "hello", { taskId, backoffS: 0 }));
    expect(events).toHaveLength(2);
    expect(events.at(-1)?.result?.eventSeq).toBe(2);
    await c.close();
  });

  it("test_stream_with_reconnect_raises_stream_protocol_error", async () => {
    const transport = sseTransport(makeSse(errorResp(-32000, "buffer expired")));
    const c = client({ streamTransport: transport });
    const error = await drain(c.streamWithReconnect("s", "hi", { backoffS: 0 })).catch(
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(StreamProtocolError);
    expect((error as StreamProtocolError).code).toBe(-32000);
    expect((error as StreamProtocolError).protocolMessage).toContain("buffer expired");
    expect(transport.calls).toBe(1); // 不重连
    await c.close();
  });

  it("test_stream_with_reconnect_max_reconnects_exceeded", async () => {
    const transport = sseTransport("", { failCalls: [1, 2, 3, 4] });
    const c = client({ streamTransport: transport });
    const error = await drain(
      c.streamWithReconnect("s", "hi", { maxReconnects: 2, backoffS: 0 }),
    ).catch((e: unknown) => e);
    expect((error as NodeJS.ErrnoException).code).toBe("ECONNRESET");
    expect(transport.calls).toBe(3);
    await c.close();
  });

  it("test_stream_with_reconnect_reconnects_on_network_error", async () => {
    const taskId = "t-recon";
    const transport = sseTransport(
      makeSse(
        taskResultResp(1, taskId, TaskState.Working),
        taskResultResp(2, taskId, TaskState.Completed),
      ),
      { failCalls: [1] },
    );
    const c = client({ streamTransport: transport });
    const events = await drain(
      c.streamWithReconnect("s", "hello", { taskId, maxReconnects: 1, backoffS: 0 }),
    );
    expect(transport.calls, "应该先失败一次再重连一次").toBe(2);
    expect(events).toHaveLength(2);
    expect(events.at(-1)?.result?.eventSeq).toBe(2);
    const [first, second] = transport.requests.map(
      (r) => (r.json as { params: { message: Record<string, unknown> } }).params.message,
    );
    expect(first?.["command"]).toBe("start");
    expect(second?.["command"]).toBe("re-stream");
    expect(second?.["taskId"]).toBe(taskId);
    await c.close();
  });

  it("does not reconnect on non-network errors", async () => {
    const transport = sseTransport("x", { status: 500 });
    const c = client({ streamTransport: transport });
    await expect(drain(c.streamWithReconnect("s", "hi", { backoffS: 0 }))).rejects.toBeInstanceOf(
      HttpStatusError,
    );
    expect(transport.calls).toBe(1);
  });

  it("isReconnectableStreamError recognises Node network failures", () => {
    expect(isReconnectableStreamError(Object.assign(new Error("x"), { code: "ECONNRESET" }))).toBe(
      true,
    );
    expect(
      isReconnectableStreamError(Object.assign(new Error("x"), { code: "ECONNREFUSED" })),
    ).toBe(true);
    expect(isReconnectableStreamError(new Error("socket hang up"))).toBe(true);
    expect(isReconnectableStreamError(new Error("other"))).toBe(false);
    expect(isReconnectableStreamError("ECONNRESET")).toBe(false);
  });
});
