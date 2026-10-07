[Home](../README_en.md)

**[English](ACPs-spec-AIP_en.md) | [中文](ACPs-spec-AIP.md)**

AIP: Agent Interaction Protocol (ACPs-spec-AIP-v02.02)

# 1. Document Definition

This document is the standard definition of the Agent Interaction Protocol (AIP) within the ACPs Agent Collaboration Protocols system, version v02.02.

The full title of the document is ACPs-spec-AIP-v02.02.

Document authors: Ke Li (Beijing University of Posts and Telecommunications), Ke Yu (Beijing University of Posts and Telecommunications), Yinming Li (Beijing University of Posts and Telecommunications), Haozhe Song (Beijing University of Posts and Telecommunications), Xiaolian Guo (Beijing University of Posts and Telecommunications), Jun Liu (Beijing University of Posts and Telecommunications), Xiaofeng Hu (Beijing University of Posts and Telecommunications), Di Ma (Beijing University of Posts and Telecommunications), Keliang Chen (Beijing University of Posts and Telecommunications).

# 2. Introduction to the Agent Interaction Protocol

For agent interconnection to become a secure and reliable agent system, a set of standardized processes and protocols is required to support interaction between agents. To build a powerful, scalable, and practical multi-agent collaboration environment, the Agent Interaction Protocol (AIP) in the ACPs protocol system focuses on interaction between agents, and its key objectives are:

(1) Collaboration: provide standardized mechanisms to promote in-depth collaboration between agents, enabling agents to proactively delegate tasks (assigning subtasks to more suitable agents) and efficiently exchange context information (sharing environment perception, goal states, knowledge, etc.).

(2) Interoperability: by defining standardized information formats, semantics, and interaction modes, shield the differences of underlying agent platforms, programming languages, or internal implementations, so that heterogeneous agent systems developed by different teams, running in different environments, and possessing different capabilities can seamlessly discover each other, understand the content of interaction, and collaborate effectively.

(3) Flexibility: support diverse interaction requirements, without mandating a single interaction mode; instead, allow agents to choose the most appropriate interaction mode according to the scenario, and to adapt to different information payloads and quality-of-service requirements.

(4) Asynchrony: support long-running tasks, ensuring that requests, intermediate status updates, and final results can be delivered reliably and on demand. After an agent initiates a request (such as delegating a time-consuming task), it does not need to wait continuously for a response and can immediately handle other matters.

This document elaborates in detail on all aspects of the Agent Interaction Protocol (AIP), including role definitions, interaction modes, interaction flows, and protocol definitions.

# 3. Role Definitions and Interaction Modes in the Agent Interaction Protocol

## 3.1 Role Definitions

In the agent interaction process, there are the following two roles:

(1) Leader: in agent interaction, the Leader is the agent that publishes tasks and organizes the interaction. In one complete interaction, there can be only one Leader.

(2) Partner: in agent interaction, a Partner is an agent that accepts tasks and provides services. After accepting a task from the Leader, a Partner executes it and returns the execution result.

## 3.2 Interaction Mode

In the Agent Interaction Protocol, there are three interaction modes between the Leader and Partners:

### (1) Direct Interaction Mode

In Direct Interaction Mode, the Leader creates and maintains a Session, and the relevant agents collaborate to complete the task within the same Session. In this Session, the Leader interacts directly with each Partner, while there is no interaction between Partners. The interaction between the Leader and a Partner can adopt three implementation approaches: remote invocation, streaming transmission, and asynchronous notification (see Section 6 for details). The interaction of agents in Direct Interaction Mode is shown in the figure below:

![7-1](7-1.png)

### (2) Grouping Interaction Mode

In Grouping Interaction Mode, the Leader creates and maintains a Session, and the interaction information between agents in that Session is distributed through a message queue (Message Queue). After creating the Session, the Leader invites the relevant Partners to join the group and subscribe to the information in that Session in the message queue. The interaction information between the Leader and Partners is sent to the message queue, which then distributes it. All agents within the same group can send and receive messages through the message distribution module.

**Supplementary Notes for v02.02:**

