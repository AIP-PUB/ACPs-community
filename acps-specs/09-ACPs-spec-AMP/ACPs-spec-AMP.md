[首页](../README.md)

AMP：智能体监控协议（ACPs-spec-AMP-v02.01）

# 1. 文档定义

本文档为 ACPs 智能体协作协议体系中的智能体监控协议（Agent Monitoring Protocol，AMP）标准定义，版本号 v02.01。

文档全称为 ACPs-spec-AMP-v02.01。

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

- **消息日志（Message Logs）**：记录智能体通过消息通道（队列、主题、流）进行的发送与接收行为，关注消息驱动架构中的可靠投递、顺序性与重试状态。与访问日志的同步请求-响应不同，消息日志通常描述异步、解耦的生产者/消费者交互，典型字段包括 topic/queue、partition/offset、deliveryAttempt、ackStatus 等。它们帮助定位消息堆积、乱序、重复投递等问题，并能通过 `traceId` / `spanId` 串联跨系统的事件驱动链路，也可以通过 `correlationId` 进行业务追踪。

- **审计日志（Audit Logs）**：记录智能体的安全相关操作和访问记录。审计日志服务于安全合规与事后取证需求，通常包含操作人、操作对象、操作类型、操作结果等字段。审计日志通常具有较长的保留周期（视法规要求而定），且对完整性和不可篡改性有较高要求。计费系统的交易日志也可归类为审计日志的一种特殊形式。

- **系统日志（System Logs）**：记录智能体运行环境和关联服务的状态信息。比如关联服务启停日志、数据库 Slow Query 日志、JVM GC 日志等。系统日志有助于诊断智能体运行环境的问题，通常与访问日志和指标日志结合使用，以获得全面的故障排查视角。系统日志的 body 内容采用**自由格式**，不做统一结构定义，由各智能体根据自身环境特点自行决定。

**不同类型的日志文件**

为确保智能体系统的综合性能、扩展性与可维护性，本规范推荐采用**按日志类型分文件写入**的方式，而非把所有日志写入同一个文件。例如：

- `heartbeat.log`：仅保存心跳日志。
- `access.log`：保存访问日志。
- `metrics.log`：保存指标日志。
- `audit.log`：保存审计日志。
- `message.log`：保存消息收发日志。

## 4.2 不同类型的日志存储策略

日志数据的长期留存、多维度复杂查询与离线深度分析，需依托适配其特性的存储系统与分层存储策略。不同类型的日志因数据结构、访问频次与业务诉求存在显著差异，因此需针对性制定差异化的存储方案：

1. **心跳日志（Heartbeat Logs）**

   - 访问模式：以“最新状态”为主，几乎没有历史回溯需求。
   - 推荐：无需保存心跳明细，消息消费完成即可删除。

2. **指标日志（Metrics Logs）**

   - 访问模式：按时间序列聚合、分位数统计、长期趋势分析。
   - 推荐：使用时序数据库（TSDB），如 Prometheus（短期监控）+ 远程存储、VictoriaMetrics、InfluxDB 等。

3. **访问日志（Access Logs）**

   - 访问模式：按时间范围查询、按接口/服务聚合统计、错误率分析等。
   - 推荐：使用面向日志/时序的检索系统，如 Elasticsearch/OpenSearch、ClickHouse 或支持列存与大规模聚合的分析型数据库。
   - 说明：访问日志通常体量较大但价值高，是性能优化与故障定位的重要依据。

4. **消息日志（Message Logs）**

   - 访问模式：按 topic/queue、partition/offset、messageId、ackStatus 等维度检索，用于排查堆积、重试、乱序和重复消费问题。
   - 推荐：采用支持高写入与列式聚合的日志/分析系统（如 ClickHouse、OpenSearch）保存结构化消息元数据；必要时保留消息原文于对象存储或压缩仓库。
   - 说明：消息日志通常不要求像审计日志那样长期保留，但需要保留最近数小时到数天以支撑问题回溯；若消息正文已在消息系统内留存，可仅记录元数据及摘要。

