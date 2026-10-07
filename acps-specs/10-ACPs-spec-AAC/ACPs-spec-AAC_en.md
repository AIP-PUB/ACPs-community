[Home](../README_en.md)

**[English](ACPs-spec-AAC_en.md) | [中文](ACPs-spec-AAC.md)**

AAC: Agent Access Control (ACPs-spec-AAC-v02.02)

# 1. Document Definition

This document is the standard definition of Agent Access Control (AAC) in the ACPs agent collaboration protocol suite, version v02.02.

The full title of the document is ACPs-spec-AAC-v02.02.

Document authors: Xiaofeng Hu (Beijing University of Posts and Telecommunications), Ke Li (Beijing University of Posts and Telecommunications), Jun Liu (Beijing University of Posts and Telecommunications), Ke Yu (Beijing University of Posts and Telecommunications), Keliang Chen (Beijing University of Posts and Telecommunications), Di Ma (Beijing University of Posts and Telecommunications).

# 2. Introduction to Agent Access Control

For agent interconnection to become a secure and reliable agent system, in addition to confirming the real identities of communicating entities and users through AIA: Agent Identity Authentication, it is also necessary to determine, on every access to a protected resource or invocation of a protected capability, whether the subject is allowed to perform the current operation. The Agent Access Control (AAC) defined in this document is used to solve the problem of "whether an authenticated subject is allowed to access a certain resource or perform a certain action".

The relationship between AAC and AIA is as follows:

(1) AIA defines the identity authentication process and answers questions such as "who is the caller", "who is the user", and "whether the communication peer is trustworthy";

(2) AAC defines the access control process and answers the question "based on the verified identity, delegation chain, resource, action, and policy, whether the current request is allowed to be executed";

(3) The peer AIC, user authentication results, certificate verification results, OIDC/OAuth2 token verification results, and so on produced by AIA can all serve as trusted authorization context sources for AAC;

(4) AAC does not replace AIA, and AIA does not replace AAC. Successful identity authentication does not mean that access control will necessarily grant access.

The core model of AAC is as follows:

```text
Trusted authorization context
  -> authorization decision
  -> enforcement and audit
```

Where:

(1) The trusted authorization context is used to confirm whether the authorization-related facts in a request are trustworthy, such as subject, actor, delegation chain, scope, audience, tenant, resource, action, and so on;

(2) The authorization decision is used to determine allow / deny according to ACL, RBAC, ABAC, ReBAC, OPA, or an equivalent policy model;

(3) Enforcement and audit are used to enforce the decision result before business processing and to record audit events that are traceable but do not leak credentials.

# 3. Term Definitions

| Term | English | Definition |
| --- | --- | --- |
| Agent access control | Agent Access Control, AAC | The specification in ACPs that controls access behavior based on trusted authorization context and policy decisions |
| Verified authorization context | Verified Authorization Context | A verified authorization context containing the subject, actor, delegation chain, resource, action, scope, environment attributes, etc. |
| Context provider | Context Provider | A mechanism that can produce a trusted authorization context, such as mTLS/CAI/AIC, OIDC/OAuth2 token, Token Exchange token, local session, or a trusted resolver |
| Subject | Subject | An entity that can appear in an authorization model; it may be a human, an agent, a service account, an organization, or a tenant |
| Executable subject | Executable Subject | A subject that can initiate or perform operations as `primary_subject` or `immediate_actor`, typically including humans, agents, and service accounts |
| Related subject | Related Subject | A subject that participates in authorization relationships, resource ownership, or scope constraints but usually does not directly initiate requests, such as an organization or a tenant |
| Primary subject | Primary Subject | The subject on whose behalf the current request is executed, that is, "on whose behalf the operation is performed" |
| Immediate actor | Immediate Actor | The direct initiator of the current hop of the request |
| Actor chain | Actor Chain | A verifiable chain from the original actor to the current immediate actor |
| Authorization event | Authorization Event | An event occurring in the authorization chain, such as human consent, approval, secondary authentication, or break-glass |
| Delegation boundary | Delegation Boundary | Constraints allowed during delegation, such as scope, purpose, tenant, Partner, chain depth, and sensitivity level |
| Policy decision point | Policy Decision Point, PDP | A component that returns allow / deny according to the authorization request and policies |
| Policy enforcement point | Policy Enforcement Point, PEP | A component that enforces the authorization decision result before business processing |
| Resource server | Resource Server | A service that receives requests and protects resources or capabilities; both the Leader API and a Partner can act as a Resource Server |
| Security token service | Security Token Service, STS | A service responsible for issuing, exchanging, validating, narrowing, or transforming security tokens |
| Delegation | Delegation | A subject is authorized to perform operations on behalf of another subject but does not pretend to be that subject |
| Impersonation | Impersonation | One subject pretends to be another subject when performing operations. AAC prohibits impersonation by default unless an explicit policy allows it and high-level auditing is performed |

# 4. Overall Access Control Flow

Any request protected by AAC should be processed according to the following flow:

```text
Inbound request
  -> authenticate / validate context providers
  -> build VerifiedAuthorizationContext
  -> resolve resource and action
  -> evaluate policy decision
  -> enforce decision before business handler
  -> audit decision
```

The specific steps are as follows:

(1) Validate the communication identity: for example, the mTLS peer certificate, the TLS server certificate, and the AIP `senderId` binding;

