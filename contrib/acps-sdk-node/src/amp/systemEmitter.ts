/**
 * AMP System 日志发射器（对应 Python `acps_sdk.amp.system_emitter`）。
 *
 * 将一次内部系统事件以 NDJSON 追加写入本地文件，供 Fluent Bit 转发到 Kafka amp.system。
 * 与 AccessEmitter 同构（事件驱动、同步核心 + 异步包装），差异：
 * - body 是自由格式对象（无 SystemBody 强类型，直接使用 LogRecord）；
 * - 使用 severity_number / severity_text（System 日志的核心过滤维度）；
 * - 不签名（System Writer 不校验 integrity）；
 * - 不传播 trace 上下文（System 日志是单侧内部事件，不跨进程传播 traceparent）。
 */

import { getLogger } from "../internal/logger.js";
import { appendLine, appendLineSync, utcNowIsoZ } from "./emitterSupport.js";
import { LOG_RECORD_DUMP, type LogRecord, LogRecordSchema, modelDump, newLogId } from "./models.js";
import { pythonJsonDumps } from "../internal/pyCompat.js";

const logger = getLogger("acps_sdk.amp.system_emitter");

export interface SystemEmitterOptions {
  /** 日志来源描述（service.name 等）；作为 LogRecord.resource 字段写入（空对象也写）。 */
  resource?: Record<string, unknown> | null;
}

/** SystemEmitter.emit / emitSync 的可选字段。 */
export interface SystemEmitOptions {
  severityNumber?: number | null;
  severityText?: string | null;
  traceId?: string | null;
  correlationId?: string | null;
}

/**
 * 把一条 system LogRecord 序列化为 NDJSON 行（不含换行符；内部使用，供跨语言对照测试复用）。
 *
 * @internal
 */
export function formatSystemLine(record: LogRecord): string {
  return pythonJsonDumps(modelDump(record, LOG_RECORD_DUMP));
}

/** AMP System 日志发射器。写入失败只记录 WARNING，不抛异常。 */
export class SystemEmitter {
  private readonly logFile: string;
  private readonly aic: string;
  private readonly resource: Record<string, unknown> | null;

  constructor(logFile: string, aic: string, { resource = null }: SystemEmitterOptions = {}) {
    this.logFile = logFile;
    this.aic = aic;
    this.resource = resource;
  }

  /** 构造并同步写入一条 system LogRecord，返回 log_id。 */
  emitSync(
    body: Record<string, unknown> | null | undefined,
    options: SystemEmitOptions = {},
  ): string {
    const logId = newLogId();
    const record = this.buildRecord(logId, body, options);
    try {
      appendLineSync(this.logFile, formatSystemLine(record));
    } catch (error) {
      logger.warn("SystemEmitter: 写入失败，跳过", error);
    }
    return logId;
  }

  /** 异步版 emitSync：写入完成后返回 log_id。 */
  async emit(
    body: Record<string, unknown> | null | undefined,
    options: SystemEmitOptions = {},
  ): Promise<string> {
    const logId = newLogId();
    const record = this.buildRecord(logId, body, options);
    try {
      await appendLine(this.logFile, formatSystemLine(record));
    } catch (error) {
      logger.warn("SystemEmitter: 写入失败，跳过", error);
    }
    return logId;
  }

  private buildRecord(
    logId: string,
    body: Record<string, unknown> | null | undefined,
    options: SystemEmitOptions,
  ): LogRecord {
    return LogRecordSchema.parse({
      schema_version: "1.0",
      log_type: "system",
      log_id: logId,
      timestamp: utcNowIsoZ(),
      aic: this.aic,
      body,
      severity_number: options.severityNumber,
      severity_text: options.severityText,
      trace_id: options.traceId,
      correlation_id: options.correlationId,
      resource: this.resource,
    });
  }
}
