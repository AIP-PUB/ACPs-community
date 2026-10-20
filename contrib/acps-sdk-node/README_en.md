# ACPs SDK for Node.js

[中文](README.md)

Node.js / TypeScript implementation of the Agent Collaboration Protocols (ACPs) SDK.

This package is a port of [`acps-sdk`](../../acps-sdk/README.md) (Python, v2.2.0). **Wire format and behavior match
the Python SDK**; the API mirrors the Python one while following Node conventions (`snake_case` → `camelCase`,
keyword arguments → an options object). The mapping rules are documented in
[docs/porting-notes.md](docs/porting-notes.md).

Contributing organization: 北京智镜科技有限责任公司

Contributor: Hongbo Xu

| Module          | Description                                                                                                     | Python counterpart |
| --------------- | --------------------------------------------------------------------------------------------------------------- | ------------------ |
| `acps-sdk/aic`  | Agent Identity Code: format validation, parsing, CRC checksum                                                   | `acps_sdk.aic`     |
| `acps-sdk/acs`  | Agent Capability Specification models                                                                           | `acps_sdk.acs`     |
| `acps-sdk/adp`  | Agent Discovery Protocol models and validators                                                                  | `acps_sdk.adp`     |
| `acps-sdk/aip`  | Agent Interaction Protocol: Direct RPC, streaming (SSE), notifications, group mode (RabbitMQ), identity binding | `acps_sdk.aip`     |
| `acps-sdk/amp`  | Agent Monitoring Protocol: heartbeat, metrics, access, message, system and audit emitters, alive sync           | `acps_sdk.amp`     |
| `acps-sdk/oidc` | Human-user OIDC authentication and authorization (device flow, JWT validation, Keycloak claims)                 | `acps_sdk.oidc`    |

## Requirements

- Node.js ^20.19 or >= 22.12 (`jose`, used by `acps-sdk/oidc`, is ESM-only; loading it from CommonJS needs a Node version that supports `require()` of ES modules)
- Runtime dependencies: `zod`, `amqplib` (group mode), `jose` (OIDC)
- TypeScript projects need `@types/node`

## Installation

Not yet published to npm. Build from source:

```bash
cd contrib/acps-sdk-node
npm ci
npm run build
# in your project
npm install /path/to/ACPs-community/contrib/acps-sdk-node
```

## Quick start

### Minimal Partner (Direct RPC over mTLS)

```ts
import { readFileSync } from "node:fs";
import https from "node:https";
import {
  type CommandHandlers,
  DefaultHandlers,
  TaskManager,
  TaskState,
  createAipRpcRequestListener,
} from "acps-sdk/aip";

const PARTNER_AIC = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4";

const handlers: CommandHandlers = {
  async onStart(command, task) {
    if (task) return task;
    const text = command.dataItems?.find((item) => item.type === "text");
    const created = TaskManager.createTask(command, TaskState.AwaitingCompletion);
    created.senderId = PARTNER_AIC;
    TaskManager.setProducts(created.taskId, [
      {
        id: `product-${created.taskId}`,
        name: "echo",
        dataItems: [{ type: "text", text: `Processed: ${text?.type === "text" ? text.text : ""}` }],
      },
    ]);
    return TaskManager.getTask(created.taskId) ?? created;
  },
  onGet: DefaultHandlers.get,
  onCancel: DefaultHandlers.cancel,
  onComplete: DefaultHandlers.complete,
};

const rpc = createAipRpcRequestListener(handlers, { localAic: PARTNER_AIC });

https
  .createServer(
    {
      key: readFileSync("agent-key.pem"),
      cert: readFileSync("agent-cert.pem"), // serverAuth certificate
      ca: readFileSync("trust-bundle.pem"),
      requestCert: true, // mTLS: require and verify the Leader's client certificate
      rejectUnauthorized: true,
    },
    (req, res) => {
      if (req.url === "/rpc" && req.method === "POST") return void rpc(req, res);
      res.statusCode = 404;
      res.end();
    },
  )
  .listen(8011);
```

Identity binding is on by default: the request `senderId` must equal the AIC in the client certificate, and the
returned `senderId` must equal the local AIC. **mTLS must terminate inside the Node process** (do not terminate TLS
at a reverse proxy such as nginx), otherwise the peer certificate is not available.

### Minimal Leader

```ts
import { readFileSync } from "node:fs";
import { AipRpcClient, TaskState } from "acps-sdk/aip";

const client = new AipRpcClient("https://partner.example.com:8011/rpc", LEADER_AIC, {
  tls: {
    cert: readFileSync("leader-cert.pem"), // clientAuth certificate
    key: readFileSync("leader-key.pem"),
    ca: readFileSync("trust-bundle.pem"),
  },
  expectedPartnerAic: PARTNER_AIC,
});

let task = await client.startTask(sessionId, "Please process this text");
while (task.status.state === TaskState.Accepted || task.status.state === TaskState.Working) {
  await new Promise((r) => setTimeout(r, 1000));
  task = await client.getTask(task.taskId, sessionId);
}
if (task.status.state === TaskState.AwaitingCompletion) {
  task = await client.completeTask(task.taskId, sessionId);
}
```

Streaming (`AipStreamClient` / `StreamHub` + `createAipStreamRequestListener`), notifications
(`AipNotificationClient` / `NotificationService` + `createAipNotificationRequestListener` / `NotificationReceiver`)
and group mode (`GroupLeader` / `GroupPartnerMqClient`) follow the Python SDK; see the source doc comments and the
[AIP SDK tutorial](../acps-docs/tutorials/aip-sdk-tutorial_en.md).

### Web framework integration

The server side does not depend on any web framework. Every endpoint provides:

- a framework-agnostic core (`handleRpcRequest`, `handleStreamRequest`, `handleNotificationRequest`) that takes the
  parsed body plus the peer identity and returns a response object;
- `createXxxRequestListener(...)`, returning `(req, res) => Promise<void>`, usable directly with
  `https.createServer` or mounted on an Express route; `createAipNotificationRequestListener` routes
  `/notification/*` itself and returns `Promise<boolean>` (`false` when the path did not match).

## Development

The development toolchain (vitest, tsup) requires Node.js >= 22.18.

```bash
npm ci
npm test              # unit + integration tests (integration tests generate throwaway certs and start local mTLS servers)
npm run qa            # format, lint, typecheck, test, build
npm run fixtures:python   # regenerate cross-language fixtures from ../../acps-sdk (requires uv)
```

Cross-language fixtures are produced by the Python SDK (`scripts/fixtures/gen_*.py` → `test/fixtures/python/`) and
compared field by field in the Node tests to keep the wire format identical.

## License

[Mulan Permissive Software License, Version 2](LICENSE)
