[首页](../README.md)

ACS：智能体能力描述（ACPs-spec-ACS-v02.02）

# 1. 文档定义

本文档为支持智能体互联的 ACPs 智能体协作协议体系中的智能体能力描述（Agent Capability Specification，ACS）标准定义，版本号 v02.02。

文档全称为 ACPs-spec-ACS-v02.02。

文档编写者：禹可（北京邮电大学），刘军（北京邮电大学），李珂（北京邮电大学），郭小练（北京邮电大学），李胤铭（北京邮电大学），宋昊哲（北京邮电大学），胡晓峰（北京邮电大学），马镝（北京邮电大学），陈科良（北京邮电大学）。

# 2. 智能体能力描述介绍及相关流程

智能体互联要能成为一个安全可靠的智能体系统，需要具备的一个重要能力是支持智能体描述自身的能力并对该描述进行保存和支持获取其它智能体对自身能力的描述。智能体能力描述方式既要保证一定的规范性以便于智能体之间互联互通，也需要具备一定的灵活性以适应基于大模型的智能体复杂能力表述。为达到以上目标，我们在本文档中定义智能体能力描述（Agent Capability Specification，ACS）规范。

每个智能体应为自己生成一个 ACS，并在智能体注册服务商（Agent Registration Service Provider，ARSP）登记。智能体注册服务商可以将智能体登记的 ACS 通过数据同步协议（Data Synchronization Protocol, DSP）同步给智能体发现服务商（Agent Discovery Service Provider，ADSP），由智能体发现服务商提供给其他智能体以支持智能体能力查询。智能体登记和获取 ACS 的流程如下图所示。

![3-1.png](3-1.png)

注：除以上通过智能体发现服务商方式获取 ACS 外，智能体也可以将自己的 ACS 文件放置于自身服务访问地址下，以 Well Known 的方式支持其他使用者直接获取，例如`https://agent.example.com/.well-known/acs.json`. 不过需要特别指出的是，使用者采用该方式获取智能体能力描述，是一种不安全的方式，我们更建议通过智能体发现服务商方式获取 ACS 以确保其安全可靠。

# 3. 智能体能力描述定义

ACPs 的智能体能力描述定义（ACS）采用 JSON 格式表述，定义格式具体如下：

## 3.1. AgentCapabilitySpec 核心对象

