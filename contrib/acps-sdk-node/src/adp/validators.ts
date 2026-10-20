/**
 * ADP (Agent Discovery Protocol) 验证工具（对应 Python `acps_sdk.adp.validators`）
 *
 * 提供转发链、扇出额度、过滤条件等的验证逻辑。
 * 基于 ACPs-spec-ADP 协议规范。
 */

import {
  FILTER_MAX_NESTING_DEPTH,
  FORWARD_DEPTH_LIMIT_DEFAULT,
  FORWARD_DEPTH_LIMIT_MAX,
  FORWARD_DEPTH_LIMIT_MIN,
  FORWARD_FANOUT_LIMIT_DEFAULT,
  FORWARD_FANOUT_LIMIT_MAX,
  FORWARD_FANOUT_LIMIT_MIN,
  QUERY_TYPE_EXPLICIT,
} from "./constants.js";
import { ADPError, ADPErrorCode } from "./errors.js";
import {
  type DiscoveryFilter,
  DiscoveryRequest,
  DiscoveryRequestSchema,
  orderDiscoveryRequestKeys,
} from "./models.js";
import { isBlank, reprStr, reprStrList } from "../internal/pyCompat.js";

// =============================================================================
// 请求参数验证
// =============================================================================

/**
 * 对 DiscoveryRequest 进行业务规则校验。
 *
 * 检查内容：
 * - type=explicit 时 query 不能为空（已由模型层验证，此处做二次防护）。
 * - forwardDepthLimit 在 [1, 5] 区间。
 * - forwardFanoutLimit 在 [1, 5] 区间。
 * - filter 嵌套深度不超过建议上限。
 *
 * @throws ADPError 校验不通过时抛出对应错误码的异常
 */
export function validateDiscoveryRequest(request: DiscoveryRequest): void {
  // 1. query 校验
  if (request.type === QUERY_TYPE_EXPLICIT) {
    if (!request.query || isBlank(request.query)) {
      throw new ADPError(
        ADPErrorCode.MISSING_QUERY,
        "type='explicit' 时 query 字段必填且不能为空字符串",
      );
    }
  }

  // 2. forwardDepthLimit 范围校验
  if (request.forwardDepthLimit !== undefined && request.forwardDepthLimit !== null) {
    if (!(
      FORWARD_DEPTH_LIMIT_MIN <= request.forwardDepthLimit &&
      request.forwardDepthLimit <= FORWARD_DEPTH_LIMIT_MAX
    )) {
      throw new ADPError(
        ADPErrorCode.FORWARD_DEPTH_LIMIT_INVALID,
        `forwardDepthLimit 必须在 ` +
          `[${FORWARD_DEPTH_LIMIT_MIN}, ${FORWARD_DEPTH_LIMIT_MAX}] 区间，` +
          `收到: ${request.forwardDepthLimit}`,
      );
    }
  }

  // 3. forwardFanoutLimit 范围校验
  if (request.forwardFanoutLimit !== undefined && request.forwardFanoutLimit !== null) {
    if (!(
      FORWARD_FANOUT_LIMIT_MIN <= request.forwardFanoutLimit &&
      request.forwardFanoutLimit <= FORWARD_FANOUT_LIMIT_MAX
    )) {
      throw new ADPError(
        ADPErrorCode.FORWARD_FANOUT_LIMIT_INVALID,
        `forwardFanoutLimit 必须在 ` +
          `[${FORWARD_FANOUT_LIMIT_MIN}, ${FORWARD_FANOUT_LIMIT_MAX}] 区间，` +
          `收到: ${request.forwardFanoutLimit}`,
      );
    }
  }

  // 4. filter 嵌套深度校验
  if (request.filter !== undefined && request.filter !== null) {
    validateFilterDepth(request.filter, 1);
  }
}

/**
 * 递归校验过滤条件嵌套深度。
 *
 * @param filter 过滤条件对象
 * @param currentDepth 当前嵌套层级（从 1 开始）
 * @throws ADPError 嵌套深度超限时抛出 FILTER_INVALID 错误
 */