5. **审计日志（Audit Logs）**

   - 访问模式：基于用户/资源/时间范围的精确查询，具有合规与取证需求。
   - 推荐：使用具备强一致性、事务支持与良好审计能力的关系型数据库（如 PostgreSQL/MySQL），或专门的审计系统。
   - 注意：审计日志通常需要较长的保存周期（视法规要求而定），必须考虑归档、分区与冷热数据分层策略。

总体原则是：

- **短期高频、低价值日志**（如心跳）以“流 + 状态”的方式处理，不做大规模明细存储。
- **中长期有分析价值的日志**（如访问、审计）使用支持索引与复杂查询的存储系统。
- **高频数值型数据**（指标）优先落地至 TSDB，以获得高效的聚合与压缩能力。

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
     *
     * 验签时，Consumer 根据 aic + kid 从 ATR 服务获取对应版本的公钥。
     * @example "aic-key-v1"
     * @example "cert-2025-001"
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
     * 请求资源标识。
     * - HTTP: URL Path，如 "/api/v1/users"。
     * - RPC: 服务/接口全限定名，如 "com.example.UserService"。
     */
    url?: string;

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

消息日志的 body 结构，用于描述消息驱动的收发过程。兼容 Kafka, RabbitMQ 等常见消息中间件语义。

```typescript
export interface MessageBody {
  /**
   * 消息流向
   * - send: 生产者发送消息
   * - receive: 消费者接收消息
   */
  direction: "send" | "receive";
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
   * 消息路由信息
   */
  routing?: {
    /**
     * 路由键 (RabbitMQ) 或 Message Key (Kafka)
     * 用于决定消息分发到哪个分区或队列
     */
    key?: string;
    /**
     * 分区 ID (Kafka specific)
     */
    partition?: number;
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
   * 投递尝试次数
   * 1 代表第一次投递，>1 代表重试
   */
  deliveryAttempt?: number;
  /**
   * 消息确认状态
   * 记录消息处理的结果
   */
  ack?: {
    /**
     * 状态
     * - ack: 确认消费成功
     * - nack: 拒绝消费 (可能重回队列)
     * - reject: 拒绝消费 (丢弃或死信)
     * - timeout: 处理超时
     */
    status: "ack" | "nack" | "reject" | "timeout";
    /** 确认操作的耗时 (ms) */
    latencyMs?: number;
    /** 拒绝或失败的原因 */
    reason?: string;
  };
  /** 错误信息 (如果处理失败) */
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
    - 签名范围包括顶层公共字段（`timestamp`、`aic`、`traceId`、`spanId`、`parentSpanId`、`correlationId`、`logType`）以及整个 `body` 对象。
    - 签名结果存入顶层 `LogRecord.integrity.sig` 字段。

2.  **传输保护**：

    - 即使日志在经过 Kafka、Fluent Bit 等中间件时被恶意篡改（如修改了 `result.status`），由于中间件无法伪造对应的签名，消费端在验签时会发现不一致。

3.  **验签与入库（Consumer/Storage 端）**：
    - 审计日志服务在消费日志时，根据 `aic` 和 `integrity.kid` 从 ATR 服务获取对应版本的公钥（Public Key）。
    - 验证 `integrity.sig` 是否有效。
    - 只有验签通过的日志才会被标记为"可信"并写入审计数据库；验签失败的日志应触发严重告警。

#### 5.6.2.2 存储防篡改：链式哈希

日志入库后，还可能面临 DBA 或内部攻击者篡改历史记录的风险。为满足不可篡改特性，建议在关系型数据库基础上实现轻量级"链式存储"：

- 每条审计日志应包含**上一条日志的哈希值（Previous Hash）**和**本条日志的哈希值（Current Hash）**。
- `Current Hash = Hash(当前日志关键字段 + Previous Hash)`。
- 这样既能校验单条数据的完整性，又能形成哈希链以防止历史数据被篡改或删除。