(2) Validate the token or session: for example, the access token signature, issuer, audience, expiry, scope, `act`, and `cnf`;

(3) Build the trusted authorization context: resolve the primary subject, immediate actor, actor chain, authorization events, resource, action, and environment;

(4) Make the authorization decision: ACL, RBAC, ABAC, ReBAC, OPA, or an equivalent policy model returns allow / deny;

(5) Enforce the decision result: only allow may enter the business handler; deny or a context error must terminate before the business logic;

(6) Record audit events: record the subject, actor, resource, action, decision, reason code, and token `jti` or delegation id, but never record the complete token.

The boundaries of AAC are as follows:

```text
Context Providers prove what is true.
Policy Decision decides what is allowed.
Policy Enforcement makes it happen.
```

Therefore, mechanisms such as OAuth 2.0, OIDC, Token Exchange, mTLS, CAI, AIC, and local session only provide trusted context and are not directly equivalent to the final business authorization result. Whether access is ultimately allowed must be determined by the policy decision of the current Resource Server.

# 5. Fail Closed Requirements

AAC must adopt the fail closed principle. A request must be rejected in the following cases:

(1) Required identity authentication credentials are missing;

(2) Validation of the token, certificate, session, or delegation chain fails;

(3) The issuer, audience, expiry, scope, actor binding, or certificate binding does not meet the requirements;

(4) The AIP `senderId` is inconsistent with the authenticated peer AIC;

(5) The resource, action, skillId, taskId, groupId, and so on cannot be resolved;

(6) The PDP is unavailable, the policy file is missing, or the policy format is incorrect, and there is no explicit degradation policy;

(7) The authorization decision result is not an explicit allow.

# 6. Definition of the Trusted Authorization Context

## 6.1 Subject ID Specification

Entities in AAC are divided into executable subjects and related subjects. All authorizable entities should be normalized into subject IDs, but only executable subjects can typically serve as `primary_subject` or `immediate_actor`.

The recommended subject ID forms are as follows:

| Type | Form | Category | Description |
| --- | --- | --- | --- |
| Human | `human:{issuer}#{sub}` | Executable subject | `sub` is unique only within the issuer, so the issuer must be included |
| Agent | `agent:{aic}` | Executable subject | The AIC should use the unified normalization rules agreed upon by the SDK or the protocol |
| Service account | `service:{issuer}#{client_id}` | Executable subject | OAuth client or service account |
| Organization | `org:{org_id}` | Related subject | Can serve as a ReBAC relationship subject, a resource owner, or a policy attribute |
| Tenant | `tenant:{tenant_id}` | Related subject | Can serve as a resource scope, an isolation boundary, or a policy attribute |

Notes:

(1) Humans, agents, and service accounts can serve as executable subjects;

(2) Organizations and tenants usually serve as related subjects and should not by default act as the direct initiator of a request;

(3) A subject ID should not be generated directly from unverified payload fields.

## 6.2 Primary Subject

`primary_subject` indicates on whose behalf the current request is executed.

Examples:

| Scenario | primary_subject |
| --- | --- |
| A human directly accesses the Leader API | `human:{issuer}#{sub}` |
| An agent calls a Partner on its own behalf | `agent:{peer_aic}` |
| An agent calls a Partner over multi hop on behalf of a human | `human:{issuer}#{sub}` |
| An agent calls a Partner over multi hop on behalf of the originating agent | `agent:{originator_aic}` |

## 6.3 Immediate Actor

`immediate_actor` indicates the direct caller of the current hop of the request.

In agent-to-agent calls:

```text
immediate_actor = agent:{mTLS peer AIC}
```

In a human-to-agent HTTP API:

```text
immediate_actor = primary_subject
```

If an OAuth client such as a browser front end, CLI, or service account exists, it may be recorded as a `client_actor` or a context attribute, but it must not override the semantics of `primary_subject` and `immediate_actor`.

## 6.4 Actor Chain

`actor_chain` indicates the verified delegation path.

The recommended order is as follows:

```text
actor_chain = [origin_actor, ..., immediate_actor]
```

Example:

```json
{
  "primary_subject": "human:https://idp.example.com/realms/acps#user-123",
  "actor_chain": [
    "agent:AIC-LEADER-1",
    "agent:AIC-PARTNER-1"
  ],
  "immediate_actor": "agent:AIC-PARTNER-1"
}
```

Constraints:

(1) `actor_chain` must come from verifiable credentials, such as a Token Exchange token, a signed delegation token, or an STS query result;

(2) The Resource Server must not trust an unsigned, unbound, or unverified `agentChain` in the AIP payload;

(3) The last actor in `actor_chain` must be consistent with the currently authenticated `immediate_actor`;

(4) Historical actors can be used for authorization, auditing, and risk control, but cannot automatically grant permissions to the current actor.

## 6.5 Authorization Event

`authorization_events` indicates events occurring in the authorization chain, such as human consent, approval, secondary authentication, organizational authorization, or break-glass.

Example:

```json
{
  "type": "human_consent",
  "subject": "human:https://idp.example.com/realms/acps#user-123",
  "purpose": "export-sensitive-data",
  "scope": ["acps.skill.invoke:data.export"],
  "time": "2026-06-30T10:15:00Z",
  "issuer": "https://sts.example.com"
}
```