- **Group invitation method**: The Leader preferentially sends an `InboxGroupInvitation` invitation message to the Partner through the MQ Inbox (the Agent's fixed personal inbox queue), and the Partner actively joins the group after receiving it. This method does not require the Partner to expose a public-network HTTP endpoint; it only requires establishing an outbound connection to the MQ Server. When the Partner's ACS declares a reachable HTTP/JSONRPC endpoint, the Leader may fall back to sending the invitation via Direct RPC (backward compatible).
- **Transport security**: Message queue communication uses AMQPS (TLS 1.3), and mutual authentication between the agent and the MQ Server uses mTLS (based on the Certificate of Agent Identity issued by the ACPs CA), replacing plaintext accessToken credentials.
- **Group resource naming**: The naming convention for the group-dedicated exchange is `group_{leader-aic}_{group-id}`, and the naming convention for each participating agent's group queue is `group_{leader-aic}_{group-id}_{aic}`.

The interaction of agents in Grouping Interaction Mode is shown in the figure below:

![7-2](7-2.png)

### (3) Hybrid Interaction Mode

In Hybrid Interaction Mode, the Leader creates and maintains a Session. Within that Session, the Leader can interact with Partners both in Direct Interaction Mode and in Grouping Interaction Mode. The interaction of agents in Hybrid Interaction Mode is shown in the figure below:

![7-3](7-3.png)

## 3.3 Interaction Network

In agent interaction scenarios, agents establish connections with other agents in both the direct and grouping modes, and some agents may simultaneously belong to different groups and reuse the message queue service, thereby forming a dynamic agent interaction network. The agent interaction structure at a certain moment is shown in the figure below:

![7-4](7-4.jpg)

# 4. Core Concepts in the Agent Interaction Protocol

Interaction between agents is based on the following core concepts:

## 4.1 Overview of Basic Data Objects

- Message: the base class of all interaction data, defining common message attributes such as identifier, sender, timestamp, etc. Message is neutral, bidirectional, and stateless.
- TaskCommand: used to send task-related commands, such as start, continue, cancel, etc.
- TaskResult: represents the status and result of a task, including information such as status and products.
- GroupMgmtCommand: used to send group management-related commands.
- GroupMgmtResult: represents the status information of group members.
- TaskStatusUpdateEvent: used for task status update notifications in SSE streaming transmission.
- ProductChunkEvent: used for chunked transmission of products in SSE streaming transmission.
- Product: represents the tangible output generated by an agent during task execution.
- DataItem: represents an independent content fragment in a message or product.

## 4.2 Session

A Session represents one complete dialogue or interaction cycle in the agent interaction process. A Session is usually initiated by a user and maintained by the Leader, and may contain multiple TaskCommands and TaskResults. The Session object exists only in the Leader and does not participate in communication between agents; only `sessionId` serves as its identifier and appears in all messages related to that Session.

**Reference structure of the Session object:**

```typescript
export interface Session {
  /**
   * The unique identifier of the session, generated by the Leader for a new session
   * The UUID format can be used to ensure global uniqueness
   * @example "123e4567-e89b-12d3-a456-426614174000"
   */
  id: string;

  /**
   * The list of task results related to this session
   * Contains the latest status of all tasks created in this session, sorted by creation time
   * @example [{ type: "task-result", id: "msg-001", taskId: "task-001", ... }, { type: "task-result", id: "msg-002", taskId: "task-002", ... }]
   */
  taskResults: TaskResult[];

  /**
   * The list of task commands related to this session
   * Contains all task commands in this session, sorted by sending time
   * @example [{ type: "task-command", id: "msg-001", ... }, { type: "task-command", id: "msg-002", ... }]
   */
  taskCommands: TaskCommand[];

  /**
   * The creation time of the session
   * Uses the ISO 8601 date-time string format, with the Beijing time zone by default
   * @example "2025-09-01T10:30:00+08:00"
   */
  createdAt: string;

  /**
   * The last update time of the session
   * Updated each time there is a new task, message, or status change in the session
   * Uses the ISO 8601 date-time string format, with the Beijing time zone by default
   * @example "2025-09-01T15:45:30+08:00"
   */
  updatedAt: string;
}
```

## 4.3 Message

Message is the base class of all interaction data and defines common message attributes. Message is neutral, bidirectional, and stateless, and all Command, Result, and Event types of interaction data inherit from Message.

```typescript
export interface Message {
  /**
   * Object type identifier
   * Used for runtime type identification; subtypes override this field
   * @example "task-command" | "task-result" | "group-mgmt-command" | "group-mgmt-result" | "task-status-update" | "product-chunk"
   */
  readonly type: string;

  /**
   * The unique identifier of the message
   * Generated by the sender; the UUID format can be used to ensure global uniqueness
   * @example "msg-123e4567-e89b-12d3-a456-426614174000"
   */
  id: string;

  /**
   * The sending time of the message
   * Uses the ISO 8601 date-time string format, with the Beijing time zone by default
   * @example "2025-09-01T14:30:45+08:00"
   */
  sentAt: string;

  /**
   * The role identifier of the message sender
   * Identifies the type of the message sender: "leader" for the client and "partner" for the service
   * @example "leader" | "partner"
   */
  senderRole: "leader" | "partner";

  /**
   * The unique identifier of the agent sending the message
   * Corresponds to the agent's AIC (Agent Identity Code)
   * @example "agent-chatbot-001"
   */
  senderId: string;

  /**
   * The list of AICs of agents mentioned in the message in Grouping Interaction Mode; mentioned agents should respond, and agents not mentioned decide for themselves whether to respond according to the context.
   * Only meaningful in Grouping Interaction Mode; Direct Interaction Mode does not need to provide this field.
   * If this field is not provided, its value is null, or its value is an empty array, it means no agent is mentioned.
   * If the value is "all", it means all partners are mentioned.
   * @example ["agent-analyst-001", "agent-writer-002"]
   */
  mentions?: "all" | string[];

  /**
   * The array of content parts that constitute the body of the message
   * A message may consist of multiple parts of different types, such as text, files, images, etc.
   * @example [{ type: "text", text: "Please analyze this file" }, { type: "file", name: "data.csv", uri: "..." }]
   */
  dataItems?: DataItem[];

  /**
   * The group ID to which this message belongs
   * This field exists only in Grouping Interaction Mode, and is used to identify the group to which the message belongs
   * @example "group-collaborative-analysis-001"
   */
  groupId?: string;

  /**
   * The session ID of this message
   * Associates the message with a specific session, facilitating session management and history tracking
   * @example "session-123e4567-e89b-12d3-a456-426614174000"
   */
  sessionId?: string;
}
```

**Applicable Boundaries of Message**

All payloads in AIP that are emitted by an Agent, independently deliverable, and represent one AIP semantic interaction MUST inherit from `Message` or contain a required identity field equivalent to `Message.senderId`. Transport envelopes, protocol errors, configuration objects, and embedded value objects are not Messages, and must not use their own fields in place of `senderId` for identity authentication.

Interaction payloads that are Messages include but are not limited to:
`TaskCommand`, `TaskResult`, `TaskStatusUpdateEvent`, `ProductChunkEvent`, `GroupMgmtCommand`, `GroupMgmtResult`, `InboxGroupInvitation`, `InboxGroupInvitationError`.

Objects that are not Messages include:
`JSONRPCRequest`, `JSONRPCResponse`, `JSONRPCError`, `Product`, `DataItem`, `TaskStatus`, `NotificationConfig`, `AMQPConfig`, `GroupInfo`, etc.

## 4.4 TaskCommand

TaskCommand is used by the Leader to send task-related commands to Partners, such as start, continue, cancel, complete, etc.

```typescript
export interface TaskCommand extends Message {
  /**
   * Object type identifier
   * For a TaskCommand object, this value is always 'task-command', used for runtime type identification
   * @example "task-command"
   */
  readonly type: "task-command";

  /**
   * The task command type
   * Specifies the operation instruction carried by the message, such as start, continue, cancel, etc.
   */
  command: TaskCommandType;

  /**
   * The parameters of the command
   * Provides additional configuration parameters for the command; the specific content depends on the command type
   * @example { "timeout": 3600, "priority": "high" }
   */
  commandParams?: { [key: string]: any };

  /**
   * The ID of the task to which this command belongs
   * Associates the command with a specific task, facilitating task status tracking
   * @example "task-987fcdeb-51d3-46e7-8a45-123456789abc"
   */
  taskId?: string;
}

export enum TaskCommandType {
  /**
   * Get task data
   * Requests information such as the current status and history messages of the task
   */
  Get = "get",

  /**
   * Start the task
   * Instructs the agent to start executing a new task
   */
  Start = "start",

  /**
   * Continue the task
   * Continues execution for a paused task (such as in the AwaitingInput state),
   * or, in the AwaitingCompletion state, continues execution instead of ending
   */
  Continue = "continue",

  /**
   * Cancel the task
   * Stops the execution of the current task; the task enters the Canceled state
   */
  Cancel = "cancel",

  /**
   * Complete the task
   * Marks the task as completed; the task enters the Completed state
   */
  Complete = "complete",

  /**
   * Re-stream the task
   * Re-establishes the streaming transmission connection of the task, used for recovery after a network interruption
   */
  ReStream = "re-stream",
}

/**
 * The parameter interface of the Get command
 * When sending the `get` command, the following parameters may be included to control the range of returned data
 */
export interface GetCommandParams {
  /**
   * The timestamp of the last command sent
   * commandHistory contains only TaskCommands newer than lastCommandSentAt
   * A value of null means all historical commands are returned
   * Uses the ISO 8601 date-time string format, with the Beijing time zone by default
   * @example "2025-09-01T10:00:00+08:00" | null
   */
  lastCommandSentAt?: string | null;

  /**
   * The timestamp of the last status change
   * statusHistory contains only TaskStatus entries newer than lastStateChangedAt
   * A value of null means all historical statuses
   * Uses the ISO 8601 date-time string format, with the Beijing time zone by default
   * @example "2025-09-01T10:00:00+08:00" | null
   */
  lastStateChangedAt?: string | null;
}

/**
 * The parameter interface of the Start command
 */
interface StartCommandParams {
  /**
   * Defines the timeout of the Start command. Unit: milliseconds
   */
  timeout?: number;

  /**
   * Defines the maximum number of bytes of the products of this task.
   * The Partner should follow this limit when generating products; if it cannot, it may switch to the Failed state and provide detailed information in the data items.
   */
  maxProductsBytes?: number;
}
```

## 4.5 TaskResult

TaskResult represents the status and result of a "stateful" unit of work in the agent interaction process. Sending a TaskResult object during communication indicates that the state of the task has changed and others need to be notified. The Leader is responsible for the creation and termination of tasks, while the Partner (one or more) is responsible for the concrete execution of tasks and feedback of results.

```typescript
export interface TaskResult extends Message {
  /**
   * Object type identifier
   * For a TaskResult object, this value is always 'task-result', used for runtime type identification
   * @example "task-result"
   */
  readonly type: "task-result";

  /**
   * The unique identifier of the task
   * Generated by the Leader for a new task; the UUID format can be used to ensure global uniqueness
   * @example "task-123e4567-e89b-12d3-a456-426614174000"
   */
  taskId: string;

  /**
   * The current status of the task
   * Contains the state enumeration value, the change time, and related data items
   */
  status: TaskStatus;

  /**
   * The set of products generated by the agent during task execution
   * An optional field, containing all tangible outputs generated by the task
   * @example [{ type: "text", text: "Analysis report..." }, { type: "file", uri: "report.pdf" }]
   */
  products?: Product[];

  /**
   * The history of commands exchanged during the task
   * Records all communication commands related to this task, arranged in chronological order
   * @example [{ type: "task-command", id: "msg-001", command: "start" }, { type: "task-command", id: "msg-002", command: "continue" }]
   */
  commandHistory?: TaskCommand[];

  /**
   * The history of task status changes
   * Records all status transitions of the task from creation to completion, used for auditing and debugging
   * @example [{ state: "accepted", stateChangedAt: "2025-09-01T10:00:00+08:00" }, { state: "working", stateChangedAt: "2025-09-01T10:05:00+08:00" }]
   */
  statusHistory?: TaskStatus[];
}

export interface TaskStatus {
  /**
   * The current status of the task
   * Uses the TaskState enumeration value to indicate the current phase of the task in its lifecycle
   */
  state: TaskState;

  /**
   * The timestamp of the status change
   * Uses the ISO 8601 date-time string format, with the Beijing time zone by default
   * @example "2025-09-01T14:30:00+08:00"
   */
  stateChangedAt: string;

  /**
   * The array of data items corresponding to the status change
   * Contains content related to the status change, which may be of various types such as text and files
   * @example [{ type: "text", text: "Task execution has started" }, { type: "file", name: "config.json", uri: "..." }]
   */
  dataItems?: DataItem[];
}

export enum TaskState {
  /**
   * The task has been accepted
   * The Partner receives the start-task message from the Leader and, based on its own capabilities,
   * decides to accept this task, and then returns the new accepted status to the Leader
   */
  Accepted = "accepted",

  /**
   * The task is being executed
   * After accepting, the Partner does not necessarily start processing the task immediately;
   * when the Partner actually starts working, it changes the status to Working
   */
  Working = "working",

  /**
   * Awaiting input state
   * The Partner has paused and is waiting for the Leader to input new data or further instructions
   */
  AwaitingInput = "awaiting-input",

  /**
   * Awaiting completion confirmation state
   * The Partner considers the Task to be complete and attaches the completed Products along with this status change,
   * expecting the Leader to complete the current task. If the Leader considers the Partner's products unsatisfactory,
   * it may choose not to end this task and continue to provide new data for the Partner to improve its answer
   */
  AwaitingCompletion = "awaiting-completion",

  /**
   * The task has completed successfully
   * All work has been completed, the products have been delivered, and the task enters a terminal state
   */
  Completed = "completed",

  /**
   * The task has been canceled
   * The task is actively canceled by the user or the Leader, or the necessary supplementary information cannot be awaited, so the task is no longer executed; this is caused by external factors.
   */
  Canceled = "canceled",

  /**
   * Task execution failed
   * The task failed during execution due to errors, exceptions, or other problems; this is caused by internal execution issues.
   */
  Failed = "failed",

  /**
   * The task was rejected
   * The task was rejected by the agent and execution did not begin, usually due to capability mismatch or insufficient resources
   */
  Rejected = "rejected",
}
```

**Task States**

(1) Task accepted (Accepted)

(2) Task rejected (Rejected)

(3) Task continues execution (Working)

(4) Task awaiting input (AwaitingInput)

(5) Task awaiting completion (AwaitingCompletion)

(6) Task completed (Completed)

(7) Task failed (Failed)

(8) Task canceled (Canceled)

**State Transition Rules**

Task state transitions follow these rules:

- After starting, the task state may be Accepted or Rejected (note that it does not first enter Accepted and then Rejected; rather, after evaluation it directly enters the corresponding state).
- From Accepted, the task may transition to Working or Canceled.
- From Working, the task may transition to AwaitingInput, AwaitingCompletion, Canceled, or Failed.
- From AwaitingInput, the task may transition to Working or Canceled.
- From AwaitingCompletion, the task may transition to Completed, Working, or Canceled.
- Completed, Canceled, Failed, and Rejected are terminal states and cannot transition to any other state.

The following commands may be used to request changes of task state:

- Start: starts the task; the task state goes from nonexistent to Accepted or Rejected. In other states, a Start command received for the same TaskID will be ignored.
- Continue: continues the task; used in the AwaitingInput and AwaitingCompletion states. If a Continue command is received in other states, it will be ignored.
- Cancel: cancels the task. Used in the Accepted, Working, AwaitingInput, and AwaitingCompletion states. If a Cancel command is received in other states, it will be ignored.
- Complete: completes the task. Used in the AwaitingCompletion state. If a Complete command is received in other states, it will be ignored.

**Task State Transition Diagram**
![TaskStatus](7-8.png)
**State Transition Table**
| No. | Current State | Command | Next State | Description |
| ---- | ------------------ | -------- | ------------------ | --------------------------------------------- |
| 1/2 | None | Start | Accepted/Rejected | Start the task; the task state goes from nonexistent to Accepted or Rejected |
| 3 | Accepted | - | Working | The task gets a chance to execute and enters the working state |
| 4 | Accepted | Cancel | Canceled | The task is canceled |
| 5 | Working | - | AwaitingCompletion | The task submits products and enters the awaiting-completion state |
| 6 | Working | - | AwaitingInput | The task lacks necessary information and enters the awaiting-input state |
| 7 | Working | - | Failed | Due to internal factors, task execution fails and the task enters the failed state |
| 8 | Working | Cancel | Canceled | The task is canceled |
| 9 | AwaitingInput | Continue | Working | New input data is obtained and the task continues |
| 10 | AwaitingInput | Cancel | Canceled | The task is canceled |
| 11 | AwaitingInput | - | Canceled | The wait times out and the task is canceled |
| 12 | AwaitingCompletion | Complete | Completed | The client is satisfied with the products and completes the task |
| 13 | AwaitingCompletion | Continue | Working | The client is dissatisfied with the products and provides new data, so the task continues |
| 14 | AwaitingCompletion | Cancel | Canceled | The task is canceled |
| 15 | AwaitingCompletion | - | Completed | The wait times out and the task ends successfully |
| 16 | Completed | - | - | The task has completed successfully; terminal state |
| 17 | Canceled | - | - | Due to external factors, the task has been canceled; terminal state |
| 18 | Failed | - | - | Due to internal factors, task execution failed; terminal state |
| 19 | Rejected | - | - | The task was rejected and execution did not begin; terminal state |

## 4.6 Product

The result or product of agent work, usually generated when the task status is `AwaitingCompletion`. A product usually contains outputs in various forms such as documents, images, and structured data, and supports combinations of multiple data types through the `dataItems` array.

The Product structure is defined as follows:

```typescript
export interface Product {
  /**
   * The unique identifier of the product within the task scope
   * The UUID format can be used to ensure uniqueness within the task scope
   * @example "product-123e4567-e89b-12d3-a456-426614174000"
   */
  id: string;

  /**
   * The name of the product
   * Usually a file name or a short descriptive name of the product
   * @example "analysis-report.pdf" | "data-processing-result.json" | "visualization-chart.png"
   */
  name?: string;

  /**
   * A detailed description of the product
   * Explains the content, purpose, or generation method of the product
   * @example "A behavior analysis report generated based on user data, including charts and recommendations"
   */
  description?: string;

  /**
   * The array of content parts that constitute the product
   * May contain multiple data items of different types, such as text, files, structured data, etc.
   * @example [{ type: "text", text: "Report summary..." }, { type: "file", name: "detailed-data.csv", uri: "..." }]
   */
  dataItems: DataItem[];
}
```

## 4.7 DataItem

The smallest content unit of agent interaction, supporting multiple data types including text, files, structured data, etc.; each type contains the corresponding content and metadata.

The DataItem structure is defined as follows:

```typescript
/**
 * The base attribute interface shared by all data items
 * Provides general metadata expression capability for data items of different types
 */
export interface DataItemBase {
  /**
   * The metadata of the data item
   * May be used to store additional information related to the data item, such as data structure descriptions and encoding information
   * @example { "encoding": "utf-8", "language": "zh-CN", "schema": "custom-format-v1" }
   */
  metadata?: { [key: string]: any };
}

/**
 * The data item union type
 * Indicates that it can be any one of the text, file, or structured data types
 */
export type DataItem = TextDataItem | FileDataItem | StructuredDataItem;

/**
 * The text data item interface
 * Used to represent plain text content
 */
export interface TextDataItem extends DataItemBase {
  /**
   * The data item type identifier
   * Used as a discriminator, always 'text'
   * @example "text"
   */
  readonly type: "text";

  /**
   * The text content
   * Contains the actual text string data
   * @example "This is a sample text content"
   */
  text: string;
}

/**
 * The file data item interface
 * Used to represent file content, supporting URL references or base64-encoded inline approaches
 */
export interface FileDataItem extends DataItemBase {
  /**
   * The data item type identifier
   * Used as a discriminator, always 'file'
   * @example "file"
   */
  readonly type: "file";

  /**
   * The file name
   * The complete file name including the file extension
   * @example "document.pdf" | "image.png" | "data.csv"
   */
  name?: string;

  /**
   * The MIME type of the file
   * Identifies the media type of the file, used to correctly process the file content
   * @example "application/pdf" | "image/png" | "text/csv"
   */
  mimeType?: string;

  /**
   * The URL address of the file
   * A URL pointing to the file resource, mutually exclusive with the bytes field
   * @example "https://example.com/files/document.pdf"
   */
  uri?: string;

  /**
   * The base64-encoded content of the file
   * A base64-encoded string of the file content, mutually exclusive with the uri field
   * Suitable for small files or scenarios requiring inline transmission
   * @example "JVBERi0xLjQKMSAwIG9iago8PAovVHlwZSAvQ2F0YWxvZw..."
   */
  bytes?: string;
}

/**
 * The structured data item interface
 * Used to represent structured data in JSON format
 */
export interface StructuredDataItem extends DataItemBase {
  /**
   * The data item type identifier
   * Used as a discriminator, always 'data'
   * @example "data"
   */
  readonly type: "data";

  /**
   * The structured data content
   * Contains arbitrary JSON structured data
   * @example { "user": { "id": 123, "name": "Zhang San" }, "score": 95.5, "tags": ["excellent", "active"] }
   */
  data: { [key: string]: any };
}
```

# 5. Agent Interaction Message Format

The transport protocol carrying agent interaction is HTTP(S). On this basis, the AIP protocol specifies that: messages in Direct Interaction Mode are formatted according to the [JSON-RPC 2.0](https://www.jsonrpc.org/specification) standard, with request/response data encapsulated in JSON; messages in Grouping Interaction Mode are transmitted directly in JSON format within the message queue.

## 5.1 JSON-RPC Data Format

All RPC client interaction requests defined by AIP are encapsulated in a JSONRPCRequest object.

The JSONRPCRequest structure is defined as follows:

```typescript
export interface JSONRPCRequest {
  /**
   * JSON-RPC protocol version
   * Fixed to "2.0", compliant with the JSON-RPC 2.0 specification
   */
  jsonrpc: "2.0";

  /**
   * Method name
   * The remote procedure to be invoked, such as "rpc", "stream", "group", etc.
   * @example "rpc" | "stream" | "group" | "notification/set"
   */
  method: string;

  /**
   * The unique identifier of the request
   * May be a string, a number, or null
   * If a response is expected, id must be provided, and the id in the response must be the same as the id in the request
   * If no response is expected, id may be set to null
   * @example "request-123" | 42 | null
   */
  id?: string | number | null;

  /**
   * Method parameters
   * May be an array or an object; the specific format depends on the method invoked
   * @example { "command": {...} } | ["param1", "param2"]
   */
  params?: any[] | { [key: string]: any };
}
```

All RPC server responses defined by AIP are encapsulated in a JSONRPCResponse object.

The JSONRPCResponse structure is defined as follows:

```typescript
export interface JSONRPCResponse {
  /**
   * JSON-RPC protocol version
   * Fixed to "2.0", compliant with the JSON-RPC 2.0 specification
   */
  jsonrpc: "2.0";

  /**
   * The unique identifier of the request
   * Corresponds to the ID in the request, used to match requests and responses
   * If the request contains an id, the response must contain the same id
   * If the id in the request cannot be identified, the id in the response must be null
   * @example "request-123" | 42 | null
   */
  id: string | number | null;

  /**
   * The result of the method invocation
   * If the invocation succeeds, it contains the result; mutually exclusive with the error field
   * @example { "type": "task", "id": "task-001", ... }
   */
  result?: any;

  /**
   * Error information
   * If the invocation fails, it contains an error object; mutually exclusive with the result field
   */
  error?: {
    /**
     * Error code
     * Indicates the error type, following the JSON-RPC 2.0 standard
     * @example -32600 | -32601 | -32602 | -32603
     */
    code: number;

    /**
     * Error message
     * A brief description of the error
     * @example "Invalid Request" | "Method not found"
     */
    message: string;

    /**
     * Optional error data
     * Provides more error details and context information
     * @example { "errorType": "CONNECTION_FAILED", "details": {...} }
     */
    data?: any;
  };
}
```

## 5.2 JSON-RPC Error Handling

The AIP protocol follows the error handling mechanism of the JSON-RPC 2.0 specification, and also defines error codes specific to agent interaction.

### (1) Standard JSON-RPC Errors

The following are the standard error codes defined by the JSON-RPC 2.0 specification:

| Error Code | Error Name | Error Message | Description |
| ---------------- | ---------------- | ------------------------- | ----------------------------------------------------------- |
| -32700 | Parse error | Invalid JSON payload | The JSON received by the server is malformed |
| -32600 | Invalid Request | Invalid JSON-RPC Request | The JSON is valid but is not a valid JSON-RPC request object |
| -32601 | Method not found | Method not found | The requested RPC method (such as "rpc" or "notification") does not exist or is not supported |
| -32602 | Invalid params | Invalid method parameters | The parameters of the method are invalid (such as a type error or a missing required field) |
| -32603 | Internal error | Internal server error | An unexpected error occurred while the server was processing |
| -32000 to -32099 | Server error | (server-defined) | Reserved for implementation-defined server errors. AIP-specific errors use this range |

### (2) AIP-Specific Errors

The following are the custom error codes defined by the AIP protocol within the JSON-RPC server error range (`-32000` to `-32099`):

| Error Code | Error Name | Error Message | Description |
| -------- | ----------------------------- | ------------------------------------ | ---------------------------------------------------- |
| -32001 | TaskNotFoundError | Task not found | The specified task ID does not exist, has expired, or has completed and been cleared |
| -32002 | TaskNotCancelableError | Task cannot be canceled | An attempt was made to cancel a task in a non-cancelable state (such as completed, failed, or canceled) |
| -32003 | NotificationNotSupportedError | Notification is not supported | The client attempted to use the notification feature, but the server does not support it |
| -32004 | UnsupportedOperationError | This operation is not supported | The requested operation, or a specific aspect of it, is not supported by this server agent implementation |
| -32005 | ContentTypeNotSupportedError | Incompatible content types | The media type provided in the request is not supported by the agent or by the specific skill |
| -32006 | InvalidAgentResponseError | Invalid agent response type | The agent generated an invalid response for the requested method |
| -32007 | GroupNotSupportedError | Group communication is not supported | The client attempted to use the group interaction feature, but the server does not support it |
| -32008 | AuthenticationRequiredError | Authentication required | The operation requires authentication, but no valid credentials were provided |
| -32009 | AuthorizationFailedError | Authorization failed | The authenticated user is not authorized to perform the requested operation |
| -32010 | AccessTokenInvalidError | Invalid access token | The provided accessToken is invalid, has expired, or is malformed |

**Error Response Format**
An error response must follow the JSON-RPC 2.0 specification, using the `error` field instead of the `result` field:

Example of an error response:

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "error": {
    "code": -32001,
    "message": "Task not found",
    "data": {
      "taskId": "task-not-exist-123",
      "details": "The task may have completed and been cleared, or the task ID is invalid"
    }
  }
}
```

# 6. Implementation Approaches of Direct Interaction Mode

In Direct Interaction Mode, the AIP protocol supports three implementation approaches — RPC style, streaming style, and notification style — for interaction and task management between agents:

- RPC style: the basic RPC methods, used to send instant messages, manage task state, or execute operations that require an immediate response. The request parameters contain information such as the message content and the task state.
- Streaming style: RPC methods used for streaming data, suitable for scenarios such as chunked transmission of large files, real-time log pushing, or long-running data transmission.
- Notification style: event-driven asynchronous callback methods, used by an agent to send notifications to other agents when specific conditions are triggered (such as a task state change, resources becoming ready, or an exception occurring).

## 6.0 Identity Binding Rules for Direct Interaction Mode

When `identity_binding_enabled=true`, the receiver in Direct Interaction Mode **MUST** extract the peer AIC from the mTLS peer certificate and check it for consistency against the `senderId` of the inbound business message. The AIC extraction rules are as follows:

1. The certificate Subject `CN` serves as the primary source of the AIC;
2. If `URI:acps://{AIC}` is present in `SubjectAlternativeName`, it serves as supplementary identity information;
3. If both the CN AIC and the SAN AIC are present but inconsistent, the certificate identity MUST be judged invalid;
4. If the CN is missing or is not a valid AIC, the certificate identity MUST be judged invalid.

The receiver **MUST** validate at least the following inbound messages:

- `RpcRequest.params.command.senderId == peer AIC`
- `StreamRequest.params.message.senderId == peer AIC`
- `NotificationStartRequest.params.message.senderId == peer AIC`
- Notification callback request body `TaskResult.senderId == peer AIC`

When receiving a direct interaction response or a stream event, the caller **MUST** likewise extract the callee AIC from the TLS server certificate and validate both of the following at the same time:

- TLS server AIC == the expected callee AIC obtained through discovery/configuration
- Response payload `senderId` == TLS server AIC

If `identity_binding_enabled=true` and the client cannot obtain the TLS server AIC, or the expected callee AIC is missing, it MUST reject the response; it must not rely solely on the `senderId` in the business message to claim by itself that identity binding has been completed.

When validation fails, a missing certificate identity or an invalid certificate identity should return `AuthenticationRequiredError` (`-32008`); a certificate identity that exists but is inconsistent with the business identity should return `AuthorizationFailedError` (`-32009`).

## 6.1 RPC Style

The following is the agent interaction interface specification based on RPC style, which clarifies the interaction approach and data format, and is used to implement structured remote interaction between the client and the server.

- **URL**: `rpc`
- **HTTP Method**: `POST`
- **Request**: `RpcRequest`
- **Response**: `RpcResponse`

### 6.1.1 RpcRequest Definition

`RpcRequest` is the standardized request structure for remote invocation between agents, inheriting from the `JSONRPCRequest` interface of JSON-RPC 2.0:

```typescript
export interface RpcRequest extends JSONRPCRequest {
  /**
   * RPC method name
   * For a basic RPC request, this value is always "rpc"
   * @example "rpc"
   */
  method: "rpc";

  /**
   * RPC request parameters
   */
  params: {
    /**
     * The TaskCommand object to be sent
     * Contains information such as the task instruction and data content
     */
    command: TaskCommand;
  };
}
```

### 6.1.2 RpcResponse Definition

`RpcResponse` is the standardized response structure for agent RPC invocation, inheriting from the `JSONRPCResponse` interface of JSON-RPC 2.0:

```typescript
export interface RpcResponse extends JSONRPCResponse {
  /**
   * RPC response result
   * Usually a task result object, containing the task status and products
   */
  result: TaskResult;
}
```

### 6.1.3 Example

The interaction sequence diagram is shown below:

![7-5](7-5.png)

#### (1) Create Task

**Request**

```json
{
  "jsonrpc": "2.0",
  "method": "rpc",
  "id": "1",
  "params": {
    "command": {
      "type": "task-command",
      "id": "msg-5678",
      "sentAt": "2025-09-01T11:58:00+08:00",
      "senderRole": "leader",
      "senderId": "agent-leader-aic",
      "command": "start",
      "dataItems": [
        {
          "type": "text",
          "text": "Please help me put together a 3-day itinerary for a cultural-themed tour of Beijing."
        }
      ],
      "taskId": "task-1234",
      "sessionId": "session-91011"
    }
  }
}
```

**Response**

- If the Partner rejects this Task, it returns a TaskResult object with the state `Rejected`.
- If the Partner accepts this Task but does not intend to execute it immediately, the Partner returns a TaskResult object with the state `Accepted`.
- If the Partner accepts this Task and starts working immediately, it may return a TaskResult object with the state `Working`.
- If the Partner accepts this Task and starts working immediately, but an error occurs during execution, it returns a TaskResult object with the state `Failed`.
- If the Partner accepts this Task, starts working immediately, and obtains a result immediately, it may return a TaskResult object with the state `AwaitingCompletion`, with the Products attached.

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "result": {
    "type": "task-result",
    "id": "msg-5679",
    "sentAt": "2025-09-01T12:00:00+08:00",
    "senderRole": "partner",
    "senderId": "agent-partner-aic",
    "taskId": "task-1234",
    "status": {
      "state": "awaiting-completion",
      "stateChangedAt": "2025-09-01T12:00:00+08:00"
    },
    "products": [
      {
        "id": "product-1",
        "name": "Beijing Cultural Tour Itinerary.pdf",
        "description": "Detailed itinerary for a 3-day cultural-themed tour of Beijing",
        "dataItems": [
          {
            "type": "file",
            "name": "beijing_cultural_tour.pdf",
            "mimeType": "application/pdf",
            "uri": "https://example.com/files/beijing_cultural_tour.pdf"
          }
        ]
      }
    ],
    "sessionId": "session-91011"
  }
}
```

#### (2) Continue Task

- If the Partner needs the Leader to provide more information during its work, the Partner changes the Task state to `AwaitingInput` and notifies the Leader through the returned TaskResult object. After receiving a TaskResult in this state, the Leader can provide more information by sending a new TaskCommand, in which the `command` is "continue". After receiving it, the Partner changes the Task state to `Working` and returns it to the Leader.
- If the Partner considers the Task to be completed and changes the state to `AwaitingCompletion`, the Partner notifies the Leader through the returned TaskResult object and attaches the Products. After receiving a TaskResult in this state, if the Leader considers the Products to be the expected products, it decides to end this Task by sending a new TaskCommand, in which the `command` is "complete". If the Leader considers the Partner's products to be unsatisfactory, it may choose not to end this task and may continue to provide new data so that the Partner can give a better answer; in this case, the `command` in the TaskCommand is "continue". After receiving it, the Partner changes the Task state to `Working` and returns it to the Leader.

**Request**

```json
{
  "jsonrpc": "2.0",
  "method": "rpc",
  "id": "2",
  "params": {
    "command": {
      "type": "task-command",
      "id": "msg-6789",
      "sentAt": "2025-09-01T12:02:00+08:00",
      "senderRole": "leader",
      "senderId": "agent-leader-aic",
      "command": "continue",
      "dataItems": [
        {
          "type": "text",
          "text": "Please continue to improve the itinerary by adding some cultural activities that can be experienced in person, rather than only sightseeing attractions."
        }
      ],
      "taskId": "task-1234",
      "sessionId": "session-91011"
    }
  }
}
```

**Response**

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "result": {
    "type": "task-result",
    "id": "msg-6790",
    "sentAt": "2025-09-01T12:05:00+08:00",
    "senderRole": "partner",
    "senderId": "agent-partner-aic",
    "taskId": "task-1234",
    "status": {
      "state": "working",
      "stateChangedAt": "2025-09-01T12:05:00+08:00"
    },
    "products": [],
    "sessionId": "session-91011"
  }
}
```

