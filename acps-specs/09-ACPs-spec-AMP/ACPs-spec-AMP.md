[首页](../README.md)

AMP：智能体监控协议（ACPs-spec-AMP-v02.02）

# 1. 文档定义

本文档为 ACPs 智能体协作协议体系中的智能体监控协议（Agent Monitoring Protocol，AMP）标准定义，版本号 v02.02。

文档全称为 ACPs-spec-AMP-v02.02。

文档编写者：禹可（北京邮电大学），郭小练（北京邮电大学），刘军（北京邮电大学），胡晓峰（北京邮电大学），马镝（北京邮电大学）。

# 2. 智能体监控协议介绍

智能体互联要能成为一个安全可靠的智能体系统，需要一套完整的智能体监控协议来对智能体的运行状态进行精准管控，并完成日志的规范化存储与全链路管理，以确保智能体的状态信息能够在统一规范下实时汇聚、有序分发，为智能体注册服务器、发现服务器及其它智能体提供一致的状态查询与追踪能力，从而为智能体间高效协作、系统安全稳定运行提供核心支撑。

# 3. 智能体监控协议的核心内容

## 3.1 日志

**日志文件**
智能体生成的原始日志数据载体。所有智能体输出的日志文件应遵循统一的外围封装格式，确保可被通用转发与解析；同时允许在统一框架下按业务类型可包含不同的日志内容，以满足不同场景的记录与追踪需求。

## 3.2 智能体监控架构框架

智能体监控框架可分为两层：

- **日志采集层**：各类日志（心跳、访问、指标、审计、消息、系统等）由智能体按类型分别写入本地日志文件，并在经过必要的解析、过滤、打标签等步骤后，由该层以可靠、可控的方式将日志按类型投递到日志存储层。

- **日志存储层**：该层负责对来自不同智能体的不同日志信息进行统一存储，根据不同日志类型的结构特征、访问模式与生命周期需求，提供差异化的存储方案，同时支持日志数据的索引构建与快速检索，为后续的监控分析、告警触发与问题溯源提供数据支撑。

# 4 日志类型及其主要功能

## 4.1 日志类型和日志文件

- **心跳日志（Heartbeat Logs）**：定期记录智能体的运行状态和健康状况。心跳日志的核心目的是表达"智能体仍然存活"这一事实，通常以固定周期（如每 10 ～ 60 秒）产生，通常仅包含状态标识、简要指标摘要等轻量信息。心跳日志具有极强的时效性，历史数据价值低，因此在存储和传输上可采用较激进的降采样与短期保留策略。

- **指标日志（Metrics Logs）**：记录智能体的性能指标和资源使用情况，侧重于记录"智能体表现如何"，以结构化的数值形式呈现。典型内容包括 任务队列情况、延迟分位数（P50/P90/P99）、CPU/内存/磁盘/网络利用率等。

- **访问日志（Access Logs）**：记录智能体与外部系统的每一次交互过程，包括请求、响应、错误、调试信息以及链路追踪数据。访问日志是"广义的交互日志"，涵盖了传统意义上的请求日志、错误日志、调试日志和分布式追踪日志。通过 `severityText` 字段（`DEBUG` / `INFO` / `WARN` / `ERROR`）区分严重程度，通过 `traceId` / `spanId` 支持链路追踪，通过 `correlationId` 进行业务追踪。访问日志通常数据量大、查询模式多样，适合存储于支持全文检索和聚合分析的系统中。

- **消息日志（Message Logs）**：记录智能体通过消息通道（队列、主题、流）进行的发送与接收行为，关注消息驱动架构中的可靠投递、顺序性与重试状态。与访问日志的同步请求-响应不同，消息日志通常描述异步、解耦的生产者/消费者交互，典型字段包括 topic/queue、partition/offset、deliveryAttempt、eventType（发送/接收/确认/死信等边事件）等。它们帮助定位消息堆积、乱序、重复投递等问题，并能通过 `traceId` / `spanId` 串联跨系统的事件驱动链路，也可以通过 `correlationId` 进行业务追踪。

- **审计日志（Audit Logs）**：记录智能体的安全相关操作和访问记录。审计日志服务于安全合规与事后取证需求，通常包含操作人、操作对象、操作类型、操作结果等字段。审计日志通常具有较长的保留周期（视法规要求而定），且对完整性和不可篡改性有较高要求。计费系统的交易日志也可归类为审计日志的一种特殊形式。

- **系统日志（System Logs）**：记录智能体运行环境和关联服务的状态信息。比如关联服务启停日志、数据库 Slow Query 日志、JVM GC 日志等。系统日志有助于诊断智能体运行环境的问题，通常与访问日志和指标日志结合使用，以获得全面的故障排查视角。系统日志的 body 内容采用**自由格式**，不做统一结构定义，由各智能体根据自身环境特点自行决定。

**不同类型的日志文件**

为确保智能体系统的综合性能、扩展性与可维护性，本规范推荐采用**按日志类型分文件写入**的方式，而非把所有日志写入同一个文件。例如：

- `heartbeat.log`：仅保存心跳日志。
- `access.log`：保存访问日志。
- `metrics.log`：保存指标日志。
- `audit.log`：保存审计日志。
- `message.log`：保存消息收发日志。
- `system.log`：保存系统日志。

## 4.2 不同类型的日志存储策略

日志数据的长期留存、多维度复杂查询与离线深度分析，需依托适配其特性的存储系统与分层存储策略。不同类型的日志因数据结构、访问频次与业务诉求存在显著差异，因此需针对性制定差异化的存储方案：

1. **心跳日志（Heartbeat Logs）**
   - 访问模式：以“最新状态”为主，几乎没有历史回溯需求。
   - 推荐：无需保存心跳明细，消息消费完成即可删除。

2. **指标日志（Metrics Logs）**
   - 访问模式：按时间序列聚合、分位数统计、长期趋势分析。
   - 推荐：使用**时序数据库（TSDB）**类存储，以获得高效的降采样、压缩与趋势聚合能力。

3. **访问日志（Access Logs）**
   - 访问模式：按时间范围查询、按接口/服务聚合统计、错误率分析等。
   - 推荐：使用**支持全文检索与大规模聚合的列存 / 检索型分析存储**。
   - 说明：访问日志通常体量较大但价值高，是性能优化与故障定位的重要依据。

4. **消息日志（Message Logs）**
   - 访问模式：按 topic/queue、partition/offset、messageId、eventType 等维度检索，用于排查堆积、重试、乱序和重复消费问题。
   - 推荐：采用**支持高写入与列式聚合的分析型存储**保存结构化消息元数据；必要时将消息原文保留于对象存储或压缩仓库。
   - 说明：消息日志通常不要求像审计日志那样长期保留，但需要保留最近数小时到数天以支撑问题回溯；若消息正文已在消息系统内留存，可仅记录元数据及摘要。

5. **审计日志（Audit Logs）**
   - 访问模式：基于用户/资源/时间范围的精确查询，具有合规与取证需求。
   - 推荐：使用**具备强一致性、事务支持与良好审计能力的存储**（如强一致关系型数据库或专门的审计系统）。
   - 注意：审计日志通常需要较长的保存周期（视法规要求而定），必须考虑归档、分区与冷热数据分层策略。

6. **系统日志（System Logs）**
   - 访问模式：按时间范围与关键字检索运行环境与关联服务状态，多作为访问/指标日志的故障排查辅助视角。
   - 推荐：使用**支持全文检索与大规模聚合的列存 / 检索型分析存储**，可与访问日志共用同类存储。
   - 说明：system body 为自由格式，原文置于 `rawBody`，仅对稳定外围字段做结构化查询，不做深层结构化解析。

总体原则是：

- **短期高频、低价值日志**（如心跳）以“流 + 状态”的方式处理，不做大规模明细存储。
- **中长期有分析价值的日志**（如访问、审计）使用支持索引与复杂查询的存储系统。
- **高频数值型数据**（指标）优先落地至时序数据库，以获得高效的聚合与压缩能力。

> 本节只约束**存储类别**与访问模式。具体存储产品的选型（如某种 TSDB、列存或关系型数据库）属于实现层决策，不在本规范约束范围内，由各实现设计文档负责论证。

# 5 日志规范与相关定义

## 5.1 日志格式定义

### 5.1.1 Schema Versioning

为应对未来日志结构的演进（如新增字段、废弃字段或结构重构），本协议引入 **Schema Versioning** 机制。

- **版本号格式**：采用 `Major.Minor.Patch` 语义化版本（如 `1.0.0`）。
- **兼容性原则**：
  - `Minor` 版本升级（如 `1.0.0` -> `1.1.0`）应保持向后兼容（Backward Compatible），仅允许新增可选字段。
  - `Major` 版本升级（如 `1.0.0` -> `2.0.0`）可能包含破坏性变更（Breaking Changes），需配套升级消费端解析逻辑。
- **字段位置**：版本号必须作为顶层字段 `schemaVersion` 存在于每一条 `LogRecord` 中。

### 5.1.2 LogRecord 结构定义

本规范推荐采用**结构化日志**，统一使用 JSON 作为主格式，便于在后续处理环节中进行解析、过滤和重放。

本章对顶层记录机构 LogRecord 进行定义，所有日志类型均采用此结构进行封装。不同日志类型在 body 字段中承载各自特定的内容结构。

```typescript
export interface LogRecord {
  /**
   * 日志 Schema 版本号。
   * 遵循语义化版本规范（如 "1.0.0"）。
   */
  schemaVersion: string;

  /**
   * 事件在源端实际发生的时间（生成时间）。
   * 使用 ISO 8601 带时区的字符串以保持可读性和一致性。
   */
  timestamp: string;

  /**
   * 事件被监控系统（如 Kafka）接收或处理的时间（观测时间）。
   * 使用 ISO 8601 带时区的字符串以保持可读性和一致性。
   */
  observedTimestamp?: string;

  /**
   * Agent Identity Code - 智能体身份码
   * ACPs 体系中智能体的唯一标识，必须全局唯一且可追溯。
   */
  aic: string;

  /**
   * 日志事件唯一标识，单事件幂等去重键（详见 5.1.3）。
   *
   * - 由产生日志的主体在写每条日志行时生成一次，推荐 UUIDv7（无需中心协调即全局唯一、且按时间有序）。
   * - 一经源端写入即不可变，流经 Forwarder / Kafka / Writer 全程保持不变，下游不得重新生成。
   * - 源端未提供时，由消费端（Writer）按 5.1.3 的确定性内容哈希兜底生成。
   * - 各查询视图（AccessEventView.logId、MessageEventView.logId、AuditRecordView.logId 等）的 logId 即来源于此。
   */
  logId?: string;

  /**
   * ACPs日志类型
   * 本协议定义的六种日志类型之一。
   */
  logType: ACPsLogType;

  /**
   * 链路的全局唯一标识。
   * 采用 16 字节（128bit）随机数，序列化为 32 个十六进制字符的字符串。
   * 可以用UUID，但不包含连字符。
   */
  traceId?: string;

  /**
   * 当前 span 的局部标识。
   * 采用 8 字节（64bit）随机数，序列化为 16 个十六进制字符的字符串。
   */
  spanId?: string;

  /**
   * 父 Span ID
   *
   * 当前 span 的父 span 标识，通过日志重建调用链。
   *
   * - 根 span 的 parentSpanId 为 null 或 undefined
   * - 子 span 必须记录其父 span 的 spanId
   * - 配合 traceId 和 spanId 可完整追溯调用链路
   */
  parentSpanId?: string;

  /**
   * correlationId：业务级关联 ID。
   * 用于在业务语义上串联多条日志（例如订单号、任务号），与 traceId 的区别：
   * - traceId 由分布式追踪生成，强调技术调用链；
   * - correlationId 通常由业务系统自定义，强调领域/业务关联，可跨多个 trace 或独立存在。
   */
  correlationId?: string;

  /**
   * 日志级别的原始字符串表示。
   * 示例："INFO"、"ERROR"、"Critical"。
   */
  severityText?: string;

  /**
   * 规范化的数值级别。
   * 取值区间：1-4(TRACE)、5-8(DEBUG)、9-12(INFO)、13-16(WARN)、17-20(ERROR)、21-24(FATAL)。
   */
  severityNumber?: SeverityNumber;

  /**
   * 日志主体内容。
   * 可以是任意 JSON 兼容的结构，具体内容根据日志类型而定。
   */
  body?: AnyValue;

  /**
   * 日志来源描述。
   * 标识产生该日志的应用或基础设施。
   */
  resource?: Resource;

  /**
   * 附加的键值对属性。
   * 用于补充描述日志的上下文信息。
   */
  attributes?: Record<string, AnyValue>;

  /**
   * 数据完整性校验信息 (Digital Signature)。
   * 用于确保日志在传输过程中未被篡改，并验证来源的真实性（不可抵赖）。
   * 签名是在 Agent 端生成，随日志流转，直到最终入库。
   *
   * 虽然目前主要用于审计日志，但设计上支持对任意关键日志进行签名。
   */
  integrity?: {
    /**
     * 签名算法。
     * 首选推荐 "EdDSA" (Ed25519)，次选 "ES256" (ECDSA using P-256 and SHA-256)，备选 "RS256" (RSA Signature with SHA-256)。
     *
     * 算法对比：
     * | 算法   | 签名长度 | 安全强度        | 速度   | 兼容性                  |
     * |--------|----------|-----------------|--------|-------------------------|
     * | EdDSA  | 64 字节  | 128 位安全强度  | 最快   | 现代系统（SSH/TLS 1.3） |
     * | ES256  | 64 字节  | 128 位安全强度  | 快     | 广泛（WebAuthn/FIDO2）  |
     * | RS256  | 256 字节 | 112 位安全强度  | 较慢   | 最广泛（遗留系统）      |
     *
     * 推荐 EdDSA (Ed25519) 的原因：
     * - 性能最优：签名和验签速度均优于 ECDSA 和 RSA
     * - 实现简单：无需随机数，天然抗侧信道攻击，避免 ECDSA 的 k 值复用漏洞
     * - 密钥紧凑：公钥仅 32 字节，私钥仅 32 字节
     * - 无专利问题：完全开放，无使用限制
     * - 生态成熟：Go/Rust/Node.js/Python 等主流语言原生支持
     *
     * 本系统为新建环境，无历史兼容负担，因此首选 EdDSA。
     * ES256 作为备选，适用于需要与浏览器 Web Crypto API 交互的场景。
     * RS256 仅在需要兼容遗留系统时使用。
     *
     * 算法名称中已隐含哈希算法：签名时会先对待签数据计算哈希，再对哈希值进行非对称签名，
     * 这是数字签名的标准做法，无需在此额外定义。
     * @example "EdDSA"
     */
    alg: string;

    /**
     * 密钥 ID (Key ID)，必填。
     * 标识用于签名的具体密钥版本或证书序列号。
     * 虽然证书通常与 AIC 绑定，但考虑到密钥轮转（Key Rotation）和多版本共存，
     * 仅凭 AIC 无法唯一确定验签所需的公钥，因此需要 kid 明确指定。
     * 通常推荐取值为签名所用 AIC 证书的序列号（X.509 serialNumber 字段，十六进制大写字符串）。
     *
     * 【来源】：Agent 从 CA 获取证书后，直接从证书文件本地解析：
     *   cert = x509.load_pem_x509_certificate(pem_bytes)
     *   kid  = format(cert.serial_number, "X")   // e.g. "3F8A91B2C4D5E6F7"
     *
     * 【唯一性保证】：X.509 标准（RFC 5280 §4.1.2.2）要求同一 CA 签发的每张证书
     * 序列号必须唯一，因此 kid 在全局范围内可唯一定位一张证书及其公钥。
     *
     * 【密钥轮转】：Agent 证书续期后会获得新序列号，用新证书私钥签名时自动携带新
     * kid；验签方用旧 kid 仍可在 CA 侧查到旧公钥，新旧日志均可验签，无需额外版本管理。
     *
     * 验签时，Consumer 用 kid 直接向 CA 服务查询对应证书的公钥。
     * @example "3F8A91B2C4D5E6F7A8B9C0D1E2F3A4B5"
     */
    kid: string;

    /**
     * 数字签名。
     * 对日志关键字段进行签名后的 Base64 字符串。
     *
     * 【签名范围】：
     * 签名覆盖顶层公共字段 + 整个 body 对象：
     * - timestamp (防止重放)
     * - aic (防止伪造来源)
     * - logId (防止幂等键被篡改以绕过去重或制造碰撞)
     * - traceId, spanId, parentSpanId (防止链路篡改)
     * - correlationId (防止业务关联被篡改)
     * - logType (防止类型混淆)
     * - body (整个对象参与签名，防止内容篡改)
     *
     * 【规范化规则】：
     * 采用 RFC 8785 (JCS - JSON Canonicalization Scheme) 进行确定性序列化，
     * 确保签名和验签时的输入完全一致。主要规则包括：
     * - 对象的键按 Unicode 码点升序排列
     * - 可选字段：不存在时跳过，存在时参与签名
     * - 数值不使用科学计数法，不保留多余小数位
     * - 不包含空白字符（无缩进、无换行）
     */
    sig: string;
  };
}

/**
 * 类型安全 AnyValue 类型
 *
 * 与 TypeScript 的 `any` 不同，AnyValue 是受约束的动态类型：
 * - 类型安全：限定为特定类型的联合，编译时可检查
 * - 可序列化：只允许可 JSON 序列化的值（排除函数、Symbol、DOM 引用等）
 *
 * JSON 原生不支持二进制类型，字节数组（bytes）在 JSON 序列化时应使用 Base64 编码。
 * 例如：Uint8Array([0x01, 0x02, 0x03]) 序列化为 "AQID"。
 */
type AnyValue =
  | string
  | number
  | boolean
  | null
  | Uint8Array
  | AnyValue[]
  | { [key: string]: AnyValue };

/**
 * 严重级别 (Severity)。
 *
 * 本定义的数值与 OTel 规范中的基本级别（每组的第一个）一致。
 * OTel 为每个基本级别提供了 4 个细分级别（如 DEBUG, DEBUG2, DEBUG3, DEBUG4），
 * 用于在不同日志系统间映射时提供细粒度空间。
 */
export enum SeverityNumber {
  UNSPECIFIED = 0,
  TRACE = 1,
  DEBUG = 5,
  INFO = 9,
  WARN = 13,
  ERROR = 17,
  FATAL = 21,
}

/**
 * ACPs 日志类型，用于区分不同用途的日志。
 */
export type ACPsLogType =
  | "heartbeat"
  | "access"
  | "metrics"
  | "audit"
  | "message"
  | "system";

/**
 * 日志来源描述，标识产生该日志的应用或基础设施。
 *
 * [命名规范说明]
 * - LogRecord 中的字段（如 traceId, severityText）是数据模型的结构字段（Schema），用小驼峰（camelCase）命名。
 * - Resource 是语义约定（Semantic Conventions），本质上是字典（Map）里的 Key，使用点连接 (dot-notation) 命名，如 service.name。
 */
export interface Resource {
  // --- Service ---
  /** 服务的逻辑名称，如 "shoppingcart" */
  "service.name": string;
  /** 服务命名空间，如 "shop" */
  "service.namespace"?: string;
  /** 服务实例的唯一标识，如 "627cc493-f310-47de-96bd-71410b7dec09" */
  "service.instance.id"?: string;
  /** 服务版本，如 "1.0.0" */
  "service.version"?: string;

  // --- Deployment ---
  /** 部署环境名称，如 "production", "staging", "development" */
  "deployment.environment.name"?: string;

  // --- Host ---
  /** 主机名 */
  "host.name"?: string;
  /** 主机 ID */
  "host.id"?: string;
  /** 主机架构，如 "x86_64", "arm64", "amd64" */
  "host.arch"?: string;
  /** 主机 IP 地址列表 */
  "host.ip"?: string | string[];

  // --- Process ---
  /** 进程 ID */
  "process.pid"?: number;
  /** 进程可执行文件名称 */
  "process.executable.name"?: string;
  /** 进程命令行 */
  "process.command_line"?: string;

  // --- Container (K8s/Docker) ---
  /** 容器名称 */
  "container.name"?: string;
  /** 容器 ID */
  "container.id"?: string;
  /** 容器镜像名称 */
  "container.image.name"?: string;
  /** 容器镜像标签 */
  "container.image.tag"?: string;

  // --- Kubernetes ---
  /** K8s Pod 名称 */
  "k8s.pod.name"?: string;
  /** K8s Pod UID */
  "k8s.pod.uid"?: string;
  /** K8s Namespace 名称 */
  "k8s.namespace.name"?: string;
  /** K8s Node 名称 */
  "k8s.node.name"?: string;
  /** K8s Deployment 名称 */
  "k8s.deployment.name"?: string;

  // --- Cloud ---
  /** 云提供商，如 "aws", "azure", "gcp" */
  "cloud.provider"?: string;
  /** 云区域，如 "us-east-1" */
  "cloud.region"?: string;
  /** 云可用区，如 "us-east-1a" */
  "cloud.availability_zone"?: string;
  /** 云平台，如 "aws_ec2", "azure_vm" */
  "cloud.platform"?: string;

  [key: string]: AnyValue | undefined;
}

/**
 * 通用错误信息结构。
 * 用于 AccessLog, MessageLog 等多种日志类型中描述错误详情。
 */
export interface ErrorInfo {
  /** 错误代码，如 404, 500, 1001 */
  code?: number | string;
  /** 错误消息，如 "User not found" */
  message?: string;
  /** 额外错误数据，如验证失败的字段详情 */
  data?: AnyValue;
  /** 堆栈追踪 */
  stackTrace?: string;
}
```

