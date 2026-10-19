/**
 * AMP Metrics 日志发射器（对应 Python `acps_sdk.amp.metrics_emitter`）。
 *
 * 将指标事件以 NDJSON 周期性追加写入本地文件，供 Fluent Bit 尾追后转发到 Kafka amp.metrics。
 *
 * 与 HeartbeatEmitter 的差异：
 * - Metrics body 由外部 SampleProvider 提供（由 Agent 测量真实负载）；
 * - resource 字段携带服务标识（service.name / service.namespace / deployment.environment.name）；
 * - 写入失败只 WARNING，循环不中断，可 stop()。
 */

import { getLogger } from "../internal/logger.js";
import { utcNowIso } from "../internal/time.js";
import {
  PeriodicLoops,
  type RunPeriodicOptions,
  appendLine,
  appendLineSync,
} from "./emitterSupport.js";
import {
  METRICS_LOG_RECORD_DUMP,
  type MetricsBodyInput,
  type MetricsLogRecord,
  MetricsLogRecordSchema,
  modelDump,
  newLogId,
} from "./models.js";
import { pythonJsonDumps } from "../internal/pyCompat.js";

const logger = getLogger("acps_sdk.amp.metrics_emitter");

/** 指标采样协议：由使用方实现，按需提供 MetricsBody（写入前按 MetricsBodySchema 校验）。 */
export interface SampleProvider {
  /** 采集一份当前指标快照，返回 MetricsBody。 */
  sample(): MetricsBodyInput;
}

export interface MetricsEmitterOptions {
  /** 日志来源描述（service.name / service.namespace 等）；作为 resource 字段写入（空对象也写）。 */
  resource?: Record<string, unknown> | null;
}

/**
 * 把一条 MetricsLogRecord 序列化为 NDJSON 行（不含换行符；内部使用，供跨语言对照测试复用）。
 *
 * @internal
 */
export function formatMetricsLine(record: MetricsLogRecord): string {
  return pythonJsonDumps(modelDump(record, METRICS_LOG_RECORD_DUMP));
}

/** AMP Metrics 日志发射器。写入失败只记录 WARNING，不抛异常；采样器抛出的异常照常传播。 */
export class MetricsEmitter {
  private readonly logFile: string;
  private readonly aic: string;
  private readonly sampler: SampleProvider;
  private readonly resource: Record<string, unknown> | null;
  private readonly loops = new PeriodicLoops();

  /**
   * @param logFile 目标 NDJSON 文件路径（不存在时自动创建父目录）
   * @param aic 发射方 Agent Instance Context 标识符
   * @param sampler 提供 MetricsBody 的采样器（实现 SampleProvider 协议）
   */
  constructor(
    logFile: string,
    aic: string,
    sampler: SampleProvider,
    { resource = null }: MetricsEmitterOptions = {},
  ) {
    this.logFile = logFile;
    this.aic = aic;
    this.sampler = sampler;
    this.resource = resource;
  }

  /** 采样并同步写入一条 MetricsLogRecord，返回 log_id。 */
  emitSync(): string {
    const record = this.buildRecord();
    try {
      appendLineSync(this.logFile, formatMetricsLine(record));
    } catch (error) {
      logger.warn("MetricsEmitter: 写入失败，跳过", error);
    }
    return record.log_id;
  }

  /** 异步版 emitSync：写入完成后返回 log_id。 */
  async emit(): Promise<string> {
    const record = this.buildRecord();
    try {
      await appendLine(this.logFile, formatMetricsLine(record));
    } catch (error) {
      logger.warn("MetricsEmitter: 写入失败，跳过", error);
    }
    return record.log_id;
  }

  /**
   * 周期性发射指标，直到调用 stop() 或 signal 中止（之后正常 resolve）。
   *
   * 立即发首条（验证更快），随后每 intervalSeconds 一条。单次写入失败已在 emit 内吞掉，循环不中断；
   * 采样器抛出的异常会结束循环并使返回的 Promise 拒绝（与 Python 版一致）。
   * 与 Python 版一样，循环运行期间保持进程存活；结束循环请调用 stop() 或中止 signal。
   */
  runPeriodic(intervalSeconds: number, options: RunPeriodicOptions = {}): Promise<void> {
    return this.loops.run(() => this.emit(), intervalSeconds, options);
  }

  /** 结束由本发射器启动的全部 runPeriodic 循环。 */
  stop(): void {
    this.loops.stop();
  }

  private buildRecord(): MetricsLogRecord {
    const body = this.sampler.sample();
    return MetricsLogRecordSchema.parse({
      log_id: newLogId(),
      timestamp: utcNowIso(),
      aic: this.aic,
      body,
      resource: this.resource,
    });
  }
}