#### (3) Get Task

The Leader may obtain the latest state of a task at any time.

**Request**

```json
{
  "jsonrpc": "2.0",
  "method": "rpc",
  "id": "3",
  "params": {
    "command": {
      "type": "task-command",
      "id": "msg-9012",
      "sentAt": "2025-09-01T12:06:00+08:00",
      "senderRole": "leader",
      "senderId": "agent-leader-aic",
      "command": "get",
      "commandParams": {
        "lastCommandSentAt": null,
        "lastStateChangedAt": null
      },
      "taskId": "task-1234",
      "sessionId": "session-91011"
    }
  }
}
```

**Response**

Here we show the Failed scenario.

```json
{
  "jsonrpc": "2.0",
  "id": "3",
  "result": {
    "type": "task-result",
    "id": "msg-9013",
    "sentAt": "2025-09-01T12:07:00+08:00",
    "senderRole": "partner",
    "senderId": "agent-partner-aic",
    "taskId": "task-1234",
    "status": {
      "state": "failed",
      "stateChangedAt": "2025-09-01T12:07:00+08:00",
      "dataItems": [
        {
          "type": "text",
          "text": "An error occurred while processing the task: unable to connect to the external data source."
        }
      ]
    },
    "products": [],
    "commandHistory": [
      {
        "type": "task-command",
        "id": "msg-5678",
        "sentAt": "2025-09-01T11:58:00+08:00",
        "senderRole": "leader",
        "senderId": "agent-leader-aic",
        "command": "start",
        "dataItems": [
          {
            "type": "text",
            "text": "Please help me put together a 3-day itinerary for a cultural-themed tour of Beijing."
          }
        ],
        "taskId": "task-1234",
        "sessionId": "session-91011"
      },
      {
        "type": "task-command",
        "id": "msg-6789",
        "sentAt": "2025-09-01T12:02:00+08:00",
        "senderRole": "leader",
        "senderId": "agent-leader-aic",
        "command": "continue",
        "dataItems": [
          {
            "type": "text",
            "text": "Please continue to improve the itinerary by adding some cultural activities that can be experienced in person, rather than only sightseeing attractions."
          }
        ],
        "taskId": "task-1234",
        "sessionId": "session-91011"
      },
      {
        "type": "task-command",
        "id": "msg-9012",
        "sentAt": "2025-09-01T12:06:00+08:00",
        "senderRole": "leader",
        "senderId": "agent-leader-aic",
        "command": "get",
        "taskId": "task-1234",
        "sessionId": "session-91011"
      }
    ],
    "statusHistory": [
      {
        "state": "accepted",
        "stateChangedAt": "2025-09-01T11:59:00+08:00"
      },
      {
        "state": "working",
        "stateChangedAt": "2025-09-01T12:00:00+08:00"
      },
      {
        "state": "awaiting-completion",
        "stateChangedAt": "2025-09-01T12:05:00+08:00"
      },
      {
        "state": "failed",
        "stateChangedAt": "2025-09-01T12:07:00+08:00",
        "dataItems": [
          {
            "type": "text",
            "text": "An error occurred while processing the task: unable to connect to the external data source."
          }
        ]
      }
    ],
    "sessionId": "session-91011"
  }
}
```

