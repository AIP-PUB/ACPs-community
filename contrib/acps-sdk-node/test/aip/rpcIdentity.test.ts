/**
 * 对应 Python `tests/test_aip_rpc_identity.py`。
 *
 * - 服务端：Python 传入带 `request.state.peer_aic` 的假 Request；Node 的 `handleRpcRequest` 直接接收
 *   `{ body, getPeerAic }`。
 * - 客户端：Python 替换 `http_client.post` 并 patch uuid；Node 注入 `transport`，由它回显请求 id，
 *   并在响应上附带 `peerCertificate`（对应 `_FakeNetworkStream`）。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

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
import { AipRpcClient } from "../../src/aip/rpcClient.js";
import type { RpcResponse } from "../../src/aip/rpcModel.js";
import { type CommandHandlers, TaskManager, handleRpcRequest } from "../../src/aip/rpcServer.js";
import {
  LEADER_AIC,
  OTHER_AIC,
  PARTNER_AIC,
  captureLogs,
  certFor,
  memoryTransport,
} from "./_helpers.js";

function taskCommand(senderId: string = LEADER_AIC): TaskCommand {
  return {
    type: "task-command",
    id: "cmd-1",
    sentAt: "2026-06-24T00:00:00Z",
    senderRole: "leader",
    senderId,
    sessionId: "session-1",
    command: TaskCommandType.Start,
    taskId: "task-1",
  };
}

function taskResult(senderId: string = PARTNER_AIC): TaskResult {
  return {
    type: "task-result",
    id: "result-1",
    sentAt: "2026-06-24T00:00:01Z",
    senderRole: "partner",
    senderId,
    taskId: "task-1",
    sessionId: "session-1",
    status: { state: TaskState.Working, stateChangedAt: "2026-06-24T00:00:01Z" },
  };
}

function rpcBody(command: TaskCommand): unknown {
  return {
    jsonrpc: "2.0",
    method: "rpc",
    id: "rpc-1",
    params: { command: JSON.parse(JSON.stringify(command)) },
  };
}

function request(body: unknown, peerAic: string | null = LEADER_AIC) {
  return { body, getPeerAic: () => peerAic };
}

afterEach(() => {
  TaskManager.clear();
});

describe("test_aip_rpc_identity (server)", () => {
  it("test_rpc_server_accepts_matching_sender_and_peer", async () => {
    const onStart = vi.fn(async () => taskResult());
    const response = await handleRpcRequest(
      request(rpcBody(taskCommand())),
      { onStart },
      {
        localAic: PARTNER_AIC,
        identityBindingEnabled: true,
      },
    );
    expect(response.error).toBeUndefined();
    expect(response.result).toBeDefined();
    expect(onStart).toHaveBeenCalledTimes(1);
  });

  it("test_rpc_server_rejects_missing_peer_aic", async () => {
    const onStart = vi.fn(async () => taskResult());
    const response = await handleRpcRequest(
      request(rpcBody(taskCommand()), null),
      { onStart },
      {
        localAic: PARTNER_AIC,
        identityBindingEnabled: true,
      },
    );
    expect(response.error?.code).toBe(AUTHENTICATION_REQUIRED_CODE);
    expect(onStart).not.toHaveBeenCalled();
  });

  it("test_rpc_server_rejects_sender_mismatch", async () => {
    const onStart = vi.fn(async () => taskResult());
    const response = await handleRpcRequest(
      request(rpcBody(taskCommand(OTHER_AIC))),
      { onStart },
      {
        localAic: PARTNER_AIC,
        identityBindingEnabled: true,
      },
    );
    expect(response.error?.code).toBe(AUTHORIZATION_FAILED_CODE);
    expect(onStart).not.toHaveBeenCalled();
  });

  it("test_rpc_server_rejects_outbound_sender_mismatch", async () => {
    const handlers: CommandHandlers = { onStart: async () => taskResult(OTHER_AIC) };
    const response = await handleRpcRequest(request(rpcBody(taskCommand())), handlers, {
      localAic: PARTNER_AIC,
      identityBindingEnabled: true,
    });
    expect(response.error?.code).toBe(AUTHORIZATION_FAILED_CODE);
  });

  it("test_rpc_server_custom_dispatch_keeps_identity_binding", async () => {
    const dispatchRequest = vi.fn(async (): Promise<RpcResponse> => ({
      jsonrpc: "2.0",
      id: "rpc-1",
      result: taskResult(),
    }));
    const response = await handleRpcRequest(
      request(rpcBody(taskCommand())),
      {},
      {
        localAic: PARTNER_AIC,
        identityBindingEnabled: true,
        dispatchRequest,
      },
    );
    expect(response.error).toBeUndefined();
    expect(response.result).toBeDefined();
    expect(dispatchRequest).toHaveBeenCalledTimes(1);
  });

  it("test_rpc_server_custom_dispatch_rejects_mismatched_result_sender", async () => {
    const dispatchRequest = async (): Promise<RpcResponse> => ({
      jsonrpc: "2.0",
      id: "rpc-1",
      result: taskResult(OTHER_AIC),
    });
    const response = await handleRpcRequest(
      request(rpcBody(taskCommand())),
      {},
      {
        localAic: PARTNER_AIC,
        identityBindingEnabled: true,
        dispatchRequest,
      },
    );
    expect(response.error?.code).toBe(AUTHORIZATION_FAILED_CODE);
  });

  it("test_rpc_server_default_handlers_backfill_local_sender_id", async () => {
    const response = await handleRpcRequest(
      request(rpcBody(taskCommand())),
      {},
      {
        localAic: PARTNER_AIC,
        identityBindingEnabled: true,
      },
    );
    expect(response.error).toBeUndefined();
    expect(response.result?.senderId).toBe(PARTNER_AIC);
  });

  it("test_rpc_server_default_get_backfills_placeholder_sender_id", async () => {
    const placeholder = TaskManager.createTask(taskCommand());
    expect(placeholder.senderId).toBe("server");
    const getCommand = { ...taskCommand(), command: TaskCommandType.Get };
    const response = await handleRpcRequest(
      request(rpcBody(getCommand)),
      {},
      {
        localAic: PARTNER_AIC,
        identityBindingEnabled: true,
      },
    );
    expect(response.error).toBeUndefined();
    expect(response.result?.senderId).toBe(PARTNER_AIC);
  });

  it("requires localAic when identity binding is enabled (Python ValueError)", async () => {
    await expect(
      handleRpcRequest(request(rpcBody(taskCommand())), {}, { identityBindingEnabled: true }),
    ).rejects.toThrow("local_aic is required when identity_binding_enabled=True");
  });
});

// ---------------------------------------------------------------------------
// client
// ---------------------------------------------------------------------------

/** 回显请求 id 的 RPC 传输层（对应 `_rpc_success_response` + patch uuid）。 */
function successTransport({
  senderId = PARTNER_AIC,
  certAic = PARTNER_AIC as string | null,
}: { senderId?: string; certAic?: string | null } = {}) {
  return memoryTransport((req) => ({
    body: {
      jsonrpc: "2.0",
      id: (req.json as { id: string }).id,
      result: JSON.parse(JSON.stringify(taskResult(senderId))) as unknown,
    },
    peerCertificate: certAic === null ? null : certFor(certAic),
  }));
}