```typescript
/**
 * 智能体能力描述规范的根对象。
 * 这是ACS（Agent Capability Specification）的核心数据结构，
 * 完整描述了智能体的身份信息、功能能力、技术特性和服务接口。
 * 用于智能体的注册、发现、匹配和协作。
 */

export interface AgentCapabilitySpec {
  /**
   * 智能体唯一身份标识符，由注册服务分配。
   *
   * @TJS-examples ["1.2.156.3088.1.34C2.478BDF.3GF546.1.0SEN"]
   */
  aic: string;

  /**
   * 智能体的激活状态，由注册服务维护。
   *
   * @TJS-examples [true, false]
   */
  active: boolean;

  /**
   * 智能体能力描述的最后修改时间，由注册服务提供。
   * 采用ISO 8601格式，包含时区偏移信息，推荐使用北京时间（UTC+8）。
   *
   * @TJS-examples ["2025-03-15T16:30:00+08:00"]
   */
  lastModifiedTime: string;

  /**
   * 此智能体支持的ACPs协议版本，用于协议兼容性检查和版本匹配。
   *
   * @TJS-examples ["02.02"]
   */
  protocolVersion: string;

  /**
   * 此智能体的名称，简洁明了地描述智能体的主要功能。
   *
   * @TJS-examples ["北京城区旅游规划助手", "北京郊区景点推荐代理", "文化导览专家"]
   */
  name: string;

  /**
   * 此智能体的详细描述，帮助用户和其他智能体理解其用途和限制。
   * 应明确说明智能体能做什么和不能做什么，包括地理范围、服务类型等限制。
   *
   * @TJS-examples ["对北京城区旅游的规划和建议。只负责城六区，不负责郊区。北京城区范围旅游景点探索规划代理，专门负责北京城六区（东城/西城/朝阳/海淀/丰台/石景山）的旅游景点推荐和行程规划。拒绝超出城区范围的请求。", "专注于北京郊区（密云/怀柔/延庆/昌平/门头沟/房山/大兴/顺义/平谷/通州）的旅游景点推荐和自然路线规划。拒绝城区范围的请求。"]
   */
  description: string;

  /**
   * 智能体的版本号，智能体提供者自行定义格式。
   * 推荐遵循语义化版本控制规范（Semantic Versioning）。
   * 格式：MAJOR.MINOR.PATCH，当API不兼容时递增MAJOR版本。
   *
   * @TJS-examples ["1.0.0", "2.1.3", "1.2.0-beta.1"]
   */
  version: string;

  /**
   * 智能体图标的URL地址，用于在用户界面中显示智能体标识。
   *
   * @TJS-examples ["https://example.com/icons/beijing-agent.png", "https://cdn.example.com/agents/tour-guide.svg"]
   */
  iconUrl?: string;

  /**
   * 智能体详细文档的URL地址，提供使用说明和API文档。
   *
   * @TJS-examples ["https://docs.example.com/agents/beijing-tour", "https://github.example.com/org/agent/blob/main/README.md"]
   */
  documentationUrl?: string;

  /**
   * 智能体能力展示的Web应用URL，用户可以通过此地址体验智能体功能。
   *
   * @TJS-examples ["https://demo.example.com/beijing-tour", "https://app.example.com/agents/tour-guide"]
   */
  webAppUrl?: string;

  /**
   * 智能体服务提供者的详细信息，包括组织、联系方式等。
   *
   * @TJS-examples [{"organization": "北京邮电大学", "url": "https://ai.bupt.edu.cn", "license": "京ICP备140xxxxx号-1"}]
   */
  provider: AgentProvider;

  /**
   * 用于授权请求的可用安全方案声明。键是方案名称，值是对应的安全方案配置。
   * 遵循 OpenAPI 3.0 安全方案对象规范。智能体可以声明支持多种安全方案，
   * 在实际调用时根据端点的security配置选择合适的认证方式。
   *
   * 本字段通常不为空。
   * 作为Partner的智能体通常作为mutualTLS的服务器端对外提供服务，所以需要定义相应的安全方案。
   * 作为Leader的智能体通常作为mutualTLS的客户端去连接其它的Partner智能体，所以也需要定义相应的安全方案。
   *
   * 目前支持的方案包括：
   * - mutualTLS: 双向TLS认证，适用于智能体间的高安全级别通信
   * - openIdConnect: OpenID Connect认证，适用于用户身份验证场景
   * - apiKey: API密钥认证，适用于简单服务鉴权场景
   * - http: HTTP认证方案，适用于Basic/Bearer等认证场景
   * - oauth2: OAuth2认证，适用于标准授权流程场景
   *
   * @TJS-examples [
   *   {
   *     "mtls": {
   *       "type": "mutualTLS",
   *       "description": "智能体间mTLS双向认证"
   *     },
   *     "oidc": {
   *       "type": "openIdConnect",
   *       "description": "用户身份认证",
   *       "openIdConnectUrl": "https://auth.example.com/.well-known/openid-configuration"
   *     }
   *   }
   * ]
   */
  securitySchemes: { [scheme: string]: SecurityScheme };

  /**
   * 智能体端点配置列表，定义智能体可访问的服务端点信息。
   * 每个端点包含URL、传输协议和安全要求。
   * 多个端点应该支持相同的业务功能，支持不同的协议和认证方式，以满足多样化的访问需求。
   *
   * 如果智能体没有提供服务端点给其它智能体使用，通常是面向最终用户的助手类智能体，则：
   * - 本字段为空数组。
   *
   * 如果作为一个智能体本体（Ontology），因为本体不直接提供服务端点，则：
   * - 此字段为空数组，
   *
   * 如果作为一个派生出来的智能体实体（Entity），则：
   * - 需要对外提供服务端点，则此字段应包含相应的端点配置。
   * - 不对外提供服务端点，则此字段为空数组。
   *
   * @TJS-examples [[{"url": "https://api.example.com/acps-v2", "transport": "JSONRPC", "security": [{"mtls": []}]}]]
   */
  endPoints: AgentEndPoint[];

  /**
   * 智能体支持的可选能力声明，如流式响应、异步通知、消息队列等。
   *
   * @TJS-examples [{"streaming": true, "notification": false, "messageQueue": ["mqtt:5.*", "kafka:>=2.8 <4.0"]}, {"streaming": false, "notification": true, "messageQueue": []}]
   */
  capabilities: AgentCapabilities;

  /**
   * 所有技能的默认支持输入MIME类型集合，可在每个技能的基础上覆盖。
   * 定义智能体可以接受的输入数据格式。
   *
   * @TJS-examples [["text/plain", "application/json"], ["text/plain", "image/jpeg", "audio/wav"]]
   */
  defaultInputModes: string[];

  /**
   * 所有技能的默认支持输出MIME类型集合，可在每个技能的基础上覆盖。
   * 定义智能体可以生成的输出数据格式。
   *
   * @TJS-examples [["text/plain", "application/json"], ["text/markdown", "application/json", "image/png"]]
   */
  defaultOutputModes: string[];

  /**
   * 智能体可以执行的技能或独特能力集合，每个技能代表一个专门化的功能模块。
   *
   * 本字段为空数组，表示本智能体没有定义任何技能。这样的智能体通常是面向最终用户的助手类智能体。
   *
   * @TJS-examples [[{"id": "beijing-tour/sight-recommender", "name": "景点推荐", "version": "1.0.0", "tags": ["旅游", "北京"]}]]
   */
  skills: AgentSkill[];

  /**
   * 实体的用户关联ID。用于将智能体实体与特定用户绑定。
   * 用于用户助手类智能体，标识该智能体为某个特定用户提供服务。
   */
  entityUserId?: string;
  /**
   * 实体的额外元数据。具体格式和内容由Agent Provider自定义。
   *
   * 如果Agent对外提供API服务，可以补充比如实体的地理位置、环境信息等。
   * 如果Agent是用户助手，可以用户相关的数据信息。
   *
   * @TJS-examples [{"location": "Beijing", "environment": "production"}, {"userRelation": "personal-assistant"}]
   */
  entityMeta?: Record<string, any>;

  /**
   * 证书相关配置（可选）。
   *
   * 将证书 SAN 条目和请求的有效期统一在此字段下。CA Server 签发证书时读取此字段。
   * 不需要自定义证书参数的 Agent 可省略（CA Server 使用默认值签发）。
   *
   * @TJS-examples [{"altNames": {"dns": ["mq.acps.example.com"], "ip": ["10.0.1.50"]}, "requestedValidity": 1825}]
   */
  certificate?: CertificateOptions;
}

/**
 * 证书配置选项。
 */
export interface CertificateOptions {
  /**
   * 证书 Subject Alternative Names 条目（可选）。
   *
   * CA Server 签发证书时，默认只生成 URI:acps://{AIC}。此字段中声明的域名和 IP 地址
   * 会作为额外的 DNS / IP SAN 条目写入证书，用于 TLS hostname 验证。
   *
   * 典型使用场景：
   * - 基础设施服务（如 RabbitMQ）需要以部署域名/IP 对外提供 TLS 服务
   * - Agent 部署在自有域名下，客户端使用该域名连接
   *
   * 不对外提供 TLS 服务的 Agent 可省略此字段。
   *
   * Registry Server 审批时会校验 dns 中的域名是否属于 provider.domainRegistrations
   * 中已注册域名的子域；IP 地址所有权由审批人人工确认。
   */
  altNames?: CertificateAltNames;

  /**
   * 请求的证书有效期（天数，可选）。
   *
   * 未指定时使用 CA Server 默认有效期（49 天，适用于普通 Agent）。
   * 指定时由人工审批确认，CA Server 以审批通过的天数签发证书。
   * 如果超出 CA Server 配置的最大允许值，则按最大允许值签发。
   *
   * 典型使用场景：基础设施服务（如 RabbitMQ）需要更长有效期以减少轮换频率。
   * 合理范围建议：365（1 年）至 1825（5 年），不建议超过 3650（10 年）。
   *
   * @TJS-examples [365, 1825]
   */
  requestedValidity?: number;
}

/**
 * 证书 Subject Alternative Names 声明。
 */
export interface CertificateAltNames {
  /**
   * 需要加入证书 SAN 的 DNS 域名列表（可选）。
   * 每个条目生成一个 x509 DNSName SAN。支持通配符（如 `*.example.com`）。
   *
   * @TJS-examples [["mq.acps.example.com", "*.acps.example.com"]]
   */
  dns?: string[];

  /**
   * 需要加入证书 SAN 的 IP 地址列表（可选，IPv4 或 IPv6）。
   * 每个条目生成一个 x509 IPAddress SAN。必须是精确单个 IP，不支持 CIDR。
   *
   * @TJS-examples [["192.168.1.100", "10.0.1.50"]]
   */
  ip?: string[];
}
```

