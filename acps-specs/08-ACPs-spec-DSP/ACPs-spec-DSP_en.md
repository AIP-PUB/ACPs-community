[Home](../README_en.md)

**[English](ACPs-spec-DSP_en.md) | [中文](ACPs-spec-DSP.md)**

DSP: Data Synchronization Protocol (ACPs-spec-DSP-v02.02)

# 1. Document Definition

This document is the definition of the Data Synchronization Protocol (DSP) standard in the ACPs agent collaboration protocol suite, version v02.02.

The full title of the document is ACPs-spec-DSP-v02.02.

Document authors: Ke Yu (Beijing University of Posts and Telecommunications), Xiaofeng Hu (Beijing University of Posts and Telecommunications), Xiaolian Guo (Beijing University of Posts and Telecommunications), Haozhe Song (Beijing University of Posts and Telecommunications), Di Ma (Beijing University of Posts and Telecommunications), Ke Li (Beijing University of Posts and Telecommunications), Jun Liu (Beijing University of Posts and Telecommunications), Keliang Chen (Beijing University of Posts and Telecommunications).

# 2. Basic Protocol Concepts

This document describes the data synchronization protocol between a data provider (Provider) and a data consumer (Consumer).
## 2.1. Key Terms

- **Snapshot Synchronization (Snapshot)**: A complete data view frozen at a certain point in time, for the Consumer to pull in batches. Used to initialize or rebuild data state.
- **Full Snapshot**: The default mode of snapshot synchronization. Returns all valid objects of the specified type.
- **Incremental Snapshot**: An optional mode of snapshot synchronization. Returns only data versions after the specified sequence number; suitable for scenarios where the lag is not large and no real deletion occurs in the business.
- **Chunking**: During snapshot synchronization, data is split into multiple Chunks that are transmitted sequentially, avoiding an overly large single response. Each Chunk is identified by an index.
- **Incremental Synchronization (Changes)**: Incremental data synchronization after snapshot synchronization, allowing the Consumer to continuously catch up with data in `seq` order.
- **Retention Window**: The time/quantity range over which the Provider retains data changes when providing incremental synchronization (Changes). If the data that the Consumer currently needs to synchronize is within the window, it can complete synchronization using normal incremental synchronization (Changes). If the data that needs to be synchronized is no longer within the window, then normal incremental synchronization (Changes) can no longer be used to complete synchronization, and snapshot synchronization (Snapshot) must be used again to complete synchronization.
- **Webhook Notification**: A mechanism by which the Provider actively sends data change notifications to the Consumer. It enhances real-time performance and reduces the polling pressure of incremental synchronization (Changes). What is sent is a summary of the change event; the Consumer still needs to obtain the complete data through incremental synchronization (Changes).

## 2.2. Main Roles and Responsibilities

- **Data Provider (Provider)** 
  The source of the data. It needs to maintain the integrity, legitimacy, and consistency of the data and provide a reliable data baseline for the data consumer. Its main responsibilities are as follows:
  - Publish the snapshot synchronization (Snapshot) API and the incremental synchronization (Changes) API, and be responsible for generating a consistent data view.
  - Maintain the snapshot's chunking capability and cleanup policy to ensure the availability of large-scale synchronization.
  - Manage the retention window to ensure that changes within a certain period/quantity can be pulled.
  - When the Consumer falls behind the retention window, support re-obtaining a snapshot to help it catch up.

- **Data Consumer (Consumer)**  
  Relies on the data provided by the data provider and is responsible for consuming and storing it locally. Its main responsibilities are as follows:
  - Complete initialization or data recovery through snapshots, and guarantee idempotent writes of `(type,id,version)`.
  - Continuously catch up with new events through the incremental synchronization (Changes) API; if `410 Gone` is received, automatically switch to re-obtaining a snapshot.
  - May connect to multiple Providers at the same time, and may also expose a Webhook endpoint to receive change notifications.

---

# 3. Data Synchronization Process

(1) When the data consumer starts for the first time, loses data, or its data lags behind that of the data provider for a long time, it should first call the capability negotiation interface (Info API) once to obtain the data provider's running status and key configuration, and choose an appropriate synchronization strategy accordingly; the data consumer then actively requests a full/incremental snapshot (Snapshot) once to complete the initial alignment of data.