Requirements:

(1) An authorization event must be verified through OIDC, OAuth2, signed approval credentials, STS records, or an equivalent trusted mechanism;

(2) An agent must not self-report that "a certain human has consented" through an ordinary business payload;

(3) If the business explicitly switches the primary subject, a new `primary_subject` may be generated; otherwise, a mid-chain consent does not change the `primary_subject`.

## 6.6 AuthorizationRequest

The PDP should not directly parse the raw HTTP request, AIP payload, or token. The PEP should first construct a normalized `AuthorizationRequest` and then hand it to the PDP for the decision. The interface fields below use camelCase; occurrences of `primary_subject`, `immediate_actor`, and similar forms elsewhere in this document are used to express concept names.

The recommended structure is as follows:

```typescript
export interface AuthorizationSubject {
  subjectId: string;
  subjectType: "human" | "agent" | "service";
  roles?: string[];
  scopes?: string[];
  attributes?: Record<string, unknown>;
}

export interface ActorContext {
  immediateActor: AuthorizationSubject;
  actorChain?: string[];
  delegationId?: string;
  authorizationEvents?: Record<string, unknown>[];
}

export interface AuthorizationResource {
  resourceType: string;
  resourceId: string;
  ownerSubject?: string;
  agentAic?: string;
  skillId?: string;
  tenantId?: string;
  attributes?: Record<string, unknown>;
}

export interface AuthorizationRequest {
  primarySubject: AuthorizationSubject;
  actor: ActorContext;
  action: string;
  resource: AuthorizationResource;
  environment?: Record<string, unknown>;
  verifiedContext?: Record<string, unknown>;
}

export interface AuthorizationDecision {
  allowed: boolean;
  reasonCode?: string;
  obligations?: Record<string, unknown>;
}
```

If the PDP returns `obligations`, the PEP must enforce or confirm these additional requirements before entering the business handler; if they cannot be enforced or confirmed, the request should be treated as deny.

## 6.7 Resource Server and Audience Identifier

Every Resource Server protected by AAC must define a stable canonical audience identifier for token `aud` validation and local resource mapping.

The recommended rules are as follows:

(1) The canonical audience of an agent Partner is recommended to use `acps:agent:{normalized_aic}`;

(2) The canonical audience of a non-agent service may use `acps:service:{service_id}` or a stable service identifier configured locally;

(3) `aud` must be equal to the canonical audience of the current Resource Server, or be mapped to that canonical audience through explicit local configuration;

(4) The audience alias mapping must be provided by the local trusted configuration of the Resource Server or by trusted STS metadata, and should be included in the audit log;

(5) URLs, display names, and endpoint names returned by ACS or ADP must not be used alone as the basis for `aud` validation;

(6) `aud` identifies the Resource Server, not a specific Skill. A specific Skill should enter the authorization decision through `acps_skill_id`, `AuthorizationResource.skillId`, or an equivalent resource field.

# 7. Context Providers

## 7.1 mTLS / CAI / AIC

mTLS, CAI, and AIC provide the agent identity context.

Example validation output:

```json
{
  "provider": "mtls-aic",
  "subject": "agent:AIC-PARTNER-1",
  "aic": "AIC-PARTNER-1",
  "certificate_fingerprint": "sha256:...",
  "trust_chain": "acps-ca",
  "validated": true
}
```

The following must be validated:

(1) The certificate chain, validity period, revocation status, and CA trust chain;

(2) The AIC format in the CAI is valid;

(3) The AIC in the certificate Subject `CN` and the `URI:acps://{AIC}` in the `SubjectAlternativeName` must conform to the identity binding extraction rules of AIP; if both are present but inconsistent, the certificate identity must be judged invalid;

(4) When AIP identity binding is enabled, the AIP `senderId` must be equal to the peer AIC.

mTLS / CAI / AIC can be used to construct the `immediate_actor` and can also serve as the agent subject input for ACL, RBAC, ABAC, ReBAC, and OPA.

## 7.2 OIDC ID Token

An OIDC ID Token provides the human login authentication context.

Uses:

(1) The Client or Leader verifies the human identity;

(2) A human login state is established;

(3) A local session is triggered or an access token is exchanged for.

Limitations:

(1) An ID Token should not serve as a Partner API authorization credential;

(2) A Resource Server should not rely only on an ID Token for API authorization;

(3) If an ID Token contains fields such as roles and groups, they should also first be converted into a server-side trusted principal before entering the AAC model.

## 7.3 OAuth 2.0 Access Token

An OAuth 2.0 access token provides the trusted authorization context for accessing a Resource Server.

Example validation output:

```json
{
  "provider": "oauth2-access-token",
  "issuer": "https://idp.example.com/realms/acps",
  "subject": "human:https://idp.example.com/realms/acps#user-123",
  "audience": ["acps:service:leader-api"],
  "scopes": ["task.submit", "task.read"],
  "roles": ["member"],
  "attributes": {
    "tenant_id": "tenant-001"
  },
  "expires_at": "2026-06-30T10:30:00Z"
}
```

The following must be validated:

(1) `iss` is trusted;

(2) `aud` contains the canonical audience or a trusted alias of the current Resource Server;

(3) `exp`, `nbf`, and `iat` are valid;

(4) The signature, introspection, or equivalent validation result is valid;