### 5.1.3 日志事件幂等键（logId）

AMP 写入链路按 at-least-once 设计，同一条事件可能被重复投递或重放。为保证去重与点查的正确性，本协议定义顶层 `logId` 作为**单事件幂等键**；它同时是各查询视图（`AccessEventView.logId`、`MessageEventView.logId`、`AuditRecordView.logId` 等）中 `logId` 字段的来源。

幂等键需要满足的本质属性不是"全局唯一"，而是：**同一物理事件在重投递时键保持稳定**，且**不同事件几乎不可能撞键**。规范要求如下：

1. **源端生成、只生成一次**：`logId` 由产生日志的主体在写每条日志行时生成一次。推荐 **UUIDv7**（前缀为毫秒时间戳、其余为随机位，无需中心协调即全局唯一，且按时间有序，利于列式存储压缩与索引）；UUIDv4 亦可。源端生成正是稳定性的来源——它保证某条事件"一辈子只被打一次标"。
2. **不可变、下游不得重生成**：`logId` 一经源端写入，流经 Forwarder、事件流平台与 Writer 全程不变。任何下游组件都不得重新生成它，否则重投递会得到不同键，去重立即失效。
3. **缺省兜底（内容寻址）**：若源端未提供 `logId`，消费端（Writer）**必须**以确定性内容哈希兜底——对签名范围字段（`timestamp`、`aic`、`traceId`、`spanId`、`parentSpanId`、`correlationId`、`logType`、`body`，但**不含** `logId` 自身）按 RFC 8785 (JCS) 规范化后取哈希（如 SHA-256，Base64 编码）。兜底哈希**只能覆盖源端不可变内容**，不得纳入 `observedTimestamp` 等管线追加字段，否则同一事件的不同投递会算出不同键。
4. **兜底的固有局限**：内容哈希会把"所有参与字段完全相同的并发事件"合并为一条（如同一主体在同一毫秒、无 trace 上下文的两次完全相同请求）。要避免这种误并，源端应显式提供 `logId`。这一取舍与 AMP 的 at-least-once、"损失可见而非追求 exactly-once"的总体取向一致。
5. **不要使用窄字段组合**：诸如 `hash(aic, timestamp, traceId, spanId)` 的窄组合在无 trace 的并发事件上会退化为 `hash(aic, timestamp)` 并产生碰撞丢数，**不得**作为幂等键。

具体的去重执行机制（去重窗口存储、与增量物化视图的关系等）属于实现细节，由实现设计文档定义（见 AMP-Design 第 7 节与各 `AMP-API-Design-*` 专题）。

## 5.2 心跳状态格式的定义

### 5.2.1 HeartbeatBody 定义

心跳日志的 body 结构，表达智能体的存活状态。

```typescript
export interface HeartbeatBody {
  /**
   * 系统运行时间，单位秒
   * 表示从系统启动到当前的累计运行时间
   * @example 86400
   */
  uptimeSeconds?: number;
}
```

**签名要求**：心跳日志通常不需要签名。如需签名，整个 body 参与签名。

## 5.3 指标日志格式的定义

### 5.3.1 MetricsBody 定义

指标日志的 body 结构。主要包含系统负载与窗口汇总指标。

```typescript
export interface MetricsBody {
  /**
   * 系统运行时间，单位秒
   * 表示从系统启动到当前的累计运行时间
   * @example 86400
   */
  uptimeSeconds?: number;

  /**
   * 即时负载信息，反映当前资源占用与队列情况。
   * @example { activeTasks: 3, queuedTasks: 1, cpuUsage: 45.6, memoryUsage: 52.1 }
   */
  loadMetrics?: LoadMetrics;

  /**
   * 基于某个时间间隔窗口的汇总指标数组。可根据需要扩展更多窗口。
   * @example [ { window: "PT1M", requestPerSecond: 95.2, ... } ]
   */
  windowMetrics?: WindowMetrics[];
}

export interface LoadMetrics {
  /**
   * 当前正在执行的任务数量。
   * 数字越大表示越繁忙。
   * @example 12
   */
  activeTasks: number;

  /**
   * 等待调度或排队中的任务数量。
   * 为0时，表示无排队，系统可接收新任务。
   * 不为0时，表示有任务在排队等待处理。此时的activeTasks数目可以表达系统上限负载能力。
   * @example 0
   */
  queuedTasks: number;

  /**
   * 最大允许执行的任务数。
   * 用于表示系统的处理能力上限。应该是一个固定值，不随时间变化。
   * @example 20
   */
  maxActiveTasks?: number;

  /**
   * 最大队列长度。
   * 用于表示系统的排队能力上限。应该是一个固定值，不随时间变化。
   * @example 50
   */
  maxQueuedTasks?: number;

  /**
   * CPU 使用率，百分比（0-100）。
   * 表示当前资源占用。数字越高表示资源占用越多。
   * 不采集CPU核心数目等信息，避免暴露过多信息，而且对表达及时负载帮助不大。只需一个整体的CPU使用率指标，方便判断系统负载情况。
   * @example 72.8
   */
  cpuUsage?: number;

  /**
   * 内存使用率，百分比（0-100）。
   * 表示当前资源占用。数字越高表示资源占用越多。
   * 但是内存可能受缓存等影响，使用率可能会很高，毕竟内存是拿来用的，所以这个指标并不一定能单独反映系统负载情况。
   * 不对内存的具体使用情况进行采集，只需一个整体的内存使用率指标，方便判断系统负载情况。
   * @example 68.4
   */
  memoryUsage?: number;

  /**
   * 磁盘使用率，百分比（0-100）。
   * 可选指标，表示当前磁盘资源占用情况。数字越高表示资源占用越多。
   * 不是所有系统都需要采集磁盘使用率，只有当磁盘资源对服务的性能和稳定性有显著影响时才考虑采集。
   * @example 55.2
   */
  diskUsage?: number;

  /**
   * 入站网络带宽使用率，百分比（0-100）。
   * 可选指标，表示当前网络资源占用情况。数字越高表示资源占用越多。
   * 不是所有系统都需要采集网络带宽使用率，只有当网络资源对服务的性能和稳定性有显著影响时才考虑采集。
   * @example 43.7
   */
  networkInUsage?: number;

  /**
   * 出站网络带宽使用率，百分比（0-100）。
   * 可选指标，表示当前网络资源占用情况。数字越高表示资源占用越多。
   * 不是所有系统都需要采集网络带宽使用率，只有当网络资源对服务的性能和稳定性有显著影响时才考虑采集。
   * @example 47.5
   */
  networkOutUsage?: number;
}

export interface WindowMetrics {
  /**
   * 统计窗口长度，采用 ISO 8601 Duration 表示。
   * 比如：5分钟表示为 PT5M。一小时表示为 PT1H。一天表示为 P1D。2天5小时30分钟表示为 P2DT5H30M。
   * @example "PT5M"
   */
  window: string;

  /**
   * 统计窗口内的请求成功率，百分比（0-100）。
   * 具体什么算成功，比如4xx的错误是客户端的原因造成的，是否算作成功请求，由 Partner Agent 自行定义，但需保持一致性。
   * 由于错误率可以通过成功率计算得出，所以只需上报成功率一个指标，避免冗余。
   * @example 98.6
   */
  successRate: number;

  /**
   * 统计窗口内的总请求数。
   * @example 15900
   */
  requestTotal?: number;

  /**
   * 统计窗口内的平均请求速率（每秒请求数）。
   * @example 88.7
   */
  requestPerSecond?: number;

  /**
   * 统计窗口内的平均吞吐量（MB/s）。
   * @example 12.5
   */
  avgThroughputMBps?: number;

  /**
   * 统计窗口内的峰值吞吐量（MB/s）。
   * @example 25.3
   */
  peakThroughputMBps?: number;

  /**
   * 统计窗口内的平均请求时延（毫秒）。
   * @example 190
   */
  avgLatencyMs?: number;

  /**
   * 时延分位数（毫秒），使用 p90、p95、p99 三个常用分位，用于刻画尾时延表现。
   *
   * 具体含义：
   * - p90：90% 的请求时延 ≤ 该值 → 反映「大部分用户」的实际体验（比如 90% 的用户觉得速度快）；
   * - p95：95% 的请求时延 ≤ 该值 → 反映「更严格的用户体验」（覆盖 5% 的慢请求，适合对时延敏感的场景，如支付、实时交互）；
   * - p99：99% 的请求时延 ≤ 该值 → 反映「极端情况下的用户体验」（覆盖 1% 的极慢请求，避免因少数异常拖垮整体体验，比如电商下单、直播卡顿）。
   *
   * 这三个分位数形成了「梯度监控」：
   * - 若 p90 偏高 → 大部分用户感受到时延，需优先优化；
   * - 若 p95/p99 偏高但 p90 正常 → 只有少数用户遇到慢请求，可能是资源瓶颈（如 CPU 峰值、网络波动）或长尾请求（如复杂查询、大文件传输），需针对性排查；
   * - 若三者都正常 → 整体时延表现稳定，用户体验一致。
   *
   * 由于时延分位数已经可以反映请求时延的分布情况，所以不需要额外上报最小/最大时延等参数，避免冗余。
   */
  p99LatencyMs?: number;
  p95LatencyMs?: number;
  p90LatencyMs?: number;

  /**
   * 特别长尾的时延分位数（毫秒）。
   * 多数业务用 p90/p95/p99 足够，能覆盖主流体验、长尾和极端慢请求；再加更多分位收益有限，反而增加采集与存储成本。
   * 如果业务分布确实特别长尾，或要求分段 SLA，才考虑补 p50/p75/p80 等，用来观察分段差异。
   */
  p80LatencyMs?: number;
  p75LatencyMs?: number;
  p50LatencyMs?: number;
}
```

**签名要求**：指标日志通常不需要签名。如需签名，整个 body 参与签名。

## 5.4 访问日志格式的定义

### 5.4.1 AccessBody 定义

访问日志的 body 结构，记录交互过程。

设计说明：

- 本结构采用了"最大公约数"设计，同时兼容 HTTP 和 RPC (gRPC, Dubbo 等) 场景。
- 对于 HTTP：method=GET/POST, url=Path
- 对于 RPC：method=MethodName, url=Service/InterfaceName

```typescript
export interface AccessBody {
  /** 请求耗时（毫秒） */
  durationMs?: number;
  /** 请求 */
  request?: {
    /**
     * 请求方法。
     * - HTTP: 动词，如 "GET", "POST"。
     * - RPC: 方法名，如 "GetUser", "PlaceOrder"。
     */
    method?: string;

    /**
     * 请求资源标识（原始值，含路径参数）。
     * - HTTP: URL Path，如 "/api/v1/users/123"。
     * - RPC: 服务/接口全限定名，如 "com.example.UserService"。
     */
    url?: string;

    /**
     * 归一化路由模板，作为稳定的 endpoint 聚合维度（与 method 组合）。
     * - HTTP: 路由模板，如 "/api/v1/users/{id}"。
     * - RPC: 方法全限定名。
     *
     * 应由源端在仍掌握框架路由上下文时填写（这是唯一能拿到真实模板的环节）；
     * 源端缺省时，由采集/写入侧对 url 做稳定的路径参数归一化推导。
     * 不要直接把高基数的 url 当作聚合维度。
     */
    route?: string;

    /**
     * 请求头或元数据。
     * - HTTP: Headers。
     * - RPC: Metadata / Attachments。
     */
    headers?: Record<string, string>;

    /** 请求体大小（字节） */
    bodySizeBytes?: number;
  };
  /** 响应 */
  response?: {
    /**
     * 响应状态码。
     * - HTTP: 200, 404, 500。
     * - RPC: 0 (OK), 5 (NotFound) 等，建议统一映射或保留原始值。
     */
    statusCode?: number;

    /** 响应头或元数据 */
    headers?: Record<string, string>;

    /** 响应体大小（字节） */
    bodySizeBytes?: number;
  };
  /**
   * 调用方信息 (Caller)。
   *
   * 显式记录调用方信息的价值：
   * 1. 预聚合：无需 Trace 记录之间做 Join 即可实时计算服务拓扑图 (A -> B)。
   * 2. 抗采样：即使上游 Trace 数据被采样丢弃，当前日志仍保留完整的对端信息。
   * 3. 快速归因：运维排查时，直接在日志中看到是谁发起的调用，无需跳转 Trace 系统。
   */
  caller?: {
    /** 调用方的智能体身份码 (AIC)，用于身份识别与安全审计 */
    aic?: string;
    /** 调用方的服务名称，如 "order-service" */
    serviceName?: string;
    /** 调用方的 IP 地址 */
    ip?: string;
  };
  /**
   * 被调用方信息 (Callee)。
   * 通常指当前服务自己，但在网关或代理场景下，可能指下游服务。
   */
  callee?: {
    /** 被调用方的智能体身份码 (AIC) */
    aic?: string;
    /** 被调用方的服务名称，如 "payment-service" */
    serviceName?: string;
    /** 被调用方的 IP 地址 */
    ip?: string;
  };
  /** 错误信息（如有） */
  error?: ErrorInfo;
}
```

