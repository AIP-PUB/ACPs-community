/**
 * ADP (Agent Discovery Protocol) 数据模型定义（对应 Python `acps_sdk.adp.models`）
 *
 * 基于 ACPs-spec-ADP 协议规范定义的数据结构。
 *
 * 注意：
 * - 字段名即线上 JSON 名（lowerCamelCase，对应 Python 的 alias）。
 * - 所有时间戳均使用 ISO 8601 格式，并必须包含时区信息。
 * - 每个模型导出 zod schema（`XxxSchema`）与解析后的类型（`Xxx`）；需要手写入参时用 `XxxInput`。
 * - Python 模型上的方法（`to_json()`、`from_dict()`、`get_effective_*()`、`success()` 等）
 *   放在与类型同名的伴生对象上，例如 `DiscoveryRequest.fromDict(data)`、
 *   `DiscoveryRequest.getEffectiveDepthLimit(request)`、`DiscoveryResponse.success(result)`。
 */

import { z } from "zod";

import { AnyDict, optional } from "../internal/schema.js";
import {
  FORWARD_DEPTH_LIMIT_DEFAULT,
  FORWARD_DEPTH_LIMIT_MAX,
  FORWARD_DEPTH_LIMIT_MIN,
  FORWARD_EACH_TIMEOUT_MS_DEFAULT,
  FORWARD_FANOUT_LIMIT_DEFAULT,
  FORWARD_FANOUT_LIMIT_MAX,
  FORWARD_FANOUT_LIMIT_MIN,
  FORWARD_TOTAL_TIMEOUT_MS_DEFAULT,
  QUERY_TYPE_EXPLICIT,
  QUERY_TYPES,
} from "./constants.js";
import { ADPError } from "./errors.js";
import { isBlank, reprStr, reprStrTuple } from "../internal/pyCompat.js";

/**
 * pydantic `model_validator(mode="after")` 只在全部字段校验通过后运行；
 * zod 的对象级 refine 默认在「可继续」的字段错误（如数值越界）之后仍会运行，这里显式对齐。
 */
const AFTER_FIELDS_VALID = {
  when: (payload: z.core.ParsePayload): boolean => payload.issues.length === 0,
};

// =============================================================================
/**
 * 解析并把超深嵌套导致的栈溢出转成校验错误。
 *
 * 嵌套极深的 `filter.groups` 会让 zod 递归耗尽调用栈（抛 RangeError）；pydantic 在同样输入下返回
 * ValidationError。`fromJson` / `fromDict` 统一转换为 ZodError，调用方可按校验失败处理。
 */
function parseGuarded<S extends z.ZodType>(schema: S, data: unknown): z.output<S> {
  try {
    return schema.parse(data);
  } catch (error) {
    if (error instanceof RangeError) {
      throw new z.ZodError([
        {
          code: "custom",
          path: [],
          message: "Recursion error - input is nested too deeply",
          input: data,
        },
      ]);
    }
    throw error;
  }
}

// FilterOperator 过滤运算符
// =============================================================================

/**
 * ADP 过滤运算符枚举。
 *
 * 通用：eq（等于）、ne（不等于）、exists（字段存在性）。
 * 比较：gt、gte、lt、lte（数值/日期/字符串字典序）、between（闭区间）。
 * 集合：in（值在列表中）、nin（值不在列表中）。
 * 字符串：contains、notContains、startsWith、endsWith（默认大小写不敏感）。
 * 大小写敏感变体：加 Cs 后缀。
 * 数组：anyOf、allOf、noneOf、size 系列。
 * Map/对象：hasKey、hasNoKey、hasAnyKey、hasAllKeys。
 */
