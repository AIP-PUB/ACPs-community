[首页](../README.md)

AAC：智能体访问控制（ACPs-spec-AAC-v02.02）

# 1. 文档定义

本文档为 ACPs 智能体协作协议体系中的智能体访问控制（Agent Access Control，AAC）标准定义，版本号 v02.02。

文档全称为 ACPs-spec-AAC-v02.02。

文档编写者：胡晓峰（北京邮电大学），李珂（北京邮电大学），刘军（北京邮电大学），禹可（北京邮电大学），陈科良（北京邮电大学），马镝（北京邮电大学）。

# 2. 智能体访问控制介绍

智能体互联要能成为一个安全可靠的智能体系统，除了需要通过《AIA：智能体身份认证》确认通信实体和用户的真实身份，还需要在每次访问受保护资源或调用受保护能力时判断该主体是否被允许执行当前操作。本文档定义的智能体访问控制（Agent Access Control，AAC）用于解决“已认证主体是否被允许访问某一资源或执行某一动作”的问题。

AAC 与 AIA 的关系如下：

（1）AIA 定义身份认证流程，回答“调用方是谁”“用户是谁”“通信对端是否可信”等问题；

（2）AAC 定义访问控制流程，回答“基于已验证身份、委托链、资源、动作和策略，当前请求是否允许执行”的问题；

（3）AIA 产生的对端 AIC、用户认证结果、证书验证结果、OIDC/OAuth2 token 验证结果等，均可作为 AAC 的可信授权上下文来源；

（4）AAC 不替代 AIA，AIA 也不替代 AAC。身份认证成功不代表访问控制必然放行。

AAC 的核心模型如下：

```text
可信授权上下文
  -> 授权裁决
  -> 执行与审计
```

其中：

（1）可信授权上下文用于确认请求中与授权有关的事实是否可信，例如主体、actor、委托链、scope、audience、tenant、resource、action 等；

（2）授权裁决用于根据 ACL、RBAC、ABAC、ReBAC、OPA 或等价策略模型判断 allow / deny；

（3）执行与审计用于在业务处理前强制执行裁决结果，并记录可追踪但不泄露凭据的审计事件。

# 3. 术语定义

| 术语 | 英文 | 定义 |
| --- | --- | --- |
| 智能体访问控制 | Agent Access Control，AAC | ACPs 中基于可信授权上下文和策略裁决控制访问行为的规范 |
| 可信授权上下文 | Verified Authorization Context | 经过验证的授权上下文，包含主体、actor、委托链、资源、动作、scope、环境属性等 |
| 上下文提供者 | Context Provider | 可产生可信授权上下文的机制，例如 mTLS/CAI/AIC、OIDC/OAuth2 token、Token Exchange token、本地 session、可信 resolver |
| 主体 | Subject | 可出现在授权模型中的实体，可以是真人、Agent、服务账号、组织或租户 |
| 可执行主体 | Executable Subject | 能作为 `primary_subject` 或 `immediate_actor` 发起或代表执行操作的主体，通常包括真人、Agent、服务账号 |
| 关联主体 | Related Subject | 参与授权关系、资源归属或范围约束但通常不直接发起请求的主体，例如组织、租户 |
| 代表主体 | Primary Subject | 当前请求所代表的主体，即“代表谁执行” |
| 直接执行者 | Immediate Actor | 当前这一跳请求的直接发起方 |
| 执行链 | Actor Chain | 从原始执行者到当前直接执行者的可验证链路 |
| 授权事件 | Authorization Event | 授权链路中出现的真人同意、审批、二次认证、break-glass 等事件 |
| 授权边界 | Delegation Boundary | 委托过程中允许的 scope、purpose、tenant、Partner、链路深度、敏感等级等约束 |
| 策略决策点 | Policy Decision Point，PDP | 根据授权请求和策略返回 allow / deny 的组件 |
| 策略执行点 | Policy Enforcement Point，PEP | 在业务处理前执行授权裁决结果的组件 |
| 资源服务器 | Resource Server | 接收请求并保护资源或能力的服务，Leader API 和 Partner 均可作为 Resource Server |
| 安全令牌服务 | Security Token Service，STS | 负责签发、交换、验证、收窄或转换安全 token 的服务 |
| 委托 | Delegation | 某主体被授权代表另一个主体执行操作，但不伪装成该主体 |
| 冒充 | Impersonation | 一个主体伪装成另一个主体执行操作。AAC 默认禁止冒充，除非有显式策略允许并进行高等级审计 |

# 4. 访问控制总体流程

任何受 AAC 保护的请求都应按照以下流程处理：