(2) After alignment is complete, it enters normal operation, that is, the data consumer continuously polls the data provider in incremental mode (Changes), pulling only newly added or changed data to ensure real-time consistency. If higher timeliness of data synchronization is required, the data provider can actively push webhook notifications (Webhook Notification), thereby reducing the query pressure on the discovery side.

(3) If the connection is unexpectedly interrupted and the interruption lasts a long time, the data consumer should again evaluate the current retention window and service status through the Info API, and re-pull a full or incremental snapshot when necessary, so as to realign the data and ensure that both parties are always synchronized. The overview diagram of the interaction between the data provider and the data consumer is as follows:

![8-1_en.png](8-1_en.png)

## 3.1 Snapshot Synchronization

**Functional Overview**

Responsible for providing a consistent batch data view at the initial stage of synchronization or when data is out of balance, enabling the Consumer to quickly align its state through a full snapshot or an incremental snapshot, and relying on chunked transmission and idempotent semantics to ensure that the processing is stable and reliable.
- Pull the full state in one pass: at the initial stage of synchronization or when data is severely inconsistent, provide a "data view" at a fixed point in time, so that the Consumer can quickly align its local state with the Provider.
- Stable and controllable large-batch transmission: support splitting a large snapshot into multiple small chunks to be pulled in batches, avoiding the network and processing pressure caused by an overly large single response.
- Repeatable consumption: the same snapshot data allows the client to "obtain repeatedly and process repeatedly" without corrupting the result, which facilitates retries after network disconnection and replay after failure.

**Implementation Mechanism**
- Consistency cut point: when a snapshot is created, a "snapshot cut point `S`" is determined, and afterwards, no matter how many chunks are pulled, what is seen is that same copy of data from the same moment.
- Two generation modes, full and incremental: Full snapshot: generates the complete data set of "currently valid" data within the specified range, used for initialization from zero or a complete rebuild; Incremental snapshot: collects change data after the specified starting point, used to quickly catch up when the lag is not large, significantly reducing transmission and processing costs.
- Chunked pulling: creating a snapshot returns the first chunk of data; afterwards the client pulls chunk by chunk according to the chunk index until completion, and supports adjusting the Chunk size on demand to adapt to network conditions and processing capability.
- Idempotent semantics: snapshot content is output in an idempotently processable manner, so the client can safely retry the same chunk or repeatedly consume it after a failure.
- Resource reclamation: a snapshot is a "temporary synchronization resource"; it supports active deletion by the client, and also supports automatic expiration and cleanup by the server according to "how long it has not been accessed / maximum lifetime"; accessing it after expiration will result in a clear notification that the snapshot needs to be rebuilt.

**Usage Scenarios**:
- First-time initialization (full snapshot): the Consumer starts from zero and needs to synchronize all types of data completely.
- Data rebuilding (full snapshot): the data is inconsistent over a large range, and directly re-pulling everything is more reliable.
- Quick catch-up (incremental snapshot): the Consumer lags only a little, but has exceeded the retention window of the incremental synchronization (Changes) API and does not want to spend time on a complete rebuild.
- Deletion semantics handling: if real deletion exists, the incremental snapshot is unreliable, and a full snapshot should be used; if soft deletion is used to express the intent to delete, then the incremental snapshot works normally.

---

## 3.2 Incremental (Changes) Synchronization

**Functional Overview**

By returning in order the changes that occurred after a specified position, it enables the Consumer to continuously align to the latest state at a relatively low cost and maintain real-time consistency between the Consumer and the Provider. At the same time, it also provides a unified entry point for long polling (reducing empty pulls), rate limiting (protecting the server), and the **retention window**.
- Maintain consistency with the latest state: after snapshot synchronization is complete, the Consumer starts from the last synchronized position and continuously pulls subsequent changes, ensuring that local data stays consistent as time advances.
- Sequentially ordered incremental return: change data is output strictly in order, so the Consumer only needs to maintain the latest globally increasing sequence number to advance synchronization progress in a stable and recoverable manner.
- Reducing empty pulls (long polling): when there are no new changes, it supports keeping the connection open for a certain period to wait for new data, reducing the request waste caused by frequent polling.