**签名要求**：访问日志通常不需要签名。如需签名，整个 body 参与签名。

### 5.4.2 Sampling Strategy

访问日志数据量通常巨大，全量采集可能带来过高的存储与处理成本。建议在 Agent 端或 Gateway 端实施采样策略：

1.  **固定比例采样（Probabilistic Sampling）**：
    - 简单粗暴，例如仅采集 10% 的流量。
    - 缺点：可能漏掉低频但重要的错误请求。

2.  **基于优先级的采样（Priority-based Sampling）**：
    - **强制采集**：所有 `ERROR` 级别的日志、耗时超过阈值（如 > 1s）的慢请求、特定 VIP 用户的请求。
    - **随机采样**：对 `INFO` 级别的正常请求进行 1% ~ 10% 的随机采样。

3.  **头部采样 vs 尾部采样**：
    - **头部采样（Head Sampling）**：在请求开始时决定是否采样（通常基于 TraceID）。优点是性能高，缺点是无法基于请求结果（如是否报错）做决策。
    - **尾部采样（Tail Sampling）**：请求完成后，根据结果决定是否保留。优点是能精准保留错误和慢请求，缺点是需要缓存整个请求周期的日志，内存开销大。
    - **推荐**：在 Agent 端采用“头部采样 + 关键特征强制保留”的混合模式；在中心化收集端（如 OpenTelemetry Collector）可实施尾部采样。

## 5.5 消息日志格式的定义

### 5.5.1 MessageBody 定义

消息日志的 body 结构，用于描述消息驱动架构中的一次收发或结算操作。兼容 Kafka、RabbitMQ 等常见消息中间件语义，并与 OpenTelemetry Messaging 语义约定对齐。

设计要点：每条消息日志通过 `eventType` 精确描述**一次**生命周期边事件（发送、接收或某一种结算）。同一条消息的发送、接收、确认是**多条独立日志**（各自拥有独立的顶层 `logId`，通过相同的 `messageId` / `correlationId` 在查询层归并为一条生命周期），不在一条日志里同时表达"已接收且已确认"。这样每个统计计数都能无歧义地对应到一种事件类型。消息"流向"（send/receive）不再作为源字段，而是由 `eventType` 派生。

```typescript
export interface MessageBody {
  /**
   * 消息生命周期边事件类型。
   * 每条消息日志精确描述【一次】操作；一次"接收"与随后的"确认"是两条独立日志。
   *
   * - send        生产者发送/发布消息
   * - receive     消费者取得/处理消息（pull/push 均归此）
   * - ack         结算：确认成功
   * - nack        结算：否认，通常重回队列
   * - reject      结算：拒绝并丢弃
   * - timeout     结算：处理或消费锁超时
   * - dead_letter 结算：进入死信
   */
  eventType:
    | "send"
    | "receive"
    | "ack"
    | "nack"
    | "reject"
    | "timeout"
    | "dead_letter";

  /**
   * 系统原生操作名（可选），仅用于保真与排查，不参与标准计数。
   * @example "publish"
   * @example "poll"
   * @example "basic.ack"
   */
  operationName?: string;

  /**
   * 消息系统类型
   * e.g., "kafka", "rabbitmq", "activemq", "rocketmq"
   */
  system: string;
  /**
   * 消息目的地信息
   * 对应 Kafka 的 Topic, RabbitMQ 的 Exchange/Queue
   */
  destination: {
    /**
     * 目的地名称
     * Kafka: Topic Name
     * RabbitMQ: Exchange Name (publish) or Queue Name (consume)
     */
    name: string;
    /**
     * 目的地类型
     * e.g., "topic", "queue", "exchange"
     */
    kind?: "topic" | "queue" | "exchange";
    /**
     * 虚拟主机 (RabbitMQ specific)
     * 默认为 "/"
     */
    virtualHost?: string;
  };
  /**
   * 消费分组名（消费侧事件填写）。
   * @example "indexer"
   */
  consumerGroupName?: string;
  /**
   * 订阅名（消费侧事件填写）。
   * @example "subscription-a"
   */
  subscriptionName?: string;
  /**
   * 消息路由信息
   */
  routing?: {
    /**
     * 路由键 (RabbitMQ) 或 Message Key (Kafka)
     * 用于决定消息分发到哪个分区或队列
     */
    key?: string;
    /**
     * 分区标识。采用字符串以兼容非 Kafka 系统，并区分"无分区概念"（字段缺省）与"0 号分区"。
     * @example "1"
     */
    partition?: string;
    /**
     * 消息偏移量 (Kafka specific)
     */
    offset?: number;
  };
  /** 消息唯一标识 ID */
  messageId?: string;
  /** 消息体大小 (字节) */
  payloadSizeBytes?: number;
  /**
   * 投递尝试次数（消费侧）。1 代表第一次投递，>1 代表重投；仅在 receive 与结算类事件上有意义。
   */
  deliveryAttempt?: number;
  /**
   * 结算补充信息，仅结算类事件（eventType 为 ack/nack/reject/timeout/dead_letter）填写。
   * 结算"结果"已由 eventType 表达，这里只补充耗时与原因，不再重复状态字段。
   */
  settlement?: {
    /** 从接收到本次结算的耗时 (ms)。 */
    latencyMs?: number;
    /** 结算原因（nack/reject/dead_letter/timeout 的具体原因）。 */
    reason?: string;
  };
  /** 错误信息 (如果操作失败) */
  error?: ErrorInfo;
  /** 扩展属性 */
  attributes?: Record<string, AnyValue>;
}
```

**签名要求**：消息日志通常不需要签名。如需签名，整个 body 参与签名。

## 5.6 审计日志格式的定义

### 5.6.1 AuditBody 定义

审计日志的 body 基本结构。

```typescript
export interface AuditBody {
  /** 操作者 (Actor) */
  actor: {
    /**
     * 用户或服务 ID
     * @example "user-12345"
     * @example "svc-payment-01"
     */
    id: string;
    /**
     * 用户类型: user, service, bot
     * @example "user"
     * @example "service"
     */
    type: string;
    /**
     * 用户名
     * @example "alice"
     * @example "payment-service"
     */
    name?: string;
    /**
     * 角色或权限组
     * @example "admin"
     * @example "editor"
     */
    role?: string;
    /**
     * 客户端 IP
     * @example "10.0.0.5"
     */
    ip?: string;
    /**
     * 客户端 UserAgent
     * @example "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)..."
     * @example "curl/7.64.1"
     */
    userAgent?: string;
  };
  /** 行为 (Action/Event) */
  action: {
    /**
     * 事件名称
     * @example "user.delete"
     * @example "order.create"
     */
    name: string;
    /**
     * 事件领域/类型
     * @example "order"
     */
    type: string;
    /**
     * 具体的 API 方法
     * @example "DELETE /api/users/bob"
     */
    method?: string;
  };
  /** 操作对象 (Target/Resource) */
  target: {
    /**
     * 资源类型
     * @example "user"
     * @example "order"
     */
    type: string;
    /**
     * 资源 ID
     * @example "user-67890"
     * @example "ord-998877"
     */
    id: string;
    /**
     * 资源名称
     * @example "bob"
     * @example "Order 998877"
     */
    name?: string;
    /**
     * 变更前快照
     * @example { "status": "active" }
     * @example { "amount": 100 }
     */
    before?: AnyValue;
    /**
     * 变更后快照
     * @example null
     * @example { "status": "deleted" }
     */
    after?: AnyValue;
  };
  /** 结果 (Result) */
  result: {
    /**
     * 结果状态
     * @example "success"
     */
    status: "success" | "failure" | "unknown";
    /** 详细原因 (如果是 failure) */
    reason?: string;
    /**
     * 错误码
     * @example "RESOURCE_NOT_FOUND"
     */
    errorCode?: string;
  };
}
```

**签名要求**：审计日志**必须签名**，整个 body 参与签名。

### 5.6.2 审计日志的防篡改方法

为了确保审计日志从**产生（Agent）**到**存储（Database）**的全链路完整性与不可抵赖性，本协议推荐采用"源端签名 + 存储链式校验"的双重防护机制。

#### 5.6.2.1 传输防篡改：源端数字签名

日志在产生后、经过 Kafka/Fluent Bit 等传输组件时，可能存在中间人篡改的风险。通过在日志中嵌入源端数字签名，可以确保：

- **完整性（Integrity）**：日志内容未被篡改。
- **不可抵赖（Non-repudiation）**：日志确实由该 AIC 对应的 Agent 产生。

**工作流程**：

1.  **签名生成（Agent 端）**：
    - 智能体在生成审计日志时，使用其私钥（Private Key）对日志的核心内容进行签名。
    - 签名范围包括顶层公共字段（`timestamp`、`aic`、`logId`、`traceId`、`spanId`、`parentSpanId`、`correlationId`、`logType`）以及整个 `body` 对象。
    - 签名结果存入顶层 `LogRecord.integrity.sig` 字段。

2.  **传输保护**：
    - 即使日志在经过 Kafka、Fluent Bit 等中间件时被恶意篡改（如修改了 `result.status`），由于中间件无法伪造对应的签名，消费端在验签时会发现不一致。

3.  **验签与入库（Consumer/Storage 端）**：
    - 审计日志服务在消费日志时，用 `integrity.kid`（证书序列号）向 CA 服务查询对应证书的公钥（Public Key）。
    - 验证 `integrity.sig` 是否有效。
    - 只有验签通过的日志才会被标记为"可信"并写入审计数据库；验签失败的日志应触发严重告警。

#### 5.6.2.2 存储防篡改：链式哈希

日志入库后，还可能面临 DBA 或内部攻击者篡改历史记录的风险。为满足不可篡改特性，建议在关系型数据库基础上实现轻量级"链式存储"：

- 每条审计日志应包含**上一条日志的哈希值（Previous Hash）**和**本条日志的哈希值（Current Hash）**。
- `Current Hash = Hash(当前日志关键字段 + Previous Hash)`。
- 这样既能校验单条数据的完整性，又能形成哈希链以防止历史数据被篡改或删除。

# 6 AMP API 定义

## 6.1 查询 API 通用规范

AMP Provider 基于 AMP 日志入库后的稳定读模型提供 HTTP 查询接口。AMP API 不接收 Agent 直接上报日志，也不重复定义 DSP 的 Snapshot / Changes / Webhook 同步语义；跨系统复制稳定读模型时，应通过 DSP 或本规范中特别声明的同步子协议完成。

AMP API 统一基础路径以 `{AMP_BASE_URL}` 表示，由部署方配置（比如 `/amp/v1`）。六类日志对应的 API 前缀如下：

| 日志类型  | API 前缀                      | 最低能力集                              | 可选 Profile                            |
| --------- | ----------------------------- | --------------------------------------- | --------------------------------------- |
| heartbeat | `{AMP_BASE_URL}/heartbeat`    | 单 AIC 存活查询、批量存活查询、summary  | alive set snapshot + delta sync         |
| metrics   | `{AMP_BASE_URL}/metrics`      | snapshot 查询、series 查询              | rankings、SLO evaluate、capacity        |
| access    | `{AMP_BASE_URL}/access`       | events 查询、operations 聚合            | errors、slow requests、trace、topology  |
| message   | `{AMP_BASE_URL}/message`      | events、lifecycles、deadletters         | destination state、throughput           |
| audit     | `{AMP_BASE_URL}/audit`        | records、record detail、summary         | export、integrity verify、chain anchors |
| system    | `{AMP_BASE_URL}/system`       | system events 查询                      | incidents、trend、component attribution |

### 6.1.1 认证、传输与错误响应

- 推荐认证方式为 `mTLS`，证书绑定调用主体（principal）。Agent 类主体应绑定 AIC；平台、运维台、监管端、计费系统等非 Agent Consumer 可绑定组织、租户、角色或服务账号。
- 部署在受控内网时，可以叠加 Bearer Token、JWT 或网关鉴权。
- 所有 AMP API 默认使用 `application/json`。
- 错误响应遵循 RFC 9457 Problem Details，并必须携带 AMP 错误码。
- 所有时间字段均使用 ISO 8601 带时区字符串。

公共 HTTP 状态语义（六组 API 通用，各组不再单独定义）：

- 认证信息缺失或无效时返回 `401 AMP_UNAUTHENTICATED`；主体已认证但无权访问该类监控数据时返回 `403 AMP_ACCESS_DENIED`。
- 调用频率超过 Provider 配额或被限流时返回 `429 AMP_RATE_LIMITED`，并可通过 `Retry-After` 提示重试间隔。
- Provider 内部未分类异常返回 `500 AMP_INTERNAL_ERROR`；对可归因的依赖不可用、读模型显著滞后等情况，应优先使用更具体的 `503` 错误码（见各组规范）。

```typescript
/**
 * AMP 统一错误响应体，遵循 RFC 9457 Problem Details。
 * 所有 4xx/5xx 响应均使用本结构，并必须携带机器可读的 AMP 错误码 code。
 */
export interface AMPProblemDetails {
  /** 错误类型的 URI 标识；无特定类型时可用 "about:blank"。 */
  type?: string;
  /** 人类可读的简短错误标题。 */
  title: string;
  /** HTTP 状态码，与响应状态行一致（如 400、404、503）。 */
  status: number;
  /** 当前问题的详细描述，便于定位排查。 */
  detail?: string;
  /** AMP 错误码，如 AMP_INVALID_TIME_RANGE。供调用方按错误分支处理。 */
  code: string;
  /** 发生该问题的具体资源实例标识（通常为请求路径）。 */
  instance?: string;
  /** 允许携带额外扩展字段。 */
  [key: string]: AnyValue | undefined;
}
```

### 6.1.2 通用请求与响应模型