(5) The revocation status is valid; when introspection is used, `active` or an equivalent status must be valid;

(6) If the token is declared one-time-use, short-lived and highly sensitive, or has replay detection enabled, the Resource Server must reject repeated use based on `jti`, delegation id, or an equivalent unique identifier;

(7) Claims such as `scope`, roles, and tenant are resolved according to the server-side configuration.

The scope, role, tenant, and other fields of an access token are inputs to the authorization decision, not the final authorization result.

## 7.4 OAuth 2.0 Token Exchange and Delegation Token

OAuth 2.0 Token Exchange or an ACPs native delegation token can be used to pass delegation context across agent hops.

It is suitable for solving the following problems:

```text
Is the current actor allowed to call the next-hop resource on behalf of the primary subject?
Is this delegation context intended for the current Partner?
Has the scope been narrowed hop by hop?
Is the actor chain auditable?
```

Token Exchange or a delegation token is not responsible for the final business decision. The receiving Partner must still use the AAC policy model to make the allow / deny decision.

It is recommended to exchange for a token facing the next hop at each hop:

```text
Current Agent -> Authorization Server / STS:
  grant_type = urn:ietf:params:oauth:grant-type:token-exchange
  subject_token = the user or agent delegation token currently held
  audience / resource = the canonical audience of the next-hop Resource Server
  scope = the least privilege required for the next hop
  actor_token = the actor token of the current agent (optional)
  client authentication = the mTLS client authentication of the current agent
```

Token Exchange output can serve as the source of the following information:

(1) primary subject;

(2) actor chain;

(3) authorization events;

(4) scope, audience, expiry, delegation id;

(5) delegation boundary.

Before issuing the next-hop token, the STS must verify that the current actor identity is consistent with the actor binding in the previous-hop token, and confirm that the requested audience, scope, purpose, tenant, and chain depth do not exceed the range allowed by the previous-hop token and the delegation boundary.

## 7.5 Local Session

A local session can serve as a trusted context source within a single service.

Requirements:

(1) The session must be created by a verified OIDC, OAuth2, local authentication, or equivalent trusted process;

(2) The session id must have sufficient randomness and be stored securely;

(3) The principal, roles, tenant, and consent in the session must have clear sources and expiration policies;

(4) If the session is used to access a Partner, it should first be converted into an access token or delegation token facing the Partner; the session id should not be passed through to the Partner.

## 7.6 Subject Resolver

A resolver can query additional subject attributes based on trusted keys.

Example:

```text
peer AIC -> roles / provider / tenant / trustLevel
human issuer+sub -> local user / org membership / subscription
delegation_id -> full actor chain / consent record
```

Requirements:

(1) The resolver's query keys must come from the verified context;

(2) If the resolver fails, the subject does not exist, or the AIC or subject is inconsistent, it must fail closed;

(3) Data returned by the resolver should be marked with its source, version, update time, and cache hit status.

# 8. Authorization Decision Model

AAC does not prescribe a single authorization model. Implementers may choose ACL, RBAC, ABAC, ReBAC, OPA, or an equivalent mechanism according to the scenario. Whichever model is adopted, the decision must be based on the trusted context in the `AuthorizationRequest`.

## 8.1 ACL

ACL is suitable for allow / deny for a small number of explicit subjects.

Example:

```text
allow if immediate_actor.subject_id in resource.allow_subjects
deny if primary_subject.subject_id in resource.deny_subjects
```

ACL can be used for:

(1) A Partner allows certain agent AICs to call it;

(2) A certain project, session, or task allows certain human subjects to access it;

(3) A certain Skill is only allowed to be initiated by a specified actor chain.

## 8.2 RBAC

RBAC makes decisions based on roles.

Example:

```text
allow data.export if
  "data_exporter" in primary_subject.roles
  and "trusted_agent" in immediate_actor.roles
```

RBAC can be used for mapping user roles, agent levels, service account roles, realm roles, or client roles to local roles.

## 8.3 ABAC

ABAC makes decisions based on attributes.

Example:

```text
allow if
  primary_subject.attributes.tenant_id == resource.tenant_id
  and immediate_actor.attributes.trust_level >= resource.attributes.required_trust_level
  and "data.export" in primary_subject.scopes
```

ABAC can be used for conditions such as tenant, organization, region, risk level, token scope, consent, device, time, IP, task sensitivity, agent certificate status, provider, and trustLevel.

## 8.4 ReBAC

ReBAC makes decisions based on relationships.

Example relationships:

```text
human:UserA member_of org:OrgA
org:OrgA subscribed_to package:DataService-Pro
package:DataService-Pro includes skill:data.export
agent:AIC-PARTNER-1 delegated_by human:UserA
agent:AIC-PARTNER-2 owns skill:data.export
```

Example question:

```text
Can agent:AIC-PARTNER-1 can_act_on_behalf_of human:UserA invoke skill:data.export ?
```

ReBAC is suitable for expressing relationships among multiple subjects, organizations, projects, and subscriptions, as well as user-delegated agents, agent cascading delegation, and the relationship between mid-chain approvers and resource owners.

## 8.5 OPA or External PDP

OPA is not a new authorization model, but a policy enforcement engine that carries ACL, RBAC, ABAC, and ReBAC rules.

A Partner can convert an `AuthorizationRequest` into OPA input:

