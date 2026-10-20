/**
 * 对应 Python `tests/test_aip_stream_identity.py`。
 *
 * Python 的 `handle_stream_request` 抛 `HTTPException`；Node 的 `handleStreamRequest` 返回
 * `{ kind: "error", status, detail }`，成功时返回 `{ kind: "stream", frames }`。
 * Python 直接调用私有 `_sse_event_generator`；Node 通过 `handleStreamRequest` 的 `frames` 走同一段逻辑。
 */

import { describe, expect, it, vi } from "vitest";

import {
  type TaskCommand,
  type TaskResult,
  TaskCommandType,
  TaskState,
} from "../../src/aip/baseModel.js";
import {
  AUTHENTICATION_REQUIRED_CODE,
  AUTHORIZATION_FAILED_CODE,
  PeerAicMissingError,
  SenderIdentityMismatchError,
} from "../../src/aip/identity.js";
import { AipStreamClient } from "../../src/aip/streamClient.js";
import type { StreamResponse } from "../../src/aip/streamModel.js";
import {
  StreamHub,
  createAipStreamRequestListener,
  formatSse,
  handleStreamRequest,
} from "../../src/aip/streamServer.js";
import {
  LEADER_AIC,
  OTHER_AIC,
  PARTNER_AIC,
  captureLogs,
  certFor,
  flushAsync,
  memoryTransport,
  sseTransport,
} from "./_helpers.js";

const NOW = "2026-06-24T00:00:00Z";

function taskCommand(
  senderId: string = LEADER_AIC,
  command: TaskCommandType = TaskCommandType.Start,
): TaskCommand {
  return {
    type: "task-command",
    id: "cmd-1",
    sentAt: NOW,
    senderRole: "leader",
    senderId,
    sessionId: "session-1",
    command,
    taskId: "task-1",
  };
}

function taskResult(senderId: string = PARTNER_AIC): TaskResult {
  return {
    type: "task-result",
    id: "result-1",
    sentAt: NOW,
    senderRole: "partner",
    senderId,
    taskId: "task-1",
    sessionId: "session-1",
    status: { state: TaskState.Working, stateChangedAt: NOW },
  };
}

function streamBody(command: TaskCommand): unknown {
  return {
    jsonrpc: "2.0",
    method: "stream",
    id: "stream-1",
    params: { message: JSON.parse(JSON.stringify(command)) },
  };
}

function event(senderId: string = PARTNER_AIC): StreamResponse {
  return {
    jsonrpc: "2.0",
    id: "stream-1",
    result: { eventSeq: 1, eventData: taskResult(senderId) },
  };
}

function streamClient(transport: ReturnType<typeof sseTransport>): AipStreamClient {
  return new AipStreamClient(
    "https://partner.local/stream",
    "https://partner.local/rpc",
    LEADER_AIC,
    {
      expectedPartnerAic: PARTNER_AIC,
      identityBindingEnabled: true,
      streamTransport: transport,
    },
  );
}

async function drain<T>(iterable: AsyncIterable<T>): Promise<T[]> {
  const items: T[] = [];
  for await (const item of iterable) items.push(item);
  return items;
}

describe("test_aip_stream_identity (server)", () => {
  it("test_stream_server_accepts_matching_sender_and_peer", async () => {
    const hub = new StreamHub();
    const onStart = vi.fn(async () => undefined);
    const outcome = await handleStreamRequest(
      { body: streamBody(taskCommand()), getPeerAic: () => LEADER_AIC },
      hub,
      onStart,
      { localAic: PARTNER_AIC, identityBindingEnabled: true },
    );
    await flushAsync();
    expect(outcome.kind).toBe("stream");
    expect(onStart).toHaveBeenCalledTimes(1);
    await hub.closeStream("task-1");
  });

  it("test_stream_server_rejects_sender_mismatch", async () => {
    const onStart = vi.fn(async () => undefined);
    const outcome = await handleStreamRequest(
      { body: streamBody(taskCommand(OTHER_AIC)), getPeerAic: () => LEADER_AIC },
      new StreamHub(),
      onStart,
      { localAic: PARTNER_AIC, identityBindingEnabled: true },
    );
    expect(outcome.kind === "error" && outcome.status).toBe(403);
    expect(outcome.kind === "error" && outcome.detail["code"]).toBe(AUTHORIZATION_FAILED_CODE);
    await flushAsync();
    expect(onStart).not.toHaveBeenCalled();
  });

  it("test_stream_server_rejects_missing_peer_aic", async () => {
    const outcome = await handleStreamRequest(
      { body: streamBody(taskCommand()), getPeerAic: () => null },
      new StreamHub(),
      async () => undefined,
      { localAic: PARTNER_AIC, identityBindingEnabled: true },
    );
    expect(outcome.kind === "error" && outcome.status).toBe(401);
    expect(outcome.kind === "error" && outcome.detail["code"]).toBe(AUTHENTICATION_REQUIRED_CODE);
  });

  it("test_sse_generator_rejects_outbound_sender_mismatch", async () => {
    const hub = new StreamHub();
    await hub.publishTaskResult("task-1", taskResult(OTHER_AIC));
    const outcome = await handleStreamRequest(
      {
        body: streamBody(taskCommand(LEADER_AIC, TaskCommandType.ReStream)),
        getPeerAic: () => LEADER_AIC,
      },
      hub,
      async () => undefined,
      { localAic: PARTNER_AIC, identityBindingEnabled: true },
    );
    expect(outcome.kind).toBe("stream");
    if (outcome.kind !== "stream") return;
    await expect(drain(outcome.frames)).rejects.toBeInstanceOf(SenderIdentityMismatchError);
    await hub.closeStream("task-1");
  });
});

