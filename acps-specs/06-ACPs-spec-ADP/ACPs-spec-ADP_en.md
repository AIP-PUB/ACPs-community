[Home](../README_en.md)

**[English](ACPs-spec-ADP_en.md) | [中文](ACPs-spec-ADP.md)**

ADP: Agent Discovery Protocol (ACPs-spec-ADP-v02.02)

# 1. Document Definition

This document is the standard definition of the Agent Discovery Protocol (ADP) within the ACPs agent collaboration protocol system, version v02.02.

The full title of the document is ACPs-spec-ADP-v02.02.

Document authors: Ke Li (Beijing University of Posts and Telecommunications), Maobin Zhang (Beijing University of Posts and Telecommunications), Yaoye Wang (Beijing University of Posts and Telecommunications), Ke Yu (Beijing University of Posts and Telecommunications), Xiaofeng Hu (Beijing University of Posts and Telecommunications), Jun Liu (Beijing University of Posts and Telecommunications), Di Ma (Beijing University of Posts and Telecommunications), Keliang Chen (Beijing University of Posts and Telecommunications).

# 2. Introduction to the Agent Discovery Process

For agent interconnection to become a secure and reliable agent system, a standardized and flexible agent discovery process is required in order to achieve collaboration between agents. This document defines the Agent Discovery Protocol (ADP), which follows the principles below and achieves the corresponding objectives:

(1) Meet the collaboration requirements of heterogeneous agents: a service-requesting agent can quickly discover agents that satisfy its capability requirements through the ADP mechanism;

(2) Support autonomous collaboration in dynamic environments: the ADP mechanism should be able to adapt to the highly dynamic nature of agent interconnection caused by the joining, leaving, or changing of agents, ensuring that the service-requesting agent can obtain agents that satisfy its capability requirements;

# 3. Role Definitions and Interaction Process in the Agent Discovery Process

The roles and the interaction process involved in the agent discovery process are shown in the figure below.
![6-1_en.png](6-1_en.png)

The agent discovery process involves the following roles:

● Requesting agent: the requesting agent is the party that initiates a service request when, according to its task objectives, it needs other agents to provide services for collaboration.

● Discovery server: a service node provided by an agent discovery service provider, whose function is to receive agent discovery requests and return matching retrieval results. The requesting agent may obtain the address of the discovery server through local configuration or through a service discovery mechanism in its environment.

● Registration server: a service node provided by an agent registration service provider, whose function is to expose agent registration information externally. The discovery server may synchronize agent description information through the *DSP: Data Synchronization Protocol*.

● Serving agent: a serving agent created and managed by an agent provider.

The agent discovery process includes the following steps:

(1) The requesting agent reasons about the task;

(2) The requesting agent decomposes the tasks that need to be carried out in collaboration with other agents, sends an agent discovery request to the discovery server, and carries the task information that requires collaboration;

(3) The discovery server performs matching retrieval based on the locally maintained agent directory; the directory may consist of registration information and related status information, and its synchronization mechanism may refer to the *DSP: Data Synchronization Protocol*;

(4) The discovery server matches the task information requiring collaboration in the request against the stored agent description information;

(5) The discovery server returns the agent or agent list information that meets the requirements to the requesting agent;

(6) According to its own policy, the requesting agent selects the agent to collaborate with (the serving agent) from the list and performs identity verification with it;

(7) After identity verification succeeds, the requesting agent establishes a connection with the serving agent and collaborates to complete the task.

# 4. Agent Discovery API

Through this API, the requesting agent interacts with the discovery server in order to discover serving agents that satisfy its task requirements.

## 4.1. API Specification

### 4.1.1. Discovery Request API Specification

- **Interface description**: for a requesting agent to discover serving agents that satisfy specific capability requirements.
- **Authentication method**: `mTLS`. The requesting agent must provide a valid certificate.
- **Request method**: `POST`
- **Request URL**: `{ADP_BASE_URL}/discover`
- **Request body**: `DiscoveryRequest`
- **Response body**: `DiscoveryResponse`
- **Response codes**:
  - 200 OK: the request succeeded.
  - 307 Temporary Redirect: redirect to a target discovery server that is valid only for this request. A `Location` response header indicates the new address. `error.code` in the response body indicates the reason for the redirect.
  - 400 Bad Request: invalid request parameters.
  - 401 Unauthorized: invalid certificate.
  - 429 Too Many Requests: the call rate exceeds the limit; a `Retry-After` response header indicates the retry time.
  - 508 Loop Detected: a forwarding loop was detected, the forwarding depth limit was exceeded, or the forwarding budget is insufficient.
  - 500 Internal Server Error: server-side exception.
- **Regarding ordering**:
  - The list of candidate serving agents in the response result should be sorted by match score from highest to lowest.
  - The sorting algorithm is determined by the discovery server implementation, but the order in the response should be directly usable for candidate selection.
- **Semantics of 307 Temporary Redirect**:
  - When a discovery server cannot complete the current query due to resource, policy, or geographic coverage limitations, it may return 307 and include a `Location` header in the response pointing to a recommended target discovery server;
  - The redirect takes effect only for the current request; the client must not cache this address, nor use it by default in subsequent requests;
  - After receiving 307, the client should re-issue the query once to the new address, carrying the original request body and mTLS credentials;
  - If multiple 307 responses are received in succession, the client may decide, according to its own policy, whether to continue redirecting or to report a failure; it is recommended that the maximum number of hops not exceed 5.

### 4.1.2. Error Code Definitions

Definitions of the error codes (`error.code`) in the response body:

| Error code | Meaning                   | Description                                                                                                                                                                                                    |
| ------ | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 30701  | CapacityRedirect          | The current discovery server is limited by load or maintenance; it returns 307 and provides a candidate server in `Location`.                                                                                  |
| 30702  | RegionRedirect            | Because of geographic or compliance policy, the query is not covered; it returns 307 to direct to a discovery server in a covered region.                                                                      |
| 30703  | MaxRedirectsExceeded      | The number of consecutive redirects by the client exceeds the limit, and the request is rejected.                                                                                                             |
| 40001  | MissingQuery              | When `type=explicit`, `query` is missing, or the text is an empty string.                                                                                                                                      |
| 40002  | ForwardDepthLimitInvalid  | `forwardDepthLimit` is not in the range 1-5.                                                                                                                                                                   |
| 40003  | ForwardChainInvalid       | The `forwardChain` carried by the client contains an invalid AIC.                                                                                                                                              |
| 40004  | FilterInvalid             | A condition in `filter` is invalid, or the conditions contradict each other.                                                                                                                                   |
| 40005  | ForwardFanoutLimitInvalid | `forwardFanoutLimit` is not in the range 1-5, or is less than 1.                                                                                                                                                |
| 40101  | CertificateInvalid        | The mTLS certificate is invalid, expired, or revoked.                                                                                                                                                          |
| 42901  | CallerRateLimited         | The call rate limit for a single requesting agent is exceeded; the `Retry-After` header must be respected.                                                                                                     |
| 42902  | TenantRateLimited         | The quota of the upper-layer tenant or organization is exhausted; the `Retry-After` header must be respected.                                                                                                  |
| 50801  | ForwardLoopDetected       | Its own AIC is found in `forwardChain`, and a loop is determined to exist.                                                                                                                                      |
| 50802  | ForwardDepthExceeded      | The forwarding depth reaches `forwardDepthLimit`, and forwarding is no longer continued.                                                                                                                       |
| 50803  | ForwardChainTampered      | Validation shows that the last AIC in `forwardChain` differs from the AIC embedded in the current request certificate, so the integrity of the chain has been compromised.                                     |
| 50804  | ForwardFanoutExceeded     | The number of branches attempted by aggregated forwarding exceeds the remaining budget, or the sum of the remaining budget allocation exceeds the permitted range. The server should provide `availableBudget` and `requiredBranches` in `error.data`. |
| 50805  | ForwardSignatureInvalid   | Signature verification in `forwardSignatures` failed; it may have been tampered with or the signature does not match.                                                                                           |
| 50001  | InternalError             | The discovery server itself is abnormal and cannot complete the query.                                                                                                                                         |

### 4.1.3. Common Semantics for Collaboration Among Multiple Discovery Servers

ADP allows multiple discovery servers to collaborate in order to cover a broader capability directory and improve availability. The externally visible collaboration modes include:

- **Redirect**: the current node does not participate in the query and instead directs the requester to access another discovery server directly;
- **Forwarding**: the current node continues the query with other discovery servers on behalf of the requester and transparently passes through the results;
- **Fan-out + Aggregation**: the current node queries multiple discovery servers concurrently and aggregates the returned candidates.

The semantics of the common fields are as follows:

- `forwardChain`: records the chain of AICs that the request passes through between discovery servers. The original request usually does not carry this field; it is maintained hop by hop by the discovery servers on the forwarding path.
- `requestId`: the globally unique identifier of this original discovery request. The original request usually does not carry this field; it is generated by the first-hop discovery server that receives the initial request and remains unchanged during forwarding, and is used for cross-branch correlation, log tracing, and result aggregation.
- `forwardDepthLimit`: the maximum forwarding depth allowed for a single chain. The server default value may be 3, and the absolute upper limit is 5.
- `forwardFanoutLimit`: the maximum number of concurrent downstream requests allowed. The server default value may be 1, and the absolute upper limit is 5. When it is not provided, the discovery server must not actively initiate multi-way concurrent forwarding.
- `forwardFanoutRemaining`: the fan-out budget still consumable by the current branch. This value is maintained by the discovery server during forwarding.
- `forwardTrustedServers`: the list of trusted discovery server AICs provided by the entry discovery server, used to constrain subsequent forwarding targets.
- `forwardSignatures`: the list of signatures appended by the discovery servers on the forwarding path, used to protect chain integrity.
- `acsMap`: the globally unique ACS data repository, containing at least the ACS data of all AICs in `agents`.
- `agents`: the list of discovered candidate agents, which is the main result consumed by the client.
- `routes`: optional; returns the skill information on each chained or aggregated path one by one, mainly used for debugging and attribution.

The specific implementations of route selection, caching, indexing, deduplication, and sorting are determined by the discovery server, but their behavior must conform to the data structures and response semantics defined in this specification.

### 4.1.4. Batch, Retry, and Cache

- **Batch discovery requests are not supported**: a requesting agent may implement batch discovery requirements by sending multiple discovery requests concurrently. This document does not define a batch interface that contains multiple queries in a single request.
- **Recommended retry policy**:
  - For 4xx errors, the parameters must be corrected before re-issuing the request; in the case of a 429 error, the waiting time indicated by `Retry-After` should be respected before retrying;
  - For 5xx or network errors, exponential backoff retry is recommended.
- **Cache recommendations**:
  - A requesting agent may cache discovery results according to its own policy, in order to reduce repeated queries and lower latency;
  - A discovery server may use standard HTTP cache control fields (such as `Cache-Control`, `ETag`) in response headers to indicate the caching policy for the response.

## 4.2. Data Structures

### 4.2.1. Data Structure Definitions