```typescript
/**
 * 统一时间范围，表示左闭右开区间 [startAt, endAt)。
 * 两端均为 ISO 8601 带时区字符串。
 */
export interface AMPTimeRange {
  /** 起始时间（含） */
  startAt: string;
  /** 结束时间（不含） */
  endAt: string;
}

/**
 * 通用分页请求。采用游标（cursor）分页而非 offset，避免数据追加导致翻页错位或重复。
 */
export interface AMPPaginationRequest {
  /** 单页最大条数，建议默认 50，最大 500 */
  limit?: number;
  /** 光标分页游标；重放时必须携带与上一页完全一致的查询参数和排序条件 */
  cursor?: string;
}

/**
 * 排序条件。可在 sort 数组中指定多个，按数组顺序逐级排序。
 */
export interface AMPSortSpec {
  /** 排序字段，取值受各组 API 的排序白名单约束 */
  field: string;
  /** 排序方向 */
  order?: "asc" | "desc";
}

/**
 * 通用查询请求，是各类列表 / 聚合 / 导出查询的基础请求体；
 * 多数 API 组通过 `extends AMPQueryRequest` 在其上追加专属字段。
 */
export interface AMPQueryRequest {
  /** 查询的时间范围。对事件流列表、聚合和导出类接口通常必填。 */
  timeRange?: AMPTimeRange;
  /** 组合过滤器。 */
  filter?: AMPFilter;
  /** 受限关键词检索；仅在 API 组明确声明支持的字段上生效。 */
  keyword?: string;
  /** 排序条件。 */
  sort?: AMPSortSpec[];
  /** 分页参数。 */
  page?: AMPPaginationRequest;
  /** 是否附带原始日志或原始 body 摘要。各组可选择支持。 */
  includeRawLog?: boolean;
}

/**
 * 单个字段的样本覆盖率信息。当读模型滞后或部分分片超时时，
 * 用于量化某个聚合 / 填充字段的可信度，避免调用方误把"部分数据"当作全量结果。
 */
export interface AMPFieldSampleCoverage {
  /** 参与该字段聚合或填充的有效样本数。 */
  availableSamples: number;
  /** 理论上应参与该字段聚合或填充的样本总数。 */
  totalSamples: number;
  /** 当前字段的样本覆盖率，等于 availableSamples / totalSamples。 */
  coverageRatio: number;
  /** 当前字段在本次响应中的可用状态。 */
  status: "complete" | "partial" | "unavailable";
}

/**
 * 查询响应的元信息。暴露数据新鲜度、分页游标与结果完整性，
 * 是调用方判断"结果是否可信、是否还有下一页"的核心依据。
 */
export interface AMPResponseMeta {
  /** 当前读模型已处理到的事件时间水位。 */
  dataFreshnessAt: string;
  /** 事件流进入读模型的估计滞后（毫秒）。 */
  ingestionLagMs?: number;
  /** 下一页游标。没有更多数据时省略。 */
  nextCursor?: string;
  /** 近似总量，可选。 */
  approximateTotal?: number;
  /** 是否为部分结果，例如后端某个分片超时或读模型滞后。 */
  partial?: boolean;
  /** 按字段路径标记为 partial 或 unavailable 的响应字段。 */
  partialDataFields?: string[];
  /** 字段级样本覆盖率信息，key 为响应字段路径。 */
  sampleCoverage?: Record<string, AMPFieldSampleCoverage>;
  /** Provider 实现侧的查询耗时（毫秒）。 */
  elapsedMs?: number;
}

/**
 * 通用分页查询响应包络。items 为当前页结果，meta 描述新鲜度与翻页信息。
 */
export interface AMPQueryResponse<T> {
  /** 当前页结果集。 */
  items: T[];
  /** 响应元信息（新鲜度、翻页游标、部分结果标记等）。 */
  meta: AMPResponseMeta;
}

/**
 * 异步任务受理回执。用于导出、完整性校验等长耗时操作；
 * 服务端返回 202 时携带，调用方凭 taskId 轮询任务状态与结果。
 */
export interface AMPTaskAccepted {
  /** 异步任务 ID。 */
  taskId: string;
}
```

通用查询规则：

1. 对 append-only 事件流上的列表查询、聚合查询和导出查询，调用方必须提供有界 `timeRange`；缺失时返回 `400 AMP_INVALID_TIME_RANGE`。
2. `filter`、`sort`、聚合 `groupBy`，以及 `keyword` 覆盖字段，都必须出现在对应 API 组声明的白名单中；未声明字段不得静默支持。
3. 对天然按时间追加的资源，如果调用方未提供 `sort`，默认排序为 `timestamp desc`；若某组 API 不适用该默认值，必须在该组规范中单独声明。
4. `keyword` 是受限检索入口，不等同于 `raw_log` 全文检索。
5. 使用 `page.cursor` 翻页时，除 `cursor` 外的查询参数和排序条件必须与上一页完全一致；若不一致，Provider 应返回 `400 AMP_CURSOR_INVALID`。
6. `includeRawLog` 默认为关闭；只有 API 组明确声明支持时，Provider 才应返回原始日志或原始 body 摘要。

### 6.1.3 复杂过滤模型

```typescript
/**
 * 组合过滤器，支持任意层级嵌套，与 ADP DiscoveryFilter 共享同一套逻辑语义。
 * 同层的 conditions 与 groups 按 logic 组合；借助 groups 可表达 (A and B) or C 这类复合条件。
 */
export interface AMPFilter {
  /** 当前层的过滤条件。与 groups 中的子条件组按 logic 指定的逻辑关系组合。 */
  conditions?: AMPFilterCondition[];
  /** 当前层的嵌套子条件组，每个子组可拥有独立的 logic。 */
  groups?: AMPFilter[];
  /** 当前层条件和子组之间的逻辑关系。"not" 表示对本层整体结果取反。默认 "and"。 */
  logic?: "and" | "or" | "not";
}

/** 单个过滤条件，语义为「字段 field 与值 value 满足运算符 op 所定义的关系」。 */
export interface AMPFilterCondition {
  /** 字段路径，使用点号分隔表示嵌套 */
  field: string;
  /** 匹配运算符 */
  op: AMPFilterOperator;
  /** 匹配值；具体形态由 op 和字段类型决定 */
  value?: AnyValue;
}

/**
 * 过滤运算符全集。字符串类运算符默认大小写不敏感，后缀 `Cs`（Case-sensitive）为其大小写敏感变体。
 * 若字段或后端不支持某运算符，必须返回 422 AMP_UNSUPPORTED_OPERATOR，不得静默降级为其它语义。
 */
export type AMPFilterOperator =
  // 相等与存在性
  | "eq" // 等于
  | "ne" // 不等于
  | "exists" // 字段是否存在（value 为 true / false）
  // 数值 / 时间比较
  | "gt" // 大于
  | "gte" // 大于等于
  | "lt" // 小于
  | "lte" // 小于等于
  | "between" // 闭区间 [a, b]，value 为二元数组
  // 集合成员
  | "in" // 取值属于给定集合
  | "nin" // 取值不属于给定集合
  // 字符串匹配（大小写不敏感）
  | "contains" // 包含子串
  | "notContains" // 不包含子串
  | "startsWith" // 前缀匹配
  | "endsWith" // 后缀匹配
  // 字符串匹配（大小写敏感变体，语义同上）
  | "eqCs"
  | "neCs"
  | "inCs"
  | "ninCs"
  | "containsCs"
  | "notContainsCs"
  | "startsWithCs"
  | "endsWithCs"
  // 数组元素匹配（字段值为数组时）
  | "anyOf" // 与给定集合存在交集
  | "allOf" // 包含给定集合的全部元素
  | "noneOf" // 与给定集合无交集
  // 数组长度
  | "size" // 长度等于
  | "sizeGt" // 长度大于
  | "sizeGte" // 长度大于等于
  | "sizeLt" // 长度小于
  | "sizeLte" // 长度小于等于
  // 对象键存在性（字段值为对象时）
  | "hasKey" // 含指定键
  | "hasNoKey" // 不含指定键
  | "hasAnyKey" // 含给定键集合中的任一键
  | "hasAllKeys"; // 含给定键集合中的全部键
```

`AMPFilter` 与 ADP `DiscoveryFilter` 保持同一套逻辑语义：`and` 表示所有条件/子组均满足，`or` 表示至少一个满足，`not` 表示对本层整体结果取反。字符串运算符默认大小写不敏感；后缀 `Cs` 表示大小写敏感变体。若某 API 组或后端存储不支持请求中的字段路径或运算符，Provider 必须返回 `422 AMP_UNSUPPORTED_FIELD` 或 `422 AMP_UNSUPPORTED_OPERATOR`，不得静默降级为其它语义。

### 6.1.4 结果新鲜度与一致性

AMP 读模型不是事务型同步视图，查询响应必须暴露真实事件时间水位。

| 日志类型  | 典型可见延迟            | `dataFreshnessAt` 定义                       | `AMP_READ_MODEL_LAGGING` 推荐阈值 |
| --------- | ----------------------- | -------------------------------------------- | --------------------------------- |
| heartbeat | < 1 秒                  | heartbeat writer 已消费到的事件时间水位      | 5,000 ms                          |
| metrics   | 5~30 秒                 | metrics 读模型已持久化并可聚合的事件时间水位 | 150,000 ms                        |
| access    | 5~60 秒                 | access 读模型已落库并可查询的事件时间水位    | 300,000 ms                        |
| message   | 5~60 秒                 | message 读模型已落库并可查询的事件时间水位   | 300,000 ms                        |
| audit     | 1~10 秒（签名校验耗时） | audit 读模型已验签并提交的事件时间水位       | 60,000 ms                         |
| system    | 5~60 秒                 | system 读模型已索引并可见的事件时间水位      | 300,000 ms                        |

本节新鲜度要求适用于所有携带 `AMPResponseMeta` 的响应，包括列表、聚合，以及按各组规范声明为 `{ data, meta }` 信封的单资源读（如 Heartbeat 的 `/liveness/{aic}`、`/summary`）。若某端点按组规范返回裸资源（无 `meta`，如 `GET /traces/{traceId}`、`GET /records/{auditId}`），该组规范须声明其新鲜度暴露方式（例如响应头或自身字段），否则视为不在本要求约束内。

任何（携带 `meta` 的）查询响应都必须在 `meta.dataFreshnessAt` 给出真实读模型事件时间水位，并在 `meta.ingestionLagMs` 中暴露 `now() - dataFreshnessAt` 的估算值。当 `ingestionLagMs` 超过对应类型阈值时，Provider 必须返回 `503 AMP_READ_MODEL_LAGGING`，或返回 `200` 且设置 `meta.partial = true`。如果某些聚合字段只基于部分样本计算，Provider 必须在 `meta.partialDataFields` 和 `meta.sampleCoverage` 中标记。

若查询时间范围超出对应读模型的保留窗口，Provider 必须返回 `422 AMP_OUT_OF_RETENTION`。

### 6.1.5 公共错误码

| 错误码                     | HTTP | 说明                                                           |
| -------------------------- | ---- | -------------------------------------------------------------- |
| `AMP_UNAUTHENTICATED`      | 401  | 缺少有效认证信息，或认证已失效                                 |
| `AMP_INVALID_FILTER`       | 400  | 过滤结构不合法                                                 |
| `AMP_UNSUPPORTED_FIELD`    | 422  | 过滤、排序或聚合引用了该 API 组未声明支持的字段路径            |
| `AMP_UNSUPPORTED_OPERATOR` | 422  | 指定字段不支持该运算符                                         |
| `AMP_INVALID_TIME_RANGE`   | 400  | 时间范围为空、倒置、超出允许上限，或在要求有界查询的端点上缺失 |
| `AMP_OUT_OF_RETENTION`     | 422  | 时间范围已超出该类型的有效保留窗口                             |
| `AMP_CURSOR_INVALID`       | 400  | 分页游标无效、已过期，或与当前查询参数/排序条件不匹配          |
| `AMP_NOT_FOUND`            | 404  | 请求的单个资源在当前保留窗口内不存在                           |
| `AMP_RESULT_TOO_LARGE`     | 413  | 请求范围过大，需要收缩时间窗口或过滤条件                       |
| `AMP_READ_MODEL_LAGGING`   | 503  | 读模型显著滞后于事件流，当前结果不可用                         |
| `AMP_ACCESS_DENIED`        | 403  | 调用方无权限访问该类监控数据                                   |
| `AMP_RATE_LIMITED`         | 429  | 调用频率超过 Provider 配额或被限流                             |
| `AMP_INTERNAL_ERROR`       | 500  | Provider 内部未分类异常                                        |

## 6.2 Heartbeat API 规范

Heartbeat API 前缀为 `{AMP_BASE_URL}/heartbeat`，用于查询主体当前 liveness。Heartbeat 只表达存活迹象，不表达 health、readiness 或 maintenance。

Heartbeat 的 alive 集合跨系统复制采用本规范声明的 **alive-delta 同步子协议**（Sync Profile）：Consumer 先通过 `/sync/snapshot` 完成全量自举，再订阅增量 delta log 持续追赶。该子协议在传输层**标准化采用 Kafka** 作为 delta log，`/sync/info` 暴露的 `kafkaTopic`、`shardCount` 以及 `AliveDeltaEnvelope` 的分片化 `seq` 即为其线缆契约。这是一个有意的传输耦合，用于保证不同 Provider 与 Consumer 之间可互操作；不提供 Kafka 的部署可不启用 Sync Profile（`/sync/*` 返回 `404 AMP_HEARTBEAT_SYNC_DISABLED`），但一旦启用，就必须遵循本节定义的 Kafka 传输与分片语义。Core 查询能力（liveness、summary 等）不依赖该子协议。

### 6.2.1 资源模型

```typescript
/**
 * 主体（AIC）的存活视图。liveness 只表达"是否仍在发心跳"，
 * 不代表健康度（health）、就绪（readiness）或维护（maintenance）状态。
 */
export interface HeartbeatLivenessView {
  /** 智能体身份码，被查询的主体。 */
  aic: string;
  /** 是否存活：在服务端当前时间下，静默时长是否仍在存活阈值内。 */
  isAlive: boolean;
  /** 存活状态：alive=近期有心跳；silent=超过静默阈值未见心跳。 */
  livenessState: "alive" | "silent";
  /** 最近一次收到该主体心跳的时间。 */
  lastSeenAt: string;
  /** 心跳事件在源端的产生时间（与 lastSeenAt 不同则反映传输/处理延迟）。 */
  sourceTimestamp?: string;
  /** 距今静默时长（秒），约等于 now - lastSeenAt。 */
  silenceDurationSeconds: number;
}

/** 静默排行项，用于按静默时长对主体排序，定位长时间失联的主体。 */
export interface HeartbeatSilenceRankItem {
  /** 智能体身份码。 */
  aic: string;
  /** 最近一次心跳时间。 */
  lastSeenAt: string;
  /** 当前静默时长（秒）。 */
  silenceDurationSeconds: number;
}

/** 静默排行查询请求（POST /silence/top）。 */
export interface HeartbeatSilenceTopRequest {
  /** 返回静默最久的前 N 个主体，受 Provider 上限约束。 */
  topN?: number;
  /** 仅返回静默时长 ≥ 该值（秒）的主体。 */
  minSilenceSeconds?: number;
  /** 仅返回静默时长 ≤ 该值（秒）的主体。 */
  maxSilenceSeconds?: number;
  /** 是否仅统计当前处于 silent 状态的主体。 */
  onlySilent?: boolean;
}

/** 存活情况汇总视图（GET /summary），给出整体存活/静默分布。 */
export interface HeartbeatSummaryView {
  /** 已知主体总数（曾上报心跳且尚未被驱逐）。 */
  totalKnown: number;
  /** 当前存活主体数。 */
  aliveCount: number;
  /** 当前静默主体数。 */
  silentCount: number;
  /**
   * 静默时长累计分桶（cumulative，"le" 语义）：leSeconds 为桶的上界（秒），
   * count 为当前已知主体中 silenceDurationSeconds ≤ leSeconds 的累计数量（含落入更小桶的主体）。
   * 因此 count 随 leSeconds 单调不减；需要单个区间（非累计）的数量时，由调用方对相邻桶做差。
   */
  silenceBuckets?: Array<{ leSeconds: number; count: number }>;
  /** 是否为部分结果（例如部分分片未响应）。 */
  partial?: boolean;
  /** 已成功响应的分片数。 */
  respondedShardCount?: number;
  /** 分片总数。 */
  totalShardCount?: number;
}

/**
 * 心跳响应的元信息：在通用 AMPResponseMeta（新鲜度、翻页、部分结果等）之上，
 * 追加本次存活判定所用的时间基准与阈值。
 * Heartbeat 全部查询端点（含 /liveness/{aic} 与 /summary 两个单资源读）统一以本类型作为响应 meta，
 * 因此 dataFreshnessAt / ingestionLagMs 等通用新鲜度字段在心跳查询上同样必填（见 6.1.4）。
 */
export interface HeartbeatResponseMetaExt extends AMPResponseMeta {
  /** 本次存活判定所基于的服务端时间。 */
  evaluatedAt: string;
  /** 判定 silent 的静默阈值（秒）：静默超过该值即视为静默。 */
  silenceThresholdSeconds: number;
  /** 驱逐阈值（秒）：静默超过该值后主体从已知集合中移除。 */
  evictAfterSeconds: number;
}

/** alive set 同步条目，描述一个当前存活主体的最小状态。 */
export interface AliveSetEntry {
  /** 智能体身份码。 */
  aic: string;
  /** 最近一次心跳时间。 */
  lastSeenAt: string;
  /** 心跳在源端的产生时间。 */
  sourceTimestamp?: string;
}

/**
 * alive set 增量同步信封（NDJSON 流中的一行），用于跨系统复制"当前存活集合"。
 * 远端通过 snapshot 全量 + 后续增量事件（enter/refresh/leave）维持一份存活副本。
 */
export interface AliveDeltaEnvelope {
  /** 所属分片。 */
  shard: string;
  /**
   * 分片内单调递增序号，用于断点续传与去重；取值为十进制非负整数的字符串编码。
   * 仅对同一 shard 有序。比较 seq（如断点续传判断 seq > cutover）必须按数值比较，不得按字符串字典序比较。
   */
  seq: string;
  /** 信封类型，固定为 "amp-alive-delta"。 */
  type: "amp-alive-delta";
  /**
   * 同步对象 ID（注意：不是每事件唯一 ID），固定为 urn:amp:alive:<aic>。
   * 同一主体的所有事件共享同一 id，由 version 区分先后；Consumer 按 (id, version) 幂等收敛。
   */
  id: string;
  /**
   * 对象版本号，取值与 seq 相同（十进制整数字符串）。
   * 同一 id 只保留更大的 version；比较必须按数值进行。
   *
   * 为何分片内 seq 可直接充当对象版本：同一 aic 的事件恒定路由到同一分片
   * （partition = shard index，按 aic 分片），故一个 id（urn:amp:alive:<aic>）生命周期内的
   * 全部事件都落在同一分片、被该分片 seq 全序化，seq 单调即版本单调。
   * 两字段并存各有职责：seq 是分片级重放/断点续传/去重游标（消费者沿 delta log 追赶时使用），
   * version 则让仅按 (id, version) 收敛的通用 upsert 消费者无需理解分片即可幂等合并。
   */
  version: string;
  /** 操作：upsert=新增/更新条目；delete=移除条目。 */
  op: "upsert" | "delete";
  /** 事件语义：snapshot=快照条目；enter_alive=新进入存活；refresh_alive=存活刷新；leave_alive=离开存活。 */
  kind: "snapshot" | "enter_alive" | "refresh_alive" | "leave_alive";
  /** 关联的存活条目数据（op=delete 时可省略）。 */
  payload?: AliveSetEntry;
}

/**
 * alive set 同步参数（GET /sync/info 响应）。
 * 描述 alive-delta 同步子协议的传输契约：增量日志主题、分片数、压缩发射间隔、
 * 保留窗口与各分片当前发布水位。Consumer 据此定位 delta log 并完成自举与追赶。
 */
export interface HeartbeatSyncInfo {
  /** 同步信封类型，固定为 "amp-alive-delta"。 */
  type: "amp-alive-delta";
  /** 同步子协议的 Schema 版本。 */
  schemaVersion: string;
  /** snapshot 流的内容类型，固定为 "application/x-ndjson"。 */
  snapshotContentType: "application/x-ndjson";
  /** 承载 alive 增量事件的 Kafka 主题名。 */
  kafkaTopic: string;
  /** 分片数；Kafka 分区数与之一致，且 partition = shard index。 */
  shardCount: number;
  /** refresh_alive 事件的压缩发射间隔（秒）。 */
  refreshEmitIntervalSeconds: number;
  /** delta log 的最小保留时长（小时），用于保护 Consumer 的重放窗口。 */
  deltaRetentionHours: number;
  /** 各分片当前已连续发布的高水位序号（shard -> seq）。 */
  currentPublishedSeqByShard: Record<string, string>;
}

/**
 * alive snapshot 流的首行元信息（GET /sync/snapshot 响应的第一行）。
 * 其后每行是一条 kind = "snapshot" 的 AliveDeltaEnvelope。
 * Consumer 用 cutoverSeqByShard 确定增量追赶在 delta log 上的起始位置。
 */
export interface AliveSnapshotMeta {
  /** 记录类型，固定为 "snapshot-meta"。 */
  recordType: "snapshot-meta";
  /** 同步信封类型，固定为 "amp-alive-delta"。 */
  type: "amp-alive-delta";
  /** 本次 snapshot 各分片的切换序号（shard -> seq）：增量追赶从该序号之后开始。 */
  cutoverSeqByShard: Record<string, string>;
  /** snapshot 生成时间。 */
  generatedAt: string;
}
```