```text
Inbound request
  -> authenticate / validate context providers
  -> build VerifiedAuthorizationContext
  -> resolve resource and action
  -> evaluate policy decision
  -> enforce decision before business handler
  -> audit decision
```

具体步骤如下：

（1）验证通信身份：例如 mTLS peer certificate、TLS server certificate、AIP `senderId` 绑定；

（2）验证 token 或 session：例如 access token 签名、issuer、audience、expiry、scope、`act`、`cnf`；

（3）构造可信授权上下文：解析 primary subject、immediate actor、actor chain、authorization events、resource、action、environment；

（4）做授权裁决：由 ACL、RBAC、ABAC、ReBAC、OPA 或等价策略模型返回 allow / deny；

（5）执行裁决结果：只有 allow 才能进入业务 handler，deny 或上下文错误必须在业务逻辑前终止；

（6）记录审计事件：记录主体、actor、资源、动作、决策、原因码、token `jti` 或 delegation id，但不得记录完整 token。

AAC 的边界如下：

```text
Context Providers prove what is true.
Policy Decision decides what is allowed.
Policy Enforcement makes it happen.
```

因此，OAuth 2.0、OIDC、Token Exchange、mTLS、CAI、AIC、本地 session 等机制只提供可信上下文，不直接等同于最终业务授权结果。最终是否允许访问必须由当前 Resource Server 的策略裁决决定。

# 5. Fail Closed 要求

AAC 必须采用 fail closed 原则。以下情况必须拒绝请求：

（1）必需的身份认证凭据缺失；

（2）token、证书、session、delegation chain 验证失败；

（3）issuer、audience、expiry、scope、actor binding、certificate binding 不满足要求；

（4）AIP `senderId` 与已认证 peer AIC 不一致；

（5）resource、action、skillId、taskId、groupId 等无法解析；

（6）PDP 不可用、策略文件缺失或策略格式错误，且没有显式降级策略；

（7）授权裁决结果不是明确 allow。

# 6. 可信授权上下文定义

## 6.1 Subject ID 规范

AAC 中的实体分为可执行主体和关联主体。所有可授权实体都应被规范化为 subject ID，但只有可执行主体通常可作为 `primary_subject` 或 `immediate_actor`。

推荐 subject ID 形态如下：

| 类型 | 形态 | 分类 | 说明 |
| --- | --- | --- | --- |
| 真人 | `human:{issuer}#{sub}` | 可执行主体 | `sub` 只在 issuer 内唯一，必须带 issuer |
| Agent | `agent:{aic}` | 可执行主体 | AIC 应使用 SDK 或协议约定的统一 normalization 规则 |
| 服务账号 | `service:{issuer}#{client_id}` | 可执行主体 | OAuth client 或 service account |
| 组织 | `org:{org_id}` | 关联主体 | 可作为 ReBAC 关系主体、资源 owner 或策略属性 |
| 租户 | `tenant:{tenant_id}` | 关联主体 | 可作为资源范围、隔离边界或策略属性 |

说明：

（1）真人、Agent 和服务账号可以作为可执行主体；

（2）组织和租户通常作为关联主体，不应默认作为直接请求发起方；

（3）subject ID 不应直接使用未经验证的 payload 字段生成。

## 6.2 Primary Subject

`primary_subject` 表示当前请求代表谁执行。

示例：

| 场景 | primary_subject |
| --- | --- |
| 真人直接访问 Leader API | `human:{issuer}#{sub}` |
| Agent 自己调用 Partner | `agent:{peer_aic}` |
| Agent 代表真人多跳调用 Partner | `human:{issuer}#{sub}` |
| Agent 代表原始 Agent 多跳调用 Partner | `agent:{originator_aic}` |

## 6.3 Immediate Actor

`immediate_actor` 表示当前这一跳请求的直接调用方。

Agent 到 Agent 调用中：

```text
immediate_actor = agent:{mTLS peer AIC}
```

真人到 Agent 的 HTTP API 中：

```text
immediate_actor = primary_subject
```

如果存在浏览器前端、CLI、service account 等 OAuth client，可将其作为 `client_actor` 或上下文属性记录，但不得覆盖 `primary_subject` 与 `immediate_actor` 的语义。

## 6.4 Actor Chain

`actor_chain` 表示经过验证的委托路径。

推荐顺序如下：

```text
actor_chain = [origin_actor, ..., immediate_actor]
```

示例：

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

约束：

（1）`actor_chain` 必须来自可验证凭据，例如 Token Exchange token、签名 delegation token、STS 查询结果；