#### (4) Complete Task

- The Partner considers the Task to be completed and changes the state to `AwaitingCompletion`.
- If the Leader considers the Partner's products to be the expected products, it decides to end this Task by sending a new TaskCommand, in which the `command` is "complete". After receiving it, the Partner changes the Task state to `Completed` and returns it to the Leader.

**Request**

```json
{
  "jsonrpc": "2.0",
  "method": "rpc",
  "id": "3",
  "params": {
    "command": {
      "type": "task-command",
      "id": "msg-7890",
      "sentAt": "2025-09-01T12:09:00+08:00",
      "senderRole": "leader",
      "senderId": "agent-leader-aic",
      "command": "complete",
      "taskId": "task-1234",
      "sessionId": "session-91011"
    }
  }
}
```

**Response**

```json
{
  "jsonrpc": "2.0",
  "id": "3",
  "result": {
    "type": "task-result",
    "id": "msg-7891",
    "sentAt": "2025-09-01T12:10:00+08:00",
    "senderRole": "partner",
    "senderId": "agent-partner-aic",
    "taskId": "task-1234",
    "status": {
      "state": "completed",
      "stateChangedAt": "2025-09-01T12:10:00+08:00"
    },
    "products": [],
    "sessionId": "session-91011"
  }
}
```

#### (5) Cancel Task

The Leader may cancel a task at any time. After the task is canceled, the task state becomes `Canceled`.

**Request**

```json
{
  "jsonrpc": "2.0",
  "method": "rpc",
  "id": "4",
  "params": {
    "command": {
      "type": "task-command",
      "id": "msg-8901",
      "sentAt": "2025-09-01T12:14:00+08:00",
      "senderRole": "leader",
      "senderId": "agent-leader-aic",
      "command": "cancel",
      "taskId": "task-1234",
      "sessionId": "session-91011"
    }
  }
}
```

**Response**

```json
{
  "jsonrpc": "2.0",
  "id": "4",
  "result": {
    "type": "task-result",
    "id": "msg-8902",
    "sentAt": "2025-09-01T12:15:00+08:00",
    "senderRole": "partner",
    "senderId": "agent-partner-aic",
    "taskId": "task-1234",
    "status": {
      "state": "canceled",
      "stateChangedAt": "2025-09-01T12:15:00+08:00"
    },
    "products": [],
    "sessionId": "session-91011"
  }
}
```

## 6.2 Streaming Style

In Direct Interaction Mode, the Streaming Style based on Server-Sent Events (SSE) allows unidirectional transmission of large volumes of data and unidirectional real-time message sending between the Leader and a Partner.

The following is the agent interaction interface specification based on the Streaming Style:

- **URL**: `stream`
- **HTTP Method**: `POST`
- **Request**: `StreamRequest`
- **Response**: Returns HTTP 200 OK, Content-Type: text/event-stream, and starts sending the SSE event stream. The data field of each SSE event contains a StreamResponse object.

### 6.2.1 StreamRequest Definition

`StreamRequest` is a structured object with which the Leader initiates a streaming request to a Partner, inheriting from the JSON-RPC 2.0 specification:

```typescript
export interface StreamRequest extends JSONRPCRequest {
  /**
   * The RPC method name
   * For a streaming request, this value is always "stream"
   * @example "stream"
   */
  method: "stream";

  /**
   * The streaming request parameters
   */
  params: {
    /**
     * The TaskCommand to be sent through streaming transmission
     * Contains information such as the task instruction and data content
     */
    message: TaskCommand;
  };
}
```

### 6.2.2 StreamResponse Definition