```json
{
  "primarySubject": {
    "id": "human:https://idp.example.com/realms/acps#user-123",
    "type": "human",
    "roles": ["member"],
    "scopes": ["acps.skill.invoke:data.export"],
    "attributes": {
      "tenantId": "tenant-001"
    }
  },
  "actor": {
    "immediateActor": {
      "id": "agent:AIC-PARTNER-1",
      "type": "agent"
    },
    "actorChain": ["agent:AIC-LEADER-1", "agent:AIC-PARTNER-1"],
    "delegationId": "dlg-123"
  },
  "action": "task.start",
  "resource": {
    "agentAic": "AIC-PARTNER-2",
    "skillId": "data.export",
    "tenantId": "tenant-001"
  },
  "environment": {
    "transport": "aip.direct",
    "time": "2026-06-30T10:15:00Z"
  }
}
```

OPA or an external PDP should return allow / deny / reason. The PEP only enforces the result and should not leak complete policy details to the requester.

## 8.6 Boundaries of ACS and ADP

The degree of capability openness in ACS, Discovery query results, or candidate lists do not constitute the final runtime authorization.

Requirements:

(1) ACS may declare the static degree of openness of a capability;

(2) ADP may perform discovery filtering according to the static degree of openness and query conditions;

(3) ADP should not replace the Partner in making the final authorization decision;

(4) Regardless of whether a Partner or Skill is returned through discovery, the receiving party must re-execute the AAC authorization process when an AIP call is made;

(5) Static visibility fields such as `public`, `restricted`, and `private` must not be interpreted as necessarily allowed or necessarily denied at runtime.

# 9. Communication Profiles

AAC unifies human, agent, single hop, and multi hop scenarios into different context construction profiles, rather than defining mutually incompatible authorization mechanisms.

## 9.1 Profile A: Human to Agent Single Hop

```text
Human -> Leader / Agent API
```

Trusted context sources:

(1) OIDC ID Token: used for login authentication;

(2) OAuth 2.0 access token or local session: used for the API authorization context.

Context construction:

```text
primary_subject = human:{issuer}#{sub}
immediate_actor = primary_subject
actor_chain = []
resource = Leader API / session / task / skill request
action = HTTP route / application action
```

Decision requirements:

The Leader should use ACL, RBAC, ABAC, ReBAC, OPA, or an equivalent policy to determine whether the human can access the API, session, task, Partner selection, or high-risk capability. OAuth scope or role can only serve as decision input and should not serve as the complete decision.

## 9.2 Profile B: Agent to Agent Single Hop

```text
Agent1 -> Partner2
```

Trusted context sources:

(1) mTLS / CAI / AIC;

(2) AIP `senderId == peer AIC` identity binding;

(3) Optionally, a resolver queries roles, attributes, or relationships according to the AIC.

Context construction:

```text
primary_subject = agent:{peer_aic}
immediate_actor = agent:{peer_aic}
actor_chain = [agent:{peer_aic}]
resource = Partner / Skill
action = AIP action
```

Decision requirements:

The Partner should use AIC-ACL, RBAC, ABAC, ReBAC, OPA, or an equivalent policy to determine whether the peer AIC is allowed access. A pure single hop agent call does not mandate the use of OAuth 2.0 Token Exchange.

## 9.3 Profile C: Human-Initiated Multi Hop Delegation

```text
Human -> Leader1 -> Partner1 -> ... -> PartnerN
```

Trusted context sources:

(1) Entry OIDC / OAuth2: authenticates the human and establishes the initial access token;

(2) mTLS / AIC at each hop: authenticates the current immediate actor;

(3) OAuth 2.0 Token Exchange or an equivalent delegation token: carries the primary subject, actor chain, scope, audience, and expiry.

Context construction:

```text
primary_subject = human:{issuer}#{sub}
immediate_actor = agent:{current_peer_aic}
actor_chain = [agent:AIC-LEADER-1, ..., agent:{current_peer_aic}]
resource = current Partner / Skill
action = AIP action
```

Decision requirements:

The current Partner should determine simultaneously:

(1) whether the immediate actor is allowed to call the current Partner or Skill;

(2) whether the immediate actor is allowed to act on behalf of the primary subject;

(3) whether the primary subject is allowed to access the target resource;

(4) whether the actor chain, scope, tenant, consent, and risk satisfy the policy.

## 9.4 Profile D: Agent-Initiated Multi Hop Delegation

```text
Agent0 -> Agent1 -> Agent2 -> ... -> PartnerN
```

Trusted context sources:

(1) The originating agent's mTLS / AIC or the initial delegation token;

(2) mTLS / AIC at each hop;

(3) Token Exchange or an ACPs native delegation token.

Context construction:

```text
primary_subject = agent:{originator_aic}
immediate_actor = agent:{current_peer_aic}
actor_chain = [agent:{originator_aic}, ..., agent:{current_peer_aic}]
resource = current Partner / Skill
action = AIP action
```

Decision requirements:

The current Partner should determine:

(1) whether the immediate actor can access the current Partner;

(2) whether the immediate actor can act on behalf of the originator;

(3) whether the originator has the right to access the target Skill;

(4) whether the Skill allows secondary or repeated re-delegation;

(5) whether the chain depth is within the allowed range.

## 9.5 Mid-Chain Human Authorization Event

