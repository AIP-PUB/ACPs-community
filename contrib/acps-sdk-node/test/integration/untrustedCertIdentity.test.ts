/**
 * 未通过证书链校验的证书不能用于身份绑定。
 *
 * Node 的 `getPeerCertificate()` 在 `rejectUnauthorized: false` 时仍会返回对端证书；
 * 如果直接据此提取 AIC，任何自签证书都能冒充任意 AIC。这里覆盖服务端与客户端两个方向。
 * （Python 的 `ssl.getpeercert()` 对未校验证书返回空，因此 Python 版没有这个问题。）
 */

import { type IncomingMessage, type ServerResponse } from "node:http";
import { type AddressInfo } from "node:net";
import { type Server, createServer } from "node:https";

import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { type TaskResult, TaskState } from "../../src/aip/baseModel.js";
import { PeerAicMissingError } from "../../src/aip/identity.js";
import { NotificationReceiver } from "../../src/aip/notificationClient.js";
import {
  NotificationService,
  createAipNotificationRequestListener,
} from "../../src/aip/notificationServer.js";
import { AipRpcClient } from "../../src/aip/rpcClient.js";
import { createAipRpcRequestListener } from "../../src/aip/rpcServer.js";
import { AipStreamClient } from "../../src/aip/streamClient.js";
import { StreamHub, createAipStreamRequestListener } from "../../src/aip/streamServer.js";
import { createNodeTransport } from "../../src/internal/http.js";
import { utcNowIso } from "../../src/internal/time.js";
import {
  LEADER_AIC,
  PARTNER_AIC,
  type TestPki,
  type TlsIdentity,
  createTestPki,
} from "./tlsTestUtils.js";

let trusted: TestPki;
let rogue: TestPki;
let partner: TlsIdentity;
let leader: TlsIdentity;
let rogueLeader: TlsIdentity;
let roguePartner: TlsIdentity;
const servers: Server[] = [];

beforeAll(async () => {
  trusted = await createTestPki();
  rogue = await createTestPki();
  partner = await trusted.issue(PARTNER_AIC);
  leader = await trusted.issue(LEADER_AIC);
  rogueLeader = await rogue.issue(LEADER_AIC);
  roguePartner = await rogue.issue(PARTNER_AIC);
});

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

function acceptedResult(taskId: string): TaskResult {
  const now = utcNowIso();
  return {
    type: "task-result",
    id: "result-1",
    sentAt: now,
    senderRole: "partner",
    senderId: PARTNER_AIC,
    taskId,
    status: { state: TaskState.Accepted, stateChangedAt: now },
  };
}

/** 可选客户端证书的 Partner：requestCert: true, rejectUnauthorized: false。 */
async function startOptionalCertPartner(serverIdentity: TlsIdentity): Promise<string> {
  const rpc = createAipRpcRequestListener(
    { onStart: async (command) => acceptedResult(command.taskId as string) },
    { localAic: PARTNER_AIC },
  );
  const server = createServer(
    {
      cert: serverIdentity.cert,
      key: serverIdentity.key,
      ca: trusted.ca,
      requestCert: true,
      rejectUnauthorized: false,
    },
    (req, res) => void rpc(req, res),
  );
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return `https://127.0.0.1:${(server.address() as AddressInfo).port}/rpc`;
}

/** 可选客户端证书的通用服务（requestCert: true, rejectUnauthorized: false），挂任意监听函数。 */
async function startOptionalCertServer(
  serverIdentity: TlsIdentity,
  listener: (req: IncomingMessage, res: ServerResponse) => unknown,
): Promise<string> {
  const server = createServer(
    {
      cert: serverIdentity.cert,
      key: serverIdentity.key,
      ca: trusted.ca,
      requestCert: true,
      rejectUnauthorized: false,
    },
    (req, res) => void Promise.resolve(listener(req, res)),
  );
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return `https://127.0.0.1:${(server.address() as AddressInfo).port}`;
}

function rawCommand(taskId: string) {
  return {
    type: "task-command",
    id: `cmd-${taskId}`,
    sentAt: utcNowIso(),
    senderRole: "leader",
    senderId: LEADER_AIC,
    command: "start",
    taskId,
    sessionId: "sess-1",
  };
}