function validateFilterDepth(filter: DiscoveryFilter, currentDepth: number): void {
  if (currentDepth > FILTER_MAX_NESTING_DEPTH) {
    throw new ADPError(
      ADPErrorCode.FILTER_INVALID,
      `过滤条件嵌套深度超过建议上限 ${FILTER_MAX_NESTING_DEPTH} 层，` + `当前深度: ${currentDepth}`,
    );
  }
  if (filter.groups) {
    for (const subGroup of filter.groups) {
      validateFilterDepth(subGroup, currentDepth + 1);
    }
  }
}

// =============================================================================
// 转发链验证
// =============================================================================

/**
 * 验证转发链的完整性和合法性。
 *
 * 检查内容：
 * 1. 环路检测：当前服务器 AIC 不能已经在 forwardChain 中。
 * 2. 深度检查：forwardChain 长度 < forwardDepthLimit。
 * 3. 链条完整性：forwardChain 最后一个 AIC 应与发送方(senderAic)一致。
 *
 * @param request 发现请求
 * @param currentServerAic 当前发现服务器的 AIC
 * @param senderAic 发送方(上一跳)的 AIC（从 mTLS 证书中提取）；对于首次收到请求（无 forwardChain）可省略
 * @throws ADPError 校验不通过时抛出对应错误码的异常
 */
export function validateForwardChain(
  request: DiscoveryRequest,
  currentServerAic: string,
  senderAic?: string | null,
): void {
  const chain = request.forwardChain || [];
  const depthLimit = DiscoveryRequest.getEffectiveDepthLimit(request);

  // 1. 环路检测
  if (chain.includes(currentServerAic)) {
    throw new ADPError(
      ADPErrorCode.FORWARD_LOOP_DETECTED,
      `在 forwardChain 中检测到当前服务器 AIC ${reprStr(currentServerAic)}，` +
        `判定为环路。forwardChain: ${reprStrList(chain)}`,
      { forwardChain: chain, currentServerAic },
    );
  }

  // 2. 深度检查
  if (chain.length >= depthLimit) {
    throw new ADPError(
      ADPErrorCode.FORWARD_DEPTH_EXCEEDED,
      `转发深度已达到 forwardDepthLimit=${depthLimit}，` +
        `当前 forwardChain 长度: ${chain.length}`,
      { forwardChain: chain, forwardDepthLimit: depthLimit },
    );
  }

  // 3. 链条完整性检查
  if (chain.length > 0 && senderAic !== undefined && senderAic !== null) {
    const lastAic = chain[chain.length - 1] as string;
    if (lastAic !== senderAic) {
      throw new ADPError(
        ADPErrorCode.FORWARD_CHAIN_TAMPERED,
        `forwardChain 最后一个 AIC ${reprStr(lastAic)} 与发送方证书 AIC ` +
          `${reprStr(senderAic)} 不一致，链条完整性受到破坏`,
        { forwardChainLastAic: lastAic, senderAic },
      );
    }
  }
}

// =============================================================================
// 扇出额度验证
// =============================================================================

/**
 * 验证当前请求的 fan-out 额度是否满足并发需求。
 *
 * @param request 发现请求
 * @param requiredBranches 计划并发的下游分支数量
 * @throws ADPError 额度不足时抛出 FORWARD_FANOUT_EXCEEDED 错误
 */
export function validateFanoutBudget(request: DiscoveryRequest, requiredBranches: number): void {
  const remaining = DiscoveryRequest.getEffectiveFanoutRemaining(request);

  if (remaining < requiredBranches) {
    throw new ADPError(
      ADPErrorCode.FORWARD_FANOUT_EXCEEDED,
      `fan-out 剩余额度 ${remaining} 不足以支撑 ${requiredBranches} 个并发分支`,
      { availableBudget: remaining, requiredBranches },
    );
  }
}

// =============================================================================
// 信任列表验证
// =============================================================================

/**
 * 检查目标发现服务器是否在信任列表中。
 *
 * 如果 forwardTrustedServers 为空或未提供，则无信任限制，返回 true。
 *
 * @returns true 表示目标可信或无信任限制，false 表示目标不在信任列表中
 */
