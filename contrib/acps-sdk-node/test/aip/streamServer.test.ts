/**
 * 对应 Python `tests/test_stream_server.py`（S2：SSE 编解码 + handle_stream_request）。
 *
 * Python 经 ASGITransport 调用 `add_aip_stream_router` 注册的路由；Node 在 127.0.0.1 上起 HTTP 服务挂
 * `createAipStreamRequestListener`。
 */

import { afterEach, describe, expect, it } from "vitest";

import { type TaskResult, TaskState } from "../../src/aip/baseModel.js";
import { SSE_MEDIA_TYPE, type StreamResponse } from "../../src/aip/streamModel.js";
import {
  StreamHub,
  TaskStreamChannel,
  buildStreamErrorResponse,
  buildStreamResponse,
  createAipStreamRequestListener,
  formatSse,
  handleStreamRequest,
} from "../../src/aip/streamServer.js";
import { utcNowIso } from "../../src/internal/time.js";
import { type TestHttpServer, startHttpServer } from "./_helpers.js";

const NOW = utcNowIso();

function taskResult(taskId = "t-1", state: TaskState = TaskState.Working): TaskResult {
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

function command(commandName: string, taskId: string | null, extra: Record<string, unknown> = {}) {
  return {
    type: "task-command",
    id: "cmd-x",
    sentAt: NOW,
    senderRole: "leader",
    senderId: "leader-1",
    sessionId: "sess-1",
    command: commandName,
    ...(taskId === null ? {} : { taskId }),
    ...extra,
  };
}

function streamBody(message: unknown, id: unknown = "1") {
  return { jsonrpc: "2.0", method: "stream", id, params: { message } };
}

const servers: TestHttpServer[] = [];

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => server.close()));
});

async function buildAppWithHub(hub: StreamHub): Promise<string> {
  const listener = createAipStreamRequestListener(hub, async () => undefined, {
    identityBindingEnabled: false,
  });
  const server = await startHttpServer(listener);
  servers.push(server);
  return `${server.url}/stream`;
}

async function post(url: string, body: unknown) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

function sseLines(text: string): StreamResponse[] {
  return text
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data: "))
    .map((line) => JSON.parse(line.slice("data: ".length)) as StreamResponse);
}

describe("test_stream_server: SSE encoding", () => {
  it("test_format_sse_basic", () => {
    const data = formatSse({
      jsonrpc: "2.0",
      id: "rpc-1",
      result: { eventSeq: 1, eventData: taskResult() },
    });
    expect(typeof data).toBe("string");
    expect(data.startsWith("data: ")).toBe(true);
    expect(data.endsWith("\n\n")).toBe(true);
  });

  it("test_format_sse_content_valid_json", () => {
    const text = formatSse({
      jsonrpc: "2.0",
      id: "1",
      result: { eventSeq: 2, eventData: taskResult() },
    });
    const parsed = JSON.parse(text.slice("data: ".length, -2)) as StreamResponse;
    expect(parsed.result?.eventSeq).toBe(2);
  });

  it("test_format_sse_excludes_none", () => {
    const text = formatSse({
      jsonrpc: "2.0",
      id: "1",
      result: { eventSeq: 1, eventData: taskResult() },
    });
    expect(text).not.toContain('"null"');
    expect(text).not.toContain('"error": null');
    expect(text).not.toContain('"error":null');
  });

  it("test_build_stream_response_seq", () => {
    const resp = buildStreamResponse("1", {
      eventSeq: 3,
      payload: taskResult(),
      isTerminal: false,
    });
    expect(resp.result?.eventSeq).toBe(3);
    expect(resp.error).toBeUndefined();
  });

  it("test_build_stream_error_response", () => {
    const resp = buildStreamErrorResponse("1", -32001, "not found");
    expect(resp.result).toBeUndefined();
    expect(resp.error?.code).toBe(-32001);
  });

  it("test_sse_generator_emits_events", async () => {
    const hub = new StreamHub();
    const taskId = "t-gen";
    hub.getOrCreateChannel(taskId);
    // 先发布 2 条，第 2 条为终态触发关闭
    await hub.publishTaskResult(taskId, taskResult(taskId, TaskState.Working));
    await hub.publishTaskResult(taskId, taskResult(taskId, TaskState.Completed));
    const outcome = await handleStreamRequest(
      { body: streamBody(command("re-stream", taskId)), getPeerAic: () => null },
      hub,
      async () => undefined,
      { identityBindingEnabled: false },
    );
    expect(outcome.kind).toBe("stream");
    const chunks: string[] = [];
    if (outcome.kind === "stream") for await (const chunk of outcome.frames) chunks.push(chunk);
    expect(chunks).toHaveLength(2);
    for (const chunk of chunks) expect(chunk.startsWith("data: ")).toBe(true);
  });
});