## 3.2. AgentProvider 对象

```typescript
/**
 * 智能体服务提供者信息对象。
 * 定义智能体的开发和维护组织信息，包括组织身份、联系方式和合规资质。
 * 用于建立信任关系和提供技术支持联系渠道。
 */
export interface AgentProvider {
  /**
   * 智能体提供者的国家或地区代码。符合ISO 3166-1 alpha-2标准。
   *
   * @default "CN"
   * @TJS-examples ["CN", "US", "GB"]
   */
  countryCode?: string;

  /**
   * 智能体提供者的组织名称，通常为公司、大学或研究机构等顶级组织。
   *
   * @TJS-examples ["北京邮电大学"]
   */
  organization?: string;

  /**
   * 智能体提供者的具体部门或院系名称，提供更精确的组织结构信息。
   *
   * @TJS-examples ["人工智能学院"]
   */
  department?: string;

  /**
   * 智能体提供者的官方网站或相关文档的URL地址。
   *
   * @TJS-examples ["https://ai.bupt.edu.cn"]
   */
  url?: string;

  /**
   * 智能体提供者的法律备案信息或许可证号，用于合规性验证。
   * 通常为URL对应网站的ICP备案号或其他相关资质证明。
   *
   * @TJS-examples ["京ICP备140xxxxx号-1"]
   */
  license?: string;

  /**
   * 智能体提供者的联系人姓名，便于技术支持和沟通。
   * @TJS-examples ["张三", "李四"]
   */
  name?: string;

  /**
   * 智能体提供者的联系人电子邮箱地址。
   *
   * @TJS-examples ["zhangsan@example.com", "lisi@example.com"]
   */
  email?: string;

  /**
   * 智能体提供者的域名注册备案信息列表。
   * 记录 Provider 所使用的各个域名对应的注册备案信息，用于审核阶段验证域名归属。
   * ACS 中所有 URL（endpoint、provider.url、documentationUrl、webAppUrl 等）
   * 的域名必须属于此列表中某个主域名的子域名或与之相同。
   *
   * @TJS-examples [[{"domain": "example.com", "registrationNumber": "京ICP备140xxxxx号-1", "registrationType": "ICP"}]]
   */
  domainRegistrations?: DomainRegistration[];
}

/**
 * 域名注册备案信息。
 */
export interface DomainRegistration {
  /**
   * 主域名/注册域名（如 `example.com`），不含协议、路径和子域名前缀。
   *
   * @TJS-examples ["example.com", "bupt.edu.cn"]
   */
  domain: string;

  /**
   * 备案号/注册号。
   * ICP 类型为中国境内域名的 ICP 备案号（如 `京ICP备140xxxxx号-1`）。
   * WHOIS 类型为 WHOIS 登记查询的注册信息标识。
   *
   * @TJS-examples ["京ICP备140xxxxx号-1", "R-2024-00xxxxxx"]
   */
  registrationNumber: string;

  /**
   * 注册类型。
   * ICP: 中国境内域名的 ICP 备案。
   * WHOIS: 国际域名的 WHOIS 登记信息查询，覆盖所有国际通用和国别域名。
   *
   * @TJS-examples ["ICP", "WHOIS"]
   */
  registrationType: "ICP" | "WHOIS";
}
```