### 6.2.2 可过滤字段

| 字段路径                 | 运算符                              | 说明                       |
| ------------------------ | ----------------------------------- | -------------------------- |
| `aic`                    | `eq`、`in`                          | AIC 字面匹配               |
| `isAlive`                | `eq`                                | 服务端当前时间下的存活判定 |
| `livenessState`          | `eq`                                | 查询层即时计算的状态       |
| `silenceDurationSeconds` | `gt`、`gte`、`lt`、`lte`、`between` | 当前静默时长               |

可排序字段（`sort`）白名单：`lastSeenAt`（按最近心跳时间排序）与 `silenceDurationSeconds`（与 `lastSeenAt` 反向等价），并以 `aic` 作为稳定的次级排序键。其余字段排序返回 `422 AMP_UNSUPPORTED_FIELD`。

### 6.2.3 端点

| Method | Path              | 请求体 / 参数                | 响应体                                       | Profile   |
| ------ | ----------------- | ---------------------------- | -------------------------------------------- | --------- |
| GET    | `/liveness/{aic}` | path `aic`                   | `{ data: HeartbeatLivenessView; meta: HeartbeatResponseMetaExt }`              | Core      |
| POST   | `/liveness/query` | `AMPQueryRequest`            | `AMPQueryResponse<HeartbeatLivenessView>`（`meta` 为 `HeartbeatResponseMetaExt`）    | Core      |
| POST   | `/silence/top`    | `HeartbeatSilenceTopRequest` | `AMPQueryResponse<HeartbeatSilenceRankItem>`（`meta` 为 `HeartbeatResponseMetaExt`） | Analytics |
| GET    | `/summary`        | 无                           | `{ data: HeartbeatSummaryView; meta: HeartbeatResponseMetaExt }`               | Core      |
| GET    | `/sync/info`      | 无                           | `HeartbeatSyncInfo`                          | Sync      |
| GET    | `/sync/snapshot`  | 无                           | `application/x-ndjson` alive snapshot        | Sync      |

`/sync/info` 响应体为 `HeartbeatSyncInfo`（见 6.2.1）。`/sync/snapshot` 为 `application/x-ndjson` 流：第一行为 `AliveSnapshotMeta`（见 6.2.1），后续每行为一条 `kind = "snapshot"` 的 `AliveDeltaEnvelope`。

响应信封约定：Heartbeat 的两个单资源读端点（`/liveness/{aic}`、`/summary`）有意使用 `{ data, meta }` 信封而非裸资源，以便与列表/聚合端点一致地携带 `meta.dataFreshnessAt` 等新鲜度字段（见 6.1.4）。这四个查询端点的 `meta` 类型统一为 `HeartbeatResponseMetaExt`（继承 `AMPResponseMeta`）。`/sync/*` 不属于读模型查询，沿用各自定义的响应体，不套用该信封。

### 6.2.4 查询约束与错误码

- `GET /liveness/{aic}` 未命中当前真相源时返回 `404 AMP_HEARTBEAT_AIC_UNKNOWN`，不得返回 `isAlive=false` 的占位对象。
- `POST /liveness/query` 如果没有 `aic` 过滤，则必须提供可下推的 selective filter 或 cursor；否则返回 `400 AMP_QUERY_REQUIRES_SELECTIVE_FILTER`。
- `POST /silence/top` 的 `topN` 必须受 Provider 上限限制；静默区间非法时返回 `400 AMP_HEARTBEAT_SILENCE_RANGE_INVALID`。
- snapshot + delta sync 未启用时，`/sync/*` 返回 `404 AMP_HEARTBEAT_SYNC_DISABLED`。

Heartbeat 专用错误码（在 6.1.5 公共错误码之外）：

| 错误码                                | HTTP | 说明                                                            |
| ------------------------------------- | ---- | --------------------------------------------------------------- |
| `AMP_QUERY_REQUIRES_SELECTIVE_FILTER` | 400  | `liveness/query` 未提供任何可下推的 selective filter，且未携带 cursor |
| `AMP_HEARTBEAT_SILENCE_RANGE_INVALID` | 400  | `silence/top` 的静默区间参数非法                                |
| `AMP_HEARTBEAT_SYNC_VIEW_UNSUPPORTED` | 400  | 请求了当前部署不支持的同步视图类型                              |
| `AMP_HEARTBEAT_AIC_UNKNOWN`           | 404  | 单 AIC 查询未命中当前真相源                                     |
| `AMP_HEARTBEAT_SYNC_DISABLED`         | 404  | 当前部署未启用 snapshot + delta 同步                            |
| `AMP_HEARTBEAT_SNAPSHOT_UNAVAILABLE`  | 503  | snapshot 导出暂不可用（如真相源不可达或导出被限流）             |
| `AMP_HEARTBEAT_DELTA_LOG_UNHEALTHY`   | 503  | 增量日志发布显著滞后或不可达，无法保证增量新鲜                  |

## 6.3 Metrics API 规范

Metrics API 前缀为 `{AMP_BASE_URL}/metrics`，用于查询指标快照、历史时序、排行、SLO 与容量饱和度。

### 6.3.1 资源模型与请求模型

```typescript
/** 指标快照视图：某主体在某观测时刻的即时负载与窗口汇总指标。 */
export interface MetricsSnapshotView {
  /** 智能体身份码。 */
  aic: string;
  /** 快照观测时间。 */
  observedAt: string;
  /** 运行时长（秒）。 */
  uptimeSeconds?: number;
  /** 即时负载（CPU/内存/任务队列等），结构定义见 5.3 LoadMetrics。 */
  loadMetrics?: LoadMetrics;
  /** 按时间窗口聚合的指标（成功率、时延分位等），结构定义见 5.3 WindowMetrics。 */
  windowMetrics?: WindowMetrics[];
}

/** 时序数据点。 */
export interface MetricSeriesPoint {
  /** 数据点时间戳（对齐到 step 边界）。 */
  timestamp: string;
  /** 该时刻的指标值。 */
  value: number;
}

/** 单条指标时间序列，由一组等间隔数据点构成。 */
export interface MetricsSeries {
  /** 指标名，如 "cpuUsage"、"p95LatencyMs"。 */
  metric: string;
  /** 该序列的维度标签（如 aic、service_name 等），用于区分同名指标的不同来源。 */
  labels: Record<string, string>;
  /** 统计窗口（ISO 8601 Duration），仅窗口型指标适用。 */
  window?: string;
  /** 按时间升序排列的数据点。 */
  points: MetricSeriesPoint[];
  /** 相邻数据点的时间步长（毫秒）。 */
  stepMs: number;
}

/** 指标排行项，用于"按某指标对主体排名"（如时延最高的 Top N）。 */
export interface MetricsRankingItem {
  /** 智能体身份码。 */
  aic: string;
  /** 参与排名的指标名。 */
  metric: string;
  /** 统计窗口（如适用）。 */
  window?: string;
  /** 分位数（如 "p95"、"p99"），仅分位型指标适用。 */
  quantile?: string;
  /** 用于排名的指标值。 */
  value: number;
  /** 排名计算时间。 */
  evaluatedAt: string;
  /** 指标原始采样时间（如与 evaluatedAt 不同）。 */
  sampledAt?: string;
}

/** 单主体在某窗口上的 SLO 评估结果。 */
export interface MetricsSLOEvaluation {
  /** 智能体身份码。 */
  aic: string;
  /** 评估窗口。 */
  window: string;
  /** 是否达标：actual 是否满足 target。 */
  meets: boolean;
  /** 目标阈值。 */
  target: number;
  /** 实际观测值。 */
  actual: number;
  /** 服务等级指标类型：成功率 / p95 时延 / p99 时延 / 平均时延。 */
  sli: "success_rate" | "p95_latency_ms" | "p99_latency_ms" | "avg_latency_ms";
  /** 评估观测时间。 */
  observedAt: string;
}

/** 容量饱和度项：反映主体当前任务并发与排队相对其上限的占用比例。 */
export interface MetricsCapacitySaturationItem {
  /** 智能体身份码。 */
  aic: string;
  /** 活跃任务占用率 = activeTasks / maxActiveTasks，取值 0~1。 */
  activeRatio?: number;
  /** 队列占用率 = queuedTasks / maxQueuedTasks，取值 0~1。 */
  queueRatio?: number;
  /** 当前活跃任务数。 */
  activeTasks?: number;
  /** 最大活跃任务数（容量上限）。 */
  maxActiveTasks?: number;
  /** 当前排队任务数。 */
  queuedTasks?: number;
  /** 最大队列长度（容量上限）。 */
  maxQueuedTasks?: number;
  /** 采样时间。 */
  sampledAt: string;
}

/** 快照查询请求（POST /snapshots/query）。 */
export interface MetricsSnapshotQueryRequest extends AMPQueryRequest {
  /** 仅返回这些窗口的 windowMetrics（如 ["PT1M","PT5M"]）。 */
  windows?: string[];
}

/** 时序查询请求（POST /series/query）。 */
export interface MetricsSeriesQueryRequest extends AMPQueryRequest {
  /** 要查询的指标名（必填）。 */
  metric: string;
  /** 期望步长（ISO 8601 Duration）；省略时由 Provider 自动选择。 */
  step?: string;
  /** 每个时间桶内的聚合方式。 */
  aggregation?:
    | "avg"
    | "min"
    | "max"
    | "sum"
    | "p50"
    | "p95"
    | "p99"
    | "latest";
  /** 是否按 aic 分组，为每个主体输出独立序列。 */
  groupByAic?: boolean;
  /** 额外的分组标签，仅允许白名单字段（见 6.3.4）。 */
  groupByLabels?: string[];
}

/** 排行查询请求（POST /rankings/query）。 */
export interface MetricsRankingQueryRequest extends AMPQueryRequest {
  /** 参与排名的指标名（必填）。 */
  metric: string;
  /** 统计窗口。 */
  window?: string;
  /** 排名前对指标的聚合方式。 */
  aggregation?: "avg" | "max" | "min" | "p95" | "p99" | "latest";
  /** 返回前 N 名。 */
  topN?: number;
  /** 排序方向：desc=从高到低（取最差/最高），asc=从低到高。 */
  direction?: "asc" | "desc";
}

/** SLO 批量评估请求（POST /slo/evaluate）。 */
export interface MetricsSLOEvaluateRequest {
  /** 评估时间范围（必填，见 6.3.4）。 */
  timeRange?: AMPTimeRange;
  /** 限定参与评估的主体范围。 */
  filter?: AMPFilter;
  /** 一组 SLO 规则；每条规则定义一个 SLI、统计窗口与目标阈值。 */
  rules: Array<{
    sli:
      | "success_rate"
      | "p95_latency_ms"
      | "p99_latency_ms"
      | "avg_latency_ms";
    window: string;
    target: number;
  }>;
  /** 是否在响应中附带未达标项的明细。 */
  includeFailedDetails?: boolean;
}

/** SLO 评估响应。 */
export interface MetricsSLOEvaluateResponse {
  /** 各主体/规则的评估结果。 */
  items: MetricsSLOEvaluation[];
  /** 评估汇总：总数、达标数、违约数。 */
  summary: {
    total: number;
    meetsCount: number;
    breachCount: number;
  };
  /** 响应元信息。 */
  meta: AMPResponseMeta;
}

/** 容量饱和度查询请求（POST /capacity/saturation）。 */
export interface MetricsCapacityRequest {
  /** 仅返回活跃占用率 ≥ 该阈值（0~1）的主体。 */
  activeRatioThreshold?: number;
  /** 仅返回队列占用率 ≥ 该阈值（0~1）的主体。 */
  queueRatioThreshold?: number;
  /** 回看窗口（ISO 8601 Duration）；不得超出原始指标保留窗口。 */
  lookback?: string;
  /** 限定主体范围。 */
  filter?: AMPFilter;
}
```

### 6.3.2 可过滤字段

| 字段路径                                                | 适用 API              |
| ------------------------------------------------------- | --------------------- |
| `aic`                                                   | 全部                  |
| `service_name` / `service_namespace` / `deployment_env` | 全部                  |
| `window`                                                | series、rankings、SLO |
| `loadMetrics.activeTasks`                               | snapshots             |
| `loadMetrics.queuedTasks`                               | snapshots             |
| `loadMetrics.maxActiveTasks`                            | snapshots、capacity   |
| `loadMetrics.maxQueuedTasks`                            | snapshots、capacity   |
| `loadMetrics.cpuUsage` / `loadMetrics.memoryUsage`      | snapshots             |
| `windowMetrics.successRate`                             | snapshots             |
| `windowMetrics.p95LatencyMs`                            | snapshots             |
| `quantile`                                              | series、rankings      |

### 6.3.3 端点

| Method | Path                   | 请求体                        | 响应体                                            | Profile    |
| ------ | ---------------------- | ----------------------------- | ------------------------------------------------- | ---------- |
| POST   | `/snapshots/query`     | `MetricsSnapshotQueryRequest` | `AMPQueryResponse<MetricsSnapshotView>`           | Core       |
| POST   | `/series/query`        | `MetricsSeriesQueryRequest`   | `AMPQueryResponse<MetricsSeries>`                 | Core       |
| POST   | `/rankings/query`      | `MetricsRankingQueryRequest`  | `AMPQueryResponse<MetricsRankingItem>`            | Analytics  |
| POST   | `/slo/evaluate`        | `MetricsSLOEvaluateRequest`   | `MetricsSLOEvaluateResponse`                      | Governance |
| POST   | `/capacity/saturation` | `MetricsCapacityRequest`      | `AMPQueryResponse<MetricsCapacitySaturationItem>` | Governance |

