/**
 * AMP Message 日志发射器（对应 Python `acps_sdk.amp.message_emitter`）。
 *
 * 将一次消息收/发/结算边事件以 NDJSON 追加写入本地文件，供 Fluent Bit 转发到 Kafka amp.message。
 * 与 AccessEmitter 完全同构（事件驱动、同步核心 emitSync + 异步包装），差异：
 * - 不签名（Message Writer 不校验 integrity）；
 * - body（MessageBody）由调用现场组装；
 * - 一次 send/receive/ack 各一条独立日志（C-MESSAGE-MODEL-2）。
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
  MESSAGE_LOG_RECORD_DUMP,
  type MessageBodyInput,
  type MessageLogRecord,
  MessageLogRecordSchema,
  modelDump,
  newLogId,
} from "./models.js";
import { pythonJsonDumps } from "../internal/pyCompat.js";

const logger = getLogger("acps_sdk.amp.message_emitter");

export interface MessageEmitterOptions {
  /** 日志来源描述（service.name 等）；非空时原样追加为记录的最后一个字段。 */
  resource?: Record<string, unknown> | null;
}

/**
 * 把一条 MessageLogRecord 序列化为 NDJSON 行（不含换行符；内部使用，供跨语言对照测试复用）。
 *
 * 与 Python 版相同，resource 在模型序列化之后追加（位于最后），且空对象不写。
 *
 * @internal
 */
export function formatMessageLine(
  record: MessageLogRecord,
  resource: Record<string, unknown> | null,
): string {
  const recordDict = modelDump(record, MESSAGE_LOG_RECORD_DUMP);
  if (isNonEmptyRecord(resource)) {
    recordDict["resource"] = resource;
  }
  return pythonJsonDumps(recordDict);
}

/** AMP Message 日志发射器。写入失败只记录 WARNING，不抛异常。 */
export class MessageEmitter {
  private readonly logFile: string;
  private readonly aic: string;
  private readonly resource: Record<string, unknown> | null;

  constructor(logFile: string, aic: string, { resource = null }: MessageEmitterOptions = {}) {
    this.logFile = logFile;
    this.aic = aic;
    this.resource = resource;
  }

  /** 构造并同步写入一条 MessageLogRecord，返回 log_id。 */
  emitSync(body: MessageBodyInput, options: RecordEmitOptions = {}): string {
    const record = this.buildRecord(body, options);
    try {
      appendLineSync(this.logFile, formatMessageLine(record, this.resource));
    } catch (error) {
      logger.warn("MessageEmitter: 写入失败，跳过", error);
    }
    return record.log_id;
  }

  /** 异步版 emitSync：写入完成后返回 log_id。 */
  async emit(body: MessageBodyInput, options: RecordEmitOptions = {}): Promise<string> {
    const record = this.buildRecord(body, options);
    try {
      await appendLine(this.logFile, formatMessageLine(record, this.resource));
    } catch (error) {
      logger.warn("MessageEmitter: 写入失败，跳过", error);
    }
    return record.log_id;
  }

  private buildRecord(body: MessageBodyInput, options: RecordEmitOptions): MessageLogRecord {
    return MessageLogRecordSchema.parse({
      log_id: newLogId(),
      timestamp: utcNowIsoZ(),
      aic: this.aic,
      body,
      ...recordTailFields(options),
    });
  }
}