The response of streaming transmission uses the `text/event-stream` content type and sends data through the SSE protocol. The data of each SSE contains a StreamResponse object.

```typescript
export interface StreamResponse extends JSONRPCResponse {
  /**
   * The streaming response result
   * Present for normal events; when a re-stream request cannot resume because the events have expired, result is absent,
   * and the error field is used to describe the reason (inherited from JSONRPCResponse).
   * result and error are mutually exclusive: exactly one of the two is present.
   */
  result?: {
    /**
     * The sequence index of the event
     * It is not required to be consecutive, but it must increase monotonically, for client-side ordering
     * @example 1, 2, 5, 7, 10
     */
    eventSeq: number;

    /**
     * The event data content
     * May be a task result, a task command, a task status update event, or a product chunk event
     */
    eventData:
      | TaskResult
      | TaskCommand
      | TaskStatusUpdateEvent
      | ProductChunkEvent;
  };
}
```

> **Note**: When the event corresponding to the `lastEventSeq` of a `re-stream` request is beyond the Partner's buffer range (expired), the Partner returns a `StreamResponse` containing only the `error` field and then closes the SSE connection, for example:
>
> ```json
> data: {
>   "jsonrpc": "2.0",
>   "id": "2",
>   "error": {
>     "code": -32001,
>     "message": "Stream events expired",
>     "data": { "lastAvailableSeq": 10 }
>   }
> }
> ```

### 6.2.3 TaskStatusUpdateEvent Definition

During streaming transmission, an agent can update the task status by sending a `TaskStatusUpdateEvent`. TaskStatusUpdateEvent inherits from Message.

```typescript
export interface TaskStatusUpdateEvent extends Message {
  /**
   * Object type identifier
   * For TaskStatusUpdateEvent, this value is always 'task-status-update'
   * @example "task-status-update"
   */
  readonly type: "task-status-update";

  /**
   * The unique identifier of the task
   * Points to the specific task whose status needs to be updated
   * @example "task-123e4567-e89b-12d3-a456-426614174000"
   */
  taskId: string;

  /**
   * The current status of the task
   * Contains the updated status information and related data
   */
  status: TaskStatus;
}
```

### 6.2.4 ProductChunkEvent Definition

During streaming transmission, an agent can transmit a product in chunks by sending a `ProductChunkEvent`. ProductChunkEvent inherits from Message.

```typescript
export interface ProductChunkEvent extends Message {
  /**
   * Object type identifier
   * For ProductChunkEvent, this value is always 'product-chunk'
   * @example "product-chunk"
   */
  readonly type: "product-chunk";

  /**
   * The unique identifier of the task
   * Points to the specific task that produced this product
   * @example "task-123e4567-e89b-12d3-a456-426614174000"
   */
  taskId: string;

  /**
   * The product data
   * Contains the product content of the current chunk
   */
  product: Product;

  /**
   * Whether this is an append chunk
   * false indicates the first chunk at the start of chunked transmission, and true indicates a subsequent append chunk
   * @example false (first chunk) | true (subsequent chunk)
   */
  append: boolean;

  /**
   * Whether this is the last chunk
   * true indicates the end of chunked transmission, and false indicates that further chunks follow
   * @example false (further chunks follow) | true (transmission ends)
   */
  lastChunk: boolean;
}
```

### 6.2.5 ReStreamCommandParams Definition

`ReStreamCommandParams` is the parameter interface of the Reconnect Stream command, used to resume an interrupted stream after a connection interruption.

```typescript
/**
 * The parameter interface of the Reconnect Stream command
 * When sending a message of the `re-stream` command, the following parameters may be included to resume an interrupted stream
 */
export interface ReStreamCommandParams {
  /**
   * The sequence number of the last event received in the previous connection
   * Used by the server to determine from which event to start resending data
   * If it is not provided, or its value is null, all events are sent from the beginning
   * @example 25 (resend starting from the 26th event) | undefined | null (start from the beginning)
   */
  lastEventSeq?: number;
}
```

### 6.2.6 Example

The interaction flow diagram is shown below:

![7-6](7-6.png)

#### (1) Create Stream

**Request**

```json
{
  "jsonrpc": "2.0",
  "method": "stream",
  "id": "1",
  "params": {
    "message": {
      "type": "task-command",
      "id": "msg-1234",
      "sentAt": "2025-09-01T11:59:00+08:00",
      "senderRole": "leader",
      "senderId": "agent-leader-aic",
      "command": "start",
      "dataItems": [
        {
          "type": "text",
          "text": "Please help me arrange a 3-day Beijing cultural themed tour itinerary."
        }
      ],
      "taskId": "task-5678",
      "sessionId": "session-91011"
    }
  }
}
```

**Response**

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
```

Event Stream:

```json
data: {
  "jsonrpc": "2.0",
  "id": "1",
  "result": {
    "eventSeq": 1,
    "eventData": {
      "type": "task-result",
      "id": "msg-1235",
      "sentAt": "2025-09-01T12:00:00+08:00",
      "senderRole": "partner",
      "senderId": "agent-partner-aic",
      "taskId": "task-5678",
      "status": {
        "state": "working",
        "stateChangedAt": "2025-09-01T12:00:00+08:00"
      },
      "products": [],
      "sessionId": "session-91011"
    }
  }
}

data: {
  "jsonrpc": "2.0",
  "id": "1",
  "result": {
    "eventSeq": 2,
    "eventData": {
      "type": "product-chunk",
      "id": "msg-1236",
      "sentAt": "2025-09-01T12:01:00+08:00",
      "senderRole": "partner",
      "senderId": "agent-partner-aic",
      "taskId": "task-5678",
      "product": {
        "id": "product-1",
        "name": "Beijing cultural tour itinerary.pdf",
        "description": "Detailed itinerary for a 3-day Beijing cultural themed tour",
        "dataItems": [
          {
            "type": "file",
            "name": "beijing_cultural_tour_part1.pdf",
            "mimeType": "application/pdf",
            "uri": "https://example.com/files/beijing_cultural_tour_part1.pdf"
          }
        ]
      },
      "append": false,
      "lastChunk": false,
      "sessionId": "session-91011"
    }
  }
}

data: {
  "jsonrpc": "2.0",
  "id": "1",
  "result": {
    "eventSeq": 3,
    "eventData": {
      "type": "product-chunk",
      "id": "msg-1237",
      "sentAt": "2025-09-01T12:03:00+08:00",
      "senderRole": "partner",
      "senderId": "agent-partner-aic",
      "taskId": "task-5678",
      "product": {
        "id": "product-1",
        "name": "Beijing cultural tour itinerary.pdf",
        "description": "Detailed itinerary for a 3-day Beijing cultural themed tour",
        "dataItems": [
          {
            "type": "file",
            "name": "beijing_cultural_tour_part2.pdf",
            "mimeType": "application/pdf",
            "uri": "https://example.com/files/beijing_cultural_tour_part2.pdf"
          }
        ]
      },
      "append": true,
      "lastChunk": true,
      "sessionId": "session-91011"
    }
  }
}

data: {
  "jsonrpc": "2.0",
  "id": "1",
  "result": {
    "eventSeq": 4,
    "eventData": {
      "type": "task-status-update",
      "id": "msg-1238",
      "sentAt": "2025-09-01T12:05:00+08:00",
      "senderRole": "partner",
      "senderId": "agent-partner-aic",
      "taskId": "task-5678",
      "status": {
        "state": "awaiting-completion",
        "stateChangedAt": "2025-09-01T12:05:00+08:00"
      },
      "sessionId": "session-91011"
    }
  }
}
```

#### (2) Close Stream

When the Partner itself decides that the task is Rejected or Failed, the Partner directly closes the streaming transmission connection after sending the end event.

When the Leader decides to Complete or Cancel the task, the SSE connection is left unchanged; the Leader establishes a new connection to send the command, and upon receiving it the Partner closes the streaming transmission connection and returns a TaskResult object in the final state to the Leader.

#### (3) Reconnect Stream

A Stream connection may be interrupted due to network problems, and the Leader may reconnect the streaming transmission connection of a task at any time. When reconnecting, the Leader needs to send a TaskCommand containing the `re-stream` command. If this command carries the command parameter `lastEventSeq`, it indicates the sequence number of the last event received in the previous connection. After receiving this command, the Partner continues sending from the event after `lastEventSeq`. If `lastEventSeq` is not provided, it means that all events are sent from the beginning.

Therefore, every SSE event sent by the Partner should contain an increasing `eventSeq` field indicating the sequence number of the event. When reconnecting, the Leader can use this field to specify from which event to continue receiving. The Partner needs to retain the events of a recent period so that they can be resent upon reconnection. If the events have expired or are unavailable, the returned data is a JSONRPCResponse object containing the `error` field, indicating that reconnection is impossible, with the specific reason given in the error field.

The `re-stream` command causes the Partner to resend messages. If this Task has not yet ended, the connection is maintained and the Partner continues sending subsequent events until the task is completed. If the Task has already ended, the Partner closes the connection after sending all events. The connection is also closed on error.

**Request**

```json
{
  "jsonrpc": "2.0",
  "method": "stream",
  "id": "2",
  "params": {
    "message": {
      "type": "task-command",
      "id": "msg-2345",
      "sentAt": "2025-09-01T12:07:00+08:00",
      "senderRole": "leader",
      "senderId": "agent-leader-aic",
      "command": "re-stream",
      "commandParams": {
        "lastEventSeq": 2
      },
      "taskId": "task-5678",
      "sessionId": "session-91011"
    }
  }
}
```

**Response**

After receiving the reconnection request, the Partner continues sending the SSE event stream from the event after `lastEventSeq`. For example, if `lastEventSeq` is 2, the Partner starts sending from eventSeq 3:

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
```

Event Stream:

```json
data: {
  "jsonrpc": "2.0",
  "id": "2",
  "result": {
    "eventSeq": 3,
    "eventData": {
      "type": "product-chunk",
      "id": "msg-2346",
      "sentAt": "2025-09-01T12:03:00+08:00",
      "senderRole": "partner",
      "senderId": "agent-partner-aic",
      "taskId": "task-5678",
      "product": {
        "id": "product-1",
        "name": "Beijing cultural tour itinerary.pdf",
        "description": "Detailed itinerary for a 3-day Beijing cultural themed tour",
        "dataItems": [
          {
            "type": "file",
            "name": "beijing_cultural_tour_part2.pdf",
            "mimeType": "application/pdf",
            "uri": "https://example.com/files/beijing_cultural_tour_part2.pdf"
          }
        ]
      },
      "append": true,
      "lastChunk": true,
      "sessionId": "session-91011"
    }
  }
}

data: {
  "jsonrpc": "2.0",
  "id": "2",
  "result": {
    "eventSeq": 4,
    "eventData": {
      "type": "task-status-update",
      "id": "msg-2347",
      "sentAt": "2025-09-01T12:05:00+08:00",
      "senderRole": "partner",
      "senderId": "agent-partner-aic",
      "taskId": "task-5678",
      "status": {
        "state": "awaiting-completion",
        "stateChangedAt": "2025-09-01T12:05:00+08:00"
      },
      "sessionId": "session-91011"
    }
  }
}
```

## 6.3 Notification Style

In Direct Interaction Mode, the Leader node can register an asynchronous notification with a Partner node through an RPC method; at appropriate moments such as a task status change or the generation of a product, the Partner node sends an HTTP POST request to the URL registered by the Leader, thereby implementing non-blocking asynchronous notification.

### 6.3.1 Register or Update Notification Configuration