### 6.3.4 查询约束与错误码

- `series/query`、`rankings/query`、`slo/evaluate` 必须提供 `timeRange`。
- `capacity/saturation` 必须提供 `lookback` 或使用 Provider 默认 lookback；`lookback` 不得超出原始指标保留窗口。
- `step` 省略时由 Provider 自动选择；若调用方显式给出的 `step` 导致点数超限，返回 `422 AMP_STEP_TOO_FINE`。
- `groupByLabels` 只允许 `service_name`、`service_namespace`、`deployment_env`、`window`、`quantile`。

Metrics 专用错误码（在 6.1.5 公共错误码之外）：

| 错误码                   | HTTP | 说明                                                     |
| ------------------------ | ---- | -------------------------------------------------------- |
| `AMP_SLO_RULE_INVALID`   | 400  | SLO 规则非法（target 越界、sli 不支持）                  |
| `AMP_METRIC_UNSUPPORTED` | 422  | 指定的指标名称不受支持                                   |
| `AMP_STEP_TOO_FINE`      | 422  | 时间跨度过大但 `step` 过细，结果点数会超过 Provider 上限 |

## 6.4 Access API 规范

Access API 前缀为 `{AMP_BASE_URL}/access`，用于查询同步请求 / 响应交互、错误、慢请求、Trace 与拓扑。

### 6.4.1 资源模型与请求模型

```typescript
/** 单条访问事件视图：一次同步请求/响应交互的规范化记录。 */
export interface AccessEventView {
  /** 源端日志唯一 ID。 */
  logId: string;
  /** 事件发生时间。 */
  timestamp: string;
  /** 产生该日志的主体。 */
  aic: string;
  /** 分布式追踪链路 ID。 */
  traceId?: string;
  /** 当前 span ID。 */
  spanId?: string;
  /** 父 span ID，用于重建调用链。 */
  parentSpanId?: string;
  /** 业务关联 ID。 */
  correlationId?: string;
  /** 严重级别文本（DEBUG/INFO/WARN/ERROR）。 */
  severity?: string;
  /** 请求耗时（毫秒）。 */
  durationMs?: number;
  /** 归一化的请求路由（method+route），作为稳定聚合键，避免直接用含变量的 url。 */
  requestRoute?: string;
  /** 请求详情，结构同 5.4 AccessBody.request。 */
  request?: AccessBody["request"];
  /** 响应详情，结构同 5.4 AccessBody.response。 */
  response?: AccessBody["response"];
  /** 调用方信息，结构同 5.4 AccessBody.caller。 */
  caller?: AccessBody["caller"];
  /** 被调用方信息，结构同 5.4 AccessBody.callee。 */
  callee?: AccessBody["callee"];
  /** 错误信息（如有），结构定义见 5.4 ErrorInfo。 */
  error?: ErrorInfo;
  /**
   * 原始日志行：仅当请求 `includeRawLog=true` 且部署启用原始日志存储时返回。
   * 用于排障核对源始记录，不参与结构化检索。
   */
  rawLog?: string;
}

/** Trace 中的单个 span（访问事件的扁平化投影），多个 span 组合即可重建一次调用链。 */
export interface AccessTraceSpan {
  /** 源端日志唯一 ID。 */
  logId: string;
  /** span 发生时间。 */
  timestamp: string;
  /** 产生该 span 的主体。 */
  aic: string;
  /** 当前 span ID。 */
  spanId?: string;
  /** 父 span ID。 */
  parentSpanId?: string;
  /** 严重级别文本。 */
  severity?: string;
  /** 请求方法（HTTP 动词或 RPC 方法名）。 */
  requestMethod?: string;
  /** 归一化请求路由。 */
  requestRoute?: string;
  /** 原始请求 URL（含变量，不用于稳定聚合）。 */
  requestUrl?: string;
  /** 响应状态码。 */
  responseStatus?: number;
  /** 该 span 耗时（毫秒）。 */
  durationMs?: number;
  /** 调用方主体。 */
  callerAic?: string;
  /** 调用方服务名。 */
  callerService?: string;
  /** 被调用方主体。 */
  calleeAic?: string;
  /** 被调用方服务名。 */
  calleeService?: string;
  /** 错误码（如有）。 */
  errorCode?: string;
  /** 产生该 span 的服务名。 */
  serviceName?: string;
  /** 部署环境（如 production / staging）。 */
  deploymentEnv?: string;
}

/** 单条 Trace 的完整视图（GET /traces/{traceId}）：包含所有 span 与汇总信息。 */
export interface AccessTraceView {
  /** 链路 ID。 */
  traceId: string;
  /** 该链路下的全部 span（按调用关系组织）。 */
  spans: AccessTraceSpan[];
  /** 关联的原始访问事件（仅当请求 include_events 时返回）。 */
  events?: AccessEventView[];
  /** 链路汇总。 */
  summary: {
    /** 链路最早事件时间。 */
    firstSeenAt: string;
    /** 链路最晚事件时间。 */
    lastSeenAt: string;
    /** span 总数。 */
    totalSpans: number;
    /** 出错 span 数。 */
    errorCount: number;
    /** 关键路径上的最大单 span 耗时（毫秒）。 */
    maxDurationMs?: number;
    /** 根 span ID。 */
    rootSpanId?: string;
  };
}

/** 操作维度聚合摘要：按 aic/service/endpoint 等维度统计的请求量、错误率与时延。 */
export interface AccessOperationSummary {
  /** 时间桶起点（按 bucketSize 分桶时存在；collapse 后省略）。 */
  bucket?: string;
  /** 本行的聚合维度组合。 */
  dimensions: {
    /** 主体维度。 */
    aic?: string;
    /** 服务名维度。 */
    serviceName?: string;
    /** 端点维度（method + route）。 */
    endpoint?: { method?: string; route: string };
  };
  /** 请求总数。 */
  requestCount: number;
  /** 错误请求数。 */
  errorCount: number;
  /** 错误率 = errorCount / requestCount，取值 0~1。 */
  errorRate: number;
  /** 平均耗时（毫秒）。 */
  avgDurationMs: number;
  /** P95 耗时（毫秒）。 */
  p95DurationMs?: number;
  /** P99 耗时（毫秒）。 */
  p99DurationMs?: number;
  /** 该维度组合最近一次出现的时间。 */
  lastSeenAt: string;
}

/** Trace 列表项摘要（POST /traces/query 的结果行），不含完整 span 明细。 */
export interface AccessTraceSummary {
  /** 链路 ID。 */
  traceId: string;
  /** 链路最早事件时间。 */
  firstSeenAt: string;
  /** 链路最晚事件时间。 */
  lastSeenAt: string;
  /** 链路总时长（毫秒），约为 lastSeenAt - firstSeenAt。 */
  durationMs: number;
  /** span 总数。 */
  totalSpans: number;
  /** 出错 span 数。 */
  errorCount: number;
  /** 根调用所在主体。 */
  rootAic?: string;
  /** 根调用的入口端点。 */
  rootEndpoint?: { method?: string; route?: string };
}

/** 服务调用拓扑边：表示"调用方 → 被调用方"一条有向边及其聚合指标，用于绘制依赖拓扑图。 */
export interface AccessTopologyEdge {
  /** 时间桶起点（分桶时存在）。 */
  bucket?: string;
  /** 该边的分组粒度：按主体（aic）或按服务（service）。 */
  groupedBy: "aic" | "service";
  /** 调用方主体（groupedBy=aic 时）。 */
  callerAic?: string;
  /** 被调用方主体（groupedBy=aic 时）。 */
  calleeAic?: string;
  /** 调用方服务名（groupedBy=service 时）。 */
  callerService?: string;
  /** 被调用方服务名（groupedBy=service 时）。 */
  calleeService?: string;
  /** 调用次数。 */
  callCount: number;
  /** 出错次数。 */
  errorCount: number;
  /** 错误率 = errorCount / callCount，取值 0~1。 */
  errorRate: number;
  /** 平均耗时（毫秒）。 */
  avgDurationMs: number;
  /** P95 耗时（毫秒）。 */
  p95DurationMs?: number;
  /** P99 耗时（毫秒）。 */
  p99DurationMs?: number;
  /** 该边最近一次调用时间。 */
  lastSeenAt: string;
}

/** 错误归因项：按错误码/状态码/端点等维度归类错误，定位"什么错误、影响了谁"。 */
export interface AccessErrorAttribution {
  /** 本行的归因维度组合。 */
  dimensions: {
    /** 错误码维度。 */
    errorCode?: string;
    /** HTTP/RPC 状态码维度。 */
    statusCode?: number;
    /** 端点维度（method + route）。 */
    endpoint?: { method?: string; route: string };
  };
  /** 一条样例错误消息，便于快速理解错误内容。 */
  errorMessageSample?: string;
  /** 该归因组合的错误总数。 */
  count: number;
  /** 受影响的主体列表。 */
  affectedAics: string[];
  /** 受影响的端点及各自错误数（method 在 RPC/无动词场景可缺省，与 dimensions.endpoint 保持一致）。 */
  affectedEndpoints: Array<{ method?: string; route: string; count: number }>;
  /** 首次出现时间。 */
  firstSeenAt: string;
  /** 最近出现时间。 */
  lastSeenAt: string;
}

/** 慢请求项（POST /slow-requests/top 的结果行）：单条超过时延阈值的请求。 */
export interface AccessSlowRequestItem {
  /** 源端日志唯一 ID。 */
  logId: string;
  /** 请求时间。 */
  timestamp: string;
  /** 产生该请求的主体。 */
  aic: string;
  /** 链路 ID，便于跳转查看完整 Trace。 */
  traceId?: string;
  /** 请求方法。 */
  requestMethod?: string;
  /** 归一化请求路由。 */
  requestRoute?: string;
  /** 原始请求 URL。 */
  requestUrl?: string;
  /** 请求耗时（毫秒）。 */
  durationMs: number;
  /** 响应状态码。 */
  responseStatus?: number;
}

/** 操作聚合查询请求（POST /operations/query）。 */
export interface AccessOperationQueryRequest extends AMPQueryRequest {
  /** 聚合维度组合（如 ["service","endpoint"]）。 */
  groupBy?: Array<"aic" | "service" | "endpoint">;
  /** 时间分桶粒度。 */
  bucketSize?: "5m" | "15m" | "1h" | "1d";
  /** 是否折叠时间桶，只返回整段时间范围的汇总（不按桶展开）。 */
  collapseBuckets?: boolean;
  /** 仅返回请求数 ≥ 该值的维度组合，过滤长尾噪声。 */
  minRequestCount?: number;
}

/** Trace 列表查询请求（POST /traces/query）。 */
export interface AccessTraceQueryRequest extends AMPQueryRequest {
  /** 仅返回包含错误 / 仅返回无错误的链路。 */
  hasError?: boolean;
  /** 仅返回总时长 ≥ 该值（毫秒）的链路。 */
  minTraceDurationMs?: number;
  /** 仅返回总时长 ≤ 该值（毫秒）的链路。 */
  maxTraceDurationMs?: number;
}

/** 拓扑查询请求（POST /topology/query）。 */
export interface AccessTopologyQueryRequest extends AMPQueryRequest {
  /** 拓扑边的分组粒度：按主体或按服务。 */
  groupBy?: "aic" | "service";
  /** 仅返回调用次数 ≥ 该值的边。 */
  minCallCount?: number;
  /** 是否折叠时间桶，只返回整段时间范围的边汇总。 */
  collapseBuckets?: boolean;
}

/** 错误归因查询请求（POST /errors/attribution）。 */
export interface AccessErrorAttributionRequest extends AMPQueryRequest {
  /** 归因维度组合。 */
  groupBy?: Array<"errorCode" | "statusCode" | "endpoint">;
  /** 返回错误数最高的前 N 个归因组合。 */
  topN?: number;
}

/** 慢请求查询请求（POST /slow-requests/top）。 */
export interface AccessSlowRequestRequest extends AMPQueryRequest {
  /** 返回最慢的前 N 条请求。 */
  topN?: number;
  /** 仅统计耗时 ≥ 该值（毫秒）的请求。 */
  minDurationMs?: number;
}
```

### 6.4.2 可过滤字段

| 字段路径                            | 适用 API                                           |
| ----------------------------------- | -------------------------------------------------- |
| `aic`                               | events、operations、traces、errors、slow           |
| `traceId`                           | events、traces、slow                               |
| `spanId` / `parentSpanId`           | events                                             |
| `correlationId`                     | events、operations、errors、slow                   |
| `severity`                          | events、operations、traces、errors、slow           |
| `request.method`                    | events、operations、traces、errors、slow           |
| `request.route`                     | events、operations、traces、errors、slow           |
| `request.url`                       | events、traces、slow                               |
| `response.statusCode`               | events、operations、traces、errors、slow           |
| `caller.aic` / `caller.serviceName` | events、operations、traces、topology、errors、slow |
| `caller.ip`                         | events、operations、errors、slow                   |
| `callee.aic` / `callee.serviceName` | events、operations、traces、topology、errors、slow |
| `callee.ip`                         | events、operations、errors、slow                   |
| `durationMs`                        | events、operations、traces、slow                   |
| `error.code`                        | events、operations、traces、errors、slow           |
| `error.message`                     | events、operations、errors、slow                   |
| `service_name` / `deployment_env`   | events、operations、traces、errors、slow           |

说明：

- `request.method` / `request.route` 是**过滤域路径**，分别映射到归一化的请求方法与路由模板（在 `AccessEventView` 中体现为顶层 `requestRoute` 与 `request.method`），不是对响应 JSON 中 `request.route` 字段的字面路径过滤。
- `traces` 列只包含已物化进 trace 读模型的字段；`error.message`、`caller.ip`、`callee.ip`、`correlationId` 等未物化字段在 `traces/query` 上必须返回 `AMP_UNSUPPORTED_FIELD`。

可排序字段（`sort`）白名单（其余字段排序返回 `422 AMP_UNSUPPORTED_FIELD`；未显式指定 `sort` 时按各端点默认排序，并以括注的稳定 tiebreak 收敛顺序）：

| API                  | 允许排序字段                                                              | 默认排序          | 稳定 tiebreak           |
| -------------------- | ------------------------------------------------------------------------- | ----------------- | ----------------------- |
| `events/query`       | `timestamp`、`durationMs`                                                 | `timestamp desc`  | `logId`                 |
| `operations/query`   | `requestCount`、`errorRate`、`avgDurationMs`、`p95DurationMs`、`lastSeenAt` | `requestCount desc` | 维度元组 + `bucket`     |
| `traces/query`       | `lastSeenAt`、`firstSeenAt`、`durationMs`、`totalSpans`、`errorCount`      | `lastSeenAt desc` | `traceId`               |
| `topology/query`     | `callCount`、`errorRate`、`avgDurationMs`、`p95DurationMs`、`lastSeenAt`   | `callCount desc`  | 边维度元组 + `bucket`   |
| `errors/attribution` | `count`、`lastSeenAt`                                                      | `count desc`      | 归因维度元组            |
| `slow-requests/top`  | `durationMs`                                                              | `durationMs desc` | `logId`                 |

### 6.4.3 端点

| Method | Path                  | 请求体                          | 响应体                                     | Profile   |
| ------ | --------------------- | ------------------------------- | ------------------------------------------ | --------- |
| POST   | `/operations/query`   | `AccessOperationQueryRequest`   | `AMPQueryResponse<AccessOperationSummary>` | Core      |
| POST   | `/events/query`       | `AMPQueryRequest`               | `AMPQueryResponse<AccessEventView>`        | Core      |
| POST   | `/traces/query`       | `AccessTraceQueryRequest`       | `AMPQueryResponse<AccessTraceSummary>`     | APM       |
| GET    | `/traces/{traceId}`   | query `include_events`          | `AccessTraceView`                          | APM       |
| POST   | `/topology/query`     | `AccessTopologyQueryRequest`    | `AMPQueryResponse<AccessTopologyEdge>`     | APM       |
| POST   | `/errors/attribution` | `AccessErrorAttributionRequest` | `AMPQueryResponse<AccessErrorAttribution>` | Analytics |
| POST   | `/slow-requests/top`  | `AccessSlowRequestRequest`      | `AMPQueryResponse<AccessSlowRequestItem>`  | Analytics |

### 6.4.4 查询约束与错误码