（2）Resource Server 不得信任 AIP payload 中未签名、未绑定、未验证的 `agentChain`；

（3）`actor_chain` 的最后一个 actor 必须与当前已认证的 `immediate_actor` 一致；

（4）历史 actor 可用于授权、审计和风控，但不能自动赋予当前 actor 权限。

## 6.5 Authorization Event

`authorization_events` 表示授权链路中出现的真人同意、审批、二次认证、组织授权、break-glass 等事件。

示例：

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

要求：

（1）authorization event 必须通过 OIDC、OAuth2、签名审批凭据、STS 记录或等价可信机制验证；

（2）Agent 不得通过普通业务 payload 自报“某真人已同意”；

（3）如业务明确切换代表主体，可以生成新的 `primary_subject`；否则中途 consent 不改变 `primary_subject`。

## 6.6 AuthorizationRequest

PDP 不应直接解析原始 HTTP request、AIP payload 或 token。PEP 应先构造规范化后的 `AuthorizationRequest`，再交给 PDP 做裁决。以下接口字段采用 camelCase；本文其他位置出现的 `primary_subject`、`immediate_actor` 等写法用于表达概念名称。

推荐结构如下：

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

若 PDP 返回 `obligations`，PEP 必须在进入业务 handler 前执行或确认这些附加要求；无法执行或无法确认时，应按 deny 处理。

## 6.7 Resource Server 与 Audience 标识

每个受 AAC 保护的 Resource Server 都必须定义稳定的 canonical audience 标识，用于 token `aud` 校验和本地资源映射。

推荐规则如下：

（1）Agent Partner 的 canonical audience 推荐使用 `acps:agent:{normalized_aic}`；

（2）非 Agent 服务的 canonical audience 可使用 `acps:service:{service_id}` 或本地配置的稳定服务标识；

（3）`aud` 必须等于当前 Resource Server 的 canonical audience，或通过本地显式配置映射到该 canonical audience；

（4）audience alias 映射必须由 Resource Server 本地可信配置或 STS 可信元数据提供，并应进入审计日志；

（5）ACS、ADP 返回的 URL、展示名称、endpoint 名称不得单独作为 `aud` 校验依据；

（6）`aud` 标识 Resource Server，不标识具体 Skill。具体 Skill 应通过 `acps_skill_id`、`AuthorizationResource.skillId` 或等价资源字段进入授权裁决。

# 7. 上下文提供者

## 7.1 mTLS / CAI / AIC

mTLS、CAI、AIC 提供 Agent 身份上下文。

验证输出示例：

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

必须校验：

（1）证书链、有效期、吊销状态、CA 信任链；

（2）CAI 中 AIC 格式合法；

（3）证书 Subject `CN` 中的 AIC 与 `SubjectAlternativeName` 中的 `URI:acps://{AIC}` 必须符合 AIP 身份绑定提取规则；若二者同时存在但不一致，必须判定证书身份无效；

（4）启用 AIP 身份绑定时，AIP `senderId` 必须等于 peer AIC。

mTLS / CAI / AIC 可用于构造 `immediate_actor`，也可作为 ACL、RBAC、ABAC、ReBAC、OPA 的 Agent subject 输入。

## 7.2 OIDC ID Token

OIDC ID Token 提供真人登录认证上下文。

用途：

（1）Client 或 Leader 验证真人身份；

（2）建立真人登录态；

（3）触发本地 session 或换取 access token。

限制：

（1）ID Token 不应作为 Partner API 授权凭据；

（2）Resource Server 不应只依赖 ID Token 做 API 授权；

（3）若 ID Token 中包含 roles、groups 等字段，也应先转换为服务端可信 principal，再进入 AAC 模型。

## 7.3 OAuth 2.0 Access Token

OAuth 2.0 access token 提供访问 Resource Server 的可信授权上下文。

验证输出示例：

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

必须校验：

（1）`iss` 可信；

（2）`aud` 包含当前 Resource Server 的 canonical audience 或可信 alias；

（3）`exp`、`nbf`、`iat` 合法；

（4）签名、introspection 或等价验证结果有效；

（5）撤销状态有效；使用 introspection 时，`active` 或等价状态必须为有效；

（6）若 token 声明为一次性、短期高敏或启用 replay 检测，Resource Server 必须基于 `jti`、delegation id 或等价唯一标识拒绝重复使用；

（7）`scope`、roles、tenant 等 claims 按服务端配置解析。

access token 的 scope、role、tenant 等字段是授权裁决输入，不是最终授权结果。

## 7.4 OAuth 2.0 Token Exchange 与 Delegation Token

