[Home](../README.md)

**[English](amp-agent-observability_en.md) | [中文](amp-agent-observability.md)**

# Integrating AMP Observability into Agents

This tutorial is for **Agent developers**: when the platform-side AMP (forwarding, ingestion, query services) has already been installed by operations, there are two things for you to do —

1. **Head**: in your Agent code, use the `acps-sdk` Emitter to write logs  
2. **Tail**: use Monitor / Discovery / `acps-cli` to confirm that the logs can be seen  

Who collects the data and how it is ingested can be treated as a black box. For how to build an installation package and configure the Forwarder, see [Ansible deployment](./install-package-ansible-deploy_en.md); this document does not cover that.

Reference implementation: `demo-partner/partners/generic_runner.py`, and the wiring in `demo-leader`; for the end-to-end acceptance story see Step D of `business.yml` at the end of this document.

---

## 0. What you are aiming for

Once the business is running, for a given Agent's AIC you should be able to:

- query several kinds of AMP records in **Monitor** (heartbeat / metrics / access / message / system / audit)  
- see that Agent in **Discovery**'s liveness view (the result after heartbeats are synchronized by the platform)

The SDK's default behavior is: **append one line to a local NDJSON file**. Collection and ingestion are the platform's job; you only have to make sure that the process keeps writing and that the AIC matches the registered identity.

```text
[Head] Agent: Emitter → local *.jsonl / *.ndjson
        ↓  (platform black box: Forwarder → bus → ingestion / alive-sync)
[Tail] Monitor query / Discovery liveness / acps-cli monitor …
```

---

## 1. Preparation: directories, AIC, dependencies

```bash
# Development machine: install the SDK with AMP support (however your project manages dependencies)
# uv add acps-sdk   or declare the dependency in pyproject and then run uv sync
```

```python
import os
from pathlib import Path

# Installation and deployment usually inject AMP_LOG_DIR (for example /opt/acps/app/logs)
# If it is not set for local development, use the logs/ directory in the repository
AMP_LOG_DIR = Path(os.environ.get("AMP_LOG_DIR", "./logs"))
AMP_LOG_DIR.mkdir(parents=True, exist_ok=True)

# Must match the AIC of this Agent instance in the Registry
AIC = os.environ["ACPS_AIC"]  # Example: replace with your real AIC
```

Suggested conventions (the same as in the demo, so that the Forwarder can collect per file):

| Type | Suggested file name example |
| --- | --- |
| heartbeat | `amp_heartbeat_<name>.jsonl` |
| metrics | `amp_metrics_<name>.jsonl` |
| access | `amp_access_<name>.jsonl` |
| message | `amp_message_<name>.jsonl` |
| system | `amp_system_<name>.jsonl` |
| audit | `amp_audit_<name>.jsonl` |

---

## 2. Six Emitter types: how to use them

This section follows the pattern "when to use it → how to wire it into the process → what to put in the fields → example". The examples match the usual style in the demo, so that you can copy them straight into your own Agent and adapt them, instead of merely proving that the API runs.

Common habits:

- Create the Emitter **once at startup** (it binds `aic` and the log file), then call `emit` / `emit_sync` repeatedly along the business path.  
- In async services prefer `await emitter.emit(...)` (it writes the file on a thread, so it does not block the event loop); use `emit_sync` in synchronous callbacks.  
- By default a write failure only logs a WARNING and does not raise — do not make your main business path depend on "continue only if emit succeeded".  
- Carry `trace_id` / `span_id` / `correlation_id` whenever you can: troubleshooting later in Monitor by session, task, or trace becomes much easier.

### 2.1 Heartbeat — telling the platform "I am still alive"

**Purpose**: the liveness views of Discovery / Monitor depend on heartbeats. Without a continuous heartbeat, the Agent "disappears" from the alive view even though the business is still running.

**How to wire it up**: after the process starts, launch a background periodic task and cancel it on shutdown. The demo uses the `AMP_HEARTBEAT_INTERVAL_SECONDS` environment variable (about 15s by default). `uptimeSeconds` is calculated automatically by the Emitter from its creation time, so you do not fill it in.

```python
import asyncio
import contextlib
import os

from acps_sdk.amp import HeartbeatEmitter

heartbeat = HeartbeatEmitter(AMP_LOG_DIR / f"amp_heartbeat_{agent_name}.jsonl", aic=AIC)


async def run_agent() -> None:
    interval = float(os.environ.get("AMP_HEARTBEAT_INTERVAL_SECONDS", "15"))
    hb_task = asyncio.create_task(heartbeat.run_periodic(interval), name="amp-heartbeat")
    try:
        await serve_forever()  # your main loop
    finally:
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb_task
```