## 3.3. AgentCapabilities 对象

```typescript
/**
 * 允许的消息队列协议名称。
 * 协议名称为固定枚举，新增协议需修改本规范。
 */
export type MQProtocol = "mqtt" | "amqp" | "kafka" | "redis" | "rabbitmq";

/**
 * 消息队列协议版本表达式。
 * 格式为 "{protocol}:{versionExpr}"，其中：
 * - protocol: 必须为 MQProtocol 中定义的协议名称之一
 * - versionExpr: 版本表达式，支持以下四种模式：
 *   1. 精确版本: "X.Y[.Z]"              —— 仅匹配指定版本
 *   2. 最低版本: ">=X.Y[.Z]"            —— 匹配该版本及以上
 *   3. 版本范围: ">=X.Y[.Z] <X.Y[.Z]"  —— 匹配闭-开区间内的版本
 *   4. 通配符:   "X.*"                   —— 匹配该主版本的所有次版本
 *
 * 生成 JSON Schema 时可使用正则约束：
 * pattern: "^(mqtt|amqp|kafka|redis|rabbitmq):(>=)?\\d+\\.(\\*|\\d+(\\.\\d+)?)( <\\d+\\.\\d+(\\.\\d+)?)?$"
 *
 * @TJS-examples ["kafka:>=2.8 <4.0", "mqtt:5.*", "amqp:>=0.9.1", "redis:>=7.0", "rabbitmq:>=4.2", "kafka:3.1"]
 */
export type MQProtocolVersion = `${MQProtocol}:${string}`;

/**
 * 智能体可选技术能力配置对象。
 * 定义智能体支持的高级功能特性，如实时通信、异步通知和消息队列集成。
 * 这些能力为智能体提供更丰富的交互方式和更强的扩展性。
 */
export interface AgentCapabilities {
  /**
   * 智能体是否支持Server Send Event（SSE）用于流式响应。
   * 启用后可以实现实时数据推送和渐进式内容生成。
   *
   * @TJS-examples [true, false]
   */
  streaming: boolean;

  /**
   * 智能体是否支持异步推送通知。
   * 启用后可以主动向指定URL推送事件和状态更新。
   *
   * @TJS-examples [true, false]
   */
  notification: boolean;

  /**
   * 智能体支持的消息队列能力配置。使用协议版本表达式字符串数组进行配置。
   * 支持多种消息队列协议，用于异步消息传递和事件通知。
   * 每个元素的格式为 "{protocol}:{versionExpr}"，协议名称必须为 MQProtocol 中定义的值，
   * 版本表达式支持精确版本、最低版本、版本范围和通配符四种模式。
   * 空数组表示不支持任何消息队列协议。
   *
   * @TJS-examples [["mqtt:5.*", "kafka:>=2.8 <4.0"], ["redis:>=7.0", "rabbitmq:>=4.2"], []]
   */
  messageQueue: MQProtocolVersion[];
}
```

## 3.4. SecurityScheme 对象