OAuth 2.0 Token Exchange 或 ACPs 原生 delegation token 可用于跨 Agent 跳转传递委托上下文。

它适合解决以下问题：

```text
当前 actor 是否被允许代表 primary subject 调用下一跳 resource？
这个委托上下文是否面向当前 Partner？
scope 是否已逐跳收窄？
actor chain 是否可审计？
```

Token Exchange 或 delegation token 不负责最终业务裁决。接收方 Partner 仍必须使用 AAC 策略模型做 allow / deny。

推荐每一跳换取面向下一跳的 token：

```text
Current Agent -> Authorization Server / STS:
  grant_type = urn:ietf:params:oauth:grant-type:token-exchange
  subject_token = 当前持有的用户或 Agent 委托 token
  audience / resource = 下一跳 Resource Server 的 canonical audience
  scope = 下一跳所需最小权限
  actor_token = 当前 Agent 的 actor token（可选）
  client authentication = 当前 Agent 的 mTLS client authentication
```

Token Exchange 输出可作为以下信息来源：

（1）primary subject；

（2）actor chain；

（3）authorization events；

（4）scope、audience、expiry、delegation id；

（5）delegation boundary。

STS 在签发下一跳 token 前必须验证当前 actor 身份与上一跳 token 中的 actor binding 一致，并确认 requested audience、scope、purpose、tenant、chain depth 没有超出上一跳 token 和 delegation boundary 所允许的范围。

## 7.5 Local Session

本地 session 可作为单服务内的可信上下文来源。

要求：

（1）session 必须由已验证的 OIDC、OAuth2、本地认证或等价可信流程创建；

（2）session id 必须具备足够随机性并安全存储；

（3）session 中的 principal、roles、tenant、consent 必须有明确来源和过期策略；

（4）若 session 用于访问 Partner，应先转换成面向 Partner 的 access token 或 delegation token，不应把 session id 透传给 Partner。

## 7.6 Subject Resolver

resolver 可根据可信键查询更多主体属性。

示例：

```text
peer AIC -> roles / provider / tenant / trustLevel
human issuer+sub -> local user / org membership / subscription
delegation_id -> full actor chain / consent record
```

要求：

（1）resolver 的查询键必须来自已验证上下文；

（2）resolver 失败、主体不存在、AIC 或 subject 不一致时必须 fail closed；

（3）resolver 返回的数据应标记来源、版本、更新时间和缓存命中情况。

# 8. 授权裁决模型

AAC 不限定唯一的授权模型。实现方可根据场景选择 ACL、RBAC、ABAC、ReBAC、OPA 或等价机制。无论采用何种模型，都必须基于 `AuthorizationRequest` 中的可信上下文做裁决。

## 8.1 ACL

ACL 适合少量明确主体的 allow / deny。

示例：

```text
allow if immediate_actor.subject_id in resource.allow_subjects
deny if primary_subject.subject_id in resource.deny_subjects
```

ACL 可用于：

（1）Partner 允许某些 Agent AIC 调用；

（2）某个 project、session 或 task 允许某些 human subject 访问；

（3）某个 Skill 只允许指定 actor chain 发起。

## 8.2 RBAC

RBAC 基于角色裁决。

示例：

```text
allow data.export if
  "data_exporter" in primary_subject.roles
  and "trusted_agent" in immediate_actor.roles
```

RBAC 可用于用户角色、Agent 等级、服务账号角色、realm role 或 client role 到本地角色的映射。

## 8.3 ABAC

ABAC 基于属性裁决。

示例：

```text
allow if
  primary_subject.attributes.tenant_id == resource.tenant_id
  and immediate_actor.attributes.trust_level >= resource.attributes.required_trust_level
  and "data.export" in primary_subject.scopes
```

ABAC 可用于租户、组织、地域、风险等级、token scope、consent、设备、时间、IP、任务敏感度、Agent 证书状态、provider、trustLevel 等条件。

## 8.4 ReBAC

ReBAC 基于关系裁决。

示例关系：

```text
human:UserA member_of org:OrgA
org:OrgA subscribed_to package:DataService-Pro
package:DataService-Pro includes skill:data.export
agent:AIC-PARTNER-1 delegated_by human:UserA
agent:AIC-PARTNER-2 owns skill:data.export
```

示例问题：

```text
agent:AIC-PARTNER-1 是否 can_act_on_behalf_of human:UserA invoke skill:data.export ?
```

ReBAC 适合多主体、多组织、多项目、多订阅、用户委托 Agent、Agent 级联委托和中途审批人与资源 owner 的关系表达。