```typescript
export interface DiscoveryRequest {
  /**
   * Query type.
   * - explicit: explicit query (default value); the content of `query` has a clear intent, and is filtered according to `filter`.
   * - exploratory: exploratory query; the user has no clear goal and hopes the system will "give something interesting". `query` may be empty, or its content may be fairly broad.
   * - trending: trending query; the system is expected to return currently popular agents. `query` may be empty, or its content may be fairly broad.
   * - filtered: filtered query; filtering is performed only according to `filter`, `query` should be empty, and it will be ignored if it is not empty.
   * @example explicit
   */
  type?: string;

  /**
   * Capability query; a natural-language description of the service capability required by the requesting agent.
   * This field is required when type = explicit.
   * @example "I need an agent that can make Beijing food recommendations"
   */
  query?: string;

  /**
   * The globally unique identifier of this original discovery request.
   * The original request usually does not carry this field; it is generated by the first-hop discovery server that receives the initial request and remains unchanged during multi-hop forwarding and fan-out aggregation.
   * This field is used for cross-branch correlation, log tracing, and result aggregation.
   * @example "req-20260520-0001"
   */
  requestId?: string;

  /**
   * Structured context information.
   * It may be used to carry multi-turn dialogue summaries, user profile slices, or other data that assists intent understanding, helping the discovery server match capabilities more precisely.
   * It is recommended to use a lightweight JSON structure and keep it within the agreed payload size (recommended <2KB); the meaning of the fields is agreed upon by the upstream and downstream parties.
   * Note that this field may be forwarded and processed by multiple discovery servers, so the caller should avoid carrying sensitive information.
   * @example {
   *   "recentTurns": ["The previous turn asked about Beijing cuisine", "The user prefers healthy food"],
   *   "userProfile": { "city": "Beijing", "budget": "medium" }
   * }
   */
  context?: DiscoveryContext;

  /**
   * Maximum number of results returned.
   * Specifies the maximum number of candidate serving agents returned; if not specified, the server default value is used.
   * Because the discovery server may perform multi-hop forwarding and aggregation, pagination is not suitable; therefore the total amount is limited to control the response size.
   * @example 5
   */
  limit?: number;

  /**
   * Structured filter conditions.
   * A generic condition array pattern is used to match ACS fields, supporting a rich set of operators and logical combinations.
   * See the ACS filtering description in Section 4.2.2 for details.
   */
  filter?: DiscoveryFilter;

  /**
   * The maximum forwarding depth allowed for a single chain. The server default is 3, the absolute upper limit is 5, and exceeding it is treated as an invalid request.
   */
  forwardDepthLimit?: number;

  /**
   * The maximum number of concurrent downstream requests allowed. The server default is 1, the absolute upper limit is 5, and exceeding it is treated as an invalid request.
   * If this field is not provided, it defaults to 1, and the discovery server must not actively initiate multi-way concurrent forwarding.
   */
  forwardFanoutLimit?: number;

  /**
   * The fan-out budget still consumable by the current branch.
   * It is usually not set in the original request; it is initialized by the first-hop discovery server according to forwardFanoutLimit, and must then be decremented and passed on with each forwarding.
   */
  forwardFanoutRemaining?: number;

  /**
   * Forwarding trace chain.
   * The type of the forwarding chain is an array of strings, where each string is an agent identity code (AIC).
   * The original request does not need to carry this field. It is maintained in turn by the discovery server at each hop. With each forwarding, its own AIC must be appended to the list, for loop detection.
   * The last AIC in the forwarding chain should be consistent with the AIC carried in the mTLS certificate of the current request, to ensure chain integrity.
   * The order of the data in the forwarding chain is the order from the discovery server of the initial request to the current server.
   */
  forwardChain?: string[];

  /**
   * List of trusted discovery server AICs.
   * It is populated by the first discovery server to receive the request (the entry node) according to its own trust configuration, and is not provided by the requesting agent.
   * When forwarding (chained or aggregated), subsequent nodes should forward only to the discovery servers corresponding to the AICs in the list,
   * to ensure the trustworthiness of the final result.
   */
  forwardTrustedServers?: string[];

  /**
   * Digital signature of the forwarding discovery server. The signature is generated using the private key corresponding to the mTLS certificate.
   * It is generated and appended by the discovery server at each hop before forwarding, and is used to guarantee the integrity and tamper resistance of the forwarding chain.
   * The order is consistent with forwardChain; each signature corresponds to the AIC at the same position in forwardChain.
   * The signed content is the signature over the hash value of forwardChain and forwardTrustedServers. Other important fields in the request may also be added to enhance integrity protection.
   * When receiving a request, subsequent nodes should use the public key corresponding to the AIC of the source discovery server (that is, the last one in forwardChain) to verify the validity of the signature, ensuring that the forwarding chain has not been tampered with.
   */
  forwardSignatures?: string[];

  /**
   * Request timeout for each forwarding, in milliseconds. The default may be 10000 ms.
   * It is used to control the maximum waiting time of a single hop and to prevent a single node from blocking the overall response.
   * This value is a configuration and does not need to be dynamically adjusted according to the remaining time.
   */
  forwardEachTimeoutMs?: number;

  /**
   * Total timeout of the forwarding request, in milliseconds. The default may be 60000 ms.
   * It is used to control the maximum waiting time of the entire forwarding chain and to prevent the overall response from timing out.
   * This value needs to be dynamically adjusted for the next hop according to the remaining time, so as to ensure completion within the total timeout.
   * When the remaining value of forwardTotalTimeoutMs is less than forwardEachTimeoutMs, forwarding should no longer continue.
   */
  forwardTotalTimeoutMs?: number;
}

/**
 * Context payload structure.
 * It is intended to describe the optional session and user background slices on the requesting side, for the discovery server to understand intent.
 */
export interface DiscoveryContext {
  /**
   * Client-defined conversation ID, used to correlate multiple turns of a query.
   */
  conversationId?: string;

  /**
   * Summary or key points of recent dialogue.
   * It is recommended to sort by time and avoid carrying the complete original dialogue, in order to control the size.
   */
  recentTurns?: string[];

  /**
   * Anonymized user profile fragments, such as geographic location and budget preference.
   */
  userProfile?: Record<string, any>;

  /**
   * Additional context extensions, for use as agreed upon by the upstream and downstream parties.
   */
  metadata?: Record<string, any>;
}

/**
 * Set of filter conditions.
 * A generic condition array pattern is used, supporting logical combinations (AND/OR/NOT), and it can match all queryable fields in the ACS.
 * The logical relationship between the conditions is controlled by the logic field (default AND).
 * Sub-condition groups may be nested through groups to express arbitrarily complex logic.
 * See the ACS filtering description in Section 4.2.2 for details.
 */
export interface DiscoveryFilter {
  /** List of filter conditions. It is combined with the sub-condition groups in groups according to the logical relationship specified by logic. */
  conditions?: FilterCondition[];

  /**
   * Nested sub-condition groups; each subgroup may have its own independent logic.
   * It is recommended that nesting not exceed 3 levels, to maintain readability and forwarding performance.
   */
  groups?: DiscoveryFilter[];

  /**
   * The logical relationship between the conditions and sub-condition groups at this level.
   * - "and" (default): all conditions and subgroups must be satisfied.
   * - "or": at least one condition or subgroup must be satisfied.
   * - "not": negates the overall result at this level.
   * @default "and"
   */
  logic?: "and" | "or" | "not";
}

/**
 * A single filter condition, describing the matching rule applied to a field of the ACS.
 */
export interface FilterCondition {
  /**
   * Field path, using dots to separate nesting.
   * For array fields, the condition applies to each element, and a match occurs if any element is satisfied.
   * For the supported field paths, see Section 4.2.2.
   */
  field: string;

  /** Matching operator; see FilterOperator for details. */
  op: FilterOperator;

  /**
   * Matching value. The type depends on the data type of field and the op operator:
   * - String operators: value is a string
   * - List operators (in/nin): value is an array of values of the same type
   * - Numeric/date comparison operators: value is a number or an ISO 8601 string
   * - Range operator (between): value is a [lower, upper] pair
   * - Boolean operators: value is a boolean
   * - Existence operator (exists): value is a boolean
   * - Array operators (anyOf/allOf/noneOf): value is the array of values expected to match
   * - Array size operators (size): value is a number
   * - Map key operators (hasKey/hasAnyKey/hasAllKeys): value is a string or string[]
   */
  value?: any;
}

/**
 * Filter operators.
 *
 * General: eq (equal), ne (not equal), exists (field existence, value: boolean).
 * Comparison: gt, gte, lt, lte (numeric/date/string lexicographic order), between (closed interval [lower, upper]).
 * Set: in (value is in the list), nin (value is not in the list).
 * String: contains, notContains, startsWith, endsWith.
 * Case sensitivity: the string operators above are case-insensitive by default; adding the Cs suffix indicates case sensitivity,
 *   such as eqCs, neCs, inCs, ninCs, containsCs, notContainsCs, startsWithCs, endsWithCs.
 * Array: anyOf (contains at least one), allOf (contains all), noneOf (contains none), size (length equals).
 * Map/object: hasKey (contains the key), hasNoKey (does not contain the key), hasAnyKey (contains any key), hasAllKeys (contains all keys).
 *
 * Note: for strings, gt/gte/lt/lte compare in lexicographic order, mainly used in scenarios such as version number comparison.
 */
export type FilterOperator =
  // -- General: equality and existence --
  | "eq"
  | "ne"
  | "exists"
  // -- Comparison: numeric, date, string lexicographic order --
  | "gt"
  | "gte"
  | "lt"
  | "lte"
  | "between"
  // -- Set: value list matching --
  | "in"
  | "nin"
  // -- String: pattern matching (case-insensitive by default) --
  | "contains"
  | "notContains"
  | "startsWith"
  | "endsWith"
  // -- String: case-sensitive variants (Cs = Case Sensitive) --
  | "eqCs"
  | "neCs"
  | "inCs"
  | "ninCs"
  | "containsCs"
  | "notContainsCs"
  | "startsWithCs"
  | "endsWithCs"
  // -- Array: set operations --
  | "anyOf"
  | "allOf"
  | "noneOf"
  | "size"
  | "sizeGt"
  | "sizeGte"
  | "sizeLt"
  | "sizeLte"
  // -- Map/object: key checks --
  | "hasKey"
  | "hasNoKey"
  | "hasAnyKey"
  | "hasAllKeys";

/**
 * Generic response structure. A specific response structure needs to extend this interface and add a result field.
 * On a successful call it contains a result field, and on a failed call it contains an error field; the two are mutually exclusive.
 */
export interface CommonResponse {
  /**
   * Result of the method call
   * If the call succeeds, it contains the result, which is mutually exclusive with the error field.
   * @example { "type": "task", "id": "task-001" }
   */
  result?: any;

  /**
   * Error information
   * If the call fails, it contains the error object, which is mutually exclusive with the result field.
   */
  error?: {
    /**
     * Error code
     * @example -32602
     */
    code: number;

    /**
     * Error message
     * A brief description of the error.
     * @example "Invalid Request"
     */
    message: string;

    /**
     * Optional error data
     * Provides further error details and context information.
     * @example { "errorType": "CONNECTION_FAILED" }
     */
    data?: any;
  };
}

/**
 * Discovery response.
 */
export interface DiscoveryResponse extends CommonResponse {
  /**
   * Request result
   * On a successful call, it returns the list of matched serving agents.
   */
  result?: DiscoveryResult;
}

/**
 * Discovery result.
 */
export interface DiscoveryResult {
  /**
   * ACS data of the agents involved in the discovery result.
   * The key is the agent identity code (AIC), and the value is the complete capability description (ACS) of that agent.
   * It contains at least the ACS data of all AICs in agents; the discovery server may include, as needed, the additional AICs appearing in routes, but this is not mandatory.
   * The aic field in DiscoveryAgentSkill may serve as the lookup key for this mapping.
   */
  acsMap: Record<string, Record<string, any>>;

  /**
   * List of discovered candidate agents.
   * When multi-way aggregation has been performed, this is the deduplicated and sorted result, convenient for direct use by the client;
   * when no aggregation has been performed, it is consistent with the agentGroups of the unique path in routes.
   */
  agents: DiscoveryAgentGroup[];

  /**
   * Set of downstream response routes (optional).
   * Each route corresponds to one chained or aggregated forwarding path and its candidate skills.
   * It is mainly used for debugging and attribution; during normal use, the client may focus only on agents and acsMap.
   */
  routes?: DiscoveryRoute[];

  /**
   * Liveness status of each AIC in the discovery result (optional).
   * The key is the agent identity code (AIC), corresponding to the keys of acsMap; the value contains:
   * - alive: whether it is in a live state (true indicates that the monitoring system has confirmed recent activity).
   * - aliveLastSeenAt: the timestamp of the last activity (ISO 8601 UTC), for display reference only; it does not constitute a strong-consistency freshness commitment.
   *
   * This field appears only when the discovery server has enabled the AMP Heartbeat alive synchronization function;
   * when it is not enabled, the field is absent, and the client must not interpret the absence of the field as "the agent is offline".
   * The data source is the alive replica maintained locally by the discovery server, which is kept near-real-time consistent with the monitoring server through the AMP Heartbeat alive-delta
   * synchronization protocol (see ACPs-spec-AMP §6.2).
   *
   * The lookup method is the same as for acsMap: use DiscoveryAgentSkill.aic as the key to look up the table.
   * A missing key indicates that the liveness status of that AIC is currently unknown (for example, alive sync has not yet covered that AIC);
   * the presence of the key with alive=false indicates that the monitoring system has confirmed that the AIC is currently inactive.
   */
  aliveMap?: Record<string, {
    alive: boolean;
    aliveLastSeenAt?: string;
  }>;
}

/**
 * Route-level breakdown of the discovery result.
 */
export interface DiscoveryRoute {
  /**
   * List of AICs of the discovery servers that the forwarding link passes through.
   * The order is from the first hop (the discovery server that received the original request) to the discovery server that finally returns the result.
   */
  forwardChain: string[];

  /**
   * List of candidate serving agents organized by group, as returned by this link.
   * Each group corresponds to one query dimension (such as a subtask or a category).
   * If the business does not require grouping, an array containing a single default group should still be returned, to keep the structure uniform.
   */
  agentGroups: DiscoveryAgentGroup[];

  /**
   * Response status of this link.
   * - ok: the result was returned successfully.
   * - timeout: a downstream node timed out.
   * - error: other errors.
   */
  status?: "ok" | "timeout" | "error";

  /**
   * Total elapsed time of this link (milliseconds).
   */
  durationMs?: number;

  /**
   * Additional link-level extension information.
   */
  metadata?: Record<string, any>;
}

/**
 * Agent matching result organized by group.
 * The discovery server may decompose the query into multiple subtasks or categories, with each group corresponding to one dimension.
 * If grouping is not required, use a single default group (for example, group is the original query text or an empty string).
 */
export interface DiscoveryAgentGroup {
  /**
   * Group identifier.
   * It may be a subtask description, a category name, etc., used to identify the source or dimension of the results in this group.
   * If there is no grouping requirement, it may be set to the original query text or an empty string.
   */
  group: string;

  /**
   * List of matched agent skills under this group.
   */
  agentSkills: DiscoveryAgentSkill[];
}

/**
 * Matching result of a single agent skill.
 */
export interface DiscoveryAgentSkill {
  /**
   * Agent identity code (AIC).
   * Used to correlate the complete ACS information in DiscoveryResult.acsMap.
   */
  aic: string;

  /**
   * The Skill ID matched in the ACS.
   */
  skillId: string;

  /**
   * Match ranking.
   * The ranking order is the natural order of the numbers; the larger the numeric value, the lower the ranking.
   */
  ranking: number;

  /**
   * Remark information.
   */
  memo?: string;
}
```