A human may appear in the middle of a multi hop chain, but must not appear as an unverified payload field.

Example:

```text
Partner1 requires consent for sensitive data export
  -> trigger the User or Approver to complete OIDC / MFA / consent
  -> the Authorization Server / STS generates a new authorization event
  -> the subsequent Token Exchange token carries the event reference
```

The PDP may make the decision based on the following condition:

```text
authorization_events contains human_consent for purpose=export-sensitive-data
```

# 10. Delegation Modes and Token Profiles

## 10.1 Delegation Chain Modes

AAC supports two modes: fixed-chain delegation and dynamic next-hop delegation.

| Mode | Description | Applicable scenario |
| --- | --- | --- |
| Fixed chain | The initial authorization context explicitly specifies the subsequent actor / Partner path, and at runtime the chain may only continue along the specified path | High sensitivity, strong compliance, designated providers, designated processing paths |
| Dynamic next hop | The initial authorization context provides boundary constraints, the current node may choose the next hop within the boundary, and the STS makes the decision hop by hop | Autonomous agent planning, capability discovery, task decomposition, multi-Partner collaboration |

The recommended default mode is:

```text
Dynamic next hop + boundary constraints + hop-by-hop decision
```

In the dynamic next-hop mode, the current node may choose the next hop according to task requirements, but may not unilaterally expand the scope, audience, tenant, purpose, chain depth, or data sensitivity level. The current node must generate context facing the next hop through the STS, Token Exchange, or an equivalent trusted mechanism.

## 10.2 Delegation Token Required Fields

When a JWT access token or an ACPs delegation token is used to carry a multi-subject delegation context, it must contain the following fields. If an opaque token is used, the receiving party or the STS must be able to obtain equivalent information for these fields through introspection, a resolver, or an equivalent trusted mechanism.

| Field | Semantics |
| --- | --- |
| `iss` | token issuer / Authorization Server / STS |
| `sub` | primary subject; may be a human or an agent |
| `aud` | The current target Resource Server / Partner; must match the canonical audience or a trusted alias defined in Section 6.7 |
| `exp` | Expiration time |
| `iat` | Issuance time |
| `jti` | Unique token ID, used for auditing, revocation, and replay detection |
| `scope` | The least scope authorized by the current token |
| `act` | The current actor; the outermost actor must be resolvable to the immediate actor |

## 10.3 Delegation Token Recommended Fields

| Field | Semantics |
| --- | --- |
| `nbf` | Effective time |
| `azp` / `client_id` | The OAuth client that requested the token |
| `cnf` | Binding information of the certificate-bound access token |
| `acps_subject_type` | `human` / `agent` / `service` |
| `acps_target_aic` | The AIC of the current target Partner |
| `acps_skill_id` | The Skill authorized by the current token |
| `acps_delegation_id` | Delegation chain ID |
| `acps_delegation_mode` | `fixed` / `dynamic` |
| `acps_boundary_id` | The boundary constraint ID of the dynamic chain |
| `acps_boundary_hash` | A digest of the boundary constraints, preventing the boundary returned by the resolver from being replaced |
| `acps_chain_depth` | The current chain depth |
| `acps_max_chain_depth` | The maximum chain depth allowed by the current delegation context |
| `acps_allowed_route` | The allowed actor / Partner path in fixed-chain mode |
| `acps_allowed_partner_aics` | The set of next-hop Partner AICs that may be selected in dynamic-chain mode |
| `acps_allowed_partner_categories` | The next-hop capability categories that may be selected in dynamic-chain mode |
| `acps_purpose` | The purpose of the current delegation |
| `acps_authorization_events` | References to or digests of consent / approval / step-up |

## 10.4 `act` Claim

It is recommended to use the `act` claim of RFC 8693 to express the actor.

Example:

```json
{
  "sub": "human:https://idp.example.com/realms/acps#user-123",
  "aud": "acps:agent:AIC-PARTNER-2",
  "scope": "acps.skill.invoke:data.export",
  "act": {
    "sub": "agent:AIC-PARTNER-1",
    "aic": "AIC-PARTNER-1",
    "act": {
      "sub": "agent:AIC-LEADER-1",
      "aic": "AIC-LEADER-1"
    }
  },
  "acps_skill_id": "data.export",
  "acps_delegation_id": "dlg-123"
}
```

Constraints:

(1) The outermost `act` must be consistent with the mTLS peer AIC;

(2) An inner `act` is a historical actor, mainly used for the authorization context, risk control, and auditing;

(3) When the chain is too long, only `acps_delegation_id` may be included, with the complete chain queried from the STS or a resolver.

## 10.5 Audience and Scope Narrowing

Each hop of Token Exchange must narrow or at least not expand. The following `scope <= subject_token.scope` is interpreted with set semantics, meaning that the scope of the new token must be a subset of or equal to the scope of the original token:

```text
new_token.aud = the canonical audience of the next-hop Resource Server
new_token.scope <= subject_token.scope
new_token.exp <= subject_token.exp
new_token.chain_depth = subject_token.chain_depth + 1
new_token.jti = a new unique ID
```

A token issued to Partner1 must not be used directly for Partner2.

## 10.6 Fixed-Chain Constraints

A fixed-chain token must allow the receiving party or the STS to determine whether the current actor is on the expected path.