## 8.5 OPA 或外部 PDP

OPA 不是新的授权模型，而是承载 ACL、RBAC、ABAC、ReBAC 规则的策略执行引擎。

Partner 可把 `AuthorizationRequest` 转换成 OPA input：

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

OPA 或外部 PDP 应返回 allow / deny / reason。PEP 只执行结果，不应向请求方泄露完整策略细节。

## 8.6 ACS 与 ADP 边界

ACS 中的能力开放程度、Discovery 查询结果或候选列表不构成最终运行时授权。

要求：

（1）ACS 可声明能力的静态开放程度；

（2）ADP 可根据静态开放程度和查询条件做 discovery 过滤；

（3）ADP 不应替代 Partner 做最终授权裁决；

（4）无论某个 Partner 或 Skill 是否通过 discovery 返回，AIP 调用时接收方都必须重新执行 AAC 授权流程；

（5）`public`、`restricted`、`private` 等静态可见性字段不得被解释为运行时一定允许或一定拒绝。

# 9. 通信 Profile

AAC 将真人、Agent、单跳和多跳场景统一为不同的上下文构造 profile，而不是定义互不兼容的授权机制。

## 9.1 Profile A：真人到 Agent 单跳

```text
Human -> Leader / Agent API
```

可信上下文来源：

（1）OIDC ID Token：用于登录认证；

（2）OAuth 2.0 access token 或本地 session：用于 API 授权上下文。

上下文构造：

```text
primary_subject = human:{issuer}#{sub}
immediate_actor = primary_subject
actor_chain = []
resource = Leader API / session / task / skill request
action = HTTP route / application action
```

裁决要求：

Leader 应使用 ACL、RBAC、ABAC、ReBAC、OPA 或等价策略判断真人是否能访问 API、session、task、Partner selection 或高风险 capability。OAuth scope 或 role 只能作为裁决输入，不应作为完整裁决。

## 9.2 Profile B：Agent 到 Agent 单跳

```text
Agent1 -> Partner2
```

可信上下文来源：

（1）mTLS / CAI / AIC；

（2）AIP `senderId == peer AIC` 身份绑定；

（3）可选 resolver 根据 AIC 查询角色、属性或关系。

上下文构造：

```text
primary_subject = agent:{peer_aic}
immediate_actor = agent:{peer_aic}
actor_chain = [agent:{peer_aic}]
resource = Partner / Skill
action = AIP action
```

裁决要求：

Partner 应使用 AIC-ACL、RBAC、ABAC、ReBAC、OPA 或等价策略判断 peer AIC 是否允许访问。纯单跳 Agent 调用不强制使用 OAuth 2.0 Token Exchange。

## 9.3 Profile C：真人发起的多跳委托

```text
Human -> Leader1 -> Partner1 -> ... -> PartnerN
```

可信上下文来源：

（1）入口 OIDC / OAuth2：认证真人并建立初始 access token；

（2）每一跳 mTLS / AIC：认证当前 immediate actor；

（3）OAuth 2.0 Token Exchange 或等价 delegation token：携带 primary subject、actor chain、scope、audience、expiry。

上下文构造：

```text
primary_subject = human:{issuer}#{sub}
immediate_actor = agent:{current_peer_aic}
actor_chain = [agent:AIC-LEADER-1, ..., agent:{current_peer_aic}]
resource = current Partner / Skill
action = AIP action
```

裁决要求：

当前 Partner 应同时判断：

（1）immediate actor 是否允许调用当前 Partner 或 Skill；

（2）immediate actor 是否允许代表 primary subject；

（3）primary subject 是否允许访问目标资源；

（4）actor chain、scope、tenant、consent、risk 是否满足策略。

## 9.4 Profile D：Agent 发起的多跳委托

```text
Agent0 -> Agent1 -> Agent2 -> ... -> PartnerN
```

可信上下文来源：

（1）原始 Agent 的 mTLS / AIC 或初始 delegation token；

（2）每一跳 mTLS / AIC；

（3）Token Exchange 或 ACPs 原生 delegation token。

上下文构造：

```text
primary_subject = agent:{originator_aic}
immediate_actor = agent:{current_peer_aic}
actor_chain = [agent:{originator_aic}, ..., agent:{current_peer_aic}]
resource = current Partner / Skill
action = AIP action
```

裁决要求：

当前 Partner 应判断：

（1）immediate actor 是否可访问当前 Partner；

（2）immediate actor 是否可代表 originator；

（3）originator 是否具备访问目标 Skill 的权利；

（4）该 Skill 是否允许二次或多次转委托；