```typescript
/**
 * 定义可用于保护智能体端点的安全方案。
 * 这是基于 OpenAPI 3.0 安全方案对象的判别联合类型。
 *
 * @see {@link https://swagger.io/specification/#security-scheme-object}
 */
export type SecurityScheme =
  | APIKeySecurityScheme
  | HTTPAuthSecurityScheme
  | OAuth2SecurityScheme
  | OpenIdConnectSecurityScheme
  | MutualTLSSecurityScheme;

/**
 * 双向TLS认证安全方案，用于智能体间的高安全级别通信。
 * 要求客户端和服务端都提供有效的证书进行相互验证。
 */
export interface MutualTLSSecurityScheme {
  /**
   * 安全方案类型，固定为"mutualTLS"。
   *
   * @TJS-examples ["mutualTLS"]
   */
  type: "mutualTLS";

  /**
   * 安全方案的描述信息，说明该方案的用途和特点。
   *
   * @TJS-examples ["双向TLS认证，确保客户端和服务端身份可信", "智能体间高安全级别通信认证"]
   */
  description?: string;

  /**
   * @deprecated 此字段已废弃，将在未来版本中移除。
   * 挑战验证机制已被 ACME External Account Binding (EAB) 替代。
   * EAB 凭证通过 Registry Server 获取，不再需要在 ACS 中声明挑战服务器地址。
   *
   * @TJS-examples ["https://certs.example.com/agent-challenge", "https://ca.example.com/challenge/v1"]
   */
  "x-caChallengeBaseUrl"?: string;
}

/**
 * OpenID Connect认证安全方案，基于OAuth 2.0协议的身份认证层。
 * 提供标准化的身份验证和用户信息获取能力。
 */
export interface OpenIdConnectSecurityScheme {
  /**
   * 安全方案类型，固定为"openIdConnect"。
   *
   * @TJS-examples ["openIdConnect"]
   */
  type: "openIdConnect";

  /**
   * 安全方案的描述信息，说明该OIDC方案的用途和特点。
   *
   * @TJS-examples ["基于OpenID Connect的统一身份认证", "支持多身份提供商的用户认证"]
   */
  description?: string;

  /**
   * OpenID Connect发现文档的URL，用于自动发现认证端点和配置信息。
   * 客户端可通过此URL获取授权服务器的元数据和端点信息。
   *
   * @TJS-examples ["https://auth.example.com/.well-known/openid-configuration", "https://accounts.google.com/.well-known/openid-configuration", "https://login.microsoftonline.com/common/.well-known/openid-configuration"]
   */
  openIdConnectUrl: string;
}

/**
 * API Key认证安全方案，通过API密钥进行身份验证。
 */
export interface APIKeySecurityScheme {
  /**
   * 安全方案类型，固定为"apiKey"。
   *
   * @TJS-examples ["apiKey"]
   */
  type: "apiKey";

  /**
   * 安全方案的描述信息，说明该方案的用途和特点。
   *
   * @TJS-examples ["基于API Key的统一身份认证"]
   */
  description?: string;

  /**
   * API密钥参数的名称。
   *
   * @TJS-examples ["example-api-key-name"]
   */
  name: string;

  /**
   * API密钥的位置：query, header 或 cookie。
   *
   * @TJS-examples ["query", "header", "cookie"]
   */
  in: "query" | "header" | "cookie";
}

/**
 *  HTTP 认证安全方案，支持 Basic、Bearer 等认证方式。
 */
export interface HTTPAuthSecurityScheme {
  /**
   * 安全方案类型，固定为"http"。
   *
   * @TJS-examples ["http"]
   */
  type: "http";

  /**
   * 安全方案的描述信息，说明该方案的用途和特点。
   *
   * @TJS-examples ["基于HTTP的统一身份认证"]
   */
  description?: string;

  /**
   * HTTP认证方案名称，如 'basic', 'bearer' 等。
   *
   * @TJS-examples ["basic", "bearer"]
   */
  scheme: string;

  /**
   * Bearer令牌的格式提示，仅当scheme为'bearer'时使用。
   *
   * @TJS-examples ["None"]
   */
  bearerFormat?: string;
}

/**
 * OAuth2 单个授权流程配置
 */
export interface OAuth2Flow {
  /**
   * 授权端点URL（如授权码流程需要）。
   *
   * @TJS-examples ["https://auth.example.com/oauth2/authorize"]
   */
  authorizationUrl?: string;

  /**
   * 令牌端点URL（如密码模式、客户端凭证、授权码流程需要）。
   *
   * @TJS-examples ["https://auth.example.com/oauth2/token"]
   */
  tokenUrl?: string;

  /**
   * 刷新令牌端点URL（可选）。
   *
   * @TJS-examples ["https://auth.example.com/oauth2/refresh"]
   */
  refreshUrl?: string;

  /**
   * 可用作用域定义，键为scope名，值为说明。
   *
   * @TJS-examples [{"read:profile": "读取用户资料", "write:data": "写入业务数据"}]
   */
  scopes: { [scope: string]: string };
}

/**
 * OAuth2 流程集合定义
 */
export interface OAuth2Flows {
  /**
   * 隐式授权流程配置
   */
  implicit?: OAuth2Flow;

  /**
   * 资源所有者密码凭据流程配置
   */
  password?: OAuth2Flow;

  /**
   * 客户端凭据流程配置
   */
  clientCredentials?: OAuth2Flow;

  /**
   * 授权码流程配置
   */
  authorizationCode?: OAuth2Flow;
}

/**
 * OAuth2 安全方案
 */
export interface OAuth2SecurityScheme {
  /**
   * 安全方案类型，固定为"oauth2"。
   *
   * @TJS-examples ["oauth2"]
   */
  type: "oauth2";

  /**
   * 安全方案描述信息。
   *
   * @TJS-examples ["基于OAuth2的授权认证方案"]
   */
  description?: string;

  /**
   * OAuth2 各授权流程配置。
   */
  flows: OAuth2Flows;
}
```

## 3.5. AgentEndPoint 对象