`run_periodic` **sends the first entry immediately** and then keeps sending at the interval, which makes it easy to verify the pipeline right after startup. Local self-check: the file is growing, and `heartbeat liveness <AIC>` in Monitor returns a hit.

### 2.2 Metrics — periodic metric snapshots

**Purpose**: provide Monitor with load and window statistics (concurrent tasks, CPU/memory, success rate, latency percentiles, and so on) that back snapshots / series queries.

**How to wire it up**: similar to heartbeat — call `run_periodic` at startup. The difference is that the body is not invented by the Emitter: you implement a `SampleProvider` (all it needs is `sample() -> MetricsBody`). In production you should read real queue lengths and process resources; for integration testing you can start with the SDK's `DemoMetricsSampler` (**demo only, never in production**).

For `resource`, include an OpenTelemetry-style service identity so that you can filter by service name:

```python
import time

from acps_sdk.amp import LoadMetrics, MetricsBody, MetricsEmitter, WindowMetrics

_start = time.monotonic()


class AgentMetricsSampler:
    """Example: sample real load from this process's task table (fill in what you can actually obtain)."""

    def __init__(self, tasks: dict) -> None:
        self._tasks = tasks

    def sample(self) -> MetricsBody:
        active = sum(1 for t in self._tasks.values() if t.state == "working")
        queued = sum(1 for t in self._tasks.values() if t.state == "submitted")
        return MetricsBody(
            uptime_seconds=round(time.monotonic() - _start, 3),
            load_metrics=LoadMetrics(
                active_tasks=active,
                queued_tasks=queued,
                max_active_tasks=10,
                max_queued_tasks=50,
                cpu_usage=None,  # fill these in once you have psutil / cgroup
                memory_usage=None,
            ),
            window_metrics=[
                WindowMetrics(
                    window="PT5M",
                    success_rate=98.5,
                    request_total=120,
                    request_per_second=0.4,
                    p50_latency_ms=80.0,
                    p95_latency_ms=220.0,
                    p99_latency_ms=450.0,
                ),
            ],
        )


metrics = MetricsEmitter(
    AMP_LOG_DIR / f"amp_metrics_{agent_name}.jsonl",
    aic=AIC,
    sampler=AgentMetricsSampler(tasks),
    resource={
        "service.name": f"my-partner-{agent_name}",
        "service.namespace": "acps-demo",
        "deployment.environment.name": "dev",
    },
)

# After startup: asyncio.create_task(metrics.run_periodic(30.0))
```

Window strings use ISO 8601 durations (such as `PT1M` / `PT5M` / `PT15M`). For the shared metric-name conventions see the SDK's `metrics_catalog`; always keep the percentile order `p50 ≤ p95 ≤ p99`.

### 2.3 Access — the edge of one request/response

**Purpose**: record who called whom with which method, how long it took, and whether it succeeded or failed. Typical wiring points are HTTP `/rpc` and outbound Partner calls — **emit in `finally`** so that exception paths are recorded too.

Key fields:

| Field | Meaning |
| --- | --- |
| `request.method` / `route` / `url` | Verb and route; use a stable template for `route` (such as `/rpc`) so that records aggregate by endpoint |
| `response.statusCode` | HTTP / RPC status |
| `caller` / `callee` | Both parties' AIC + `serviceName` |
| `durationMs` | Total duration |
| `error` | The `ErrorInfo` on failure |
| `trace_id` / `span_id` / `correlation_id` | Trace and session (the demo commonly uses `sessionId` as the correlation) |

Below is the teaching version for an inbound Partner RPC (structured to match `demo-partner/partners/main.py`):

```python
import time

from acps_sdk.amp import (
    AccessBody,
    AccessEmitter,
    AccessParticipant,
    AccessRequest,
    AccessResponse,
    ErrorInfo,
)

access = AccessEmitter(AMP_LOG_DIR / f"amp_access_{agent_name}.jsonl", aic=AIC)


async def handle_rpc(command, request_headers: dict[str, str]) -> object:
    t0 = time.monotonic()
    request_bytes = len(command.model_dump_json().encode("utf-8"))
    resp = None
    status_code = 200
    error_info = None
    try:
        resp = await dispatch(command)
        status_code = status_from_rpc_response(resp)
        error_info = error_info_from_rpc_response(resp)
        return resp
    except Exception as exc:
        status_code = 500
        error_info = ErrorInfo(code=500, message=str(exc))
        raise
    finally:
        duration_ms = int((time.monotonic() - t0) * 1000)
        response_bytes = (
            len(resp.model_dump_json().encode("utf-8")) if resp is not None else 0
        )
        method = str(command.command)
        body = AccessBody(
            request=AccessRequest(
                method=method,
                url="/rpc",
                route="/rpc",
                headers=request_headers,
                bodySizeBytes=request_bytes,
            ),
            response=AccessResponse(statusCode=status_code, bodySizeBytes=response_bytes),
            caller=AccessParticipant(aic=command.senderId, serviceName="demo-leader"),
            callee=AccessParticipant(aic=AIC, serviceName=f"demo-partner-{agent_name}"),
            error=error_info,
            durationMs=duration_ms,
        )
        try:
            await access.emit(
                body,
                trace_id=trace_id,          # taken from the inbound headers / context
                span_id=span_id,
                parent_span_id=parent_span_id,
                correlation_id=command.sessionId,
            )
        except Exception:
            logger.warning("Access emit failed", exc_info=True)
```