- 除 `GET /traces/{traceId}` 外，access 查询接口都必须带 `timeRange`。
- `endpoint` 聚合维度统一映射为 `request.method + request.route`，不得使用原始 `request.url` 作为稳定聚合维度。
- `traces/query` 和 `topology/query` 只支持已物化字段；遇到未物化字段必须返回 `AMP_UNSUPPORTED_FIELD`。
- `includeRawLog=true` 仅在部署启用原始日志存储时有效。
- **错误事件判定**（用于 `AccessOperationSummary` / `AccessTopologyEdge` 的 `errorCount`、`errorRate` 与 `errors/attribution`）：`response.statusCode >= 500` **或** `error.code` 非空即视为错误；`4xx` 默认不计入错误（部署可调整状态码下界）。同一口径必须在 operations、topology、errors 之间保持一致。
- **`aic` 维度**指**产生该访问日志的主体**（即 `LogRecord.aic`），既非 `caller.aic` 也非 `callee.aic`；按 `aic` 聚合会混合该主体作为调用方与被调用方两种视角，需要区分调用方向时应改用 `caller.*` / `callee.*`。
- **`topology/query` 的时间对齐**：拓扑读模型按固定分桶粒度（实现建议 5 分钟）维护，`timeRange` 会向桶边界外扩取整（起点向下、终点向上），返回的 `bucket` 为桶起点；调用方不应期望拓扑在亚桶粒度上精确切分。
- **`GET /traces/{traceId}` 的新鲜度与截断**（裸资源响应，满足 6.1.4 对无 `meta` 端点的声明要求）：读模型新鲜度通过响应头 `AMP-Data-Freshness-At`（ISO 8601）与 `AMP-Ingestion-Lag-Ms` 暴露；单条 trace 返回的 span 数受 Provider 上限约束，超限时截断并置响应头 `AMP-Trace-Truncated: true`。

Access 专用错误码（在 6.1.5 公共错误码之外）：

| 错误码                            | HTTP | 说明                                       |
| --------------------------------- | ---- | ------------------------------------------ |
| `AMP_TRACE_NOT_FOUND`             | 404  | 指定 `traceId` 不在当前保留范围内          |
| `AMP_TOPOLOGY_GROUPBY_INVALID`    | 422  | `topology/query` 的 `groupBy` 取值不受支持 |
| `AMP_ATTRIBUTION_GROUPBY_INVALID` | 422  | `errors/attribution` 的聚合维度不受支持    |

## 6.5 Message API 规范

Message API 前缀为 `{AMP_BASE_URL}/message`，用于查询异步消息收发事件、消息生命周期、死信、目的地当前状态与吞吐趋势。

### 6.5.1 资源模型与请求模型

```typescript
/** 单条消息事件视图：一次异步消息的发送或接收/处理行为的规范化记录。 */
export interface MessageEventView {
  /** 源端日志唯一 ID。 */
  logId: string;
  /** 事件发生时间。 */
  timestamp: string;
  /** 产生该日志的主体。 */
  aic: string;
  /** 分布式追踪链路 ID。 */
  traceId?: string;
  /** 业务关联 ID。 */
  correlationId?: string;
  /** 消息流向（由 Writer 从 eventType 派生）：send=生产者发送；receive=消费者侧（接收/结算）。 */
  direction: "send" | "receive";
  /**
   * 生命周期边事件类型，与 5.5 MessageBody.eventType 一一对应（由源端直接给出，非派生）。
   * send=发送；receive=接收/处理；ack/nack/reject/timeout=结算；dead_letter=进入死信。
   */
  eventType:
    | "send"
    | "receive"
    | "ack"
    | "nack"
    | "reject"
    | "timeout"
    | "dead_letter";
  /** 消息系统类型，如 "kafka"、"rabbitmq"。 */
  system: string;
  /** 消息目的地（topic/queue/exchange 等），结构同 5.5 MessageBody.destination。 */
  destination: MessageBody["destination"];
  /** 订阅名（如适用）。 */
  subscriptionName?: string;
  /** 消费者组名（如适用）。 */
  consumerGroupName?: string;
  /** 路由信息（key/partition/offset），结构同 5.5 MessageBody.routing。 */
  routing?: MessageBody["routing"];
  /** 消息唯一 ID。 */
  messageId?: string;
  /** 生命周期归并键：用于把同一条消息的多次收发事件聚合为一条生命周期。 */
  lifecycleKey?: string;
  /** 消息体大小（字节）。 */
  payloadSizeBytes?: number;
  /** 投递尝试次数，1 为首投，>1 为重试；仅消费侧（receive/结算）事件有值，缺省表示不适用或未知。 */
  deliveryAttempt?: number;
  /** 结算补充信息（latencyMs/reason），结构同 5.5 MessageBody.settlement；结算结果由 eventType 表达。 */
  settlement?: MessageBody["settlement"];
  /** 该事件是否已被投入死信（等价于 eventType === "dead_letter"）。 */
  deadLettered?: boolean;
  /** 进入死信的原因。 */
  deadLetterReason?: string;
  /** 错误信息（如有），结构定义见 5.4 ErrorInfo。 */
  error?: ErrorInfo;
  /** 扩展属性。 */
  attributes?: Record<string, AnyValue>;
  /**
   * 原始日志行：仅当请求 `includeRawLog=true` 且部署启用原始日志存储时返回。
   * 对应存储层 message_events.raw_log，用于排障核对源始记录，不参与结构化检索。
   */
  rawLog?: string;
}

/**
 * 消息生命周期视图：把同一条消息（按 lifecycleKey 归并）的多次收发事件聚合为一条记录，
 * 用于排查重复消费、未确认、重试与死信等可靠性问题。
 */
export interface MessageLifecycleView {
  /** 生命周期归并键。 */
  lifecycleKey: string;
  /** 消息唯一 ID。 */
  messageId?: string;
  /** 业务关联 ID。 */
  correlationId?: string;
  /** 分布式追踪链路 ID。 */
  traceId?: string;
  /** 消息系统类型。 */
  system: string;
  /** 消息目的地，结构同 5.5 MessageBody.destination。 */
  destination: MessageBody["destination"];
  /** 订阅名。 */
  subscriptionName?: string;
  /** 消费者组名。 */
  consumerGroupName?: string;
  /** 该消息首个事件时间。 */
  firstSeenAt: string;
  /** 该消息最后事件时间。 */
  lastSeenAt: string;
  /** 进入死信的时间（如有）。 */
  deadLetteredAt?: string;
  /** 参与发送的生产者主体集合。 */
  producerAics: string[];
  /** 参与接收的消费者主体集合。 */
  consumerAics: string[];
  /** 发送事件次数。 */
  sendCount: number;
  /** 接收事件次数。 */
  receiveCount: number;
  /** 观察到的最大投递尝试次数。 */
  maxDeliveryAttempt?: number;
  /** 终态：成功确认/拒绝/超时/已死信/未知。 */
  terminalState?:
    | "ack"
    | "nack"
    | "reject"
    | "timeout"
    | "dead_lettered"
    | "unknown";
  /** 是否最终进入死信。 */
  deadLettered: boolean;
  /** 进入死信的原因。 */
  deadLetterReason?: string;
  /** 是否疑似重复消费（接收次数多于预期）。 */
  duplicateConsumed: boolean;
  /** 是否处于未确认（unacked）状态。 */
  unacked: boolean;
}

/**
 * 目的地当前状态快照：直接读取消息系统的队列/主题真实状态（积压、在途、死信等），
 * 而非用"发送数 - 消费数"差值推算。
 */
export interface MessageDestinationState {
  /** 状态快照采集时间。 */
  capturedAt: string;
  /** 本快照所对应的目的地维度。 */
  dimensions: {
    /** 消息系统类型。 */
    system?: string;
    /** 目的地标识。 */
    destination?: { name?: string; kind?: string; virtualHost?: string };
  };
  /** 可见（可被消费）消息数，即积压量 backlog。 */
  visibleMessages?: number;
  /** 在途（已投递未确认）消息数。 */
  inflightMessages?: number;
  /** 延迟（尚未到投递时间）消息数。 */
  delayedMessages?: number;
  /** 死信消息数。 */
  deadLetterMessages?: number;
  /** 最老消息的滞留时长（秒），用于衡量积压严重程度。 */
  oldestMessageAgeSeconds?: number;
  /** 当前活跃消费者数。 */
  activeConsumers?: number;
  /** 目的地占用的存储大小（字节）。 */
  sizeBytes?: number;
}

/** 目的地吞吐时间序列：表达生产/消费/确认/重试/死信等活动随时间的变化趋势。 */
export interface MessageThroughputSeries {
  /** 消息系统类型。 */
  system: string;
  /** 目的地名称。 */
  destinationName: string;
  /** 目的地类型（topic/queue/exchange）。 */
  destinationKind?: string;
  /** 虚拟主机（RabbitMQ）。 */
  virtualHost?: string;
  /** 按时间排列的吞吐数据点。 */
  points: Array<{
    /** 数据点时间戳。 */
    timestamp: string;
    /** 生产（发送）数。 */
    producedCount: number;
    /** 消费（接收）数。 */
    consumedCount: number;
    /** 确认成功数。 */
    ackCount?: number;
    /** 拒绝（可重入队列）数。 */
    nackCount?: number;
    /** 拒绝（丢弃/死信）数。 */
    rejectCount?: number;
    /** 处理超时数。 */
    timeoutCount?: number;
    /** 进入死信数。 */
    deadLetterCount?: number;
    /** 重试投递数。 */
    retryCount: number;
    /** 平均确认时延（毫秒）。 */
    avgAckLatencyMs?: number;
  }>;
}

/** 死信消息视图（POST /deadletters/query 的结果行）：聚焦最终进入死信的消息。 */
export interface MessageDeadLetterView {
  /** 生命周期归并键。 */
  lifecycleKey: string;
  /** 消息唯一 ID。 */
  messageId?: string;
  /** 业务关联 ID。 */
  correlationId?: string;
  /** 分布式追踪链路 ID。 */
  traceId?: string;
  /** 消息系统类型。 */
  system: string;
  /** 消息目的地，结构同 5.5 MessageBody.destination。 */
  destination: MessageBody["destination"];
  /** 进入死信的时间。 */
  deadLetteredAt?: string;
  /** 进入死信的原因。 */
  deadLetterReason?: string;
  /** 接收（投递尝试被消费）次数。 */
  receiveCount: number;
  /** 最大投递尝试次数。 */
  maxDeliveryAttempt?: number;
  /** 生产者主体集合。 */
  producerAics: string[];
  /** 消费者主体集合。 */
  consumerAics: string[];
}

/** 生命周期查询请求（POST /lifecycles/query）。 */
export interface MessageLifecycleQueryRequest extends AMPQueryRequest {
  /** 是否要求必须带 messageId。 */
  requireMessageId?: boolean;
  /** 仅返回接收次数 ≥ 该值的消息（用于排查重复消费）。 */
  minReceiveCount?: number;
  /** 仅返回已确认的消息。 */
  onlyAcked?: boolean;
  /** 仅返回未确认（unacked）的消息。 */
  onlyUnacked?: boolean;
  /**
   * 仅返回存活时长 ≥ 该值（ISO 8601 Duration）的消息。
   * 存活时长按 `now - firstSeenAt` 计算（自该消息首个事件以来的时长），
   * 与 `onlyUnacked` 组合即"已存在足够久但仍无终态"的未确认扫描。
   */
  minAge?: string;
  /** 是否包含超时事件。 */
  includeTimeout?: boolean;
  /** 仅返回处于指定终态的消息。 */
  terminalStates?: Array<
    "ack" | "nack" | "reject" | "timeout" | "dead_lettered" | "unknown"
  >;
}

/** 目的地状态查询请求（POST /destinations/query）。 */
export interface MessageDestinationStateQueryRequest extends AMPQueryRequest {
  /** 聚合维度组合（按系统 / 目的地名 / 类型 / 虚拟主机）。 */
  groupBy?: Array<
    | "system"
    | "destination.name"
    | "destination.kind"
    | "destination.virtualHost"
  >;
}

/** 死信查询请求（POST /deadletters/query）。 */
export interface MessageDeadletterQueryRequest extends AMPQueryRequest {
  /** 仅返回接收次数 ≥ 该值的死信消息。 */
  minReceiveCount?: number;
}

/** 吞吐趋势查询请求（POST /destinations/throughput），针对单个目的地。 */
export interface MessageThroughputRequest {
  /** 查询时间范围（必填）。 */
  timeRange: AMPTimeRange;
  /** 消息系统类型（必填）。 */
  system: string;
  /** 目的地名称（必填）。 */
  destinationName: string;
  /** 目的地类型。 */
  destinationKind?: string;
  /** 虚拟主机（RabbitMQ）。 */
  virtualHost?: string;
  /** 时间步长（ISO 8601 Duration）；省略时由 Provider 自动选择。 */
  step?: string;
}
```

### 6.5.2 可过滤字段

| 字段路径                                               | 适用 API                        |
| ------------------------------------------------------ | ------------------------------- |
| `aic`                                                  | events、lifecycles、deadletters |
| `traceId` / `correlationId`                            | events、lifecycles、deadletters |
| `direction` / `eventType`                              | events                          |
| `system`                                               | 全部                            |
| `destination.name`                                     | 全部                            |
| `destination.kind`                                     | 全部                            |
| `destination.virtualHost`                              | 全部                            |
| `subscriptionName` / `consumerGroupName`               | events、lifecycles、deadletters |
| `routing.key` / `routing.partition` / `routing.offset` | events                          |
| `messageId` / `lifecycleKey`                           | events、lifecycles、deadletters |
| `deliveryAttempt` / `settlement.latencyMs`             | events                          |
| `maxDeliveryAttempt` / `terminalState`                 | lifecycles、deadletters         |
| `deadLettered` / `deadLetterReason`                    | events、lifecycles、deadletters |
| `error.code`                                           | events                          |

### 6.5.3 端点

| Method | Path                       | 请求体                                | 响应体                                      | Profile     |
| ------ | -------------------------- | ------------------------------------- | ------------------------------------------- | ----------- |
| POST   | `/events/query`            | `AMPQueryRequest`                     | `AMPQueryResponse<MessageEventView>`        | Core        |
| POST   | `/lifecycles/query`        | `MessageLifecycleQueryRequest`        | `AMPQueryResponse<MessageLifecycleView>`    | Reliability |
| GET    | `/lifecycles/{messageId}`  | path + destination query params       | `MessageLifecycleView`                      | Reliability |
| POST   | `/destinations/query`      | `MessageDestinationStateQueryRequest` | `AMPQueryResponse<MessageDestinationState>` | Destination |
| POST   | `/deadletters/query`       | `MessageDeadletterQueryRequest`       | `AMPQueryResponse<MessageDeadLetterView>`   | Reliability |
| POST   | `/destinations/throughput` | `MessageThroughputRequest`            | `MessageThroughputSeries`                   | Destination |

### 6.5.4 查询约束与错误码

- 除 `GET /lifecycles/{messageId}` 外，message 查询接口都必须带 `timeRange` 或等价窗口。
- 时间窗口语义：`lifecycles/query` 以 `lastSeenAt` 解释 `timeRange`，`deadletters/query` 以 `deadLetteredAt` 解释；`destinations/query` 把 `timeRange.end` 视为 as-of 时间、`timeRange.start` 视为快照新鲜度下界——在窗口内取每个目的地最新一条状态快照，窗口内无可用快照时返回 `503 AMP_MESSAGE_STATE_SNAPSHOT_UNAVAILABLE`。
- `lifecycles/query` 至少要给出 `messageId`、`lifecycleKey`、`correlationId`、`traceId`，或 `system + destination.name + timeRange` 这类足够收敛的约束。
- duplicate / unacked 是 `lifecycles/query` 的预置过滤语义，不再定义独立端点。
- `destinations/query` 必须读取目的地状态快照；不得用 produced-consumed 差值伪造 backlog 真值。
- `destinations/throughput` 只表达 produced / consumed / ack / retry / dead-letter 等活动趋势，不承担 current state 字段查询。
- 裸资源端点的新鲜度暴露（满足 6.1.4 对无 `meta` 端点的声明要求）：`GET /lifecycles/{messageId}` 与 `POST /destinations/throughput` 返回裸资源（无 `meta`），其读模型新鲜度通过响应头 `AMP-Data-Freshness-At`（ISO 8601）与 `AMP-Ingestion-Lag-Ms` 暴露；当滞后超过 message 类型阈值时，同样可返回 `503 AMP_READ_MODEL_LAGGING`。

Message 专用错误码（在 6.1.5 公共错误码之外）：

| 错误码                                   | HTTP | 说明                                                             |
| ---------------------------------------- | ---- | ---------------------------------------------------------------- |
| `AMP_MESSAGE_LIFECYCLE_KEY_REQUIRED`     | 422  | 生命周期查询未给出足够收敛的聚合约束                             |
| `AMP_MESSAGE_LIFECYCLE_AMBIGUOUS`        | 422  | 按 `messageId` 查询命中多条 lifecycle，需补充 destination 身份   |
| `AMP_MESSAGE_GROUPBY_INVALID`            | 422  | 目的地聚合维度不受支持                                           |
| `AMP_MESSAGE_DESTINATION_REQUIRED`       | 422  | throughput 查询缺少必需的 destination 参数                       |
| `AMP_MESSAGE_STATE_SNAPSHOT_UNAVAILABLE` | 503  | 目的地当前状态快照未启用或已严重过期                             |