```typescript
/**
 * 智能体服务端点配置对象。
 * 定义智能体对外提供服务的网络访问点，包括访问地址、
 * 传输协议和安全认证要求。支持多端点配置以实现不同的服务模式。
 */
export interface AgentEndPoint {
  /**
   * 此端点的完整URL地址。根据传输协议类型，URL的含义有所不同：
   *
   * JSONRPC: 固定的RPC端点URL，所有RPC调用都发送到此地址
   * HTTP_JSON: API的Base URL，实际调用时会在此基础上拼接具体的API路径
   * AMQP: AMQPS 连接 URL，格式为 `amqps://{host}:{port}/{vhost}?inbox={inbox-queue-name}`
   *        其中 `{AIC}` 为占位符，ACS 注册时填写，Registry Server 审批通过后替换为实际分配的 AIC。
   *        例：`amqps://mq.acps.example.com:5671/acps?inbox=inbox_{AIC}`
   *        审批后变为：`amqps://mq.acps.example.com:5671/acps?inbox=inbox_1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4`
   *
   * 示例：
   * - JSONRPC: "https://api.example.com/rpc" (固定端点)
   * - HTTP_JSON: "https://api.example.com/v1" (基础URL，实际调用如 /v1/skills/search)
   * - AMQP: "amqps://mq.acps.example.com:5671/acps?inbox=inbox_{AIC}" (Inbox 接收端点)
   *
   * @TJS-examples ["https://api.example.com/rpc", "https://api.example.com/v1", "amqps://mq.acps.example.com:5671/acps?inbox=inbox_{AIC}"]
   */
  url: string;

  /**
   * 此端点支持的传输协议类型。不同协议有不同的调用方式和URL解释：
   *
   * JSONRPC: 基于JSON-RPC 2.0协议的远程过程调用
   * HTTP_JSON: 基于HTTP的JSON请求/响应，RESTful风格的API调用
   * AMQP: 基于消息队列的 AMQP 协议端点（如 RabbitMQ AMQPS）
   *
   * @TJS-examples ["JSONRPC", "HTTP_JSON", "AMQP"]
   */
  transport: string;

  /**
   * 适用于此端点的安全要求配置列表。定义了调用此端点时必须满足的认证要求。
   * 遵循 OpenAPI 3.0 安全要求对象规范。
   *
   * 数组结构说明：
   * - 外层数组表示 "OR" 关系：满足任意一个安全要求组合即可
   * - 内层对象表示 "AND" 关系：同一对象内的所有方案都必须满足
   * - 键名必须与 securitySchemes 中定义的方案名称一致
   * - 值数组表示所需的权限范围（scopes），对于某些方案可以为空数组
   *
   * 常见配置模式：
   * 1. 单一认证：[{"mtls": []}] - 仅需要mTLS认证
   * 2. 多选一：[{"mtls": []}, {"oidc": ["read"]}] - mTLS或OIDC任选其一
   * 3. 组合认证：[{"mtls": [], "oidc": ["profile"]}] - 同时需要mTLS和OIDC
   *
   * @TJS-examples [
   *   [{"mtls": []}],
   *   [{"mtls": []}, {"oidc": ["openid", "profile"]}],
   *   [{"mtls": [], "oidc": ["read"]}, {"oidc": ["admin"]}]
   * ]
   */
  security?: { [scheme: string]: string[] }[];
}
```

## 3.6. AgentSkill 对象

```typescript
/**
 * 表示智能体可以执行的某方面的独特能力或功能。
 * 每个技能代表智能体的一个专门化能力，具有明确的功能边界和输入输出规范。
 */
export interface AgentSkill {
  /**
   * 智能体技能的唯一标识符。由提供者定义，建议使用分层命名空间格式。
   *
   * 推荐的命名空间方案：
   * 1. 点分层格式：{agent-domain}.{skill-category}.{specific-skill}
   * 2. 冒号分层格式：{agent-domain}:{skill-category}:{specific-skill}
   *·
   * @TJS-examples ["beijing-urban-tour.sight-recommender", "beijing-urban-tour:itinerary-planner"]
   */
  id: string;
  /**
   * 技能的名称，简洁明了地描述技能的主要功能。
   *
   * @TJS-examples ["北京城区旅游景点推荐", "北京城区行程规划", "文化体验优化"]
   * */
  name: string;
  /**
   * 技能的详细描述，帮助客户端或用户理解其目的、功能范围和限制。
   * 应明确说明技能能做什么和不能做什么，包括地理范围、服务类型等限制。
   *
   * @TJS-examples ["根据客户需求推荐北京城六区（东城/西城/朝阳/海淀/丰台/石景山）内的旅游景点，提供文化深度体验建议。拒绝郊区景点推荐请求，如八达岭长城、古北水镇等。", "为北京城区游客提供个性化行程规划，基于文化匹配度、交通便捷度和预算进行优化，支持亲子游、文化深度游等不同需求场景。"]
   */
  description: string;
  /**
   * 技能的版本号，由智能体提供者自行定义格式。
   * 建议遵循语义化版本控制规范（Semantic Versioning）。
   * 格式：MAJOR.MINOR.PATCH，当API不兼容时递增MAJOR版本。
   *
   * @TJS-examples ["1.0.0", "2.1.3", "1.2.0-beta.1"]
   */
  version: string;