（5）chain depth 是否在允许范围内。

## 9.5 中途真人授权事件

真人可以出现在多跳链路中间，但不得作为未验证 payload 字段出现。

示例：

```text
Partner1 需要敏感数据导出 consent
  -> 触发 User 或 Approver 完成 OIDC / MFA / consent
  -> Authorization Server / STS 生成新的 authorization event
  -> 后续 Token Exchange token 携带 event reference
```

PDP 可基于以下条件做裁决：

```text
authorization_events contains human_consent for purpose=export-sensitive-data
```

# 10. 委托模式与 Token Profile

## 10.1 委托链路模式

AAC 支持固定链路委托和动态下一跳委托两种模式。

| 模式 | 说明 | 适用场景 |
| --- | --- | --- |
| 固定链路 | 初始授权上下文中明确后续 actor / Partner 路径，运行时只能沿指定链路继续 | 高敏感、强合规、指定供应商、指定处理路径 |
| 动态下一跳 | 初始授权上下文给出边界约束，当前节点可在边界内选择下一跳，并由 STS 逐跳裁决 | Agent 自主规划、能力发现、任务分解、多 Partner 协作 |

推荐默认模式为：

```text
动态下一跳 + 边界约束 + 逐跳裁决
```

动态下一跳模式下，当前节点可以根据任务需要选择下一跳，但不能自行扩大 scope、audience、tenant、purpose、chain depth 或数据敏感等级。当前节点必须通过 STS、Token Exchange 或等价可信机制生成面向下一跳的上下文。

## 10.2 Delegation Token 必需字段

当使用 JWT access token 或 ACPs delegation token 承载多主体委托上下文时，必须包含以下字段。若使用 opaque token，接收方或 STS 必须能通过 introspection、resolver 或等价可信机制取得这些字段的等价信息。

| 字段 | 语义 |
| --- | --- |
| `iss` | token issuer / Authorization Server / STS |
| `sub` | primary subject，可以是真人或 Agent |
| `aud` | 当前目标 Resource Server / Partner，必须匹配第 6.7 节定义的 canonical audience 或可信 alias |
| `exp` | 过期时间 |
| `iat` | 签发时间 |
| `jti` | token 唯一 ID，用于审计、撤销和 replay 检测 |
| `scope` | 当前 token 授权的最小 scope |
| `act` | 当前 actor；最外层 actor 必须能解析为 immediate actor |

## 10.3 Delegation Token 推荐字段

| 字段 | 语义 |
| --- | --- |
| `nbf` | 生效时间 |
| `azp` / `client_id` | 请求 token 的 OAuth client |
| `cnf` | certificate-bound access token 绑定信息 |
| `acps_subject_type` | `human` / `agent` / `service` |
| `acps_target_aic` | 当前目标 Partner AIC |
| `acps_skill_id` | 当前 token 授权的 Skill |
| `acps_delegation_id` | 委托链路 ID |
| `acps_delegation_mode` | `fixed` / `dynamic` |
| `acps_boundary_id` | 动态链路的边界约束 ID |
| `acps_boundary_hash` | 边界约束摘要，防止 resolver 返回边界被替换 |
| `acps_chain_depth` | 当前链路深度 |
| `acps_max_chain_depth` | 当前委托上下文允许的最大链路深度 |
| `acps_allowed_route` | 固定链路模式下允许的 actor / Partner 路径 |
| `acps_allowed_partner_aics` | 动态链路模式下允许选择的下一跳 Partner AIC 集合 |
| `acps_allowed_partner_categories` | 动态链路模式下允许选择的下一跳能力类别 |
| `acps_purpose` | 当前委托目的 |
| `acps_authorization_events` | consent / approval / step-up 的引用或摘要 |

## 10.4 `act` Claim

推荐使用 RFC 8693 的 `act` claim 表达 actor。

示例：

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

约束：

（1）最外层 `act` 必须与 mTLS peer AIC 一致；

（2）内层 `act` 是历史 actor，主要用于授权上下文、风控和审计；

（3）链路过长时，可只放 `acps_delegation_id`，完整链路由 STS 或 resolver 查询。

## 10.5 Audience 与 Scope 收窄

每一跳 Token Exchange 必须收窄或保持不扩大。以下 `scope <= subject_token.scope` 按集合语义解释，表示新 token 的 scope 必须是原 token scope 的子集或等集：

```text
new_token.aud = 下一跳 Resource Server 的 canonical audience
new_token.scope <= subject_token.scope
new_token.exp <= subject_token.exp
new_token.chain_depth = subject_token.chain_depth + 1
new_token.jti = 新的唯一 ID
```