function client(transport: ReturnType<typeof memoryTransport>): AipRpcClient {
  return new AipRpcClient("https://partner.local/rpc", LEADER_AIC, {
    expectedPartnerAic: PARTNER_AIC,
    identityBindingEnabled: true,
    transport,
  });
}

describe("test_aip_rpc_identity (client)", () => {
  it("test_rpc_client_validates_outbound_sender_before_request", async () => {
    const transport = successTransport();
    const rpc = client(transport);
    await expect(rpc.sendRequest(taskCommand(OTHER_AIC))).rejects.toBeInstanceOf(
      SenderIdentityMismatchError,
    );
    expect(transport.requests).toHaveLength(0);
    await rpc.close();
  });

  it("test_rpc_client_requires_expected_partner_aic_when_enabled", () => {
    expect(
      () =>
        new AipRpcClient("https://partner.local/rpc", LEADER_AIC, { identityBindingEnabled: true }),
    ).toThrow("expected_partner_aic is required when identity_binding_enabled=True");
  });

  it("test_rpc_client_accepts_matching_tls_server_and_result_sender", async () => {
    const rpc = client(successTransport());
    const response = await rpc.sendRequest(taskCommand());
    expect(response.result).toBeDefined();
    await rpc.close();
  });

  it("test_rpc_client_rejects_missing_tls_server_aic", async () => {
    const rpc = client(successTransport({ certAic: null }));
    await expect(rpc.sendRequest(taskCommand())).rejects.toBeInstanceOf(PeerAicMissingError);
  });

  it("test_rpc_client_rejects_tls_server_aic_mismatch", async () => {
    const rpc = client(successTransport({ certAic: OTHER_AIC }));
    await expect(rpc.sendRequest(taskCommand())).rejects.toBeInstanceOf(
      SenderIdentityMismatchError,
    );
  });

  it("rejects a result whose senderId differs from the TLS server AIC", async () => {
    const rpc = client(successTransport({ senderId: OTHER_AIC }));
    await expect(rpc.sendRequest(taskCommand())).rejects.toBeInstanceOf(
      SenderIdentityMismatchError,
    );
  });

  it("test_rpc_client_validates_tls_server_before_jsonrpc_error", async () => {
    const transport = memoryTransport((req) => ({
      body: {
        jsonrpc: "2.0",
        id: (req.json as { id: string }).id,
        error: { code: -32000, message: "boom" },
      },
      peerCertificate: certFor(OTHER_AIC),
    }));
    await expect(client(transport).sendRequest(taskCommand())).rejects.toBeInstanceOf(
      SenderIdentityMismatchError,
    );
  });

  it("raises on JSON-RPC errors and mismatched response ids (identity binding disabled)", async () => {
    const errorTransport = memoryTransport((req) => ({
      body: {
        jsonrpc: "2.0",
        id: (req.json as { id: string }).id,
        error: { code: -32000, message: "boom" },
      },
    }));
    const noBinding = (transport: ReturnType<typeof memoryTransport>) =>
      new AipRpcClient("http://partner.local/rpc", LEADER_AIC, {
        identityBindingEnabled: false,
        transport,
      });
    await expect(noBinding(errorTransport).sendRequest(taskCommand())).rejects.toThrow(
      "RPC Error: -32000 - boom",
    );
    const wrongId = memoryTransport(() => ({
      body: { jsonrpc: "2.0", id: "other", result: taskResult() },
    }));
    await expect(noBinding(wrongId).sendRequest(taskCommand())).rejects.toThrow(
      "RPC Error: Response ID does not match request ID.",
    );
    const http500 = memoryTransport(() => ({ status: 500, rawBody: "boom" }));
    await expect(noBinding(http500).sendRequest(taskCommand())).rejects.toThrow(
      "HTTP Error: 500 - boom",
    );
  });

  it("test_rpc_client_logs_warning_when_identity_binding_disabled", () => {
    const logs = captureLogs();
    try {
      new AipRpcClient("http://partner.local/rpc", LEADER_AIC, { identityBindingEnabled: false });
      expect(logs.messages.join("\n").toLowerCase()).toContain("identity binding disabled");
    } finally {
      logs.restore();
    }
  });
});
