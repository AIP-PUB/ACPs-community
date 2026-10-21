/**
 * ADP (Agent Discovery Protocol) 常量定义（对应 Python `acps_sdk.adp.constants`）
 *
 * 定义 ADP 协议中使用的默认值、限制参数和协议常量。
 * 基于 ACPs-spec-ADP 协议规范。
 */

// =============================================================================
// 转发深度 (Forward Depth) 相关常量
// =============================================================================

/** 转发深度默认值 */
export const FORWARD_DEPTH_LIMIT_DEFAULT = 3;

/** 转发深度绝对上限 */
export const FORWARD_DEPTH_LIMIT_MAX = 5;

/** 转发深度最小值 */
export const FORWARD_DEPTH_LIMIT_MIN = 1;

// =============================================================================
// 转发扇出 (Forward Fanout) 相关常量
// =============================================================================

/** 转发扇出默认值（未提供时默认不允许并发转发） */
export const FORWARD_FANOUT_LIMIT_DEFAULT = 1;

/** 转发扇出绝对上限 */
export const FORWARD_FANOUT_LIMIT_MAX = 5;

/** 转发扇出最小值 */
export const FORWARD_FANOUT_LIMIT_MIN = 1;

// =============================================================================
// 超时相关常量 (毫秒)
// =============================================================================

/** 单跳转发默认超时 (ms) */
export const FORWARD_EACH_TIMEOUT_MS_DEFAULT = 10_000;

/** 转发总超时默认值 (ms) */
export const FORWARD_TOTAL_TIMEOUT_MS_DEFAULT = 60_000;

// =============================================================================
// 重定向相关常量
// =============================================================================

/** 客户端最大连续重定向次数（建议值） */
export const MAX_REDIRECT_HOPS = 5;

// =============================================================================
// 过滤条件相关常量
// =============================================================================

/** 过滤条件嵌套深度建议上限 */
export const FILTER_MAX_NESTING_DEPTH = 3;

// =============================================================================
// 上下文载荷相关常量
// =============================================================================

/** DiscoveryContext 建议最大载荷大小 (字节) */
export const CONTEXT_MAX_PAYLOAD_BYTES = 2048;

// =============================================================================
// API 路径
// =============================================================================

/** 发现请求 API 路径 */
export const DISCOVER_API_PATH = "/discover";

// =============================================================================
// HTTP 请求/响应头字段名称
// =============================================================================

// 分布式追踪相关头（示例名称，实际使用参考监控日志规范）
export const HEADER_TRACE_ID = "X-Trace-Id";
export const HEADER_SPAN_ID = "X-Span-Id";
export const HEADER_PARENT_SPAN_ID = "X-Parent-Span-Id";

// =============================================================================
// 查询类型常量
// =============================================================================

/** 明确查询（默认值） */
export const QUERY_TYPE_EXPLICIT = "explicit";

/** 探索性查询 */
export const QUERY_TYPE_EXPLORATORY = "exploratory";

/** 热门查询 */
export const QUERY_TYPE_TRENDING = "trending";

/** 过滤查询 */
export const QUERY_TYPE_FILTERED = "filtered";

/** 所有合法查询类型 */
export const QUERY_TYPES: readonly string[] = Object.freeze([
  QUERY_TYPE_EXPLICIT,
  QUERY_TYPE_EXPLORATORY,
  QUERY_TYPE_TRENDING,
  QUERY_TYPE_FILTERED,
]);

// =============================================================================
// 路由状态常量
// =============================================================================

export const ROUTE_STATUS_OK = "ok";
export const ROUTE_STATUS_TIMEOUT = "timeout";
export const ROUTE_STATUS_ERROR = "error";

// =============================================================================
// 过滤逻辑运算符常量
// =============================================================================

export const FILTER_LOGIC_AND = "and";
export const FILTER_LOGIC_OR = "or";
export const FILTER_LOGIC_NOT = "not";