### 4.2.2. ACS Filtering Description

`DiscoveryFilter` adopts the generic "condition array + logical combination" pattern (drawing on the operator naming of MongoDB Query / SCIM 2.0), and has the following design advantages:

- **JSON-native**: it is embedded directly in the request body, with no string parsing required.
- **Unified mechanism**: one `{ field, op, value }` structure covers all ACS field types, and adding or removing fields does not change the protocol structure.
- **Logical combination**: arbitrary depths of AND/OR/NOT are supported through nested `groups` + `logic`.
- **Forwarding pass-through**: an intermediate discovery server can forward the filter conditions directly, without needing to understand the semantics of every field.

After receiving filter conditions, the discovery server should validate the validity of field paths, the compatibility between operators and field types, and whether the nesting depth exceeds the limit (it is recommended not to exceed 3 levels). Invalid conditions should return error code 40004 (FilterInvalid).

#### Operator Applicability Table

| Operator      | String | Numeric | Boolean | Date-time | Array | Map/Object | Description                                                |
| ------------- | ------ | ---- | ---- | -------- | ---- | -------- | -------------------------------------- |
| eq            | ✓      | ✓    | ✓    | ✓        |      |          | Equal to                                   |
| ne            | ✓      | ✓    | ✓    | ✓        |      |          | Not equal to                                 |
| gt            | ✓¹     | ✓    |      | ✓        |      |          | Greater than                                   |
| gte           | ✓¹     | ✓    |      | ✓        |      |          | Greater than or equal to                               |
| lt            | ✓¹     | ✓    |      | ✓        |      |          | Less than                                   |
| lte           | ✓¹     | ✓    |      | ✓        |      |          | Less than or equal to                               |
| between       |        | ✓    |      | ✓        |      |          | Closed interval [lower, upper]                  |
| in            | ✓      | ✓    |      |          |      |          | The value is in the given list                         |
| nin           | ✓      | ✓    |      |          |      |          | The value is not in the given list                       |
| contains      | ✓      |      |      |          |      |          | Contains the substring                               |
| notContains   | ✓      |      |      |          |      |          | Does not contain the substring                             |
| startsWith    | ✓      |      |      |          |      |          | Starts with the specified prefix                         |
| endsWith      | ✓      |      |      |          |      |          | Ends with the specified suffix                         |
| eqCs          | ✓      |      |      |          |      |          | Equal to (case-sensitive)                     |
| neCs          | ✓      |      |      |          |      |          | Not equal to (case-sensitive)                   |
| inCs          | ✓      |      |      |          |      |          | The value is in the given list (case-sensitive)           |
| ninCs         | ✓      |      |      |          |      |          | The value is not in the given list (case-sensitive)         |
| containsCs    | ✓      |      |      |          |      |          | Contains the substring (case-sensitive)                 |
| notContainsCs | ✓      |      |      |          |      |          | Does not contain the substring (case-sensitive)               |
| startsWithCs  | ✓      |      |      |          |      |          | Starts with the specified prefix (case-sensitive)           |
| endsWithCs    | ✓      |      |      |          |      |          | Ends with the specified suffix (case-sensitive)           |
| exists        | ✓      | ✓    | ✓    | ✓        | ✓    | ✓        | Whether the field exists and is non-empty (value=true/false) |
| anyOf         |        |      |      |          | ✓    |          | The array contains at least one of the given values             |
| allOf         |        |      |      |          | ✓    |          | The array contains all of the given values                 |
| noneOf        |        |      |      |          | ✓    |          | The array contains none of the given values           |
| size          |        |      |      |          | ✓    |          | The array length equals the specified value                     |
| sizeGt        |        |      |      |          | ✓    |          | The array length is greater than the specified value                     |
| sizeGte       |        |      |      |          | ✓    |          | The array length is greater than or equal to the specified value             |
| sizeLt        |        |      |      |          | ✓    |          | The array length is less than the specified value                     |
| sizeLte       |        |      |      |          | ✓    |          | The array length is less than or equal to the specified value             |
| hasKey        |        |      |      |          |      | ✓        | The Map/object contains the specified key                     |
| hasNoKey      |        |      |      |          |      | ✓        | The Map/object contains none of the given keys       |
| hasAnyKey     |        |      |      |          |      | ✓        | The Map/object contains at least one of the given keys         |
| hasAllKeys    |        |      |      |          |      | ✓        | The Map/object contains all of the given keys             |