## 6.6 Audit API 规范

Audit API 前缀为 `{AMP_BASE_URL}/audit`，用于查询审计记录、聚合统计、导出任务、完整性校验任务和链锚定证据。

### 6.6.1 资源模型与请求模型

```typescript
/**
 * 审计记录视图：一条审计日志及其完整性校验结果。
 * 审计记录采用"源端签名 + 存储链式哈希"双重防篡改（见 5.6.2），integrity 字段暴露其校验状态。
 */
export interface AuditRecordView {
  /** 审计记录在 AMP 侧的唯一 ID。 */
  auditId: string;
  /** 对应的源端日志 ID。 */
  logId: string;
  /** 事件发生时间。 */
  timestamp: string;
  /** 产生该日志的主体。 */
  aic: string;
  /** 分布式追踪链路 ID。 */
  traceId?: string;
  /** 业务关联 ID。 */
  correlationId?: string;
  /** 所属哈希链 ID（同一链内记录按序构成防篡改链）。 */
  chainId: string;
  /** 在所属链内的序号（自增），与 chainId 共同定位记录。 */
  chainSeq: number;
  /** 审计内容主体（操作者/动作/对象/结果），结构定义见 5.6 AuditBody。 */
  body: AuditBody;
  /** 完整性校验信息：签名与哈希链的校验状态。 */
  integrity: {
    /** 签名算法，如 "EdDSA"、"ES256"。 */
    signatureAlg: string;
    /** 签名所用公钥的密钥 ID。 */
    signatureKeyId: string;
    /** 源端签名是否校验通过。 */
    signatureVerified: boolean;
    /** 最近一次签名校验时间。 */
    signatureCheckedAt: string;
    /** 校验失败类型：签名错误 / 公钥缺失 / 哈希链断裂 / 存储缺口。 */
    verificationFailureType?:
      | "signature"
      | "missing_public_key"
      | "hash_chain"
      | "storage_gap";
    /** 链上前一条记录的哈希值。 */
    previousHash?: string;
    /** 本条记录的哈希值，= Hash(关键字段 + previousHash)。 */
    currentHash: string;
    /** 哈希链是否校验通过。 */
    chainVerified?: boolean;
    /** 最近一次链校验时间。 */
    chainCheckedAt?: string;
    /** 该记录被锚定到的锚点 ID（如已锚定）。 */
    chainAnchorId?: string;
  };
}

/** 单条完整性校验失败明细。 */
export interface AuditIntegrityFailure {
  /** 校验失败的审计记录 ID。 */
  auditId: string;
  /** 失败类型：签名错误 / 哈希链断裂 / 公钥缺失 / 存储缺口。 */
  failureType:
    | "signature"
    | "hash_chain"
    | "missing_public_key"
    | "storage_gap";
  /** 失败详情描述。 */
  detail: string;
}

/** 完整性校验响应（同步返回时使用），汇总校验结果与失败明细。 */
export interface AuditIntegrityVerifyResponse {
  /** 校验执行时间。 */
  checkedAt: string;
  /** 校验汇总。 */
  summary: {
    /** 已校验记录数。 */
    checkedCount: number;
    /** 校验失败记录数。 */
    failedCount: number;
    /** 已锚定覆盖到的时间点（该时间前的记录已有锚点背书）。 */
    anchoredUntil?: string;
  };
  /** 失败明细列表。 */
  failures: AuditIntegrityFailure[];
}

/**
 * 链锚定证据视图：把某条哈希链截至某点的状态锚定到外部不可篡改介质（如另一系统或区块链），
 * 作为"该时间点前的记录未被篡改"的对外可验证证据。
 */
export interface AuditChainAnchorView {
  /** 锚点唯一 ID。 */
  anchorId: string;
  /** 被锚定的哈希链 ID。 */
  chainId: string;
  /** 锚定时间。 */
  anchoredAt: string;
  /** 锚定时链上最后一条记录的审计 ID。 */
  lastAuditId: string;
  /** 锚定时链上最后一条记录的序号。 */
  lastChainSeq: number;
  /** 锚定时链上最后一条记录的哈希值。 */
  lastCurrentHash: string;
  /** 锚定方法（如外部时间戳服务、区块链交易等）。 */
  anchorMethod: string;
  /** 锚定证明（其形态由 anchorMethod 决定，如交易哈希、签名时间戳）。 */
  anchorProof: AnyValue;
}

/** 审计导出任务视图（GET /export/{taskId}）：导出在 v1 中一律异步执行，凭 taskId 查询进度与产物。 */
export interface AuditExportTaskView {
  /** 异步任务 ID。 */
  taskId: string;
  /** 任务状态：排队 / 执行中 / 成功 / 失败。 */
  status: "pending" | "running" | "succeeded" | "failed";
  /** 任务创建时间。 */
  createdAt: string;
  /** 任务完成时间（成功或失败）。 */
  finishedAt?: string;
  /** 导出的记录条数。 */
  recordCount?: number;
  /** 导出产物的 SHA-256 校验值，用于校验下载完整性。 */
  artifactSha256?: string;
  /** 产物下载地址（短期有效、即时生成）。 */
  downloadUrl?: string;
  /** 下载地址过期时间。 */
  downloadUrlExpiresAt?: string;
  /** 导出清单（manifest）的哈希值，用于校验导出内容范围。 */
  manifestHash?: string;
  /** 失败原因（status=failed 时）。 */
  error?: string;
}

/** 异步完整性校验任务视图（GET /integrity/verify/{taskId}）。 */
export interface AuditIntegrityTaskView {
  /** 异步任务 ID。 */
  taskId: string;
  /** 任务状态：排队 / 执行中 / 成功 / 失败。 */
  status: "pending" | "running" | "succeeded" | "failed";
  /** 任务创建时间。 */
  createdAt: string;
  /** 任务最近更新时间。 */
  updatedAt: string;
  /** 校验汇总（完成后填充）。 */
  summary?: {
    /** 已校验记录数。 */
    checkedCount: number;
    /** 校验失败记录数。 */
    failedCount: number;
    /** 已锚定覆盖到的时间点。 */
    anchoredUntil?: string;
  };
  /** 失败明细列表（完成后填充）。 */
  failures?: AuditIntegrityFailure[];
  /** 失败原因（status=failed 时）。 */
  error?: string;
}

/** 审计记录查询请求（POST /records/query）。 */
export interface AuditRecordQueryRequest extends AMPQueryRequest {
  /** 查询时间范围（必填）。 */
  timeRange: AMPTimeRange;
  /** 受限关键词检索；只匹配 logId、actor、action、target、result.errorCode 等高信号字段。 */
  keyword?: string;
}

/**
 * 完整性校验请求（POST /integrity/verify）。
 * 至少提供 recordIds、timeRange、filter 之一；带 filter 时必须同时带 timeRange。
 */
export interface AuditIntegrityVerifyRequest {
  /** 校验时间范围。 */
  timeRange?: AMPTimeRange;
  /** 限定校验范围的过滤器。 */
  filter?: AMPFilter;
  /** 直接指定要校验的记录 ID 列表。 */
  recordIds?: string[];
  /** 是否遇到首个失败即停止（快速失败）。 */
  stopOnFirstFailure?: boolean;
  /** 是否同时校验链锚定证据。 */
  verifyAnchor?: boolean;
}

/** 审计导出请求（POST /export），异步执行并返回 202 + taskId。 */
export interface AuditExportRequest {
  /** 导出时间范围（必填）。 */
  timeRange: AMPTimeRange;
  /** 限定导出范围的过滤器。 */
  filter?: AMPFilter;
  /** 受限关键词检索。 */
  keyword?: string;
  /** 导出格式：ndjson（逐行 JSON）或 parquet（列式）。 */
  format: "ndjson" | "parquet";
  /** 是否包含原始日志内容。 */
  includeRaw?: boolean;
  /** 对导出产物签名所用算法。 */
  signatureAlg?: "EdDSA" | "ES256";
}

/** 审计聚合统计请求（POST /summary/aggregate），按指定维度做计数与时间范围汇总。 */
export interface AuditAggregateRequest extends AMPQueryRequest {
  /** 聚合时间范围（必填）。 */
  timeRange: AMPTimeRange;
  /** 聚合分组维度组合，仅允许以下白名单字段。 */
  groupBy: Array<
    | "body.actor.id"
    | "body.actor.name"
    | "body.actor.role"
    | "body.action.type"
    | "body.action.name"
    | "body.target.type"
    | "body.result.status"
    | "body.result.errorCode"
    | "integrity.signatureVerified"
    | "integrity.chainVerified"
    | "integrity.verificationFailureType"
    | "integrity.signatureKeyId"
    | "chainId"
  >;
}
```

### 6.6.2 可过滤字段

| 字段路径                                                                                      | 说明                  |
| --------------------------------------------------------------------------------------------- | --------------------- |
| `auditId` / `logId`                                                                           | 审计记录与源端日志 ID |
| `aic`                                                                                         | 产生日志的主体        |
| `traceId` / `correlationId`                                                                   | 链路 / 业务关联       |
| `chainId` / `chainSeq`                                                                        | 审计子链定位字段      |
| `body.actor.id` / `body.actor.name` / `body.actor.type` / `body.actor.role` / `body.actor.ip` | 操作者                |
| `body.action.name` / `body.action.type` / `body.action.method`                                | 动作                  |
| `body.target.type` / `body.target.id` / `body.target.name`                                    | 目标资源              |
| `body.result.status` / `body.result.errorCode`                                                | 结果                  |
| `integrity.signatureVerified` / `integrity.chainVerified`                                     | 完整性状态            |
| `integrity.verificationFailureType` / `integrity.signatureKeyId`                              | 完整性元数据          |

### 6.6.3 端点

| Method | Path                         | 请求体 / 参数                 | 响应体                                                                                                      | Profile    |
| ------ | ---------------------------- | ----------------------------- | ----------------------------------------------------------------------------------------------------------- | ---------- |
| POST   | `/records/query`             | `AuditRecordQueryRequest`     | `AMPQueryResponse<AuditRecordView>`                                                                         | Core       |
| GET    | `/records/{auditId}`         | path `auditId`                | `AuditRecordView`                                                                                           | Core       |
| POST   | `/integrity/verify`          | `AuditIntegrityVerifyRequest` | `AuditIntegrityVerifyResponse` 或 `202 + AMPTaskAccepted`                                                   | Compliance |
| GET    | `/integrity/verify/{taskId}` | path `taskId`                 | `AuditIntegrityTaskView`                                                                                    | Compliance |
| GET    | `/anchors/latest`            | query `chainId?`              | `AMPQueryResponse<AuditChainAnchorView>`                                                                    | Compliance |
| POST   | `/export`                    | `AuditExportRequest`          | `202 + AMPTaskAccepted`                                                                                     | Export     |
| GET    | `/export/{taskId}`           | path `taskId`                 | `AuditExportTaskView`                                                                                       | Export     |
| POST   | `/summary/aggregate`         | `AuditAggregateRequest`       | `AMPQueryResponse<{ key: Record<string, string>; count: number; firstSeenAt: string; lastSeenAt: string }>` | Core       |

### 6.6.4 查询约束与错误码

- `records/query`、`summary/aggregate`、`export` 必须带 `timeRange`。
- `integrity/verify` 至少要提供 `recordIds`、`timeRange`、`filter` 之一；带 `filter` 时必须同时带 `timeRange`。
- v1 中 `integrity/verify` 即使发现完整性失败也返回 `200` 且 `failures` 非空（"发现完整性失败"是正常校验结论，不映射为 4xx）；`AMP_AUDIT_VERIFICATION_FAILED`（422）为将来"断言全部通过"的严格模式预留。
- v1 在线查询不支持 `body.target.before/after` 与 `raw_log` 深层查询。
- `records/query` 默认按 `timestamp desc` 排序；v1 只允许按 `timestamp` 排序，或在 `chainId` 精确命中时按 `chainSeq` 排序。
- `keyword` 只匹配 `logId`、actor、action、target 和 result errorCode 等高信号字段。
- 导出在 v1 中一律异步执行；`downloadUrl` 必须短期有效且即时生成。

Audit 专用错误码（在 6.1.5 公共错误码之外）：

| 错误码                          | HTTP | 说明                                     |
| ------------------------------- | ---- | ---------------------------------------- |
| `AMP_AUDIT_RECORD_NOT_FOUND`    | 404  | 审计记录不存在                           |
| `AMP_AUDIT_EXPORT_TOO_LARGE`    | 413  | 导出范围过大，需要拆分或使用归档专用通道 |
| `AMP_AUDIT_VERIFICATION_FAILED` | 422  | （v1 保留）严格断言模式下校验未全部通过；v1 常规校验发现失败仍返回 `200`，不使用此码 |
| `AMP_AUDIT_KEY_UNAVAILABLE`     | 503  | 暂时无法获取签名公钥（如 ATR 不可达）    |

## 6.7 System API 规范

System API 前缀为 `{AMP_BASE_URL}/system`，用于查询系统日志事件。系统日志的 `body` 可自由扩展，协议只约束稳定外围字段与受限关键词检索。

### 6.7.1 资源模型与请求模型

```typescript
/**
 * 系统事件视图：系统日志的规范化外围字段投影。
 * 系统日志 body 自由扩展，协议只约束这些稳定外围字段；原始内容置于 rawBody，不做深层结构化查询。
 */
export interface SystemEventView {
  /** 源端日志唯一 ID。 */
  logId: string;
  /** 事件发生时间。 */
  timestamp: string;
  /** 产生该日志的主体。 */
  aic: string;
  /** 规范化的严重级别数值（取值约定见 5.1 SeverityNumber）。 */
  severityNumber: number;
  /** 严重级别的原始文本，如 "INFO"、"ERROR"。 */
  severityText?: string;
  /** 分布式追踪链路 ID。 */
  traceId?: string;
  /** 业务关联 ID。 */
  correlationId?: string;
  /** 规范化日志消息正文，是 keyword 全文检索的主要作用对象。 */
  message: string;
  /** 日志类别。 */
  category?: string;
  /** 产生日志的组件名。 */
  component?: string;
  /** 产生日志的模块名。 */
  module?: string;
  /** 规范化标签键值对。 */
  tags?: Record<string, string>;
  /** 原始 body 内容（自由格式），仅作展示，不支持深层路径过滤。 */
  rawBody?: AnyValue;
}

/** 系统事件查询请求（POST /events/query）。 */
export interface SystemEventQueryRequest extends AMPQueryRequest {
  /** 全文检索关键词，会作用在 message 与搜索投影上 */
  keyword?: string;
}
```

### 6.7.2 可过滤字段

| 字段路径         | 说明           |
| ---------------- | -------------- |
| `aic`            | 产生日志的主体 |
| `severityNumber` | 严重级别数值   |
| `severityText`   | 严重级别文本   |
| `traceId`        | Trace 标识     |
| `correlationId`  | 关联标识       |
| `message`        | 规范化日志消息 |
| `category`       | 类别           |
| `component`      | 组件名         |
| `module`         | 模块名         |
| `tags.*`         | 规范化标签     |

`keyword` 不是字段路径，而是单独的全文检索条件，作用在 `message` 与搜索投影上。对 `rawBody` 的深层过滤不作为 v1 协议能力。

### 6.7.3 端点

| Method | Path            | 请求体                    | 响应体                              | Profile |
| ------ | --------------- | ------------------------- | ----------------------------------- | ------- |
| POST   | `/events/query` | `SystemEventQueryRequest` | `AMPQueryResponse<SystemEventView>` | Core    |

可选扩展端点包括 `/incidents/query`、`/incidents/{incidentId}`、`/severity/trend`、`/components/attribution`。若 Provider 实现这些扩展，必须声明扩展资源模型，并保证扩展结果可以回溯到 `SystemEventView` 对应的原始事件。

### 6.7.4 查询约束与错误码

- `events/query` 必须带 `timeRange`。
- `keyword` 全文搜索即使没有其它过滤条件，也必须受时间窗口和最短长度限制。
- 对 `rawBody` 深层路径查询默认返回 `AMP_UNSUPPORTED_FIELD`。
- 默认按 `timestamp desc` 排序。

System 专用错误码（在 6.1.5 公共错误码之外）：

| 错误码                         | HTTP | 说明                               |
| ------------------------------ | ---- | ---------------------------------- |
| `AMP_SYSTEM_KEYWORD_TOO_BROAD` | 422  | 全文关键词过于宽泛，需要附加过滤条件 |