export const FilterOperator = {
  // ── 通用：等值与存在性 ──
  EQ: "eq",
  NE: "ne",
  EXISTS: "exists",

  // ── 比较：数值、日期、字符串字典序 ──
  GT: "gt",
  GTE: "gte",
  LT: "lt",
  LTE: "lte",
  BETWEEN: "between",

  // ── 集合：值列表匹配 ──
  IN: "in",
  NIN: "nin",

  // ── 字符串：模式匹配（默认大小写不敏感） ──
  CONTAINS: "contains",
  NOT_CONTAINS: "notContains",
  STARTS_WITH: "startsWith",
  ENDS_WITH: "endsWith",

  // ── 字符串：大小写敏感变体（Cs = Case Sensitive） ──
  EQ_CS: "eqCs",
  NE_CS: "neCs",
  IN_CS: "inCs",
  NIN_CS: "ninCs",
  CONTAINS_CS: "containsCs",
  NOT_CONTAINS_CS: "notContainsCs",
  STARTS_WITH_CS: "startsWithCs",
  ENDS_WITH_CS: "endsWithCs",

  // ── 数组：集合运算 ──
  ANY_OF: "anyOf",
  ALL_OF: "allOf",
  NONE_OF: "noneOf",
  SIZE: "size",
  SIZE_GT: "sizeGt",
  SIZE_GTE: "sizeGte",
  SIZE_LT: "sizeLt",
  SIZE_LTE: "sizeLte",

  // ── Map/对象：键检查 ──
  HAS_KEY: "hasKey",
  HAS_NO_KEY: "hasNoKey",
  HAS_ANY_KEY: "hasAnyKey",
  HAS_ALL_KEYS: "hasAllKeys",
} as const;
export type FilterOperator = (typeof FilterOperator)[keyof typeof FilterOperator];
export const FilterOperatorSchema = z.enum(FilterOperator);

// =============================================================================
// FilterCondition 单个过滤条件
// =============================================================================

/**
 * 单个过滤条件，描述对 ACS 某个字段施加的匹配规则。
 *
 * - `field`: 字段路径，使用点号分隔表示嵌套（如 `active`、`provider.countryCode`、`skills.tags`）。
 *   对于数组字段，条件应用于每个元素，任意元素满足即匹配。
 * - `op`: 匹配运算符。
 * - `value`: 匹配值，类型取决于 field 的数据类型和 op 运算符：
 *   字符串运算符: string；列表运算符(in/nin): 同类型值数组；数值/日期比较: number 或 ISO 8601 字符串；
 *   区间运算符(between): [lower, upper] 二元组；布尔运算符: boolean；存在性运算符(exists): boolean；
 *   数组运算符(anyOf/allOf/noneOf): 期望匹配的值数组；数组大小运算符(size): number；
 *   Map 键运算符(hasKey/hasAnyKey/hasAllKeys): string 或 string[]。
 */
export const FilterConditionSchema = z.object({
  field: z.string(),
  op: FilterOperatorSchema,
  value: optional(z.unknown()),
});
export type FilterCondition = z.output<typeof FilterConditionSchema>;
export type FilterConditionInput = z.input<typeof FilterConditionSchema>;

// =============================================================================
// DiscoveryFilter 过滤条件集合
// =============================================================================

/** 本层条件和子条件组之间的逻辑关系。 */
export type FilterLogic = "and" | "or" | "not";
export const FilterLogicSchema = z.enum(["and", "or", "not"]);

/**
 * 过滤条件集合（解析后的形状）。
 *
 * 采用通用条件数组模式，支持逻辑组合（AND/OR/NOT），可对 ACS 中所有可查询字段进行匹配。
 * 各条件之间的逻辑关系由 logic 字段控制（默认 AND）。
 * 支持通过 groups 嵌套子条件组，实现任意复杂的逻辑表达。建议嵌套不超过 3 层。
 */
export interface DiscoveryFilter {
  /** 过滤条件列表。与 groups 中的子条件组按 logic 指定的逻辑关系组合。 */
  conditions?: FilterCondition[] | undefined;
  /** 嵌套的子条件组，每个子组可拥有独立的 logic。 */
  groups?: DiscoveryFilter[] | undefined;
  /**
   * 'and'（默认）：所有条件和子组均须满足；'or'：至少一个条件或子组满足即可；'not'：对本层整体结果取反。
   *
   * 与 Python `Optional[Literal[...]] = "and"` 一致：缺省时为 "and"；显式传 null 时为 undefined（序列化时省略）。
   */
  logic: FilterLogic | undefined;
}

/**
 * {@link DiscoveryFilter} 的入参形状。
 *
 * 与其它 `XxxInput` 一致：经 `optional()` 包装的字段，zod 推导出的入参类型是 `unknown`
 * （实际接受 `FilterConditionInput[]` / `DiscoveryFilterInput[]` / null / 缺省）。
 */