export function validateTrustedTarget(request: DiscoveryRequest, targetAic: string): boolean {
  const trusted = request.forwardTrustedServers;
  if (!trusted || trusted.length === 0) {
    return true;
  }
  return trusted.includes(targetAic);
}

// =============================================================================
// 转发超时检查
// =============================================================================

/**
 * 根据超时配置判断是否应该继续转发。
 *
 * 当 forwardTotalTimeoutMs 剩余值小于 forwardEachTimeoutMs 时，不应继续转发。
 *
 * @returns true 表示可以继续转发，false 表示不应继续
 */
export function shouldContinueForwarding(request: DiscoveryRequest): boolean {
  const totalRemaining = DiscoveryRequest.getEffectiveTotalTimeoutMs(request);
  const eachTimeout = DiscoveryRequest.getEffectiveEachTimeoutMs(request);
  return totalRemaining >= eachTimeout;
}

// =============================================================================
// 转发请求构建工具
// =============================================================================

/**
 * 基于原始请求构建转发给下游的请求。
 *
 * 该方法会：
 * 1. 深拷贝原始请求（经 `toDict` → 重新解析，与 Python 的 `from_dict(to_dict())` 一致，可能抛出 z.ZodError）。
 * 2. 将当前服务器 AIC 追加到 forwardChain。
 * 3. 更新 forwardFanoutRemaining。
 * 4. 动态调整 forwardTotalTimeoutMs（减去已用时间，最小为 0）。
 * 5. 若提供 trustedServers（仅首个节点需要），填充 forwardTrustedServers。
 * 6. 若提供 signature，追加到 forwardSignatures。
 * 7. 确保 forwardDepthLimit、forwardFanoutLimit 已设置（缺省时填默认值）。
 *
 * 与 Python 一致，步骤 2-7 的赋值不再经过模型校验（例如总超时耗尽时 forwardTotalTimeoutMs 会是 0）。
 *
 * @param original 原始发现请求
 * @param currentServerAic 当前发现服务器的 AIC
 * @param fanoutRemainingForBranch 分配给本分支的剩余扇出额度
 * @param elapsedMs 当前节点已消耗的时间（毫秒），用于调整总超时
 * @param signature 当前节点的数字签名（可选）
 * @param trustedServers 信任的发现服务器 AIC 列表（仅首个节点需要提供）
 * @returns 适合转发给下游的新 DiscoveryRequest 对象
 */
export function buildForwardedRequest(
  original: DiscoveryRequest,
  currentServerAic: string,
  fanoutRemainingForBranch: number,
  elapsedMs = 0,
  signature?: string | null,
  trustedServers?: readonly string[] | null,
): DiscoveryRequest {
  // 深拷贝避免影响原始请求
  const forwarded = DiscoveryRequestSchema.parse(DiscoveryRequest.toDict(original));

  // 1. 追加当前 AIC 到 forwardChain
  forwarded.forwardChain = [...(forwarded.forwardChain ?? []), currentServerAic];

  // 2. 更新 forwardFanoutRemaining
  forwarded.forwardFanoutRemaining = fanoutRemainingForBranch;

  // 3. 动态调整 forwardTotalTimeoutMs
  const effectiveTotal = DiscoveryRequest.getEffectiveTotalTimeoutMs(original);
  forwarded.forwardTotalTimeoutMs = Math.max(0, effectiveTotal - elapsedMs);

  // 4. 填充 forwardTrustedServers（仅首个节点）
  if (trustedServers !== undefined && trustedServers !== null) {
    forwarded.forwardTrustedServers = [...trustedServers];
  }

  // 5. 追加签名
  if (signature !== undefined && signature !== null) {
    forwarded.forwardSignatures = [...(forwarded.forwardSignatures ?? []), signature];
  }

  // 6. 确保 forwardDepthLimit 已设置
  if (forwarded.forwardDepthLimit === undefined) {
    forwarded.forwardDepthLimit = FORWARD_DEPTH_LIMIT_DEFAULT;
  }

  // 7. 确保 forwardFanoutLimit 已设置
  if (forwarded.forwardFanoutLimit === undefined) {
    forwarded.forwardFanoutLimit = FORWARD_FANOUT_LIMIT_DEFAULT;
  }

  // 新增的键按 Python 字段声明顺序归位，保证 toJson 输出与 Python to_json 一致
  return orderDiscoveryRequestKeys(forwarded);
}

