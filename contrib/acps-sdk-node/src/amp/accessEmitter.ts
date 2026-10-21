/**
 * AMP Access 日志发射器（对应 Python `acps_sdk.amp.access_emitter`）。
 *
 * 将一次同步请求/响应交互以 NDJSON 追加写入本地文件，供 Fluent Bit 转发到 Kafka amp.access。
 * 与 AuditEmitter 同构（事件驱动、同步核心 + 异步包装），差异：
 * - 不签名（Access Writer 不校验 integrity）；
 * - body（AccessBody）由调用现场组装；
 * - 顶层接受 trace_id/span_id/parent_span_id/correlation_id 等追踪字段。
 */

import { getLogger } from "../internal/logger.js";
import {
  type RecordEmitOptions,
  appendLine,
  appendLineSync,
  isNonEmptyRecord,
  recordTailFields,
  utcNowIsoZ,
} from "./emitterSupport.js";
import {
  ACCESS_LOG_RECORD_DUMP,
  type AccessBodyInput,
  type AccessLogRecord,
  AccessLogRecordSchema,
  modelDump,
  newLogId,
} from "./models.js";
import { pythonJsonDumps } from "../internal/pyCompat.js";

const logger = getLogger("acps_sdk.amp.access_emitter");

export interface AccessEmitterOptions {
  /** 日志来源描述（service.name 等）；非空时原样追加为记录的最后一个字段。 */
  resource?: Record<string, unknown> | null;
}

/**
 * 把一条 AccessLogRecord 序列化为 NDJSON 行（不含换行符；内部使用，供跨语言对照测试复用）。
 *
 * 与 Python 版相同，resource 在模型序列化之后追加（位于最后），且空对象不写。
 *
 * @internal
 */
export function formatAccessLine(
  record: AccessLogRecord,
  resource: Record<string, unknown> | null,
): string {
  const recordDict = modelDump(record, ACCESS_LOG_RECORD_DUMP);
  if (isNonEmptyRecord(resource)) {
    recordDict["resource"] = resource;
  }
  return pythonJsonDumps(recordDict);
}

/** AMP Access 日志发射器。写入失败只记录 WARNING，不抛异常。 */
export class AccessEmitter {
  private readonly logFile: string;
  private readonly aic: string;
  private readonly resource: Record<string, unknown> | null;

  constructor(logFile: string, aic: string, { resource = null }: AccessEmitterOptions = {}) {
    this.logFile = logFile;
    this.aic = aic;
    this.resource = resource;
  }

  /** 构造并同步写入一条 AccessLogRecord，返回 log_id。 */
  emitSync(body: AccessBodyInput, options: RecordEmitOptions = {}): string {
    const record = this.buildRecord(body, options);
    try {
      appendLineSync(this.logFile, formatAccessLine(record, this.resource));
    } catch (error) {
      logger.warn("AccessEmitter: 写入失败，跳过", error);
    }
    return record.log_id;
  }

  /** 异步版 emitSync：写入完成后返回 log_id。 */
  async emit(body: AccessBodyInput, options: RecordEmitOptions = {}): Promise<string> {
    const record = this.buildRecord(body, options);
    try {
      await appendLine(this.logFile, formatAccessLine(record, this.resource));
    } catch (error) {
      logger.warn("AccessEmitter: 写入失败，跳过", error);
    }
    return record.log_id;
  }

  private buildRecord(body: AccessBodyInput, options: RecordEmitOptions): AccessLogRecord {
    return AccessLogRecordSchema.parse({
      log_id: newLogId(),
      timestamp: utcNowIsoZ(),
      aic: this.aic,
      body,
      ...recordTailFields(options),
    });
  }
}