When a Leader makes an outbound call to a Partner you can emit access in the same way; just swap `caller` / `callee` and write the peer's address as `url`. To query, use `acps-cli monitor access events`, or stitch the whole call chain together by `trace_id`.

### 2.4 Message — the edges of a message's lifecycle

**Purpose**: group / RabbitMQ / Kafka scenarios. For one and the same business message, **write a separate log entry for each of send, receive, ack/nack, …**, and let the query layer stitch the lifecycle together from the shared `messageId` (plus the optional `correlationId`).

`event_type` values: `send` | `receive` | `ack` | `nack` | `reject` | `timeout` | `dead_letter`. For settlement events, add duration and reason with `MessageSettlement`.

If you go through the SDK's group MQ client, you can inject a `MessageEmitter` and the client instruments the send/receive paths automatically; when you assemble bodies yourself you can also call the Emitter directly. When building a body, refer to the assembly logic the group client uses in the SDK (`build_send_body` / `build_receive_body` / `build_settlement_body` in `acps_sdk/amp/_message_tap.py`):

```python
from acps_sdk.amp import MessageBody, MessageDestination, MessageEmitter, MessageRouting, MessageSettlement

message = MessageEmitter(AMP_LOG_DIR / f"amp_message_{agent_name}.jsonl", aic=AIC)

# Producer: publish to the group exchange
await message.emit(
    MessageBody(
        event_type="send",
        operation_name="publish",
        system="rabbitmq",
        destination=MessageDestination(
            name="group.demo.exchange",
            kind="exchange",
            virtual_host="/",
        ),
        routing=MessageRouting(key="group.demo"),
        message_id="msg-001",
        payload_size_bytes=256,
    ),
    correlation_id="group-session-1",
    trace_id=trace_id,
)

# Consumer: after receiving
await message.emit(
    MessageBody(
        event_type="receive",
        operation_name="deliver",
        system="rabbitmq",
        destination=MessageDestination(name="group.demo.exchange", kind="exchange"),
        subscription_name="partner-queue-1",
        routing=MessageRouting(key="group.demo"),
        message_id="msg-001",
        payload_size_bytes=256,
        delivery_attempt=1,
    ),
    correlation_id="group-session-1",
)

# Settlement: ack (nack + reason on failure)
await message.emit(
    MessageBody(
        event_type="ack",
        operation_name="basic.ack",
        system="rabbitmq",
        destination=MessageDestination(name="group.demo.exchange", kind="exchange"),
        subscription_name="partner-queue-1",
        message_id="msg-001",
        delivery_attempt=1,
        settlement=MessageSettlement(latency_ms=35.0),
    ),
    correlation_id="group-session-1",
)
```

To query: `message events` by time window; when you have a `messageId` you can also look up the lifecycle. Do not dump the whole payload into the log — recording the size and the identifiers is enough.

### 2.5 System — internal agent events

**Purpose**: internal observability with no strict schema — LLM calls, Skill execution, process lifecycle, internal errors, and so on. `body` is a free-form `dict`; in Monitor you filter on `severity_text` / `severity_number` plus the fields your team has agreed on (such as `category`, `component`).

Suggested conventions (the ones the demo usually uses):

- `message`: a one-line human-readable summary  
- `category`: `llm` / `skill` / `lifecycle` …  
- `component` / `module`: where it happened  
- `elapsed_ms`, and on errors `error_type` / `error_message`  
- `correlation_id=task_id`: strings together the multiple system records of the same task  

Severity example: success INFO → `severity_number=9`; failure ERROR → `17` (matching the demo is enough — being consistent inside your team matters more than the "absolute value").