> Note ¹: for strings, gt/gte/lt/lte compare in lexicographic order, mainly used in scenarios such as version number comparison.
>
> Note ²: operators applicable to strings (eq, ne, in, nin, contains, notContains, startsWith, endsWith) match in a **case-insensitive** manner by default. If case-sensitive matching is required, use the corresponding `Cs` suffix variant (such as `eqCs`, `containsCs`). The Cs variants apply only to the string type.

#### Filtering Examples

**Example 1: simple conditions (AND logic)**

Query agents that are active, whose protocol version is 02.02, that support JSONRPC, and that have streaming responses:

```json
{
  "conditions": [
    { "field": "active", "op": "eq", "value": true },
    { "field": "protocolVersion", "op": "in", "value": ["02.02"] },
    { "field": "endPoints.transport", "op": "in", "value": ["JSONRPC"] },
    { "field": "capabilities.streaming", "op": "eq", "value": true },
    { "field": "skills.tags", "op": "anyOf", "value": ["data processing", "Beijing"] }
  ]
}
```

**Example 2: date range + version comparison**

Query agents updated within 2025 whose version is ≥ 2.0.0:

```json
{
  "conditions": [
    {
      "field": "lastModifiedTime",
      "op": "between",
      "value": ["2025-01-01T00:00:00+08:00", "2025-12-31T23:59:59+08:00"]
    },
    { "field": "version", "op": "gte", "value": "2.0.0" }
  ]
}
```