export interface DiscoveryFilterInput {
  conditions?: unknown;
  groups?: unknown;
  logic?: FilterLogic | null | undefined;
}

export const DiscoveryFilterSchema: z.ZodType<DiscoveryFilter, DiscoveryFilterInput> = z.object({
  conditions: optional(z.array(FilterConditionSchema)),
  get groups() {
    return optional(z.array(DiscoveryFilterSchema));
  },
  logic: FilterLogicSchema.nullable()
    .default("and")
    .transform((value) => value ?? undefined),
});

// =============================================================================
// DiscoveryContext 上下文载荷
// =============================================================================

/**
 * 上下文载荷结构。
 *
 * 旨在描述请求侧可选的会话与用户背景切片，供发现服务器理解意图。
 * 建议控制在约 2KB 以内，调用方应避免携带敏感信息。
 */
export const DiscoveryContextSchema = z.object({
  /** 客户端自定义的会话 ID，用于关联多轮查询。 */
  conversationId: optional(z.string()),
  /** 近期对话摘要或要点。建议按时间排序，避免携带完整原始对话以控制大小。 */
  recentTurns: optional(z.array(z.string())),
  /** 匿名化用户画像片段，如地理位置、预算偏好等。 */
  userProfile: optional(AnyDict),
  /** 额外的上下文扩展，供上下游约定使用。 */
  metadata: optional(AnyDict),
});
export type DiscoveryContext = z.output<typeof DiscoveryContextSchema>;
export type DiscoveryContextInput = z.input<typeof DiscoveryContextSchema>;

// =============================================================================
// DiscoveryRequest 发现请求
// =============================================================================

const QUERY_TYPES_REPR = reprStrTuple(QUERY_TYPES);

/**
 * ADP 发现请求数据结构。
 *
 * 请求智能体通过此结构向发现服务器发起能力发现请求。
 *
 * 校验规则（与 Python 版一致）：
 * - `type` 缺省为 "explicit"；显式传 null 时为 undefined（此时不要求 query）；其它值必须属于 {@link QUERY_TYPES}。
 * - `type="explicit"` 时 `query` 必填且不能只含空白。
 * - `limit >= 1`；`forwardDepthLimit`、`forwardFanoutLimit` 在 [1, 5]；`forwardFanoutRemaining >= 0`；
 *   两个超时字段 > 0。
 */
export const DiscoveryRequestSchema = z
  .object({
    /**
     * 查询类型。explicit: 明确查询（默认），query 有明确意图并按 filter 过滤；
     * exploratory: 探索性查询，用户没有明确目标；trending: 热门查询，返回当前流行的智能体；
     * filtered: 过滤查询，只按 filter 过滤，query 被忽略。
     */
    type: z
      .string()
      .superRefine((value, ctx) => {
        if (!QUERY_TYPES.includes(value)) {
          ctx.addIssue({
            code: "custom",
            message: `无效的查询类型: ${reprStr(value)}，允许的类型: ${QUERY_TYPES_REPR}`,
          });
        }
      })
      .nullable()
      .default(QUERY_TYPE_EXPLICIT)
      .transform((value) => value ?? undefined),
    /** 能力查询，自然语言描述的请求智能体所需的服务能力。type=explicit 时该字段必填。 */
    query: optional(z.string()),
    /** 结构化上下文信息（多轮对话摘要、用户画像切片等），推荐控制在约 2KB 以内。 */
    context: optional(DiscoveryContextSchema),
    /** 最大返回数量；未指定则使用服务器默认值。 */
    limit: optional(z.number().int().min(1)),
    /** 结构化过滤条件。 */
    filter: optional(DiscoveryFilterSchema),
    /** 单条链路允许的最大转发深度。服务器默认 3，绝对上限 5。 */
    forwardDepthLimit: optional(
      z.number().int().min(FORWARD_DEPTH_LIMIT_MIN).max(FORWARD_DEPTH_LIMIT_MAX),
    ),
    /** 允许的最大并发下游请求数量。服务器默认 1，绝对上限 5。 */
    forwardFanoutLimit: optional(
      z.number().int().min(FORWARD_FANOUT_LIMIT_MIN).max(FORWARD_FANOUT_LIMIT_MAX),
    ),
    /** 当前分支尚可消耗的 fan-out 额度。原始请求通常不设置，由第一跳发现服务器根据 forwardFanoutLimit 初始化。 */
    forwardFanoutRemaining: optional(z.number().int().min(0)),
    /** 转发追踪链。每个字符串为一个智能体身份码（AIC），由每一跳的发现服务器依次维护。 */
    forwardChain: optional(z.array(z.string())),
    /** 信任的发现服务器 AIC 列表。由首个接收请求的发现服务器根据自身信任配置填充。 */
    forwardTrustedServers: optional(z.array(z.string())),
    /** 转发发现服务器的数字签名，顺序与 forwardChain 保持一致。 */
    forwardSignatures: optional(z.array(z.string())),
    /** 每次转发的请求超时，单位毫秒。缺省可为 10000ms。 */
    forwardEachTimeoutMs: optional(z.number().int().gt(0)),
    /** 转发请求的总超时，单位毫秒。缺省可为 60000ms。此值需要根据剩余时间对下一跳做动态调整。 */
    forwardTotalTimeoutMs: optional(z.number().int().gt(0)),
  })
  .superRefine((request, ctx) => {
    // 当 type=explicit 时，query 不能为空。
    if (request.type === QUERY_TYPE_EXPLICIT && (!request.query || isBlank(request.query))) {
      ctx.addIssue({
        code: "custom",
        message: "type='explicit' 时 query 字段必填且不能为空字符串",
      });
    }
  }, AFTER_FIELDS_VALID);