describe("untrusted certificates are never used for identity binding", () => {
  it("server accepts a client certificate issued by the trusted CA", async () => {
    const url = await startOptionalCertPartner(partner);
    const client = new AipRpcClient(url, LEADER_AIC, {
      tls: { cert: leader.cert, key: leader.key, ca: trusted.ca },
      expectedPartnerAic: PARTNER_AIC,
    });
    const result = await client.startTask("sess-1", "hi", "task-trusted");
    expect(result.status.state).toBe(TaskState.Accepted);
  });

  it("server rejects a rogue-CA client certificate that claims the leader AIC", async () => {
    const url = await startOptionalCertPartner(partner);
    const client = new AipRpcClient(url, LEADER_AIC, {
      tls: { cert: rogueLeader.cert, key: rogueLeader.key, ca: trusted.ca },
      expectedPartnerAic: PARTNER_AIC,
    });
    await expect(client.startTask("sess-1", "hi", "task-rogue")).rejects.toThrow(
      "RPC Error: -32008 - AuthenticationRequiredError",
    );
  });

  it("client ignores an unverified server certificate even with rejectUnauthorized: false", async () => {
    const url = await startOptionalCertPartner(roguePartner);
    const client = new AipRpcClient(url, LEADER_AIC, {
      tls: { cert: leader.cert, key: leader.key, ca: trusted.ca, rejectUnauthorized: false },
      expectedPartnerAic: PARTNER_AIC,
    });
    await expect(client.startTask("sess-1", "hi", "task-rogue-server")).rejects.toBeInstanceOf(
      PeerAicMissingError,
    );
  });

  it("stream listener rejects a rogue-CA client certificate", async () => {
    const base = await startOptionalCertServer(
      partner,
      createAipStreamRequestListener(new StreamHub(), async () => undefined, {
        localAic: PARTNER_AIC,
      }),
    );
    const transport = createNodeTransport({
      cert: rogueLeader.cert,
      key: rogueLeader.key,
      ca: trusted.ca,
    });
    const response = await transport({
      url: `${base}/stream`,
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: "s1",
        method: "stream",
        params: { message: rawCommand("t-s") },
      }),
    });
    expect(response.status).toBe(401);
    expect(JSON.parse(response.body.toString("utf-8"))).toMatchObject({ detail: { code: -32008 } });
  });

  it("notification listener rejects a rogue-CA client certificate", async () => {
    const service = new NotificationService({
      localAic: PARTNER_AIC,
      callbackTls: { ca: trusted.ca },
    });
    const notifications = createAipNotificationRequestListener(service);
    const base = await startOptionalCertServer(partner, async (req, res) => {
      if (!(await notifications(req, res))) {
        res.statusCode = 404;
        res.end();
      }
    });
    const transport = createNodeTransport({
      cert: rogueLeader.cert,
      key: rogueLeader.key,
      ca: trusted.ca,
    });
    const response = await transport({
      url: `${base}/notification/set`,
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: "n1",
        method: "notification/set",
        params: { url: "https://leader/cb", token: "t", taskId: "t-n" },
      }),
    });
    expect(response.status).toBe(401);
  });

  it("notification receiver rejects a callback from a rogue-CA certificate", async () => {
    const receiver = new NotificationReceiver("tok", async () => undefined);
    const base = await startOptionalCertServer(leader, receiver.createRequestListener());
    const transport = createNodeTransport({
      cert: roguePartner.cert,
      key: roguePartner.key,
      ca: trusted.ca,
    });
    const response = await transport({
      url: `${base}/callback`,
      method: "POST",
      headers: { "Content-Type": "application/json", "X-ACPs-AIP-Notification-Token": "tok" },
      body: JSON.stringify(acceptedResult("t-cb")),
    });
    expect(response.status).toBe(401);
  });

  it("stream client ignores an unverified server certificate even with rejectUnauthorized: false", async () => {
    const base = await startOptionalCertServer(
      roguePartner,
      createAipStreamRequestListener(new StreamHub(), async () => undefined, {
        localAic: PARTNER_AIC,
      }),
    );
    const client = new AipStreamClient(`${base}/stream`, `${base}/rpc`, LEADER_AIC, {
      tls: { cert: leader.cert, key: leader.key, ca: trusted.ca, rejectUnauthorized: false },
      expectedPartnerAic: PARTNER_AIC,
    });
    await expect(
      (async () => {
        for await (const _event of client.startStream("sess-1", { taskId: "t-rogue" })) {
          // drain
        }
      })(),
    ).rejects.toBeInstanceOf(PeerAicMissingError);
  });
});