**Example 3: OR logic - cross-field combination**

Query agents for which (streaming=true and the organization contains "university") or (OIDC authentication is present and MQTT is supported):

```json
{
  "logic": "or",
  "groups": [
    {
      "conditions": [
        { "field": "capabilities.streaming", "op": "eq", "value": true },
        { "field": "provider.organization", "op": "contains", "value": "university" }
      ]
    },
    {
      "conditions": [
        { "field": "securitySchemes", "op": "hasKey", "value": "oidc" },
        {
          "field": "capabilities.messageQueue",
          "op": "anyOf",
          "value": ["mqtt:5.0"]
        }
      ]
    }
  ]
}
```

**Example 4: NOT logic - excluding specific organizations**

```json
{
  "conditions": [
    { "field": "active", "op": "eq", "value": true },
    { "field": "provider.organization", "op": "nin", "value": ["a blocklisted organization"] }
  ]
}
```

**Example 5: existence and array length checks**

Query agents that have service endpoints, have a web application URL, and have at least 3 skills:

```json
{
  "conditions": [
    { "field": "endPoints", "op": "exists", "value": true },
    { "field": "webAppUrl", "op": "exists", "value": true },
    { "field": "skills", "op": "sizeGte", "value": 3 }
  ]
}
```

**Example 6: AIC prefix matching**

```json
{
  "conditions": [{ "field": "aic", "op": "startsWith", "value": "1000100001" }]
}
```

### 4.2.3. Other Notes

The fields of `DiscoveryContext` are advisory only; the caller and the discovery server may extend them with more key-value pairs according to the scenario, and should agree on a security policy and a maximum payload, so as to ensure secure transmission within the server-side risk control limits.

`agents` and `acsMap` are the main fields consumed by the client. `acsMap` is located at the top level of `DiscoveryResult` and is the globally unique ACS data repository; it contains at least the ACS data of all AICs in `agents`. The discovery server may include, as needed, the additional AICs appearing in `routes`, but this is not mandatory. The client should not assume that an AIC referenced in `routes` can necessarily be found in `acsMap`.

During aggregated forwarding, the discovery server needs to maintain an independent `DiscoveryRoute` for each downstream request, and to return the deduplicated and sorted aggregated result in `agents`. `routes` is an optional field, mainly used for debugging and attribution; if a certain route fails, the specific reason may be reflected through the `status` field, making it easier for the requesting agent to perform fault-tolerant handling.