Through this interface, the Leader registers a new notification configuration with a Partner or updates an existing configuration, specifying the receiving address, verification information, and associated task of the notification.

- **URL**: `notification/set`
- **HTTP Method**: `POST`
- **Request**: `NotificationRequest`
- **Response**: `NotificationResponse`

#### NotificationRequest Definition

```typescript
export interface NotificationRequest extends JSONRPCRequest {
  /**
   * The notification configuration parameters
   */
  params: NotificationConfig;
}

export interface NotificationConfig {
  /**
   * The unique identifier of the configuration
   * Generated by the Partner, used to identify a specific notification configuration
   * If it is not provided, or its value is null, it means the Leader wants to create a new configuration
   * If an existing ID is provided, it means the Leader wants to update this existing configuration
   * @example "notif-config-123e4567-e89b-12d3-a456-426614174000" | undefined | null (create a new configuration)
   */
  id?: string;

  /**
   * The URL provided by the Leader for receiving notifications
   * The Partner will send asynchronous notifications to this URL
   * @example "https://leader.example.com/api/notifications"
   */
  url: string;

  /**
   * The token used to verify notification requests
   * The Partner needs to include this token when sending a notification to verify its identity
   * @example "bearer-token-abc123xyz789"
   */
  token: string;

  /**
   * The associated task ID
   * Specifies which task this notification configuration is associated with
   * @example "task-123e4567-e89b-12d3-a456-426614174000"
   */
  taskId: string;
}
```

#### NotificationResponse Definition

```typescript
export interface NotificationResponse extends JSONRPCResponse {
  /**
   * The returned notification configuration
   * Contains the complete configuration information after creation or update
   */
  result: NotificationConfig;
}
```

### 6.3.2 Delete Notification Configuration

Through this interface, the Leader deletes a registered notification configuration and stops receiving notifications for the specified task.

- **URL**: `notification/delete`
- **HTTP Method**: `POST`
- **Request**: `NotificationIdRequest`
- **Response**: `NotificationDeleteResponse`

#### NotificationIdRequest Definition

If `notificationConfigId` is not provided, it means that all Notification configurations under this task are deleted.

```typescript
export interface NotificationIdRequest extends JSONRPCRequest {
  params: {
    /**
     * The associated task ID
     * Specifies the task whose notification configuration is to be deleted
     * @example "task-123e4567-e89b-12d3-a456-426614174000"
     */
    taskId: string;

    /**
     * The unique identifier of the notification configuration
     * If it is not provided or its value is null, it means that all notification configurations under this task are deleted
     * @example "notif-config-123e4567-e89b-12d3-a456-426614174000" | undefined | null (delete all configurations)
     */
    notificationConfigId?: string;
  };
}
```

#### NotificationDeleteResponse Definition

Returned after successful deletion:

```typescript
export interface NotificationDeleteResponse extends JSONRPCResponse {
  /**
   * The result of the deletion operation
   * This field is returned when the deletion succeeds
   */
  result?: {
    /**
     * The deletion success indicator
     * @example true
     */
    success: true;
  };
  // The error field is inherited from JSONRPCResponse and is used when deletion fails
}
```

### 6.3.3 Query Notification Configuration

Through this interface, the Leader queries registered notification configurations and obtains detailed information about a specified task or a specified configuration.

- **URL**: `notification/get`
- **HTTP Method**: `POST`
- **Request**: `NotificationIdRequest` (the same as for deletion)
- **Response**: `NotificationGetResponse`

The query parameters are the same as those for deletion. If `notificationConfigId` is provided, the corresponding Notification configuration is returned; if it is not provided, all Notification configurations under this task are returned.

#### NotificationGetResponse Definition

```typescript
export interface NotificationGetResponse extends JSONRPCResponse {
  /**
   * Returns the list of notification configurations associated with the task
   * If notificationConfigId is specified, only the corresponding configuration is returned;
   * otherwise all notification configurations under this task are returned
   * @example [{ id: "config-001", url: "...", token: "...", taskId: "..." }]
   */
  result: NotificationConfig[];
}
```

### 6.3.4 Start a Task with Notification

Through this interface, the Leader starts a task and specifies that a registered notification configuration be used, ensuring that the Partner can send asynchronous notifications according to the configuration during task execution.

- **URL**: `notification/start`
- **HTTP Method**: `POST`
- **Request**: `NotificationStartRequest`
- **Response**: `RpcResponse` (the same as an ordinary RPC response)

#### NotificationStartRequest Definition

```typescript
export interface NotificationStartRequest extends JSONRPCRequest {
  /**
   * The method name, fixed as "notification/start"
   */
  method: "notification/start";

  /**
   * The request parameters
   * Consistent with StreamRequest.params, carrying the TaskCommand in the message field
   */
  params: {
    /**
     * The task command object to be sent
     * The command field must be "start", and the notification configuration is specified in commandParams
     */
    message: TaskCommand;
  };
}
```

#### NotificationStartParams Definition

The notification start parameters carried in `TaskCommand.commandParams`:

```typescript
export interface NotificationStartParams {
  /**
   * The unique identifier of the notification configuration to be used
   * References a previously created notification configuration, used to determine the URL and authentication information of the notification
   * @example "notif-config-123e4567-e89b-12d3-a456-426614174000"
   */
  notificationConfigId: string;

  /**
   * Specifies the list of task states that trigger notifications
   * An optional parameter; if it is not provided, its value is null, or it is an empty array, notifications are sent on all state changes
   * @example ["working", "awaiting-completion", "completed", "failed"] | undefined | null (notify on all state changes)
   */
  notifyOnStates?: TaskState[];
}
```

### 6.3.5 Sending of Asynchronous Notifications

At appropriate moments such as a task status change or the generation of a product, the Partner sends an HTTP POST request to the URL registered by the Leader, thereby implementing asynchronous notification.

When a Partner needs to send a notification to the Leader, it sends an HTTP POST request to the registered URL, with the verification token included in the request headers and a TaskResult object as the request body.

- **URL**: the URL provided at registration time
- **HTTP Method**: `POST`
- **Request headers**:
  - `Content-Type`: `application/json`
  - `X-ACPs-AIP-Notification-Token`: `your_token`
- **Request body**: a TaskResult object
- **Response**:
  - **HTTP status code**: `200 OK` indicates that the notification was successfully received; any other status code indicates failure.
  - **Response body**: optional, usually empty.

### 6.3.6 Example

The interaction flow diagram is shown below:

![7-7](7-7.jpg)

#### (1) Registering a Notification

**Request**:

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "notification/set",
  "params": {
    "url": "https://example.com/notifications",
    "token": "your_token",
    "taskId": "task-5678"
  }
}
```

**Response**:

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "result": {
    "id": "notification-1",
    "url": "https://example.com/notifications",
    "token": "your_token",
    "taskId": "task-5678"
  }
}
```

#### (2) Deleting a Notification

**Request**:

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "method": "notification/delete",
  "params": {
    "taskId": "task-5678",
    "notificationConfigId": "notification-1"
  }
}
```

**Response**:

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "result": {
    "success": true
  }
}
```

#### (3) Querying a Notification

**Request**:

```json
{
  "jsonrpc": "2.0",
  "id": "3",
  "method": "notification/get",
  "params": {
    "taskId": "task-5678"
  }
}
```

**Response**:

```json
{
  "jsonrpc": "2.0",
  "id": "3",
  "result": [
    {
      "id": "notification-1",
      "url": "https://example.com/notifications",
      "token": "your_token",
      "taskId": "task-5678"
    }
  ]
}
```

#### (4) Starting a Task Using a Notification

**Request**:

```json
{
  "jsonrpc": "2.0",
  "id": "4",
  "method": "notification/start",
  "params": {
    "message": {
      "type": "task-command",
      "id": "msg-1234",
      "sentAt": "2025-09-01T11:59:30+08:00",
      "senderRole": "leader",
      "senderId": "agent-leader-aic",
      "command": "start",
      "commandParams": {
        "notificationConfigId": "notification-1",
        "notifyOnStates": ["working", "awaiting-completion", "failed"]
      },
      "dataItems": [
        {
          "type": "text",
          "text": "Please help me put together a 3-day itinerary for a Beijing cultural theme tour."
        }
      ],
      "taskId": "task-5678",
      "sessionId": "session-91011"
    }
  }
}
```

**Response**:

```json
{
  "jsonrpc": "2.0",
  "id": "4",
  "result": {
    "type": "task-result",
    "id": "result-1234",
    "sentAt": "2025-09-01T12:00:00+08:00",
    "senderRole": "partner",
    "senderId": "agent-partner-aic",
    "taskId": "task-5678",
    "status": {
      "state": "working",
      "stateChangedAt": "2025-09-01T12:00:00+08:00"
    },
    "products": [],
    "sessionId": "session-91011"
  }
}
```

#### (5) Partner Sends an Asynchronous Notification

Assume the registered URL is `https://example.com/notifications`. When the task status is updated, the Partner sends an HTTP POST request to that URL.

**Request headers**:

```http
POST /notifications HTTP/1.1
Host: example.com
Content-Type: application/json
X-ACPs-AIP-Notification-Token: your_token
```

**Request body**:

```json
{
  "type": "task-result",
  "id": "result-5678",
  "sentAt": "2025-09-01T12:00:00+08:00",
  "senderRole": "partner",
  "senderId": "agent-partner-aic",
  "taskId": "task-5678",
  "status": {
    "state": "completed",
    "stateChangedAt": "2025-09-01T12:00:00+08:00"
  },
  "products": [],
  "sessionId": "session-91011"
}
```

**Response headers**:

```http
HTTP/1.1 200 OK
```

**The response body is usually empty.**

# 7. Interaction Transport Mechanisms in Grouping Interaction Mode

In grouping interaction mode, the Leader creates and maintains a group, invites multiple Partners to join, and implements interaction between the Leader and the multiple Partners through a message queue. All business messages must be transmitted strictly through the group message queue, ensuring transparency so that all group members can be aware of them.

In v02.02, the group message queue uses AMQPS (TLS 1.3), and mutual authentication between agents and the MQ Server uses mTLS (based on agent identity certificates issued by the ACPs CA). On the MQ Server, all agents share the same `acps` vhost; the naming convention for the group-dedicated Exchange is `group_{leader-aic}_{group-id}`, the naming convention for each agent's group queue is `group_{leader-aic}_{group-id}_{aic}`, and the naming convention for the Inbox queue is `inbox_{aic}`.

## 7.0 Identity Binding Rules in Grouping Interaction Mode

In grouping interaction mode, an agent connecting to the MQ Server **MUST** use mTLS. The MQ Server **MUST** extract the AIC from the client certificate Subject `CN` as the authenticated username; if the certificate also contains the SAN `URI:acps://{AIC}`, its AIC **MUST** be consistent with the CN.

When `identity_binding_enabled=true`:

1. The publisher **MUST** set the AMQP message property `user_id`, and `user_id == normalize(senderId)`;
2. RabbitMQ **MUST** rely on validated user-id semantics to verify `user_id == authenticated username`;
3. An ordinary agent must not hold permissions or tags that allow forging `user_id` (for example, `impersonator`);
4. Before entering the business handler, the consumer SDK **MUST** verify `body.senderId == AMQP user_id`, and verify that `body.groupId` is consistent with the current group context.

If a group message lacks `user_id`, lacks `senderId`, has `senderId != user_id`, or has a `groupId` inconsistent with the current group context, the consumer **MUST** refuse to process the message.