```python
import time

from acps_sdk.amp import SystemEmitter

system = SystemEmitter(
    AMP_LOG_DIR / f"amp_system_{agent_name}.jsonl",
    aic=AIC,
    resource={
        "service.name": f"demo-partner-{agent_name}",
        "service.namespace": "acps-demo",
        "deployment.environment.name": "dev",
    },
)


async def call_llm(stage: str, model: str, task_id: str) -> str:
    t0 = time.monotonic()
    try:
        content = await client.chat.completions.create(...)
        elapsed = int((time.monotonic() - t0) * 1000)
        system.emit_sync(
            {
                "message": f"LLM call completed: stage={stage}, model={model}, elapsed={elapsed}ms",
                "category": "llm",
                "component": "llm_client",
                "module": stage,
                "model": model,
                "tags": {"model": model, "stage": stage, "task_id": task_id},
                "elapsed_ms": elapsed,
                "task_id": task_id,
                "token_total": getattr(getattr(response, "usage", None), "total_tokens", None),
            },
            severity_number=9,
            severity_text="INFO",
            correlation_id=task_id,
        )
        return content
    except Exception as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        system.emit_sync(
            {
                "message": f"LLM call failed: stage={stage}, model={model}, error={type(e).__name__}",
                "category": "llm",
                "component": "llm_client",
                "module": stage,
                "model": model,
                "tags": {"model": model, "stage": stage, "error_type": type(e).__name__},
                "elapsed_ms": elapsed,
                "task_id": task_id,
                "error_type": type(e).__name__,
                "error_message": str(e)[:500],
            },
            severity_number=17,
            severity_text="ERROR",
            correlation_id=task_id,
        )
        raise
```

Process start and stop can each emit a lifecycle system record too (see the `main` of `demo-partner` / `demo-leader`). When querying, use `--severity-min` to see only errors, or `--correlation-id` to follow a task.

### 2.6 Audit — key actions that need a trail

**Purpose**: who did what to which resource, and with what result — a good fit for task acceptance / final state, permission-related operations, and non-repudiable state changes. The structure is fixed as **actor / action / target / result**.