给 Partner1 的 token 不得直接用于 Partner2。

## 10.6 固定链路约束

固定链路 token 必须让接收方或 STS 能判断当前 actor 是否位于预期路径上。

示例：

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

要求：

（1）`actor_chain` 必须是 `acps_allowed_route` 的前缀或与当前 hop 对齐；

（2）当前 `aud` 必须匹配固定路径中下一跳 Resource Server 的 canonical audience 或可信 alias；

（3）当前 actor 不得跳过固定路径中的中间节点；

（4）固定路径上任一节点变更时，必须重新获得授权上下文。

## 10.7 动态下一跳约束

动态链路 token 不固定完整路径，而是携带或引用边界约束。

示例：

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

要求：

（1）STS 必须在每次 Token Exchange 时根据边界约束裁决 requested audience、scope、purpose、tenant 和 chain depth，其中 requested audience 必须能解析为下一跳 Resource Server 的 canonical audience；

（2）接收方 Partner 可以只校验当前 token，也可以通过 `acps_boundary_id` 查询边界用于本地 ABAC、ReBAC 或 OPA 裁决；

（3）`acps_chain_depth` 不得超过 `acps_max_chain_depth`；

（4）`scope` 不得超过边界允许的最大 scope；

（5）若请求触发 consent、approval 或 step-up 条件，STS 必须要求新的 authorization event，不能静默签发下一跳 token。

# 11. AIP 承载与执行规则

## 11.1 Direct / Stream / Notification Start

用户委托或 Agent 委托 token 推荐通过 HTTP `Authorization` header 承载：

```http
Authorization: Bearer <access-token-or-delegation-token>
```

要求：

（1）token 不应放入 prompt、Products、`commandParams` 或普通业务 payload；

（2）SDK 和日志系统必须默认脱敏 `Authorization` header；

（3）Stream 重连必须重新携带有效 token；

（4）token 过期时，调用方应先 refresh 或 Token Exchange；

（5）Notification callback 如需访问接收方受保护资源，也应使用面向 callback 接收方的 token。

## 11.2 Group / MQ

普通 Group 广播消息中不得携带面向单一接收方的 bearer token。

原因：

（1）面向 PartnerA 的 bearer token 被广播给 PartnerB 会破坏 audience 和最小暴露原则；

（2）MQ 消息可被多个成员消费，bearer token 泄露风险高。

若后续支持 Group 用户委托，应单独设计 per-recipient encrypted envelope、per-recipient inbox token、aud=group 且受限的 group token，或消费方再 exchange 成 aud=自身 Partner 的 token。未具备这些机制时，Group / MQ 消息不得把 bearer token 放入消息正文或普通消息属性。

## 11.3 Partner / Resource Server 校验顺序

收到受保护请求后，PEP 应按顺序执行：

```text
1. 验证传输层身份：
   - Agent-to-Agent: mTLS peer certificate -> peer AIC
   - Human-to-Agent: HTTPS + session / access token

2. 验证协议身份绑定：
   - AIP senderId == peer AIC
   - TLS server AIC == expected callee AIC

3. 验证 token / session：
   - issuer / signature / introspection
   - canonical audience / expiry / scope
   - revocation / active status
   - jti / replay status（如启用）
   - act 与 peer AIC 绑定
   - cnf 与 mTLS client certificate 绑定
   - 固定链路模式下当前 hop 与 allowed route 对齐
   - 动态链路模式下 delegation boundary id / hash / chain depth 合法

4. 解析资源和动作：
   - action
   - skillId
   - taskId / groupId / notificationConfigId
   - tenant / owner / sensitivity

5. 构造 AuthorizationRequest。

6. 调用 PDP。

7. allow 才进入业务 handler。

8. deny 或 error 返回稳定错误，并记录审计。
```

# 12. 错误映射

AAC 沿用 AIP 中定义的认证和授权错误。

| 情况 | 错误码 |
| --- | --- |
| 缺少必需认证凭据，例如 mTLS peer certificate、Bearer token、session | `-32008 AuthenticationRequiredError` |
| peer certificate 无效、AIC 无法解析 | `-32008 AuthenticationRequiredError` |
| AIP `senderId != peer AIC` | `-32009 AuthorizationFailedError` |
| token 格式错误、签名无效、尚未生效、过期、撤销、issuer 不可信 | `-32010 AccessTokenInvalidError` |
| token `aud` 不包含当前 Resource Server 的 canonical audience 或可信 alias | `-32010 AccessTokenInvalidError` |
| token `jti` 已被重放，或一次性 token 被重复使用 | `-32010 AccessTokenInvalidError` |
| token `act` 与 mTLS peer AIC 不一致 | `-32009 AuthorizationFailedError` |
| token `cnf` 与当前 mTLS certificate 不匹配 | `-32009 AuthorizationFailedError` |
| 上下文可信但策略不允许 | `-32009 AuthorizationFailedError` |
| resource / skillId 显式不存在或不可访问 | `-32009 AuthorizationFailedError` |
| PDP 不可用且无显式降级策略 | `-32009 AuthorizationFailedError` |