  /**
   * 描述技能能力特征的关键词集合，用于技能发现和匹配。
   * 包括功能类型、地域范围、专业领域、目标用户等维度的标签。
   *
   * @TJS-examples [["旅游", "景点推荐", "北京", "城区", "文化体验", "行程规划"], ["博物馆", "历史文化", "亲子游", "深度游", "交通便捷"]]
   */
  tags: string[];
  /**
   * 此技能可以处理的示例提示或场景，帮助用户理解如何使用该技能。
   * 提供具体的用户输入示例和期望的处理场景。
   *
   * @TJS-examples [["推荐几个适合带小孩的北京城区景点", "不要太累的故宫周边一日游安排", "朝阳区有什么文化体验好的地方", "预算500元的海淀区半日游"], ["我想深度了解北京的历史文化", "安排一个周末的亲子游行程", "推荐几个交通便利的博物馆"]]
   */
  examples?: string[];
  /**
   * 此技能支持的输入 MIME 类型集合，覆盖智能体的默认值。
   * 定义该技能可以接受的输入数据格式，如文本、图片、音频等。
   *
   * @TJS-examples [["text/plain", "application/json"], ["text/plain", "image/jpeg", "image/png"]]
   */
  inputModes?: string[];
  /**
   * 此技能支持的输出 MIME 类型集合，覆盖智能体的默认值。
   * 定义该技能可以生成的输出数据格式，如文本、结构化数据、图片等。
   *
   * @TJS-examples [["text/plain", "application/json", "text/markdown"], ["text/plain", "application/json"]]
   */
  outputModes?: string[];
}
```

# 4. 智能体能力描述示例

以下提供两个智能体能力描述示例，用于理解上述定义格式。

## 4.1. 北京城区旅游规划助手示例

```json
{
  // 智能体身份信息。由注册服务分配和维护，不是由智能体提供者定义。
  "aic": "1.2.156.3088.1.34C2.478BDF.3GF546.1.0SEN",
  "active": true,
  "lastModifiedTime": "2025-03-15T16:30:00+08:00",

  // ACPs协议版本
  "protocolVersion": "02.02",

  // 智能体基本描述信息
  "name": "北京城区旅游规划助手",
  "description": "专门负责北京城六区（东城/西城/朝阳/海淀/丰台/石景山）的旅游景点推荐和行程规划。提供个性化旅游建议，支持亲子游、文化深度游、商务游等多种场景。拒绝超出城区范围的请求，如八达岭长城、古北水镇等郊区景点。",
  "version": "1.2.0",

  // 智能体附加信息
  "iconUrl": "https://cdn.example.com/icons/urban-tour-planner.png",
  "documentationUrl": "https://docs.example.com/urban-tour-planner",
  "webAppUrl": "https://demo.example.com/urban-tour-planner",

  // 智能体提供者信息
  "provider": {
    "organization": "ACPs工作组",
    "department": "",
    "url": "https://ioa.pub",
    "license": "京ICP备2025124884号-4"
  },

  // 安全方案定义
  "securitySchemes": {
    "mtls": {
      "type": "mutualTLS",
      "description": "智能体间mTLS双向认证，确保高安全级别通信"
    },
    "oidc": {
      "type": "openIdConnect",
      "description": "基于OpenID Connect的用户身份认证",
      "openIdConnectUrl": "https://auth.example.com/.well-known/openid-configuration"
    }
  },

  // 服务端点配置
  "endPoints": [
    {
      "url": "https://api.example.com/urban-tour-planner/rpc",
      "transport": "JSONRPC",
      "security": [{ "mtls": [] }]
    }
  ],

  // 技术能力声明
  "capabilities": {
    "streaming": true,
    "notification": true,
    "messageQueue": ["rabbitmq:3.*"]
  },

  // 默认输入输出格式
  "defaultInputModes": ["text/plain", "application/json"],
  "defaultOutputModes": ["text/plain", "application/json", "text/markdown"],

  // 智能体技能列表
  "skills": [
    {
      "id": "beijing-urban-tour.sight-recommender",
      "name": "景点推荐",
      "description": "根据用户需求推荐北京城六区内的旅游景点，提供详细的景点信息、开放时间、门票价格和文化背景介绍。支持按兴趣偏好、年龄群体、预算范围进行个性化推荐。",
      "version": "1.2.0",
      "tags": ["旅游", "景点推荐", "北京", "城区", "文化体验", "历史古迹"],
      "examples": [
        "推荐几个适合带小孩的北京城区景点",
        "我想了解故宫周边有什么文化景点",
        "朝阳区有什么现代艺术展馆",
        "预算300元的海淀区景点推荐"
      ],
      "inputModes": ["text/plain", "application/json"],
      "outputModes": ["text/plain", "application/json", "text/markdown"]
    },
    {
      "id": "beijing-urban-tour.itinerary-planner",
      "name": "行程规划",
      "description": "为北京城区游客提供个性化行程规划服务，基于交通便捷度、游览时间、预算控制和兴趣匹配进行智能优化。支持半日游、一日游、多日游等不同时长的行程安排。",
      "version": "1.1.0",
      "tags": ["行程规划", "路线优化", "时间管理", "交通指南", "预算控制"],
      "examples": [
        "安排一个周末的故宫天安门一日游",
        "3天2夜的北京文化深度游行程",
        "半天时间逛完三里屯和国贸",
        "预算1000元的情侣两日游安排"
      ],
      "inputModes": ["text/plain", "application/json"],
      "outputModes": ["text/plain", "application/json", "text/markdown"]
    },
    {
      "id": "beijing-urban-tour.transport-advisor",
      "name": "交通指南",
      "description": "提供北京城区内景点间的最优交通路线建议，包括地铁、公交、出租车、共享单车等多种交通方式的组合推荐。实时考虑交通状况和费用对比。",
      "version": "1.0.0",
      "tags": ["交通导航", "路线规划", "公共交通", "费用优化", "实时路况"],
      "examples": [
        "从故宫到颐和园怎么走最方便",
        "天安门到三里屯的最省钱路线",
        "晚高峰时段从国贸到西单的建议",
        "适合老人的无障碍交通路线"
      ]
    }
  ]
}
```

## 4.2. 全国范围旅游助手示例

```json
{
  "aic": "1.2.156.3088.1.34C2.478BDE.3GF546.1.0RBK",
  "active": true,
  "lastModifiedTime": "2025-04-10T09:45:00+08:00",

  "protocolVersion": "02.02",

  "name": "全国范围旅游助手",
  "description": "提供中国全国范围内的旅游信息服务和行程规划。覆盖全国34个省级行政区的主要旅游景点、特色文化、交通指南和住宿推荐。支持跨地区旅游路线规划，可协调其他地区专业智能体提供深度服务。",
  "version": "2.2.0",

  "iconUrl": "https://cdn.example.com/icons/national-tour-guide.png",
  "documentationUrl": "https://docs.example.com/national-tour-guide",
  "webAppUrl": "https://demo.example.com/national-tour-guide",

  "provider": {
    "organization": "ACPs工作组",
    "department": "",
    "url": "https://ioa.pub",
    "license": "京ICP备2025124884号-4"
  },

  "securitySchemes": {
    "mtls": {
      "type": "mutualTLS",
      "description": "智能体间高安全级别通信认证"
    },
    "oidc": {
      "type": "openIdConnect",
      "description": "用户身份认证和授权管理",
      "openIdConnectUrl": "https://auth.example.com/.well-known/openid-configuration"
    }
  },

  "endPoints": [
    {
      "url": "https://api.example.com/national-tour-guide/v2",
      "transport": "HTTP_JSON",
      "security": [{ "oidc": ["openid", "profile", "tour:coordinate"] }]
    },
    {
      "url": "https://api.example.com/national-tour-guide/rpc",
      "transport": "JSONRPC",
      "security": [{ "mtls": [] }]
    }
  ],

  "capabilities": {
    "streaming": true,
    "notification": true,
    "messageQueue": ["kafka:>=2.8 <4.0", "mqtt:5.*"]
  },

  "defaultInputModes": ["text/plain", "application/json", "image/jpeg"],
  "defaultOutputModes": [
    "text/plain",
    "application/json",
    "text/markdown",
    "application/xml"
  ],

  "skills": [
    {
      "id": "national-tour:destination-discovery",
      "name": "目的地发现",
      "description": "基于用户偏好、季节、预算等条件，在全国范围内发现和推荐合适的旅游目的地。涵盖自然风光、历史文化、美食体验、休闲度假等多种旅游类型。",
      "version": "2.2.0",
      "tags": [
        "目的地推荐",
        "全国旅游",
        "个性化匹配",
        "季节性推荐",
        "预算规划"
      ],
      "examples": [
        "春天适合去哪里看花",
        "推荐几个避暑胜地",
        "适合亲子游的自然景区",
        "预算5000元的7天国内游推荐",
        "想体验少数民族文化的地方"
      ],
      "inputModes": ["text/plain", "application/json", "image/jpeg"],
      "outputModes": ["text/plain", "application/json", "text/markdown"]
    },
    {
      "id": "national-tour:route-planning",
      "name": "跨地区路线规划",
      "description": "设计跨省市的旅游路线，优化交通连接、时间安排和成本控制。支持环线游、直线游、主题游等多种路线类型，可协调沿途各地专业智能体提供详细服务。",
      "version": "2.0.0",
      "tags": ["路线规划", "跨地区旅游", "交通优化", "时间管理", "协调服务"],
      "examples": [
        "设计一条从北京到西藏的自驾路线",
        "江南水乡7日深度游路线",
        "丝绸之路文化之旅规划",
        "东北三省美食探索路线",
        "华南海岛跳岛游安排"
      ]
    },
    {
      "id": "national-tour:agent-coordination",
      "name": "智能体协调",
      "description": "作为Leader角色，协调和调度其他地区专业旅游智能体，为用户提供无缝的全程服务体验。负责任务分发、结果整合和服务质量监控。",
      "version": "1.5.0",
      "tags": ["智能体协调", "任务调度", "服务整合", "质量监控", "Leader模式"],
      "examples": [
        "协调北京和西安的智能体安排古都文化游",
        "整合多个智能体提供川藏线完整服务",
        "调度沿海城市智能体规划海岸线自驾游",
        "协调西南地区智能体安排民族风情体验"
      ],
      "inputModes": ["application/json"],
      "outputModes": ["application/json", "application/xml"]
    },
    {
      "id": "national-tour:weather-integration",
      "name": "天气与季节指导",
      "description": "整合全国天气数据和季节性旅游信息，为用户提供最佳旅游时机建议和天气相关的行程调整方案。",
      "version": "1.2.0",
      "tags": ["天气预报", "季节指导", "行程调整", "最佳时机", "风险提醒"],
      "examples": [
        "现在去云南旅游天气怎么样",
        "什么时候去新疆最合适",
        "台风季节如何调整海南行程",
        "雨季期间的桂林旅游建议"
      ]
    }
  ]
}
```

# 5. 补充说明

上述 ACS 定义中的智能体身份认证方式示例为 mTLS，具体认证流程，我们将在后续文档中详细阐述。

本文档定义的智能体能力描述（Agent Capability Specification，ACS）充分考虑了可管理性和兼容性，并无偿提供给相关研发人员和机构参考。我们欢迎从事智能体研发和智能体互联协议制定的其他业界同仁支持并采纳此定义，以形成利于互联互通和兼容性好的智能体身份码定义。