export type DiscoveryRequest = z.output<typeof DiscoveryRequestSchema>;
export type DiscoveryRequestInput = z.input<typeof DiscoveryRequestSchema>;

/** DiscoveryRequest 的字段顺序（与 Python 字段声明顺序一致，用于保持序列化键顺序）。 */
const DISCOVERY_REQUEST_KEYS = [
  "type",
  "query",
  "context",
  "limit",
  "filter",
  "forwardDepthLimit",
  "forwardFanoutLimit",
  "forwardFanoutRemaining",
  "forwardChain",
  "forwardTrustedServers",
  "forwardSignatures",
  "forwardEachTimeoutMs",
  "forwardTotalTimeoutMs",
] as const satisfies readonly (keyof DiscoveryRequest)[];

/**
 * 按 Python 字段声明顺序重排 DiscoveryRequest 的键（值为 undefined 的键省略）。
 *
 * 用于在对已解析对象赋值新字段后，保持 `toJson()` 的键顺序与 Python `to_json()` 一致。
 * @internal
 */
export function orderDiscoveryRequestKeys(request: DiscoveryRequest): DiscoveryRequest {
  const ordered: Record<string, unknown> = {};
  for (const key of DISCOVERY_REQUEST_KEYS) {
    if (request[key] !== undefined) {
      ordered[key] = request[key];
    }
  }
  return ordered as DiscoveryRequest;
}