## 7.1 Creating a Group and Joining as a Member

The Leader can invite a Partner to join a group in two ways, selected by priority:

**Preferred path: MQ Inbox invitation (v02.02)**

This applies when the Partner's ACS `endPoints` declares an AMQP transport endpoint (that is, the Partner is already connected to the MQ Server).

1. Through the Auth Service, the Leader pre-configures group ACLs for all group members (the Leader itself + each Partner), granting the corresponding Exchange / Queue read and write permissions.
2. On the MQ Server, the Leader creates the group Exchange (`group_{leader-aic}_{group-id}`, fanout type) and the Leader's own group queue (`group_{leader-aic}_{group-id}_{leader-aic}`), and binds the Leader queue to that Exchange.
3. The Leader publishes an `InboxGroupInvitation` invitation message to each Partner's Inbox queue (`inbox_{partner-aic}`).
4. After receiving the invitation from its Inbox, the Partner evaluates whether to accept it, then creates its own group queue (`group_{leader-aic}_{group-id}_{partner-aic}`) and binds it to the group Exchange, after which it broadcasts `GroupMgmtResult(status: connected=true)` to the group Exchange to announce that it has joined successfully.
5. The Leader and the other members that have already joined receive this confirmation message through their respective group queues, and group establishment is complete.

If a Partner is unable or refuses to join, the Partner replies with an `InboxGroupInvitationError` message to the Leader's Inbox (`inbox_{leader-aic}`); after receiving it, the Leader cleans up that Partner's ACL records.

`InboxGroupInvitation` and `InboxGroupInvitationError` are control messages delivered independently, and therefore **MUST** carry Message-level identity semantics:

- `InboxGroupInvitation.senderRole` **MUST** be `"leader"`;
- `InboxGroupInvitation.senderId` **MUST** equal `group.leader.aic`;
- `InboxGroupInvitation.groupId` **MUST** equal `group.groupId`;
- `InboxGroupInvitationError.senderRole` **MUST** be `"partner"`;
- `InboxGroupInvitationError.senderId` **MUST** be the AIC of the Partner that sent the error message;
- `InboxGroupInvitationError.partnerAic` is a compatibility field and must be marked as Deprecated; if this field is present, its value **MUST** equal `senderId`. New SDK / demo code **MUST NOT** send or depend on this field.

**Fallback path: Direct RPC invitation (backward compatible)**

This applies when the Partner's ACS does not declare an AMQP endpoint but does declare a JSONRPC endpoint. The Leader sends the invitation by directly calling the Partner's `group` method over RPC, and the Partner still joins the group by creating a queue, binding the Exchange, and broadcasting `GroupMgmtResult`.

Changes to the Direct RPC invitation format in v02.02:

- `server.port`: updated from `5672` (plaintext AMQP) to `5671` (AMQPS)
- `server.vhost`: updated from a vhost dedicated to each Leader to the shared `acps` vhost
- `server.accessToken`: marked as deprecated (`@deprecated`); the Partner determines the authentication method according to whether this field is present: missing or `null` → use mTLS; non-empty string → use username and password as in v02.00 (backward compatible)
- `amqp.exchange`: the naming convention is updated to `group_{leader-aic}_{group-id}`

## 7.2 Group Member Management

### 7.2.1 Member Status Monitoring

The Leader needs to periodically send status query requests to all Partners, and upon receipt each Partner returns a status response (containing connection and activity status). If the Leader does not receive a response from a Partner, that Partner is judged to be in the "offline" state.
The two types `GroupMgmtCommand` and `GroupMgmtResult` can be used to send management commands and return status information, so as to distinguish management operations from ordinary business messages.

> **Note: On the explicit redundancy of `groupId`**
>
> In grouping interaction mode, messages are transmitted through the message queue Exchange dedicated to the group, and the transport channel itself already implies group membership. Carrying `groupId` again in the message body constitutes information redundancy, but this is a deliberate design decision: a message retains self-describing capability after leaving the transport context, so the receiver can verify whether the `groupId` in the message is consistent with the transport channel in order to prevent message routing errors.

**GroupMgmtCommand definition**

The group management command (GroupMgmtCommand) is used to send commands related to group management, such as getting status, requesting exit, and muting. A group management command must carry `groupId` to specify the target group; `sessionId` is an optional field, and when carried it can associate the management operation with a specific session, facilitating auditing and historical tracing.

```typescript
export interface GroupMgmtCommand extends Message {
  /**
   * Message type identifier
   * For a group management command, this value is always 'group-mgmt-command', used to distinguish it from business messages
   * @example "group-mgmt-command"
   */
  readonly type: "group-mgmt-command";

  /**
   * Group management command type
   * Specifies the type of management operation to perform
   * The target member of the command is specified through the mentions field, and the specified member must execute the command and respond.
   * If mentions is not provided, is null, or is an empty array, it means that although all members can see it, whether to execute and respond is left to the members themselves.
   * If the value of mentions is "all", it means that all members must execute and respond.
   */
  command: GroupMgmtCommandType;

  /**
   * The ID of the group to which this command belongs (required, overriding the optional declaration in the base class)
   * A group management command must explicitly specify the target group
   * @example "group-session-91011"
   */
  groupId: string;
}

export enum GroupMgmtCommandType {
  /**
   * Get member status
   * Query the connection and activity status of a specified member or all members
   */
  GET_STATUS = "get-status",

  /**
   * Request exit from the group
   * Notify a specified member to exit the group
   */
  LEAVE_GROUP = "leave-group",

  /**
   * Mute a member
   * Prohibit a specified member from sending messages within the group, unless the member is specifically mentioned.
   */
  MUTE = "mute",

  /**
   * Unmute
   * Restore a specified member's permission to send messages within the group; the member may also send messages when not specifically mentioned.
   */
  UNMUTE = "unmute",
}
```

**GroupMgmtResult definition**

The group management result (GroupMgmtResult) is used to return status information about group members. A group management result must carry `groupId` to specify the group it belongs to; `sessionId` is an optional field, and when replying a Partner should echo the `sessionId` from the received `GroupMgmtCommand` (rather than generating its own), to ensure consistency of session association.

```typescript
export interface GroupMgmtResult extends Message {
  /**
   * Message type identifier
   * For a group management result, this value is always 'group-mgmt-result', used to distinguish it from business messages
   * @example "group-mgmt-result"
   */
  readonly type: "group-mgmt-result";

  /**
   * Group member status
   * A member describes its own current status through this field
   * Used for status query responses or status change notifications
   */
  status: GroupMemberStatus;

  /**
   * The ID of the group to which this result belongs (required, overriding the optional declaration in the base class)
   * A group management result must explicitly specify the group it belongs to
   * @example "group-session-91011"
   */
  groupId: string;
}

export interface GroupMemberStatus {
  /**
   * Connection status
   * Indicates whether the member remains connected to the group
   * If the member has exited the group, this is false
   */
  connected: boolean;

  /**
   * Mute status
   * Indicates whether the member is muted and unable to send messages within the group
   * If the member is muted, this is true
   */
  muted: boolean;
}
```

**Example of using a group management command**
Getting member status: a Partner may have gone offline due to network problems, and the Leader needs to periodically check the status of each Partner to ensure they are all online. The following example shows how the Leader sends a get-status command and how a Partner replies with its own status.

```json
{
  "type": "group-mgmt-command",
  "id": "mgmt-msg-1234",
  "sentAt": "2025-09-01T12:10:00+08:00",
  "senderRole": "leader",
  "senderId": "agent-leader-aic",
  "groupId": "group123",
  "sessionId": "session-91011",
  "command": "get-status",
  "mentions": ["agent-partner-1"]
}
```

After receiving it, the Partner replies with its own status (echoing the received `groupId` and `sessionId`):

```json
{
  "type": "group-mgmt-result",
  "id": "mgmt-msg-1235",
  "sentAt": "2025-09-01T12:10:05+08:00",
  "senderRole": "partner",
  "senderId": "agent-partner-1",
  "groupId": "group123",
  "sessionId": "session-91011",
  "status": {
    "connected": true,
    "muted": false
  }
}
```

### 7.2.2 Member Exit Mechanism

A group member can only be asked to exit by the Leader; it cannot exit on its own initiative. If a member goes offline because of a network problem, the Leader can check the status and, upon discovering the disconnection, use the forced exit mechanism to clean up the resources of the disconnected member.

- Normal exit: the Leader sends an exit command (a `GroupMgmtCommand` with `command` set to `LEAVE_GROUP`, where the `mentions` field specifies the AIC number of the Partner that is to exit). The member responds (by sending a `GroupMgmtResult` message whose status is `connected = false`), then performs resource cleanup and disconnects. The Leader updates the group member list.

- Forced exit: if a Partner does not exit through the normal procedure, the Leader may force the cleanup. The Leader forcibly disconnects the Partner, deletes the Partner queue, and checks the cleanup result.

### 7.2.3 Group Dissolution Process

The Leader dissolves a group in two phases:
(1) Notification phase: this includes broadcasting the dissolution notice and waiting for members to exit, that is, sending a `GroupMgmtCommand` with `command` set to `LEAVE_GROUP` and `mentions` set to `all`, which indicates that all members must perform the exit;
(2) Forced cleanup phase: this includes checking member status, forcibly disconnecting members, deleting group resources, cleaning up access permissions, and disconnecting.

## 7.3 Example

#### (1) Leader Creates a Group — MQ Inbox Approach (v02.02 preferred path)

This example uses the RabbitMQ ≥ 4.2 message queue, which follows the AMQP protocol. The Leader sends the group invitation to the Partner through the Inbox mechanism, so the Partner does not need to expose a public HTTP endpoint.

**Leader Sends an Invitation to the Partner's Inbox**

Message delivery target: `exchange="inbox.topic"`, `routing_key="inbox_1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU1"`

```json
{
  "type": "group-invitation",
  "id": "invite-1001",
  "sentAt": "2025-09-01T12:00:00+08:00",
  "senderRole": "leader",
  "senderId": "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4",
  "groupId": "group123",
  "protocol": "rabbitmq:4.2",
  "invitationToken": "dGhpcyBpcyBhIHRlc3QgdG9rZW4",
  "expiresAt": "2025-09-01T04:05:00Z",
  "group": {
    "groupId": "group123",
    "leader": {
      "aic": "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
    },
    "partners": [
      { "aic": "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU1" },
      { "aic": "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU2" }
    ]
  },
  "amqp": {
    "exchange": "group_1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4_group123",
    "exchangeType": "fanout",
    "routingKey": ""
  }
}
```

**After Creating the Group Queue and Binding the Exchange, the Partner Broadcasts a Join Confirmation Through the Group Exchange**

Message delivery target: `exchange="group_1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4_group123"`

```json
{
  "type": "group-mgmt-result",
  "id": "mgmt-join-1001",
  "sentAt": "2025-09-01T12:00:03+08:00",
  "senderRole": "partner",
  "senderId": "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU1",
  "groupId": "group123",
  "status": {
    "connected": true,
    "muted": false
  }
}
```

If the Partner rejects the invitation or cannot join, the Partner replies with an error message to the Leader's Inbox:

Message delivery target: `exchange="inbox.topic"`, `routing_key="inbox_1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"`