Example:

```json
{
  "acps_delegation_mode": "fixed",
  "acps_allowed_route": [
    "agent:AIC-LEADER-1",
    "agent:AIC-PARTNER-1",
    "agent:AIC-PARTNER-2"
  ],
  "aud": "acps:agent:AIC-PARTNER-2"
}
```

Requirements:

(1) `actor_chain` must be a prefix of `acps_allowed_route` or aligned with the current hop;

(2) The current `aud` must match the canonical audience or trusted alias of the next-hop Resource Server in the fixed path;

(3) The current actor must not skip intermediate nodes in the fixed path;

(4) When any node on the fixed path changes, the authorization context must be obtained again.

## 10.7 Dynamic Next-Hop Constraints

A dynamic-chain token does not fix the complete path but carries or references boundary constraints.

Example:

```json
{
  "acps_delegation_mode": "dynamic",
  "acps_boundary_id": "boundary-123",
  "acps_boundary_hash": "sha256:...",
  "acps_purpose": "report.generate",
  "acps_chain_depth": 2,
  "acps_max_chain_depth": 3,
  "acps_allowed_partner_categories": ["ocr", "data-analysis"],
  "scope": "acps.skill.invoke:ocr.extract",
  "aud": "acps:agent:AIC-PARTNER-2"
}
```

Requirements:

(1) At each Token Exchange, the STS must decide the requested audience, scope, purpose, tenant, and chain depth according to the boundary constraints, where the requested audience must be resolvable to the canonical audience of the next-hop Resource Server;

(2) The receiving Partner may validate only the current token, or may query the boundary through `acps_boundary_id` for local ABAC, ReBAC, or OPA decisions;

(3) `acps_chain_depth` must not exceed `acps_max_chain_depth`;

(4) `scope` must not exceed the maximum scope allowed by the boundary;

(5) If a request triggers consent, approval, or step-up conditions, the STS must require a new authorization event and must not silently issue the next-hop token.

# 11. AIP Carriage and Enforcement Rules

## 11.1 Direct / Stream / Notification Start

User delegation or agent delegation tokens are recommended to be carried through the HTTP `Authorization` header:

```http
Authorization: Bearer <access-token-or-delegation-token>
```

Requirements:

(1) The token should not be placed in the prompt, Products, `commandParams`, or an ordinary business payload;

(2) The SDK and logging systems must mask the `Authorization` header by default;

(3) A Stream reconnection must carry a valid token again;

(4) When the token expires, the caller should first refresh it or perform Token Exchange;

(5) If a Notification callback needs to access a protected resource of the receiving party, it should also use a token facing the callback receiver.

## 11.2 Group / MQ

A bearer token facing a single recipient must not be carried in an ordinary Group broadcast message.

Reasons:

(1) Broadcasting a bearer token intended for PartnerA to PartnerB breaks the audience and least-exposure principles;

(2) An MQ message may be consumed by multiple members, so the risk of bearer token leakage is high.

If Group user delegation is supported in the future, per-recipient encrypted envelopes, per-recipient inbox tokens, a restricted group token with aud=group, or exchange by the consumer into a token with aud=its own Partner should be designed separately. Without these mechanisms, Group / MQ messages must not place bearer tokens in the message body or ordinary message attributes.

## 11.3 Partner / Resource Server Validation Order

After receiving a protected request, the PEP should execute in order:

```text
1. Validate the transport-layer identity:
   - Agent-to-Agent: mTLS peer certificate -> peer AIC
   - Human-to-Agent: HTTPS + session / access token

2. Validate the protocol identity binding:
   - AIP senderId == peer AIC
   - TLS server AIC == expected callee AIC

3. Validate the token / session:
   - issuer / signature / introspection
   - canonical audience / expiry / scope
   - revocation / active status
   - jti / replay status (if enabled)
   - act binding to the peer AIC
   - cnf binding to the mTLS client certificate
   - in fixed-chain mode, alignment of the current hop with the allowed route
   - in dynamic-chain mode, validity of the delegation boundary id / hash / chain depth

4. Resolve the resource and action:
   - action
   - skillId
   - taskId / groupId / notificationConfigId
   - tenant / owner / sensitivity

5. Construct the AuthorizationRequest.

6. Call the PDP.

7. Only allow enters the business handler.

8. deny or error returns a stable error and records an audit event.
```

# 12. Error Mapping

AAC follows the authentication and authorization errors defined in AIP.

| Case | Error code |
| --- | --- |
| Required authentication credentials are missing, such as the mTLS peer certificate, Bearer token, or session | `-32008 AuthenticationRequiredError` |
| The peer certificate is invalid or the AIC cannot be resolved | `-32008 AuthenticationRequiredError` |
| AIP `senderId != peer AIC` | `-32009 AuthorizationFailedError` |
| The token format is invalid, the signature is invalid, the token is not yet valid, has expired, has been revoked, or the issuer is untrusted | `-32010 AccessTokenInvalidError` |
| The token `aud` does not contain the canonical audience or a trusted alias of the current Resource Server | `-32010 AccessTokenInvalidError` |
| The token `jti` has been replayed, or a one-time token has been used repeatedly | `-32010 AccessTokenInvalidError` |
| The token `act` is inconsistent with the mTLS peer AIC | `-32009 AuthorizationFailedError` |
| The token `cnf` does not match the current mTLS certificate | `-32009 AuthorizationFailedError` |
| The context is trusted but the policy does not allow it | `-32009 AuthorizationFailedError` |
| The resource / skillId explicitly does not exist or is not accessible | `-32009 AuthorizationFailedError` |
| The PDP is unavailable and there is no explicit degradation policy | `-32009 AuthorizationFailedError` |