**Implementation Mechanism**
- A pull model based on a "starting position": each request returns changes starting after a starting sequence number; after processing the response, the client uses the "latest position" returned by the server as the starting point of the next request, forming a stable and progressive synchronization chain.
- Long polling behavior: when the client declares a waiting duration, the server waits for new changes within that window; if there is new data it returns immediately, otherwise it returns "no changes" after the timeout, and clearly tells the client that the current position has not advanced, making it easy to continue waiting next time or switch to short polling.
- Recoverable and idempotent processing: the client can safely retry the same round of requests when the network fluctuates or processing fails; as long as the client advances in order and avoids skipping updates, eventual consistency is ensured.
- Retention window constraint: the server retains only historical changes within a limited time/range; when the position requested by the client is too old, the server returns "cannot catch up" and requires the client to return to snapshot synchronization to re-establish a baseline.
- Deletion semantics handling: incremental synchronization can express changes such as object deletion, and the Consumer needs to update its local state according to the meaning of the change; if the business requires auditing, audit records and reason traces should be added to the deletion-related chain.

**Usage Scenarios**
- Normal catch-up after a snapshot is complete: as the main chain, it continuously advances the Consumer from a certain snapshot cut point to the latest state.
- Near-real-time synchronization requirements: when lower latency and fewer empty pulls are desired, long polling can be used to quickly obtain new changes under low-frequency requests.
- Catch-up after reconnection: after the Consumer restarts or is briefly stopped, it continues to catch up from the last position; if the lag is too long and exceeds the retention window, the snapshot synchronization method in Section 3.1 is used.

---

## 3.3 Webhook Notification

**Functional Overview**
When data changes, the Provider can actively notify the Consumer (rather than having the Consumer repeatedly poll for queries), thereby significantly reducing polling frequency and further improving the real-time performance of synchronization on the basis of incremental synchronization.
- Active notification, less polling: the Provider actively calls back the Consumer when there are changes, reducing invalid pulls and resource consumption.
- Improved real-time performance: through immediate or batch notifications, the Consumer can perceive changes faster and trigger pull processing.
- Controllable notification strategies: supports configuring different strategies by event type (for example, business changes use batch, and operational events use immediate), striking a balance between real-time performance and load.

**Implementation Mechanism**
- Registering the callback endpoint and scope of interest: the Consumer registers a callback address with the Provider and declares the data types and event scope of interest; the Provider pushes only matching notifications.

- Endpoint verification: after registration is complete, callback reachability and control ownership are verified first; real events are sent only after verification passes, avoiding misconfiguration or hijacking risks.

- Two notification modes, immediate and batch: immediate notifications are used for rapid delivery of critical events; batch notifications are used to merge and send high-frequency changes, reducing request storms and processing overhead.

- Failure and suspension mechanism: when consecutive delivery failures reach a threshold, the Provider can mark the Webhook as unavailable and stop retrying; delivery resumes only after the Consumer explicitly reactivates it, avoiding long-term invalid retries.

After receiving a notification, the Consumer uses the scope and position indicated by the notification as a reference, calls Changes to pull the changes and advance synchronization progress; if it finds that it has fallen behind by more than the retention window, it falls back to Snapshot to rebuild the baseline.

**Usage Scenarios**
- Real-time perception of high-frequency changes: synchronization scenarios where fewer polls and faster responses are desired (for example, online systems with continuously updated data).
- Production environments with hybrid strategies: business changes use batch to reduce cost, while events such as system maintenance/retention window cleanup use immediate to ensure observability.
- Assistance for exception recovery: through notifications such as retention window cleanup and maintenance status, the Consumer can enter the standard flow of "snapshot rebuilding/suspending synchronization/resuming synchronization" more quickly.

---

## 3.4. Capability Negotiation and Health Check (Info)

**Functional Overview**

Used to provide the Consumer with the Provider's **basic capability description** and **running status**, helping the Consumer complete capability negotiation and health check during the initialization phase. Through this interface, the Consumer can confirm in advance the supported object types, the key configurations of snapshot and incremental synchronization, and the runtime metrics of the notification capability, so as to adjust its strategy before synchronization begins and avoid problems such as protocol mismatch or missing capabilities in subsequent flows.

