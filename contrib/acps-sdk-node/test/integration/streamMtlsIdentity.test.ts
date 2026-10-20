/**
 * 对应 Python `tests/integration/test_aip_stream_mtls_identity.py`：真实 mTLS 下的流式身份绑定。
 */

import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { type TaskResult, TaskState } from "../../src/aip/baseModel.js";
import { SenderIdentityMismatchError } from "../../src/aip/identity.js";
import { AipStreamClient } from "../../src/aip/streamClient.js";
import type { StreamResponse } from "../../src/aip/streamModel.js";
import { StreamHub, createAipStreamRequestListener } from "../../src/aip/streamServer.js";
import { createNodeTransport } from "../../src/internal/http.js";
import { utcNowIso } from "../../src/internal/time.js";
import {
  LEADER_AIC,
  OTHER_AIC,
  PARTNER_AIC,
  type RunningTlsServer,
  type TestPki,
  type TlsIdentity,
  clientTls,
  createTestPki,
  route,
  runTlsServer,
} from "./tlsTestUtils.js";

let pki: TestPki;
let partner: TlsIdentity;
let leader: TlsIdentity;
const servers: RunningTlsServer[] = [];

beforeAll(async () => {
  pki = await createTestPki();
  partner = await pki.issue(PARTNER_AIC);
  leader = await pki.issue(LEADER_AIC);
});

afterEach(async () => {
  await Promise.all(servers.splice(0).map((server) => server.close()));
});

function buildTaskResult(taskId: string, senderId: string = PARTNER_AIC): TaskResult {
  const now = utcNowIso();
  return {
    type: "task-result",
    id: "result-1",
    sentAt: now,
    senderRole: "partner",
    senderId,
    taskId,
    sessionId: "sess-1",
    status: { state: TaskState.Completed, stateChangedAt: now },
  };
}

async function startStreamApp({
  eventSenderId = PARTNER_AIC,
  localAic = PARTNER_AIC,
}: { eventSenderId?: string; localAic?: string } = {}): Promise<string> {
  const hub = new StreamHub();
  const listener = createAipStreamRequestListener(
    hub,
    {
      onStreamStart: async (command) => {
        await hub.publishTaskResult(
          command.taskId as string,
          buildTaskResult(command.taskId as string, eventSenderId),
        );
      },
    },
    { localAic, identityBindingEnabled: true },
  );
  const server = await runTlsServer(route({ "POST /stream": listener }), {
    pki,
    identity: partner,
    requireClientCert: true,
  });
  servers.push(server);
  return server.baseUrl;
}

function streamClient(baseUrl: string): AipStreamClient {
  return new AipStreamClient(`${baseUrl}/stream`, `${baseUrl}/rpc`, LEADER_AIC, {
    tls: clientTls(pki, leader),
    expectedPartnerAic: PARTNER_AIC,
    identityBindingEnabled: true,
  });
}

describe("test_aip_stream_mtls_identity", () => {
  it("test_stream_mtls_identity_binding_accepts_matching_request_and_event", async () => {
    const client = streamClient(await startStreamApp());
    const events: StreamResponse[] = [];
    try {
      for await (const item of client.startStream("sess-1", {
        taskId: "task-1",
        textContent: "hello",
      })) {
        events.push(item);
      }
    } finally {
      await client.close();
    }
    expect(events).toHaveLength(1);
    expect(events[0]?.result?.eventData.senderId).toBe(PARTNER_AIC);
  });

  it("test_stream_mtls_identity_binding_rejects_forged_request_sender", async () => {
    const baseUrl = await startStreamApp();
    const response = await createNodeTransport(clientTls(pki, leader))({
      url: `${baseUrl}/stream`,
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: "stream-1",
        method: "stream",
        params: {
          message: {
            type: "task-command",
            id: "cmd-1",
            sentAt: utcNowIso(),
            senderRole: "leader",
            senderId: OTHER_AIC,
            taskId: "task-1",
            sessionId: "sess-1",
            command: "start",
            dataItems: [{ type: "text", text: "hello" }],
          },
        },
      }),
      timeoutMs: 10_000,
    });
    expect(response.status).toBe(403);
    expect((JSON.parse(response.body.toString()) as { detail: { code: number } }).detail.code).toBe(
      -32009,
    );
  });

  it("test_stream_mtls_identity_binding_rejects_forged_event_sender", async () => {
    const client = streamClient(
      await startStreamApp({ eventSenderId: OTHER_AIC, localAic: OTHER_AIC }),
    );
    try {
      await expect(
        (async () => {
          for await (const _item of client.startStream("sess-1", {
            taskId: "task-1",
            textContent: "hello",
          })) {
            // drain
          }
        })(),
      ).rejects.toBeInstanceOf(SenderIdentityMismatchError);
    } finally {
      await client.close();
    }
  });
});