How this divides the work with system: system leans towards "runtime observability", audit towards "post-hoc accountability and compliance". The same business flow can write both (for example, LLM details go to system, the task's final state goes to audit).

**Signing**: in production you should pass a `signer` to `AuditEmitter` (the demo prefers a CA certificate and falls back to `audit_keys.json` / `load_signer_from_keys_json` when that fails). Unsigned records can still be written, but Monitor may flag them as `missing_public_key`. The specification requires audit records to carry integrity; during integration you can get the pipeline working first and complete the signature and trust material before go-live.

```python
from pathlib import Path

from acps_sdk.amp import (
    AuditAction,
    AuditActor,
    AuditBody,
    AuditEmitter,
    AuditResult,
    AuditTarget,
    load_signer_from_keys_json,
)

signer = load_signer_from_keys_json(Path("config/audit_keys.json"), AIC)
# or CertificateAuditSigner(private_key_pem=..., cert_pem=...)

audit = AuditEmitter(
    AMP_LOG_DIR / f"amp_audit_{agent_name}.jsonl",
    aic=AIC,
    signer=signer,
)

# After receiving Start and completing the decision (aligned with demo B1)
await audit.emit(
    AuditBody(
        actor=AuditActor(id=command.senderId or "unknown", type="agent"),
        action=AuditAction(name="receive_task_start", type="aip_protocol"),
        target=AuditTarget(type="task", id=task_id),
        result=AuditResult(
            status="success" if accepted else "failure",
            reason=None if accepted else reject_reason,
        ),
    ),
    trace_id=command.sessionId,
    correlation_id=task_id,
)

# When the task reaches a final state (aligned with demo B2; use emit_sync in a synchronous state machine)
audit.emit_sync(
    AuditBody(
        actor=AuditActor(id=AIC, type="agent"),
        action=AuditAction(name="task_state_transition", type="aip_protocol"),
        target=AuditTarget(type="task", id=task_id),
        result=AuditResult(status="success", reason="completed"),
    ),
    trace_id=session_id,
    correlation_id=task_id,
)
```

`result.status` can only be `success` / `failure` / `unknown`. For `action.name` / `action.type`, agree on one stable vocabulary inside your team so that records can be searched by action.

---

## 3. What happens in between (the black box)

After installation and deployment are complete, the rough picture is:

1. The agent writes NDJSON into `AMP_LOG_DIR`  
2. The **AMP Forwarder** (Fluent Bit and the like) tails these files and feeds them into the platform bus  
3. **Monitor**'s Writers ingest them; heartbeat also joins **alive-sync**, which Discovery uses to show liveness  

As a developer you usually do **not** need to change the Forwarder configuration. You only need:

- the file paths to sit under the directory the deployment agrees on  
- the process to keep writing  
- the `aic` field to be correct  

Platform-side troubleshooting (the Forwarder not hooked up, a missing partition, and so on) is down to operations; see the installation guide and [Day-2 operations](./install-package-day2-ops_en.md).

---

## 4. How to observe it (the tail)

### 4.1 Look at the local files first (the quickest self-check)

```bash
ls -la "${AMP_LOG_DIR:-./logs}"/amp_*.jsonl
tail -n 1 "${AMP_LOG_DIR:-./logs}"/amp_heartbeat_demo.jsonl
```

If the files are growing, the "head" is already working.

### 4.2 Querying Monitor with acps-cli

Prerequisite: you can reach a deployed monitor-server, and the CLI is configured with the address and authentication (OIDC or local-auth; see `acps-cli monitor` in the [CLI reference](../references/cli-reference_en.md)).

You can use **Beijing time (UTC+8)** for the time window, written as ISO 8601 with `+08:00`; the CLI passes it to Monitor unchanged and the server parses it as an offset-aware time. Replace the range with a window around when you actually emitted the logs (for the `events` / `records` of access / message / system / audit, you generally need `--start` / `--end` unless a full request is supplied):

```bash
# Example: the whole of 2026-07-25 (Beijing time)
START=2026-07-25T00:00:00+08:00
END=2026-07-25T23:59:59+08:00

# Health
acps-cli monitor status

# Heartbeat (point query for a single AIC)
acps-cli monitor heartbeat liveness "$AIC"

# Metrics snapshots
acps-cli monitor metrics snapshots --aic "$AIC"

# Access / message / system / audit (by AIC + time window)
acps-cli monitor access events --aic "$AIC" --start "$START" --end "$END"
acps-cli monitor message events --start "$START" --end "$END"
acps-cli monitor system events --aic "$AIC" --start "$START" --end "$END"
acps-cli monitor audit records --aic "$AIC" --start "$START" --end "$END"
```

There may be a short delay right after writing, so wait a moment before querying. For more subcommands (trace, series, verify, and others) see the CLI reference.

### 4.3 The Discovery liveness view

Once the platform has processed the heartbeat, Discovery's alive view should cover that AIC (see the Discovery / ADP documentation for the specific API). Business acceptance Step D checks "liveness coverage for the Leader + Partner".

---

## 5. The end-to-end story: Step D of `business.yml`

This is not about teaching you to write Ansible; it shows how **demo code + platform + queries** prove that AMP works.

1. **Operations** runs `site.yml` from the installation package and brings up demo-leader / demo-partner (see the [deployment tutorial](./install-package-ansible-deploy_en.md)).  
2. **Head**: the demo process has already created the six Emitter types at startup (for Partner see `generic_runner.py`). When running business acceptance A/B/C:  
   - the RPC path writes **access**  
   - the group path writes **message**  
   - LLM / lifecycle writes **system**  
   - periodic tasks write **heartbeat** / **metrics**  
   - the task's final state and the like write **audit**  
3. **Middle**: the Forwarder and Monitor work as the installation prescribes (not the developer's concern).  
4. **Tail**: **Step D** of `business.yml` polls Monitor for whether the demo-related AICs turn up with metrics / system / access / message / audit records, and checks Discovery liveness.  

So: when you write your own agent, what you align with is **the same kind of Emitter + the correct AIC**, not edits to `business.yml`. Step D is simply the platform side's "acceptance script".

---

## 6. Developer self-check list

- [ ] the corresponding `amp_*.jsonl` under `AMP_LOG_DIR` (or a local `logs/`) is growing  
- [ ] the `aic` of every record matches the registered identity  
- [ ] every type you enabled among the six can be found in Monitor by AIC (a short delay is acceptable)  
- [ ] after Heartbeat has been running for a while, Discovery liveness can see you  
- [ ] if Audit requires signature verification, `signer` is configured and the trust material on the Monitor side is complete  

---

## 7. What to do next

- How to write AIP business logic: [Agent quick development guide](./agent-development_en.md)  
- SDK / protocol details: `acps_sdk/amp/` in `acps-sdk`, plus the AMP-related specifications in `acps-specs`  
- CLI query parameters: [CLI reference · monitor](../references/cli-reference_en.md)  
- How to install and operate the platform: [Ansible deployment](./install-package-ansible-deploy_en.md), [Day-2 operations](./install-package-day2-ops_en.md)  
- Reference implementation assembly: AMP initialization and call sites in `demo-partner/partners/generic_runner.py` and `demo-leader`  