对外错误信息不得泄露具体策略、名单、角色、关系链或 token claims。详细原因应进入审计日志。

AIP JSON-RPC 响应中的 `message` 应与 AIP 错误表保持一致，例如 `Authentication required`、`Authorization failed`、`Invalid access token`。实现方可在内部日志中记录更细的 `reason_code`。

# 13. 审计要求

每次授权裁决都应记录审计事件。

审计事件示例：

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

审计约束如下：

（1）不得记录完整 access token、refresh token、ID Token；

（2）真人 subject 应优先 hash 或使用 pairwise subject；

（3）应记录 issuer、audience、jti、delegation id、actor chain、resource、action、decision、reason code；

（4）高风险事件包括 impersonation、chain depth 过深、audience 不匹配、actor 不匹配、token replay、break-glass；

（5）审计日志应满足完整性保护、访问控制和保留周期要求。

# 14. 安全要求

## 14.1 最小权限

（1）token scope 必须最小化；

（2）Token Exchange 后的新 token 不得扩大 audience、scope 或有效期；

（3）高敏感 Skill 应使用短期 token、一次性 token 或 replay 检测；

（4）refresh token 不得透传给 Partner，不得写入 AIP 消息、MQ 消息、Products、prompt 或日志。

## 14.2 Token 绑定

Agent-to-Agent 的委托 token 推荐使用 mTLS certificate-bound access tokens。

当 token 包含 `cnf` claim 时：

（1）Authorization Server 或 STS 必须把 token 绑定到将持有并出示该 token 的 Agent 证书；

（2）Partner 必须校验 `cnf` 与当前 mTLS peer certificate 匹配；

（3）token 被窃取后不能被其他 Agent 直接重放。

## 14.3 Token 撤销与重放防护

Resource Server 必须根据 token 类型和风险等级执行撤销与重放防护：

（1）可撤销 token 必须通过 introspection、撤销列表、短期缓存失效或等价机制确认 token 未被撤销；

（2）一次性 token、短期高敏 token 或启用 replay 检测的 delegation token 必须记录 `jti`、delegation id 或等价唯一标识，并在有效期内拒绝重复使用；

（3）token 验证缓存不得超过 token `exp`，也不得超过本地撤销策略允许的最大缓存时间；

（4）检测到 replay、撤销 token 使用、audience 错配或 actor 绑定错配时，必须记录高风险审计事件。

## 14.4 冒充默认禁止

AAC 默认只支持 delegation，不支持 impersonation。

```text
允许：Agent 代表 primary subject，经授权调用下游。
禁止：Agent 伪装成另一个 Agent 或真人。
```

若业务必须支持 impersonation：

（1）必须有显式策略；

（2）token 或 context 必须清楚标记 impersonation；

（3）PDP 必须能区分 delegation 与 impersonation；

（4）审计等级必须高于普通 delegation。

## 14.5 不信任自报字段

不得把以下字段直接作为可信授权输入：

（1）AIP payload 中自报的 `userId`、`username`、`role`；

（2）`commandParams` 中自报的用户属性或 agent chain；

（3）prompt 或任务文本中的身份描述；

（4）未经验证的 ID Token；

（5）`aud` 不包含当前 Resource Server 的 access token；

（6）未签名、未绑定、未验证的 delegation record。

可信输入只能来自：

（1）已验证证书；

（2）已验证 token；

（3）已验证 session；

（4）已验证 STS 或 resolver 查询结果；

（5）当前系统自己维护的资源状态和关系数据。

# 15. 补充说明

本文档定义的智能体访问控制流程与《AIA：智能体身份认证》《AIP：智能体交互协议》《ACS：智能体能力描述》《ADP：智能体发现过程》共同构成 ACPs 安全协作基础。

AAC 的基本原则是：认证产生可信身份上下文，授权基于可信上下文做策略裁决，执行点在业务处理前强制执行裁决结果。任何未验证、未绑定、未签名或仅由请求 payload 自报的身份、角色、用户、委托链信息，都不得被直接作为授权依据。