describe("test_aip_stream_identity (client)", () => {
  it("test_stream_client_accepts_matching_tls_server_and_event_sender", async () => {
    const client = streamClient(
      sseTransport(formatSse(event()), { peerCertificate: certFor(PARTNER_AIC) }),
    );
    const events = await drain(client.startStream("session-1", { taskId: "task-1" }));
    expect(events).toHaveLength(1);
    await client.close();
  });

  it("test_stream_client_rejects_missing_tls_server_aic", async () => {
    const client = streamClient(sseTransport(formatSse(event()), { peerCertificate: null }));
    await expect(
      drain(client.startStream("session-1", { taskId: "task-1" })),
    ).rejects.toBeInstanceOf(PeerAicMissingError);
  });

  it("test_stream_client_rejects_tls_server_aic_mismatch", async () => {
    const client = streamClient(
      sseTransport(formatSse(event()), { peerCertificate: certFor(OTHER_AIC) }),
    );
    await expect(
      drain(client.startStream("session-1", { taskId: "task-1" })),
    ).rejects.toBeInstanceOf(SenderIdentityMismatchError);
  });

  it("test_stream_client_rejects_event_sender_mismatch", async () => {
    const client = streamClient(
      sseTransport(formatSse(event(OTHER_AIC)), { peerCertificate: certFor(PARTNER_AIC) }),
    );
    await expect(
      drain(client.startStream("session-1", { taskId: "task-1" })),
    ).rejects.toBeInstanceOf(SenderIdentityMismatchError);
  });

  it("test_stream_client_rejects_outbound_sender_before_request", async () => {
    const transport = sseTransport(formatSse(event()), { peerCertificate: certFor(PARTNER_AIC) });
    const client = streamClient(transport);
    await expect(drain(client.openStream(taskCommand(OTHER_AIC)))).rejects.toBeInstanceOf(
      SenderIdentityMismatchError,
    );
    expect(transport.calls).toBe(0);
  });

  it("test_stream_client_passes_expected_partner_aic_to_internal_rpc_client", async () => {
    const rpcTransport = memoryTransport((req) => ({
      body: { jsonrpc: "2.0", id: (req.json as { id: string }).id, result: taskResult() },
      peerCertificate: certFor(OTHER_AIC),
    }));
    const client = new AipStreamClient(
      "https://partner.local/stream",
      "https://partner.local/rpc",
      LEADER_AIC,
      {
        expectedPartnerAic: PARTNER_AIC,
        identityBindingEnabled: true,
        streamTransport: sseTransport(""),
        transport: rpcTransport,
      },
    );
    const internal = (client as unknown as { rpcClient: Record<string, unknown> }).rpcClient;
    expect(internal["expectedPartnerAic"]).toBe(PARTNER_AIC);
    expect(internal["identityBindingEnabled"]).toBe(true);
    // 行为上：内部 RPC 客户端同样校验 TLS 服务端身份
    await expect(client.complete("task-1", "session-1")).rejects.toBeInstanceOf(
      SenderIdentityMismatchError,
    );
  });

  it("test_stream_client_logs_warning_when_identity_binding_disabled", () => {
    const logs = captureLogs();
    try {
      new AipStreamClient("https://partner.local/stream", "https://partner.local/rpc", LEADER_AIC, {
        identityBindingEnabled: false,
        streamTransport: sseTransport(""),
      });
      expect(logs.messages.join("\n").toLowerCase()).toContain(
        "identity binding disabled for stream client",
      );
    } finally {
      logs.restore();
    }
  });

  it("test_add_aip_stream_router_logs_warning_when_identity_binding_disabled", () => {
    const logs = captureLogs();
    try {
      createAipStreamRequestListener(new StreamHub(), async () => undefined, {
        identityBindingEnabled: false,
      });
      expect(logs.messages.join("\n").toLowerCase()).toContain(
        "identity binding disabled for stream server",
      );
    } finally {
      logs.restore();
    }
  });
});