/** DiscoveryRequest 的方法（对应 Python `DiscoveryRequest` 上的实例方法与类方法）。 */
export const DiscoveryRequest = {
  /** 获取有效的转发深度限制（若未指定则返回默认值）。 */
  getEffectiveDepthLimit(request: DiscoveryRequest): number {
    return request.forwardDepthLimit || FORWARD_DEPTH_LIMIT_DEFAULT;
  },

  /** 获取有效的转发扇出限制（若未指定则返回默认值）。 */
  getEffectiveFanoutLimit(request: DiscoveryRequest): number {
    return request.forwardFanoutLimit || FORWARD_FANOUT_LIMIT_DEFAULT;
  },

  /** 获取有效的剩余扇出额度（若未指定则使用 fanout limit）。 */
  getEffectiveFanoutRemaining(request: DiscoveryRequest): number {
    if (request.forwardFanoutRemaining !== undefined && request.forwardFanoutRemaining !== null) {
      return request.forwardFanoutRemaining;
    }
    return DiscoveryRequest.getEffectiveFanoutLimit(request);
  },

  /** 获取有效的单跳超时（若未指定则返回默认值）。 */
  getEffectiveEachTimeoutMs(request: DiscoveryRequest): number {
    return request.forwardEachTimeoutMs || FORWARD_EACH_TIMEOUT_MS_DEFAULT;
  },

  /** 获取有效的总超时（若未指定则返回默认值）。 */
  getEffectiveTotalTimeoutMs(request: DiscoveryRequest): number {
    return request.forwardTotalTimeoutMs || FORWARD_TOTAL_TIMEOUT_MS_DEFAULT;
  },

  /** 获取当前转发链长度。 */
  getForwardChainLength(request: DiscoveryRequest): number {
    return request.forwardChain ? request.forwardChain.length : 0;
  },

  /** 序列化为 JSON 字符串（camelCase 字段名，省略值为空的字段）。 */
  toJson(request: DiscoveryRequest): string {
    return JSON.stringify(request);
  },

  /** 序列化为普通对象（camelCase 字段名，省略值为空的字段；返回深拷贝）。 */
  toDict(request: DiscoveryRequest): Record<string, unknown> {
    return JSON.parse(JSON.stringify(request)) as Record<string, unknown>;
  },

  /**
   * 从 JSON 字符串反序列化。
   *
   * @throws SyntaxError JSON 格式错误
   * @throws z.ZodError 校验不通过
   */
  fromJson(json: string): DiscoveryRequest {
    return parseGuarded(DiscoveryRequestSchema, JSON.parse(json));
  },

  /**
   * 从普通对象反序列化。
   *
   * @throws z.ZodError 校验不通过
   */
  fromDict(data: unknown): DiscoveryRequest {
    return parseGuarded(DiscoveryRequestSchema, data);
  },
} as const;

// =============================================================================
// DiscoveryAgentSkill 单个智能体技能匹配结果
// =============================================================================

/** 单个智能体技能的匹配结果。 */
export const DiscoveryAgentSkillSchema = z.object({
  /** 智能体身份码（AIC）。用于关联 DiscoveryResult.acsMap 中的完整 ACS 信息。 */
  aic: z.string(),
  /** 在 ACS 中匹配的 Skill ID。 */
  skillId: z.string(),
  /** 匹配排名。排名顺序是数字的自然顺序，数字值越大排名越靠后。 */
  ranking: z.number().int(),
  /** 备注信息。 */
  memo: optional(z.string()),
});
export type DiscoveryAgentSkill = z.output<typeof DiscoveryAgentSkillSchema>;
export type DiscoveryAgentSkillInput = z.input<typeof DiscoveryAgentSkillSchema>;

// =============================================================================
// DiscoveryAgentGroup 按分组组织的智能体匹配结果
// =============================================================================

/**
 * 按分组组织的智能体匹配结果。
 *
 * 发现服务器可将查询拆解为多个子任务或类别，每个分组对应一个维度。
 * 若无需分组，使用单条默认分组（如 group 为原始查询文本或空字符串）。
 */
export const DiscoveryAgentGroupSchema = z.object({
  /** 分组标识。可以是子任务描述、类别名称等；若无分组需求，可设为原始查询文本或空字符串。 */
  group: z.string(),
  /** 该分组下匹配到的智能体技能列表。 */
  agentSkills: z.array(DiscoveryAgentSkillSchema).default(() => []),
});
export type DiscoveryAgentGroup = z.output<typeof DiscoveryAgentGroupSchema>;
export type DiscoveryAgentGroupInput = z.input<typeof DiscoveryAgentGroupSchema>;

// =============================================================================
// DiscoveryRoute 发现结果的路由级细分
// =============================================================================

/** 路由状态。 */
export const DiscoveryRouteStatusSchema = z.enum(["ok", "timeout", "error"]);
export type DiscoveryRouteStatus = z.output<typeof DiscoveryRouteStatusSchema>;

/**
 * 发现结果的路由级细分。
 *
 * 每条路由对应一次链式或聚合转发路径及其候选结果。
 */
export const DiscoveryRouteSchema = z.object({
  /** 转发链路所经过的发现服务器 AIC 列表，从第一跳到最终返回结果的发现服务器。 */
  forwardChain: z.array(z.string()),
  /** 此链路返回的按分组组织的候选服务智能体列表。 */
  agentGroups: z.array(DiscoveryAgentGroupSchema).default(() => []),
  /** 该链路的响应状态。ok: 成功返回结果；timeout: 下游节点超时；error: 其它错误。 */
  status: optional(DiscoveryRouteStatusSchema),
  /** 该链路的总耗时（毫秒）。 */
  durationMs: optional(z.number().int().min(0)),
  /** 额外的链路级扩展信息。 */
  metadata: optional(AnyDict),
});
export type DiscoveryRoute = z.output<typeof DiscoveryRouteSchema>;
export type DiscoveryRouteInput = z.input<typeof DiscoveryRouteSchema>;