`agents` and `DiscoveryAgentGroup` in `DiscoveryRoute.agentGroups` should contain at least one group. If the business does not require grouping (such as a simple single-intent query), an array containing a single default group should still be returned, and `group` may be set to the original query text or an empty string, so as to keep the response structure uniform.

`aliveMap` is optional additional liveness status information, populated in batches by the discovery server from the locally maintained AMP alive replica, keyed by AIC. The lookup method is the same as for `acsMap`, using `DiscoveryAgentSkill.aic` as the key. When consuming it, the client should observe the following rules:

- **Field absent**: the discovery server has not enabled AMP alive synchronization, and the client must not judge the liveness status of an agent on this basis.
- **Key absent** (`aliveMap` exists but does not contain a certain AIC): the liveness status of that AIC is currently unknown (alive sync has not yet covered it), which is not equivalent to being inactive.
- **`alive=false`**: the monitoring server has confirmed that the AIC is currently inactive (for example, its heartbeat timed out), and the client may lower its priority or skip that AIC on this basis.
- **`aliveLastSeenAt`**: for display reference only; it is not guaranteed to be strictly consistent with the current state of the monitoring server, and must not be used for precise freshness judgments.
- **Forwarding scenario**: `aliveMap` reflects the local alive replica of the **discovery server that is the source of the results**, and is therefore populated only by the source server that produced that batch of results. An intermediate (forwarding/aggregating) discovery server should **pass through** the `aliveMap` returned by downstream **as is**, and **must not** use its own local replica to **overwrite or merge** the downstream entries (the source node has greater authority over the liveness status of its own agents, and cross-node merging would introduce inconsistency and misleading information). In other words: the `aliveMap` entry for each AIC comes from at most one source — the discovery server that produced it.

## 4.3. Example Requests and Responses

This section presents typical external invocation scenarios. The examples include distributed tracing fields such as `X-Trace-Id` and `X-Span-Id`; the field names are examples only, and in actual use they may be extended according to the system logging specification.

### 4.3.1. Basic Query (Explicit Query)

**Example request**

```http
POST /discover HTTP/1.1
Host: ds-a.example.com
Content-Type: application/json
X-Trace-Id: trace-20251125-abcd1234
X-Span-Id: span-client-001

{
  "type": "explicit",
  "query": "I need an agent that can make Beijing food recommendations",
  "limit": 5
}
```

**Example response**

```http
HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: max-age=300
X-Trace-Id: trace-20251125-abcd1234
X-Span-Id: span-ds-a-001

{
  "result": {
    "acsMap": {
      "AIC-AGENT-FOOD-01": {
        "name": "BeijingFoodGuide",
        "description": "A professional agent for Beijing food recommendations",
        "provider": { "organization": "FoodTech Inc." }
      },
      "AIC-AGENT-CUISINE-01": {
        "name": "ChinaCuisineBot",
        "description": "Food recommendations from all over China",
        "provider": { "organization": "TravelAI" }
      }
    },
    "agents": [
      {
        "group": "I need an agent that can make Beijing food recommendations",
        "agentSkills": [
          {
            "aic": "AIC-AGENT-FOOD-01",
            "skillId": "skill-beijing-food-001",
            "ranking": 1
          },
          {
            "aic": "AIC-AGENT-CUISINE-01",
            "skillId": "skill-china-cuisine-002",
            "ranking": 2
          }
        ]
      }
    ],
    "routes": [
      {
        "forwardChain": ["AIC-DS-A"],
        "agentGroups": [
          {
            "group": "I need an agent that can make Beijing food recommendations",
            "agentSkills": [
              {
                "aic": "AIC-AGENT-FOOD-01",
                "skillId": "skill-beijing-food-001",
                "ranking": 1
              },
              {
                "aic": "AIC-AGENT-CUISINE-01",
                "skillId": "skill-china-cuisine-002",
                "ranking": 2
              }
            ]
          }
        ],
        "status": "ok",
        "durationMs": 150
      }
    ]
  }
}
```

### 4.3.2. Query with a Filter

**Example request**

```http
POST /discover HTTP/1.1
Host: ds-a.example.com
Content-Type: application/json
X-Trace-Id: trace-20251125-efgh5678
X-Span-Id: span-client-002

{
  "type": "explicit",
  "query": "I need a data analysis agent that supports streaming responses",
  "limit": 3,
  "filter": {
    "conditions": [
      { "field": "active", "op": "eq", "value": true },
      { "field": "endPoints.transport", "op": "in", "value": ["JSONRPC"] },
      { "field": "capabilities.streaming", "op": "eq", "value": true },
      { "field": "provider.countryCode", "op": "in", "value": ["CN", "US"] }
    ]
  },
  "context": {
    "conversationId": "conv-12345",
    "recentTurns": ["The user asked about sales data trends"],
    "userProfile": {
      "industry": "retail",
      "dataSize": "large"
    }
  }
}
```

**Example response**

```http
HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: max-age=300
X-Trace-Id: trace-20251125-efgh5678
X-Span-Id: span-ds-a-002

{
  "result": {
    "acsMap": {
      "AIC-AGENT-STREAM-01": {
        "name": "StreamAnalyzer",
        "description": "A big data analysis agent that supports streaming responses",
        "capabilities": { "streaming": true },
        "provider": { "organization": "DataCorp", "countryCode": "CN" }
      }
    },
    "agents": [
      {
        "group": "I need a data analysis agent that supports streaming responses",
        "agentSkills": [
          {
            "aic": "AIC-AGENT-STREAM-01",
            "skillId": "skill-data-analytics-001",
            "ranking": 1,
            "memo": "Fully matches the streaming response requirement"
          }
        ]
      }
    ]
  }
}
```

### 4.3.3. Redirect Response

**Example request**

```http
POST /discover HTTP/1.1
Host: ds-a.example.com
Content-Type: application/json

{
  "type": "explicit",
  "query": "I need an agent that can make Beijing food recommendations",
  "forwardDepthLimit": 3,
  "forwardFanoutLimit": 1
}
```