```json
{
  "type": "group-invitation-error",
  "id": "invite-err-1001",
  "sentAt": "2025-09-01T12:00:02+08:00",
  "senderRole": "partner",
  "senderId": "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU1",
  "groupId": "group123",
  "partnerAic": "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU1",
  "invitationToken": "dGhpcyBpcyBhIHRlc3QgdG9rZW4",
  "error": {
    "code": -32001,
    "message": "Invitation rejected by partner policy",
    "data": {
      "errorType": "INVITATION_REJECTED",
      "details": "Leader AIC not in trust list"
    }
  }
}
```

Here `partnerAic` is a deprecated compatibility field used only to parse old messages; new SDKs / demos must not send this field.

#### (2) Leader Creates a Group — Direct RPC Approach (backward-compatible path)

When a Partner's ACS does not declare an AMQP endpoint but does declare a JSONRPC endpoint, the Leader sends the invitation through a direct RPC connection. In v02.02, `server.accessToken` is deprecated, `server.port` is updated to `5671` (AMQPS), and `server.vhost` is updated to the shared `acps` vhost. This request sends the AICs of all Partners that are to join the group under the current circumstances to the newly joined Partner.

**Request**

```json
{
  "jsonrpc": "2.0",
  "method": "group",
  "id": "2",
  "params": {
    "protocol": "rabbitmq:4.2",
    "group": {
      "groupId": "group123",
      "leader": {
        "aic": "agent-leader-aic"
      },
      "partners": [
        {
          "aic": "agent-partner-1"
        },
        {
          "aic": "agent-partner-2"
        }
      ]
    },
    "server": {
      "host": "mq.example.com",
      "port": 5671,
      "vhost": "acps"
    },
    "amqp": {
      "exchange": "group_agent-leader-aic_group123",
      "exchangeType": "fanout",
      "routingKey": ""
    }
  }
}
```

> **Note**: The `server.accessToken` field is marked as deprecated in v02.02. The v02.02 MQ Server uses mTLS authentication, and the Partner connects directly to the AMQPS port using the identity certificate issued by the ACPs CA. If compatibility with a v02.00 MQ Server is still required, the sender may retain the `server.accessToken` field, and the Partner determines the authentication method based on whether that field is present.

**Response**

After the Partner creates the group queue and binds the Exchange, it broadcasts a `GroupMgmtResult` through the group Exchange to confirm joining (the mechanism is the same as in the MQ Inbox path). The JSONRPC method itself returns a brief confirmation:

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "result": {
    "status": "joining",
    "groupId": "group123"
  }
}
```

If the Partner cannot join the group (for example, because the connection fails), an example response is:

```json
{
  "jsonrpc": "2.0",
  "id": "2",
  "error": {
    "code": -32603,
    "message": "Internal error",
    "data": {
      "errorType": "CONNECTION_FAILED",
      "details": "Connection timed out"
    }
  }
}
```

#### (3) Leader Sends a Task Message

The Leader sends the task message through the message queue, and the Partners consume the task message from the message queue and process it.

```json
{
  "type": "task-command",
  "id": "msg-1234",
  "sentAt": "2025-09-01T11:59:45+08:00",
  "senderRole": "leader",
  "senderId": "agent-leader-aic",
  "command": "start",
  "dataItems": [
    {
      "type": "text",
      "text": "Please help me plan a 3-day Beijing cultural themed tour itinerary."
    }
  ],
  "taskId": "task-5678",
  "groupId": "group123",
  "sessionId": "session-91011"
}
```

#### (4) Each Partner Accepts the Task

After consuming the task message from the message queue, each Partner attempts to accept the task and sends a task status update message through the message queue.

Partner 1 accepts the task:

```json
{
  "type": "task-result",
  "id": "result-1001",
  "sentAt": "2025-09-01T12:00:00+08:00",
  "senderRole": "partner",
  "senderId": "agent-partner-1",
  "taskId": "task-5678",
  "status": {
    "state": "working",
    "stateChangedAt": "2025-09-01T12:00:00+08:00"
  },
  "products": [],
  "groupId": "group123",
  "sessionId": "session-91011"
}
```

Partner 2 accepts the task and has already completed it:

```json
{
  "type": "task-result",
  "id": "result-1002",
  "sentAt": "2025-09-01T12:05:00+08:00",
  "senderRole": "partner",
  "senderId": "agent-partner-2",
  "taskId": "task-5678",
  "status": {
    "state": "awaiting-completion",
    "stateChangedAt": "2025-09-01T12:05:00+08:00"
  },
  "products": [
    {
      "id": "product-1",
      "name": "Beijing Cultural Tour Itinerary.pdf",
      "description": "Detailed itinerary for a 3-day Beijing cultural themed tour",
      "dataItems": [
        {
          "type": "file",
          "name": "beijing_cultural_tour.pdf",
          "mimeType": "application/pdf",
          "uri": "https://example.com/files/beijing_cultural_tour.pdf"
        }
      ]
    }
  ],
  "groupId": "group123",
  "sessionId": "session-91011"
}
```

#### (5) Leader Sends a Command to a Specific Partner (mentions)

The Leader can use the mentions field to designate a specific Partner to handle a task or command.

```json
{
  "type": "task-command",
  "id": "msg-2345",
  "sentAt": "2025-09-01T12:08:00+08:00",
  "senderRole": "leader",
  "senderId": "agent-leader-aic",
  "mentions": ["agent-partner-2"],
  "command": "complete",
  "dataItems": [
    {
      "type": "text",
      "text": "Please complete the task; your itinerary is very good."
    }
  ],
  "taskId": "task-5678",
  "groupId": "group123",
  "sessionId": "session-91011"
}
```

After receiving the designated command, Partner 2 completes the task:

```json
{
  "type": "task-result",
  "id": "result-1003",
  "sentAt": "2025-09-01T12:10:00+08:00",
  "senderRole": "partner",
  "senderId": "agent-partner-2",
  "taskId": "task-5678",
  "status": {
    "state": "completed",
    "stateChangedAt": "2025-09-01T12:10:00+08:00"
  },
  "products": [],
  "groupId": "group123",
  "sessionId": "session-91011"
}
```

#### (6) Partner Rejects the Task

Some Partners may reject a task because of a capability mismatch or other reasons even though they have joined the group.

The message in which Partner 3 rejects the task:

```json
{
  "type": "task-result",
  "id": "result-1004",
  "sentAt": "2025-09-01T12:01:00+08:00",
  "senderRole": "partner",
  "senderId": "agent-partner-3",
  "taskId": "task-5678",
  "status": {
    "state": "rejected",
    "stateChangedAt": "2025-09-01T12:01:00+08:00",
    "dataItems": [
      {
        "type": "text",
        "text": "Sorry, travel itinerary planning is outside my capabilities; I suggest looking for a dedicated travel planning agent."
      }
    ]
  },
  "products": [],
  "groupId": "group123",
  "sessionId": "session-91011"
}
```

#### (7) Partner Task Failure

A Partner may encounter an error while executing a task, causing the task to fail.

The message in which Partner 1's task fails:

```json
{
  "type": "task-result",
  "id": "result-1005",
  "sentAt": "2025-09-01T12:03:00+08:00",
  "senderRole": "partner",
  "senderId": "agent-partner-1",
  "taskId": "task-5678",
  "status": {
    "state": "failed",
    "stateChangedAt": "2025-09-01T12:03:00+08:00",
    "dataItems": [
      {
        "type": "text",
        "text": "An error occurred while executing the task: unable to connect to the travel data source API; the service is temporarily unavailable."
      }
    ]
  },
  "products": [],
  "groupId": "group123",
  "sessionId": "session-91011"
}
```

#### (8) Leader Cancels the Task

The Leader can send a command to cancel the task to the group.

```json
{
  "type": "task-command",
  "id": "msg-3456",
  "sentAt": "2025-09-01T12:14:30+08:00",
  "senderRole": "leader",
  "senderId": "agent-leader-aic",
  "command": "cancel",
  "dataItems": [
    {
      "type": "text",
      "text": "The user has canceled this task."
    }
  ],
  "taskId": "task-5678",
  "groupId": "group123",
  "sessionId": "session-91011"
}
```

After receiving the cancel command, all Partners currently working on the task stop working and return the canceled status:

```json
{
  "type": "task-result",
  "id": "result-1006",
  "sentAt": "2025-09-01T12:15:00+08:00",
  "senderRole": "partner",
  "senderId": "agent-partner-1",
  "taskId": "task-5678",
  "status": {
    "state": "canceled",
    "stateChangedAt": "2025-09-01T12:15:00+08:00"
  },
  "products": [],
  "groupId": "group123",
  "sessionId": "session-91011"
}
```

#### (9) Partner Requests More Input

A Partner may need more information while executing a task.

Partner 2 requests more input:

```json
{
  "type": "task-result",
  "id": "result-2001",
  "sentAt": "2025-09-01T12:20:00+08:00",
  "senderRole": "partner",
  "senderId": "agent-partner-2",
  "taskId": "task-7890",
  "status": {
    "state": "awaiting-input",
    "stateChangedAt": "2025-09-01T12:20:00+08:00",
    "dataItems": [
      {
        "type": "text",
        "text": "More information is needed: please provide the budget range, accommodation preference (hotel/guesthouse), and whether there are any special dietary requirements?"
      }
    ]
  },
  "products": [],
  "groupId": "group123",
  "sessionId": "session-91011"
}
```

The Leader provides more information:

```json
{
  "type": "task-command",
  "id": "msg-4567",
  "sentAt": "2025-09-01T12:23:00+08:00",
  "senderRole": "leader",
  "senderId": "agent-leader-aic",
  "mentions": ["agent-partner-2"],
  "command": "continue",
  "dataItems": [
    {
      "type": "text",
      "text": "A budget of 3,000 yuan, a preference for four-star hotels, and no special dietary requirements."
    }
  ],
  "taskId": "task-7890",
  "groupId": "group123",
  "sessionId": "session-91011"
}
```

Partner 2 continues working:

```json
{
  "type": "task-result",
  "id": "result-2002",
  "sentAt": "2025-09-01T12:25:00+08:00",
  "senderRole": "partner",
  "senderId": "agent-partner-2",
  "taskId": "task-7890",
  "status": {
    "state": "working",
    "stateChangedAt": "2025-09-01T12:25:00+08:00"
  },
  "products": [],
  "groupId": "group123",
  "sessionId": "session-91011"
}
```

#### (10) Leader Asks a Partner to Exit

The Leader finds that a Partner is performing poorly and decides to have it exit the group.

```json
{
  "type": "group-mgmt-command",
  "id": "mgmt-msg-5678",
  "sentAt": "2025-09-01T12:30:00+08:00",
  "senderRole": "leader",
  "senderId": "agent-leader-aic",
  "groupId": "group123",
  "sessionId": "session-91011",
  "command": "leave-group",
  "mentions": ["agent-partner-1"]
}
```

After receiving the exit command, Partner 1 sends a final status update message in the group (echoing the received `groupId` and `sessionId`) and then exits the group.

```json
{
  "type": "group-mgmt-result",
  "id": "mgmt-msg-5679",
  "sentAt": "2025-09-01T12:30:05+08:00",
  "senderRole": "partner",
  "senderId": "agent-partner-1",
  "groupId": "group123",
  "sessionId": "session-91011",
  "status": {
    "connected": false,
    "muted": false
  }
}
```

# 8. Supplementary Notes

The Agent Interaction Protocol defined in this document gives full consideration to manageability and compatibility, and is provided free of charge to relevant researchers, developers, and institutions for reference. We welcome other industry colleagues engaged in agent development and in the formulation of agent interconnection protocols to support and adopt this process definition, so as to form an agent interaction mechanism that is conducive to interconnection and good compatibility.