Externally visible error messages must not leak specific policies, lists, roles, relationship chains, or token claims. Detailed reasons should be recorded in the audit log.

The `message` in an AIP JSON-RPC response should be consistent with the AIP error table, for example `Authentication required`, `Authorization failed`, and `Invalid access token`. Implementers may record a more detailed `reason_code` in internal logs.

# 13. Audit Requirements

An audit event should be recorded for every authorization decision.

Example audit event:

```json
{
  "event": "authorization_decision",
  "decision": "deny",
  "reason_code": "scope_not_allowed",
  "primary_subject": "human:https://idp.example.com/realms/acps#hash:user-123",
  "immediate_actor": "agent:AIC-PARTNER-1",
  "actor_chain": ["agent:AIC-LEADER-1", "agent:AIC-PARTNER-1"],
  "resource": {
    "agent_aic": "AIC-PARTNER-2",
    "skill_id": "data.export",
    "tenant_id": "tenant-001"
  },
  "action": "task.start",
  "context_providers": ["mtls-aic", "oauth2-token-exchange"],
  "token": {
    "issuer": "https://sts.example.com",
    "audience": "acps:agent:AIC-PARTNER-2",
    "jti": "jti-123",
    "delegation_id": "dlg-123"
  }
}
```

The audit constraints are as follows:

(1) Complete access tokens, refresh tokens, and ID Tokens must not be recorded;

(2) A human subject should preferably be hashed or use a pairwise subject;

(3) The issuer, audience, jti, delegation id, actor chain, resource, action, decision, and reason code should be recorded;

(4) High-risk events include impersonation, excessive chain depth, audience mismatch, actor mismatch, token replay, and break-glass;

(5) Audit logs should satisfy integrity protection, access control, and retention period requirements.

# 14. Security Requirements

## 14.1 Least Privilege

(1) The token scope must be minimized;

(2) A new token after Token Exchange must not expand the audience, scope, or validity period;

(3) Highly sensitive Skills should use short-lived tokens, one-time tokens, or replay detection;

(4) Refresh tokens must not be passed through to a Partner and must not be written into AIP messages, MQ messages, Products, prompts, or logs.

## 14.2 Token Binding

Delegation tokens for Agent-to-Agent are recommended to use mTLS certificate-bound access tokens.

When a token contains the `cnf` claim:

(1) The Authorization Server or STS must bind the token to the agent certificate that will hold and present the token;

(2) The Partner must validate that `cnf` matches the current mTLS peer certificate;

(3) After the token is stolen, it cannot be directly replayed by another agent.

## 14.3 Token Revocation and Replay Protection

A Resource Server must perform revocation and replay protection according to the token type and risk level:

(1) A revocable token must be confirmed as not revoked through introspection, a revocation list, short-term cache invalidation, or an equivalent mechanism;

(2) A one-time token, a short-lived highly sensitive token, or a delegation token with replay detection enabled must record the `jti`, delegation id, or an equivalent unique identifier, and must reject repeated use within the validity period;

(3) The token validation cache must not exceed the token `exp`, nor exceed the maximum cache time allowed by the local revocation policy;

(4) When replay, use of a revoked token, audience mismatch, or actor binding mismatch is detected, a high-risk audit event must be recorded.

## 14.4 Impersonation Prohibited by Default

AAC supports only delegation by default, not impersonation.

```text
Allowed: an agent calls downstream on behalf of the primary subject with authorization.
Prohibited: an agent pretends to be another agent or a human.
```

If the business must support impersonation:

(1) There must be an explicit policy;

(2) The token or context must clearly mark impersonation;

(3) The PDP must be able to distinguish delegation from impersonation;

(4) The audit level must be higher than that of ordinary delegation.

## 14.5 Do Not Trust Self-Reported Fields

The following fields must not be used directly as trusted authorization input:

(1) The `userId`, `username`, and `role` self-reported in the AIP payload;

(2) User attributes or the agent chain self-reported in `commandParams`;

(3) Identity descriptions in the prompt or task text;

(4) An unverified ID Token;

(5) An access token whose `aud` does not contain the current Resource Server;

(6) An unsigned, unbound, or unverified delegation record.

Trusted input can only come from:

(1) A verified certificate;

(2) A verified token;

(3) A verified session;

(4) A verified STS or resolver query result;

(5) Resource state and relationship data maintained by the current system itself.

# 15. Supplementary Notes

The agent access control process defined in this document, together with AIA: Agent Identity Authentication, AIP: Agent Interaction Protocol, ACS: Agent Capability Specification, and ADP: Agent Discovery Process, forms the foundation of secure collaboration in ACPs.

The basic principle of AAC is: authentication produces a trusted identity context, authorization makes a policy decision based on the trusted context, and the enforcement point enforces the decision result before business processing. Any identity, role, user, or delegation chain information that is unverified, unbound, unsigned, or merely self-reported by the request payload must not be used directly as the basis for authorization.