/** Python 整数地板除 `a // b`（b 为 0 时与 Python 一样抛错）。 */
function floorDiv(a: number, b: number): number {
  if (b === 0) {
    throw new RangeError("integer division or modulo by zero");
  }
  return Math.floor(a / b);
}

/**
 * 分配 fan-out 额度到各分支。
 *
 * 在进行聚合转发（fan-out）时，当前节点需要从 forwardFanoutRemaining 中为每个下游分支分配额度。
 * 所有下游收到的剩余额度之和（不含本次并发占用的 branchCount）
 * 不得超过节点接收时的剩余额度减去 branchCount。
 *
 * @param totalRemaining 当前节点接收到的 forwardFanoutRemaining
 * @param branchCount 计划并发的分支数量
 * @param strategy 分配策略，"equal" 表示均分，"weighted" 表示按权重分配
 * @param weights 权重列表，仅在 strategy="weighted" 时生效
 * @returns 每个分支被分配的 forwardFanoutRemaining 列表
 * @throws ADPError 额度不足时抛出 FORWARD_FANOUT_EXCEEDED 错误
 * @throws Error 加权策略下权重列表长度与分支数不一致
 *
 * @example
 * ```ts
 * allocateFanoutBudget(4, 3); // [0, 0, 1]
 * allocateFanoutBudget(4, 3, "weighted", [2, 1, 1]); // [1, 0, 0]
 * ```
 *
 * （Python 版 docstring 把加权示例写成 `[0, 0, 1]`，但其实现与本实现一样返回 `[1, 0, 0]`：
 * 可分配的 1 个余量给了权重最大的第 0 个分支。）
 */
export function allocateFanoutBudget(
  totalRemaining: number,
  branchCount: number,
  strategy = "equal",
  weights?: readonly number[] | null,
): number[] {
  if (totalRemaining < branchCount) {
    throw new ADPError(
      ADPErrorCode.FORWARD_FANOUT_EXCEEDED,
      `fan-out 剩余额度 ${totalRemaining} 不足以支撑 ${branchCount} 个并发分支`,
      { availableBudget: totalRemaining, requiredBranches: branchCount },
    );
  }

  const distributable = totalRemaining - branchCount;

  if (strategy === "weighted" && weights !== undefined && weights !== null) {
    if (weights.length !== branchCount) {
      throw new Error(`权重列表长度 ${weights.length} 与分支数 ${branchCount} 不一致`);
    }
    const totalWeight = weights.reduce((sum, weight) => sum + weight, 0);
    if (totalWeight === 0) {
      return new Array<number>(branchCount).fill(0);
    }

    // 按权重分配（整数向下取整，余量分配给权重最大的分支）
    const allocated = weights.map((weight) => Math.trunc((distributable * weight) / totalWeight));
    const remainder = distributable - allocated.reduce((sum, value) => sum + value, 0);
    // 将余量按权重从大到小依次分配（稳定排序：同权重保持原顺序）
    const sortedIndices = weights
      .map((_, index) => index)
      .sort((a, b) => (weights[b] as number) - (weights[a] as number));
    for (let i = 0; i < remainder; i += 1) {
      const target = sortedIndices[i % branchCount] as number;
      allocated[target] = (allocated[target] as number) + 1;
    }
    return allocated;
  }

  // 均分策略
  const base = floorDiv(distributable, branchCount);
  const remainder = distributable - base * branchCount;
  const allocated = new Array<number>(Math.max(0, branchCount)).fill(base);
  // 余量从最后一个分支开始分配
  for (let i = 0; i < remainder; i += 1) {
    const target = allocated.length - (i + 1);
    allocated[target] = (allocated[target] as number) + 1;
  }
  return allocated;
}