- Capability negotiation: provides the object types supported by the Provider and the key capability switches, based on which the Consumer can decide which data to synchronize and which synchronization methods to choose (for example, whether incremental snapshots are available and whether long polling is available).
- Health check and availability determination: clarifies whether the service is currently available (normal/maintenance), avoiding repeatedly initiating snapshot or incremental requests while it is unavailable.
- Synchronization strategy pre-determination: key information such as the snapshot expiration policy and the incremental retention window is obtained before synchronization begins, so the Consumer can more reasonably set the pull cadence, retry, and fallback strategies (for example, when it must fall back to snapshot rebuilding).
- Operations and capacity reference: optionally provides runtime metrics related to snapshots and notifications, assisting in judging the current resource pressure and system status, and facilitating troubleshooting or planning of call frequency.

**Usage Scenarios**
- Startup initialization: when the Consumer starts, it first calls the Info API to confirm service availability, the object type range, and capability switches, and then decides whether to use snapshot synchronization or directly catch up incrementally.
- Strategy adjustment: periodically calls the Info API at low frequency (or calls it on error/degradation) to update cached configuration and dynamically adjust the long polling wait duration, batch size, retry and backoff strategies, etc.
- Troubleshooting and operations coordination: when phenomena such as persistent 410 (lagging too long), frequent snapshot expiration, or a large number of Webhook failures occur, the Info API can serve as a fast "source of truth" for localization, helping determine whether it is a configuration problem, an unsupported capability, or a change in service status.

---

# 4. Key Data

## 4.1. Basic Data Model (Envelope)

All transmitted data uses a unified envelope (Envelope) structure, ensuring idempotency and adaptability.

- **Idempotency**: means that no matter how many times the same operation is executed, the result is the same as executing it once, avoiding data inconsistency caused by repeated processing. For example, when the Consumer repeatedly pulls a full snapshot, it uses seq (the globally increasing sequence number) to determine whether the data has already been synchronized, and duplicate data is automatically skipped, ensuring that local data is not redundant due to repeated operations.
- **Adaptability**: means that the basic data model can be flexibly extended as business requirements change, supporting new fields or functions without modifying the overall structure.

```typescript
export interface Envelope {
  /**
   * Globally increasing sequence number (a 64-bit long integer is recommended).
   *
   * seq is global, and can span data changes of multiple different object types (type); seq is guaranteed to be monotonically increasing within the global scope.
   *
   * Use the string type to avoid precision loss between different languages. For example: integers greater than 2^53-1 lose precision in JavaScript.
   * Languages that can support 64-bit integers use real 64-bit integers internally; languages that cannot use strings. Cross-language transmission uses strings.
   * @example "42001"
   */
  seq: string;

  /**
   * Change timestamp (optional)
   * Uses the ISO 8601 format and includes time zone information. Beijing time is recommended, for the convenience of users to view and understand.
   *
   * This timestamp must exist in the database, and is mainly used to calculate whether the data has exceeded the expiration time of the Retention Window.
   * However, it may optionally not be returned in the API.
   * @example "2025-08-17T13:12:19+08:00"
   */
  ts?: string;

  /**
   * Operation type (optional)
   * Defaults to upsert, meaning insert or update.
   * In some business scenarios the data is really deleted, and only then is delete needed,
   * most soft-delete scenarios do not need delete, so it can be omitted.
   * @example "upsert"
   */
  op?: "upsert" | "delete";

  /**
   * Object type
   * @example "dataset"
   */
  type: string;

  /**
   * Globally unique ID of the object
   * @example "urn:reg:abc:dataset:123"
   */
  id: string;

  /**
   * Object version number, a number.
   * Used to identify different versions of the same object, and together with type and id forms the triple (type, id, version) that guarantees idempotency.
   * If a string were used, then comparing the strings "2" and "10" would give the result "2" > "10", which is contrary to common sense. Therefore we mandate the use of the number type.
   * @example 42
   */
  version: number;

  /**
   * Actual data
   * Can be complete object data (FULL_JSON) or partial update data of the object (JSON_PATCH)
   */
  payload: any;
}
```