// =============================================================================
// DiscoveryResult 发现结果
// =============================================================================

/** 发现结果。 */
export const DiscoveryResultSchema = z.object({
  /**
   * 发现结果中涉及的智能体 ACS 数据。键为智能体身份码（AIC），值为该智能体的完整能力描述（ACS）。
   * 至少包含 agents 中所有 AIC 的 ACS 数据。
   */
  acsMap: z.record(z.string(), AnyDict).default(() => ({})),
  /** 发现到的候选智能体列表。 */
  agents: z.array(DiscoveryAgentGroupSchema).default(() => []),
  /** 下游响应路由集合（可选），主要用于调试和归因。 */
  routes: optional(z.array(DiscoveryRouteSchema)),
  /**
   * AMP alive-sync 注入的在线状态映射（可选）。
   * 键为 AIC，值为对应的 AliveView 输出对象（含 alive、lastSeenAt 等字段，内部的 null 保留）。
   * 仅本地产出的结果（非转发透传）携带此字段；转发结果原样透传上游的 aliveMap，不覆盖。
   */
  aliveMap: optional(z.record(z.string(), AnyDict)),
});
export type DiscoveryResult = z.output<typeof DiscoveryResultSchema>;
export type DiscoveryResultInput = z.input<typeof DiscoveryResultSchema>;

/** `iterAgentSkills` 产出的四元组：`[aic, acsData, agentSkill, group]`。 */
export type AgentSkillEntry = readonly [
  aic: string,
  acsData: Record<string, unknown>,
  agentSkill: DiscoveryAgentSkill,
  group: string,
];

/**
 * Python `DiscoveryResult._alive_enrichable`（PrivateAttr）的对应物。
 *
 * 用 WeakSet 记录「由本地 DiscoveryService 产出、可注入 aliveMap」的结果对象：
 * 不是对象上的属性，因此不会进入 `JSON.stringify` / `toDict`；
 * 与 pydantic 一样跟随对象身份，重新解析或拷贝得到的新对象默认是 false。
 */
const aliveEnrichableResults = new WeakSet<DiscoveryResult>();

/** DiscoveryResult 的方法（对应 Python `DiscoveryResult` 上的实例方法与私有属性）。 */
export const DiscoveryResult = {
  /**
   * 遍历所有分组中的智能体技能，自动关联 acsMap 中的 ACS 数据。
   *
   * 产出 `[aic, acsData, agentSkill, group]`：acsData 为 acsMap 中查找到的 ACS 对象，未找到时为空对象。
   */
  *iterAgentSkills(result: DiscoveryResult): Generator<AgentSkillEntry, void, undefined> {
    const acsMap = result.acsMap ?? {};
    for (const agentGroup of result.agents) {
      for (const agentSkill of agentGroup.agentSkills) {
        const acsData = Object.hasOwn(acsMap, agentSkill.aic) ? acsMap[agentSkill.aic] : undefined;
        yield [agentSkill.aic, acsData ?? {}, agentSkill, agentGroup.group];
      }
    }
  },

  /**
   * 是否为本地产出、可注入 aliveMap 的结果（Python `result._alive_enrichable`，默认 false）。
   *
   * 转发回传结果（来自下游）保持 false，防止本地覆盖透传的 aliveMap（ADP §4.2.3）。
   */
  isAliveEnrichable(result: DiscoveryResult): boolean {
    return aliveEnrichableResults.has(result);
  },

  /** 设置 aliveMap 可注入标记（Python `result._alive_enrichable = value`）。不影响序列化。 */
  setAliveEnrichable(result: DiscoveryResult, value: boolean): void {
    if (value) {
      aliveEnrichableResults.add(result);
    } else {
      aliveEnrichableResults.delete(result);
    }
  },
} as const;

// =============================================================================
// ErrorDetail 通用错误信息
// =============================================================================