**Example response**

```http
HTTP/1.1 307 Temporary Redirect
Location: https://ds-b.example.com/discover
Content-Type: application/json

{
  "error": {
    "code": 30701,
    "message": "CapacityRedirect",
    "data": {
      "reason": "maintenance-window"
    }
  }
}
```

### 4.3.4. Aggregated Result

**Example response**

```json
{
  "result": {
    "acsMap": {
      "AIC-AGENT-MUSIC-GLOBAL": { "name": "MusicGlobalAgent" }
    },
    "agents": [
      {
        "group": "I need an agent that can make music recommendations",
        "agentSkills": [
          {
            "aic": "AIC-AGENT-MUSIC-GLOBAL",
            "skillId": "skill-music-recommend",
            "ranking": 1,
            "memo": "Highest score after deduplication"
          }
        ]
      }
    ],
    "routes": [
      {
        "forwardChain": ["AIC-DS-01", "AIC-DS-NA"],
        "agentGroups": [
          {
            "group": "I need an agent that can make music recommendations",
            "agentSkills": [
              {
                "aic": "AIC-AGENT-MUSIC-NA",
                "skillId": "skill-music-recommend",
                "ranking": 4
              }
            ]
          }
        ],
        "status": "ok",
        "durationMs": 420
      },
      {
        "forwardChain": ["AIC-DS-01", "AIC-DS-EU"],
        "agentGroups": [],
        "status": "timeout",
        "metadata": { "reason": "downstream-timeout" }
      },
      {
        "forwardChain": ["AIC-DS-01", "AIC-DS-AP"],
        "agentGroups": [
          {
            "group": "I need an agent that can make music recommendations",
            "agentSkills": [
              {
                "aic": "AIC-AGENT-MUSIC-AP",
                "skillId": "skill-music-recommend",
                "ranking": 2
              }
            ]
          }
        ],
        "status": "ok",
        "durationMs": 510
      }
    ]
  }
}
```

### 4.3.5. No Matching Result

**Example request**

```http
POST /discover HTTP/1.1
Host: ds-a.example.com
Content-Type: application/json
X-Trace-Id: trace-20251125-mnop3456
X-Span-Id: span-client-004

{
  "type": "explicit",
  "query": "I need a Mars exploration data analysis agent",
  "filter": {
    "conditions": [
      { "field": "skills.tags", "op": "anyOf", "value": ["Mars exploration", "aerospace data"] }
    ]
  }
}
```

**Example response**

```http
HTTP/1.1 200 OK
Content-Type: application/json
X-Trace-Id: trace-20251125-mnop3456
X-Span-Id: span-ds-a-004

{
  "result": {
    "acsMap": {},
    "agents": [],
    "routes": [
      {
        "forwardChain": ["AIC-DS-A"],
        "agentGroups": [],
        "status": "ok",
        "durationMs": 80
      }
    ]
  }
}
```

### 4.3.6. Response Carrying aliveMap (AMP alive Synchronization Enabled)

When the discovery server has enabled the AMP Heartbeat alive synchronization function, the response carries an additional `aliveMap` field, on the basis of which the client can perceive the liveness status of candidate agents. In this example, `AIC-NLP-AGENT-003` is currently marked as inactive by the monitoring system, and the client may lower its priority when selecting.

**Example request**

```http
POST /discover HTTP/1.1
Host: ds-a.example.com
Content-Type: application/json
X-Trace-Id: trace-20251125-qrst7890
X-Span-Id: span-client-005

{
  "type": "explicit",
  "query": "I need a natural language processing agent"
}
```

**Example response**

```http
HTTP/1.1 200 OK
Content-Type: application/json
X-Trace-Id: trace-20251125-qrst7890
X-Span-Id: span-ds-a-005

{
  "result": {
    "acsMap": {
      "AIC-NLP-AGENT-001": {
        "aic": "AIC-NLP-AGENT-001",
        "name": "NLP Agent Alpha",
        "skills": [{ "id": "nlp-001", "name": "Text classification", "tags": ["NLP", "classification"] }]
      },
      "AIC-NLP-AGENT-003": {
        "aic": "AIC-NLP-AGENT-003",
        "name": "NLP Agent Gamma",
        "skills": [{ "id": "nlp-003", "name": "Named entity recognition", "tags": ["NLP", "NER"] }]
      }
    },
    "agents": [
      {
        "group": "Natural language processing",
        "skills": [
          { "aic": "AIC-NLP-AGENT-001", "skillId": "nlp-001", "score": 0.95 },
          { "aic": "AIC-NLP-AGENT-003", "skillId": "nlp-003", "score": 0.82 }
        ]
      }
    ],
    "aliveMap": {
      "AIC-NLP-AGENT-001": {
        "alive": true,
        "aliveLastSeenAt": "2025-11-25T03:41:00Z"
      },
      "AIC-NLP-AGENT-003": {
        "alive": false,
        "aliveLastSeenAt": "2025-11-25T01:12:33Z"
      }
    }
  }
}
```

> **Client consumption note**: the keys of `aliveMap` correspond to the keys of `acsMap`. In this example, `AIC-NLP-AGENT-003` has `alive=false`,
> so the client may preferentially select `AIC-NLP-AGENT-001`, or display an "offline" marker for `AIC-NLP-AGENT-003` in the UI.
> When the `aliveMap` field is absent, the client should treat the liveness status as "unknown", and must not treat the absence as meaning that all are offline.

# 5. Supplementary Notes

The agent discovery process defined in this document fully takes manageability and compatibility into account, and is provided free of charge to the relevant researchers, developers, and institutions for reference. We welcome colleagues in the industry who are engaged in agent research and development and in the formulation of agent interconnection protocols to support and adopt this process definition, so as to form an agent discovery mechanism that is conducive to interconnection and interoperability and to good compatibility.