**Characteristics**
- `seq` must be monotonically increasing and must not repeat, but it need not be contiguous.
- `seq` is recommended to be a 64-bit long integer, ensuring a sufficiently large sequence number space. However, strings are used for cross-language transmission to avoid precision loss.
- The triple `(type, id, version)` is used to guarantee idempotency.

**Payload Types**
The Payload field can have different types depending on the specific implementation. The currently supported types can be queried through the `info` API endpoint.

- **FULL_JSON**: complete object data, containing all fields. Only this type can be used in the snapshot synchronization (Snapshot) API.
- **JSON_PATCH**: partial update data of the object, containing only the changed fields. It can only be used in the incremental synchronization (Changes) API.

The format of JSON_PATCH must follow [RFC 6902 — JSON Patch specification](https://datatracker.ietf.org/doc/html/rfc6902).


The payload of the snapshot synchronization (Snapshot) API can only be of the FULL_JSON type, while the payload of the incremental synchronization (Changes) API can be FULL_JSON or JSON_PATCH.

## 4.2. Other Basic Data Models

```typescript
export interface CommonResponse {
  /**
   * Response status
   * Indicates the result of request processing; possible values include "ok" and "error".
   * @example "ok"
   */
  status: "ok" | "error";

  /**
   * Result of the method call
   * Contains the result if the call succeeds, mutually exclusive with the error field.
   * @example { "type": "task", "id": "task-001" }
   */
  result?: any;

  /**
   * Error information
   * Contains an error object if the call fails, mutually exclusive with the result field.
   */
  error?: {
    /**
     * Error code
     * @example -32602
     */
    code: number;

    /**
     * Error message
     * Describes brief information about the error.
     * @example "Invalid Request"
     */
    message: string;

    /**
     * Optional error data
     * Provides more error details and context information.
     * @example { "errorType": "CONNECTION_FAILED" }
     */
    data?: any;
  };
}
```

---

# 5. Interface Design

The APIs involved in this Data Synchronization Protocol are shown in the following table:

| API Name           | Core Function                                                                      | Applicable Scenarios                                                                 |
| ---------------- | ---------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| Snapshot API     | Provides a consistent snapshot view for batch synchronization; supports full/incremental snapshots; supports Chunking chunked pulling                                         | Initialization/first access; data rebuilding (local database lost/polluted); returning to a snapshot when catch-up fails and the lag is too large; realigning after exceeding the Retention Window (410) |
| Snapshot Chunk   | Pulls the specified chunk based on `snapshotId + chunk_index`; supports retrying the same chunk (idempotent)                                   | Chunked transmission of large-volume snapshots; adjusting `chunk_size` when network/processing capability is limited; retrying chunk by chunk after a failure                         |
| Snapshot Delete  | The Consumer actively deletes snapshot resources to release Provider storage/cache (idempotent, returns 204)                                      | Actively cleaning up after snapshot consumption is complete; avoiding waiting for automatic expiration and reclamation by the server                                            |
| Changes API      | Pulls incremental changes by `start_seq`; optional long polling `wait`; advances the cursor with `X-Changes-Last-Seq`                      | Normal continuous catch-up; using long polling to reduce empty pulls when greater real-time performance is needed; merged pulling of multiple types of changes                        |
| Webhook Management API   | Registers/updates/deletes/queries Webhooks and notification strategies (batch/immediate); supports reactivation                                   | Reducing Changes polling pressure; configuring notification strategies by event importance; operations query/troubleshooting/recovery of failed webhooks               |
| Webhook Callback | The Provider actively pushes change event summaries (excluding full data); the Consumer pulls Changes/Snapshot after receiving them                       | "Pull only when there is a change"; improving real-time performance; triggering remediation or operations flows after receiving retention/maintenance events                          |
| Webhook Verify   | Returns the challenge or manually triggers verification to confirm that the callback endpoint is reachable and its ownership; business events are delivered only after verification                                        | Creating a new Webhook; re-verifying after updating the URL/key configuration; manually triggering when the verification event is not received                             |
| Info API         | Queries Provider capabilities and running status | Initialization capability negotiation; health check; dynamically adjusting the synchronization strategy based on retention/capabilities                                 |