/** 通用错误信息对象。 */
export const ErrorDetailSchema = z.object({
  /** 错误代码，如 40001、50801。 */
  code: z.number().int(),
  /** 错误消息，描述错误的简要信息，如 "MissingQuery"。 */
  message: z.string(),
  /** 可选的错误数据，提供更多的错误细节和上下文信息。 */
  data: optional(z.unknown()),
});
export type ErrorDetail = z.output<typeof ErrorDetailSchema>;
export type ErrorDetailInput = z.input<typeof ErrorDetailSchema>;

// =============================================================================
// DiscoveryResponse 发现响应
// =============================================================================

/**
 * ADP 发现响应数据结构。
 *
 * 遵循 CommonResponse 规范：调用成功时包含 result 字段，调用失败时包含 error 字段，二者互斥。
 */
export const DiscoveryResponseSchema = z
  .object({
    /** 请求结果。调用成功时返回匹配到的服务智能体列表。 */
    result: optional(DiscoveryResultSchema),
    /** 错误信息。调用失败时包含错误对象。 */
    error: optional(ErrorDetailSchema),
  })
  .superRefine((response, ctx) => {
    // 确保 result 和 error 互斥。
    if (response.result !== undefined && response.error !== undefined) {
      ctx.addIssue({ code: "custom", message: "result 和 error 字段互斥，不能同时存在" });
    } else if (response.result === undefined && response.error === undefined) {
      ctx.addIssue({ code: "custom", message: "result 和 error 字段必须至少存在一个" });
    }
  }, AFTER_FIELDS_VALID);
export type DiscoveryResponse = z.output<typeof DiscoveryResponseSchema>;
export type DiscoveryResponseInput = z.input<typeof DiscoveryResponseSchema>;

/** DiscoveryResponse 的方法（对应 Python `DiscoveryResponse` 上的实例方法与类方法）。 */
export const DiscoveryResponse = {
  /**
   * 构建成功响应。
   *
   * 与 Python 一致，响应持有传入的同一个 result 对象（不做拷贝），
   * 因此 {@link DiscoveryResult.setAliveEnrichable} 标记与之后对 result 的原地修改都会保留。
   *
   * @throws z.ZodError result 为空或不合法
   */
  success(result: DiscoveryResult): DiscoveryResponse {
    DiscoveryResponseSchema.parse({ result });
    return { result };
  },

  /**
   * 构建失败响应。
   *
   * @throws z.ZodError code / message 不合法
   */
  failure(code: number, message: string, data?: unknown): DiscoveryResponse {
    return DiscoveryResponseSchema.parse({ error: { code, message, data } });
  },

  /** 判断是否为成功响应。 */
  isSuccess(response: DiscoveryResponse): boolean {
    return response.result !== undefined && response.result !== null;
  },

  /** 判断是否为错误响应。 */
  isError(response: DiscoveryResponse): boolean {
    return response.error !== undefined && response.error !== null;
  },

  /**
   * 将响应中的 error 字段转换为 ADPError 异常对象。
   *
   * 成功响应（无 error）返回 null；error.code 无论是否为已知的 ADPErrorCode，都原样作为 ADPError 的 code。
   */
  getAdpError(response: DiscoveryResponse): ADPError | null {
    if (!response.error) {
      return null;
    }
    return new ADPError(response.error.code, response.error.message, response.error.data);
  },

  /** 序列化为 JSON 字符串（camelCase 字段名，省略值为空的字段）。 */
  toJson(response: DiscoveryResponse): string {
    return JSON.stringify(response);
  },

  /** 序列化为普通对象（camelCase 字段名，省略值为空的字段；返回深拷贝）。 */
  toDict(response: DiscoveryResponse): Record<string, unknown> {
    return JSON.parse(JSON.stringify(response)) as Record<string, unknown>;
  },

  /**
   * 从 JSON 字符串反序列化。
   *
   * @throws SyntaxError JSON 格式错误
   * @throws z.ZodError 校验不通过
   */
  fromJson(json: string): DiscoveryResponse {
    return parseGuarded(DiscoveryResponseSchema, JSON.parse(json));
  },

  /**
   * 从普通对象反序列化。
   *
   * @throws z.ZodError 校验不通过
   */
  fromDict(data: unknown): DiscoveryResponse {
    return parseGuarded(DiscoveryResponseSchema, data);
  },
} as const;
