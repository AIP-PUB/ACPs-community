# Resumable AIP group tasks

This opt-in module composes the official `GroupLeader` and
`GroupPartnerMqClient`. Existing constructors and task APIs keep their defaults.
The local adapter does not establish exactly-once delivery by the broker.

```python
from acps_sdk.aip.resumable import (
    CoordinatorTerm, Fence, OperationLedger, ResumableGroupLeader, TaskJournal,
)

journal = TaskJournal("state/tasks.sqlite")
term = CoordinatorTerm(journal)  # once per coordinator process startup
resumable_leader = ResumableGroupLeader(native_leader, journal, term)
ledger = OperationLedger("state/database.sqlite", resource_domain="database")
outcome = await ledger.execute(
    operation_id="incident-123:repair-connection-limit",
    parameters={"limit": 30},
    fence=Fence(int(term), takeover_version=0),
    action=apply_change,
    inspect=read_current_limit,
    expected_before=100,
    expected_after=30,
)
```

`TaskJournal.record_attempt` writes dispatch intent before sending. The leader
adapter connects `journal.on_task_update` through the new public
`GroupLeader.add_task_listener(listener)` API; sync and async listeners receive
`(group_session, task_result)` after the in-memory task update.
`list_tasks(in_flight_only=True)` remains available after process restart.
Transport exceptions are `delivery_unknown`, since publication can succeed
before a reply is lost. Events retain artifact data and SHA-256 digests.

All partners modifying one resource domain must share the same SQLite file and
domain name. Use a local filesystem with working SQLite locking, not separate
copies or an unsuitable network filesystem. The SQLite writer lock covers the
pre-execution fence comparison and the effect. A fence activation waits until
that effect finishes. `next_fence(term)` allocates a distinct takeover version
atomically; call synchronous lock-taking methods through `asyncio.to_thread`
from async code. `activate_fence` is already asynchronous.

Replay decisions:

| Previous state | Action |
| --- | --- |
| No operation record | Execute; observations can bypass the modification ledger |
| Observation evidence available | `ResumePackage.reusable_evidence()` returns only references with a future `expires_at`; expired evidence must be collected again |
| `received` | Inspect and compare an expected precondition when specified, then execute |
| `executing`, `unknown`, `failed` | Without inspection, return `reconciliation_required`; matching `expected_after` becomes `reconciled_applied` |
| `applied` | Return the stored result as `skipped_already_applied` |

An older token is rejected before replay lookup as `rejected_stale_fence`, even
when the operation was applied. Reusing an ID with different parameters returns
`rejected_parameter_mismatch`. Any action exception, cancellation, failed
postcondition, or unserializable result leaves an uncertain record; tool errors
can arrive after an effect, so they do not establish safe failure. Result and
state storage require JSON-compatible values. Exceptions are stored by type,
without raw tool output or credentials.

Uncertain records never retry by default. `retry_if_unchanged=True` additionally
requires a resource query matching an explicit `expected_before`. Enable it only
when resource-specific verification proves that the old effect cannot still
arrive. A process termination can leave an independent MCP subprocess running;
that subprocess must be stopped or the resource itself must enforce fencing.
SQLite cannot cancel effects that bypass its lock. There is deliberately no
claim that an arbitrary external tool has exactly-once effects.

`ResumePackage` stores logical task identity, goal, evidence references,
hypotheses, remaining tasks, issued operations, and a fence. `TakeoverManager`
accepts authenticated ADP discovery transport as an injected callable receiving
the official `DiscoveryRequest`; the SDK ADP module itself only supplies models.
It confirms capability, advances the shared ledger fence, removes the previous
member, invites the replacement using native leader methods, waits for observed
membership, and dispatches a `start` command containing `type=resume` and the
package. Resume is a payload kind, not an invented native AIP command enum.
Discovery, capability probing, status replies, and heartbeat lookup must be wired
to the deployment. Successful publication of a status command is not a liveness
response. Failure reasons and takeover progress are persisted to the journal.

For a coordinator that cannot access a resource domain's files, pass
`ledger=None, advance_fence=remote_advance` to `TakeoverManager`.
`remote_advance(candidate, proposed_fence)` must use an authenticated domain
endpoint to atomically advance the candidate's shared ledger, and return its
confirmed `Fence` (or a dict with its two fields). The coordinator never reads a
resource credential or ledger in this mode. A remote failure prevents resume
dispatch. The domain must reject stale proposals or allocate an even newer
version atomically, including across concurrent coordinators.

An optional `resume_payload_factory(package)` adapts the package to an existing
business contract. Both callbacks may be sync or async. For incident-pilot's
`ifc/1.0` gateway contract, use the *original* operation identity, not a new ID:

```python
def make_resume(package):
    return {
        "schema_version": "ifc/1.0", "incident_id": incident_id,
        "logical_task_id": package.logical_task_id, "kind": "resume",
        "tool": original_tool, "operation_id": original_operation_id,
        "fence": package.fence.to_dict(),
        "args": {"resume_package": package.to_dict()},
    }
```

The existing default payload remains `{type: resume, resume_package: ...}` for
applications without this business schema. Configuring these callbacks is an
integration step; this module does not claim that a live platform F15 takeover
has been completed.

`ResumableGroupPartner.set_command_handler` wraps policy-classified `write` and
`high` task payloads, requiring `operation_id` and `fence`. Supply `result_handler`
to publish cached/reconciled outcomes using the normal AIP result APIs. Tool
authorization and risk classification belong in the gateway; caller-provided
`risk` must not be trusted as a policy decision.

## Group access records

`GroupLeader`, `GroupLeaderMqClient`, and `GroupPartnerMqClient` accept optional
`access_emitter=AccessEmitter(...)` alongside the existing official
`message_emitter=MessageEmitter(...)`. No logs are emitted without opt-in.
`amp.group_access.GroupAccessMiddleware` records only the first `start` for each
`(sessionId, taskId, direction)` on the leader and addressed partner. Completing,
continuing, querying, or redelivering the same TaskId adds no access record;
the command still runs normally. A new attempt with a new TaskId adds one.
The first handler failure remains that attempt's one access outcome, including
after a redelivery succeeds. Deduplication retains the most recently used 8192
task keys in memory for the middleware lifetime; an evicted key can be counted
again. `seen_capacity` can configure this bound without adding a database.
An unmentioned recipient does not add a handled-task record.

Records contain caller/callee AIC, command method, skill/tool route (when present
in `StructuredDataItem.data`, JSON text content, or command parameters), elapsed time, status,
`trace_id=sessionId`, and `correlation_id=taskId`. Leader send status 202 means
published; it does not mean completed. A single-target callee is its AIC; a
broadcast send uses `group:<groupId>`. Count each side separately: N single-target
task attempts produce N leader access records plus N partner access records. Message
transport send/receive/ack records remain separate official MessageEmitter data.

## Local validation

```powershell
& .venv/Scripts/python.exe -m unittest discover -s vendor/ACPs-community/acps-sdk/tests/resumable -v
& .venv/Scripts/python.exe vendor/ACPs-community/acps-sdk/examples/resumable_group_demo.py --output output/sdk-resumable-demo
```

The tests cover durable restart, all five replay rules, stale fences, parameter
conflicts, concurrent connections, uncertain retry opt-in, and a real child
process calling `os._exit` after its resource change. They also exercise native
leader listeners, task dispatch, and partner access hooks. The example uses
official group clients with in-process delivery and proves one applied effect,
a cached replay, and a rejected stale replay. It does not validate live RabbitMQ,
cloud AMP ingestion, authenticated ADP, or production MCP resource recovery.
