/**
 * AMP Heartbeat 日志发射器（对应 Python `acps_sdk.amp.heartbeat_emitter`）。
 *
 * 将心跳事件以 NDJSON 周期性追加写入本地文件，供 Fluent Bit 尾追后转发到 Kafka amp.heartbeat。
 *
 * 与 AuditEmitter 的差异：
 * - 心跳是周期性事件 → 提供 runPeriodic() 后台循环与 stop()；
 * - 心跳 integrity 可选且 Writer 不校验 → 不接收 signer，不写 integrity；
 * - body 仅含可选 uptimeSeconds，由 emitter 依据进程内单调时钟自动计算。
 */

import { performance } from "node:perf_hooks";

import { getLogger } from "../internal/logger.js";
import { utcNowIso } from "../internal/time.js";
import {
  PeriodicLoops,
  type RecordEmitOptions,
  type RunPeriodicOptions,
  appendLine,
  appendLineSync,
  recordTailFields,
} from "./emitterSupport.js";
import {
  HEARTBEAT_LOG_RECORD_DUMP,
  type HeartbeatLogRecord,
  HeartbeatLogRecordSchema,
  modelDump,
  newLogId,
} from "./models.js";
import { pyRound, pythonJsonDumps } from "../internal/pyCompat.js";

const logger = getLogger("acps_sdk.amp.heartbeat_emitter");

/**
 * 把一条 HeartbeatLogRecord 序列化为 NDJSON 行（不含换行符；内部使用，供跨语言对照测试复用）。
 *
 * @internal
 */
export function formatHeartbeatLine(record: HeartbeatLogRecord): string {
  return pythonJsonDumps(modelDump(record, HEARTBEAT_LOG_RECORD_DUMP));
}

/** AMP Heartbeat 日志发射器。写入失败只记录 WARNING，不抛异常。 */
export class HeartbeatEmitter {
  private readonly logFile: string;
  private readonly aic: string;
  private readonly startMonotonic: number;
  private readonly loops = new PeriodicLoops();

  /**
   * @param logFile 目标 NDJSON 文件路径（不存在时自动创建父目录）
   * @param aic 发射方 Agent Instance Context 标识符
   */
  constructor(logFile: string, aic: string) {
    this.logFile = logFile;
    this.aic = aic;
    this.startMonotonic = performance.now();
  }

  /** 构造并同步写入一条 HeartbeatLogRecord，返回 log_id。 */
  emitSync(options: RecordEmitOptions = {}): string {
    const record = this.buildRecord(options);
    try {
      appendLineSync(this.logFile, formatHeartbeatLine(record));
    } catch (error) {
      logger.warn("HeartbeatEmitter: 写入失败，跳过", error);
    }
    return record.log_id;
  }

  /** 异步版 emitSync：写入完成后返回 log_id。 */
  async emit(options: RecordEmitOptions = {}): Promise<string> {
    const record = this.buildRecord(options);
    try {
      await appendLine(this.logFile, formatHeartbeatLine(record));
    } catch (error) {
      logger.warn("HeartbeatEmitter: 写入失败，跳过", error);
    }
    return record.log_id;
  }

  /**
   * 周期性发射心跳，直到调用 stop() 或 signal 中止（之后正常 resolve）。
   *
   * 立即发首条（验证更快），随后每 intervalSeconds 一条。单次写入失败已在 emit 内吞掉，循环不中断。
   * 与 Python 版一样，循环运行期间保持进程存活；结束循环请调用 stop() 或中止 signal。
   */
  runPeriodic(intervalSeconds: number, options: RunPeriodicOptions = {}): Promise<void> {
    return this.loops.run(() => this.emit(), intervalSeconds, options);
  }

  /** 结束由本发射器启动的全部 runPeriodic 循环。 */
  stop(): void {
    this.loops.stop();
  }

  private buildRecord(options: RecordEmitOptions): HeartbeatLogRecord {
    const uptime = pyRound((performance.now() - this.startMonotonic) / 1000, 3);
    return HeartbeatLogRecordSchema.parse({
      log_id: newLogId(),
      timestamp: utcNowIso(),
      aic: this.aic,
      body: { uptimeSeconds: uptime },
      ...recordTailFields(options),
    });
  }
}