describe("test_stream_server: HTTP endpoint", () => {
  it("test_stream_endpoint_success", async () => {
    const hub = new StreamHub();
    const taskId = "t-http-ok";
    // 先写入终态事件，让通道关闭（响应会读完后结束）
    hub.getOrCreateChannel(taskId);
    await hub.publishTaskResult(taskId, taskResult(taskId, TaskState.Completed));
    const response = await post(await buildAppWithHub(hub), streamBody(command("start", taskId)));
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain(SSE_MEDIA_TYPE);
    expect(response.headers.get("cache-control")).toBe("no-cache");
    expect(sseLines(await response.text())).toHaveLength(1);
  });

  it("test_stream_endpoint_restream_not_found_404", async () => {
    const response = await post(
      await buildAppWithHub(new StreamHub()),
      streamBody(command("re-stream", "t-nonexistent")),
    );
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({
      detail: { code: -32001, message: "Stream not found: t-nonexistent" },
    });
  });

  it("test_stream_endpoint_unsupported_command_400", async () => {
    const response = await post(
      await buildAppWithHub(new StreamHub()),
      streamBody(command("get", "t-get")),
    );
    expect(response.status).toBe(400);
  });

  it("test_stream_endpoint_invalid_body_400", async () => {
    const url = await buildAppWithHub(new StreamHub());
    expect((await post(url, { invalid: "body" })).status).toBe(400);
    expect((await post(url, "{not json")).status).toBe(400);
  });

  it("test_restream_endpoint_replays_buffered_events", async () => {
    const hub = new StreamHub();
    const taskId = "t-restream-ok";
    hub.getOrCreateChannel(taskId);
    // 发布 3 条事件，第 3 条为终态
    await hub.publishTaskResult(taskId, taskResult(taskId, TaskState.Working));
    await hub.publishTaskResult(taskId, taskResult(taskId, TaskState.Working));
    await hub.publishTaskResult(taskId, taskResult(taskId, TaskState.Completed));
    const response = await post(
      await buildAppWithHub(hub),
      streamBody(command("re-stream", taskId, { commandParams: { lastEventSeq: 1 } }), "re-1"),
    );
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain(SSE_MEDIA_TYPE);
    const frames = sseLines(await response.text());
    expect(frames.length).toBeGreaterThanOrEqual(2);
    const seqs = frames.flatMap((frame) => (frame.result ? [frame.result.eventSeq] : []));
    expect(seqs).toContain(2);
    expect(seqs).toContain(3);
  });

  it("test_restream_endpoint_buffer_expired_returns_error_frame", async () => {
    const hub = new StreamHub();
    const taskId = "t-restream-expired";
    // 使用小缓冲区（2），手动装入通道
    const ch = new TaskStreamChannel(2);
    (hub as unknown as { channels: Map<string, TaskStreamChannel> }).channels.set(taskId, ch);
    for (let i = 0; i < 4; i += 1) {
      await ch.publish(taskResult(taskId, i < 3 ? TaskState.Working : TaskState.Completed));
    }
    const response = await post(
      await buildAppWithHub(hub),
      streamBody(command("re-stream", taskId, { commandParams: { lastEventSeq: 1 } }), "re-2"),
    );
    expect(response.status).toBe(200);
    const text = await response.text();
    expect(text).toContain("error");
    expect(sseLines(text)[0]?.error?.code).toBe(-32002);
  });

  // 与 Python 一致：onReStream 返回的通道只用于判断能否续传，事件仍从 hub 中按 taskId 读取，
  // 所以钩子需要把恢复出的通道放回 hub（这里用 getOrCreateChannel 占位）。
  it("onReStream is consulted when the hub does not hold the channel", async () => {
    const hub = new StreamHub();
    const recovered = hub.getOrCreateChannel("t-recover");
    await recovered.publish(taskResult("t-recover", TaskState.Completed), { isTerminal: true });
    hub.removeChannel("t-recover");
    let asked = 0;
    const outcome = await handleStreamRequest(
      { body: streamBody(command("re-stream", "t-recover")), getPeerAic: () => null },
      hub,
      {
        onStreamStart: async () => undefined,
        onReStream: async () => {
          asked += 1;
          hub.getOrCreateChannel("t-recover");
          return recovered;
        },
      },
      { identityBindingEnabled: false },
    );
    expect(asked).toBe(1);
    expect(outcome.kind).toBe("stream");
  });

  it("the stream ends when the client disconnects", async () => {
    const hub = new StreamHub();
    hub.getOrCreateChannel("t-live");
    await hub.publishTaskResult("t-live", taskResult("t-live", TaskState.Working));
    const url = await buildAppWithHub(hub);
    const controller = new AbortController();
    const response = await fetch(url, {
      method: "POST",
      body: JSON.stringify(streamBody(command("re-stream", "t-live"))),
      signal: controller.signal,
    });
    const reader = response.body?.getReader();
    const decoder = new TextDecoder();
    let received = "";
    while (reader && !received.includes("\n\n")) {
      const chunk = await reader.read();
      if (chunk.done) break;
      received += decoder.decode(chunk.value, { stream: true });
    }
    expect(received).toContain('"eventSeq":1');
    expect(hub.getChannel("t-live")?.subscriberCount).toBe(1);
    controller.abort();
    // 断开后订阅应被释放（不依赖后续事件）
    for (let i = 0; i < 50 && (hub.getChannel("t-live")?.subscriberCount ?? 0) > 0; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    expect(hub.getChannel("t-live")?.subscriberCount).toBe(0);
    await hub.publishTaskResult("t-live", taskResult("t-live", TaskState.Completed));
  });
});
