/**
 * ADP (Agent Discovery Protocol) 智能体发现协议（对应 Python `acps_sdk.adp`）
 *
 * 本模块实现了 ACPs 协议体系中 ADP 协议的共用功能，
 * 包括数据模型定义、错误码、验证工具和常量。
 *
 * 使用示例：
 *
 * ```ts
 * import {
 *   ADPErrorCode,
 *   DiscoveryRequest,
 *   DiscoveryRequestSchema,
 *   DiscoveryResponse,
 *   DiscoveryResult,
 *   DiscoveryResultSchema,
 *   FilterOperator,
 *   buildForwardedRequest,
 *   isRedirectErrorCode,
 *   makeErrorResponse,
 *   validateForwardChain,
 * } from "acps-sdk/adp";
 *
 * // 构建发现请求
 * const request = DiscoveryRequestSchema.parse({
 *   type: "explicit",
 *   query: "我需要一个可以做北京美食推荐的智能体",
 *   limit: 5,
 *   filter: {
 *     conditions: [
 *       { field: "active", op: FilterOperator.EQ, value: true },
 *       { field: "skills.tags", op: FilterOperator.ANY_OF, value: ["美食", "北京"] },
 *     ],
 *   },
 * });
 * console.log(DiscoveryRequest.toJson(request));
 *
 * // 构建发现响应
 * const response = DiscoveryResponse.success(
 *   DiscoveryResultSchema.parse({
 *     acsMap: { "AIC-001": { name: "FoodAgent", description: "美食推荐智能体" } },
 *     agents: [{ group: "美食推荐", agentSkills: [{ aic: "AIC-001", skillId: "food-rec", ranking: 1 }] }],
 *   }),
 * );
 *
 * // 验证转发链 / 构建转发请求
 * validateForwardChain(request, "AIC-DS-B", "AIC-DS-A");
 * const forwarded = buildForwardedRequest(request, "AIC-DS-A", 1);
 *
 * // 错误处理
 * const errorResp = makeErrorResponse(ADPErrorCode.MISSING_QUERY);
 *
 * // 遍历发现结果中的智能体技能（自动关联 acsMap）
 * for (const [aic, acsData, skill, group] of DiscoveryResult.iterAgentSkills(response.result!)) {
 *   console.log(`[${group}] ${aic} -> skill=${skill.skillId}, acs=${String(acsData.name)}`);
 * }
 *
 * // 错误分类（重定向 / 可重试 / 客户端错误 / 转发错误）
 * if (isRedirectErrorCode(ADPErrorCode.CAPACITY_REDIRECT)) console.log("需要重定向到其他发现服务器");
 *
 * // 从错误响应中提取 ADPError
 * const adpError = DiscoveryResponse.getAdpError(DiscoveryResponse.failure(40001, "MissingQuery"));
 * if (adpError?.isClientError()) console.log(`客户端参数错误: ${adpError.message}`);
 * ```
 */

// ── 数据模型 ──
export {
  type AgentSkillEntry,
  type DiscoveryAgentGroupInput,
  type DiscoveryAgentSkillInput,
  type DiscoveryContextInput,
  type DiscoveryFilterInput,
  type DiscoveryRequestInput,
  type DiscoveryResponseInput,
  type DiscoveryResultInput,
  type DiscoveryRouteInput,
  type DiscoveryRouteStatus,
  type ErrorDetailInput,
  type FilterConditionInput,
  type FilterLogic,
  type DiscoveryAgentGroup,
  type DiscoveryAgentSkill,
  type DiscoveryContext,
  type DiscoveryFilter,
  type DiscoveryRoute,
  type ErrorDetail,
  type FilterCondition,
  DiscoveryAgentGroupSchema,
  DiscoveryAgentSkillSchema,
  DiscoveryContextSchema,
  DiscoveryFilterSchema,
  DiscoveryRequest,
  DiscoveryRequestSchema,
  DiscoveryResponse,
  DiscoveryResponseSchema,
  DiscoveryResult,
  DiscoveryResultSchema,
  DiscoveryRouteSchema,
  DiscoveryRouteStatusSchema,
  ErrorDetailSchema,
  FilterConditionSchema,
  FilterLogicSchema,
  FilterOperator,
  FilterOperatorSchema,
} from "./models.js";

// ── 错误处理 ──
export {
  type ADPErrorBody,
  type ADPErrorResponseBody,
  ADP_ERROR_HTTP_STATUS,
  ADP_ERROR_NAMES,
  ADPError,
  ADPErrorCode,
  ADPErrorCodeSchema,
  getHttpStatusForError,
  isADPErrorCode,
  isClientErrorCode,
  isForwardErrorCode,
  isRedirectErrorCode,
  isRetryableErrorCode,
  makeErrorResponse,
} from "./errors.js";

// ── 常量 ──
export {
  CONTEXT_MAX_PAYLOAD_BYTES,
  DISCOVER_API_PATH,
  FILTER_LOGIC_AND,
  FILTER_LOGIC_NOT,
  FILTER_LOGIC_OR,
  FILTER_MAX_NESTING_DEPTH,
  FORWARD_DEPTH_LIMIT_DEFAULT,
  FORWARD_DEPTH_LIMIT_MAX,
  FORWARD_DEPTH_LIMIT_MIN,
  FORWARD_EACH_TIMEOUT_MS_DEFAULT,
  FORWARD_FANOUT_LIMIT_DEFAULT,
  FORWARD_FANOUT_LIMIT_MAX,
  FORWARD_FANOUT_LIMIT_MIN,
  FORWARD_TOTAL_TIMEOUT_MS_DEFAULT,
  HEADER_PARENT_SPAN_ID,
  HEADER_SPAN_ID,
  HEADER_TRACE_ID,
  MAX_REDIRECT_HOPS,
  QUERY_TYPE_EXPLICIT,
  QUERY_TYPE_EXPLORATORY,
  QUERY_TYPE_FILTERED,
  QUERY_TYPE_TRENDING,
  QUERY_TYPES,
  ROUTE_STATUS_ERROR,
  ROUTE_STATUS_OK,
  ROUTE_STATUS_TIMEOUT,
} from "./constants.js";

// ── 验证工具 ──
export {
  allocateFanoutBudget,
  buildForwardedRequest,
  shouldContinueForwarding,
  validateDiscoveryRequest,
  validateFanoutBudget,
  validateForwardChain,
  validateTrustedTarget,
} from "./validators.js";
