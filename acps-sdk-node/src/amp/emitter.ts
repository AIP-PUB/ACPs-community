/**
 * AMP Audit 日志发射器（对应 Python `acps_sdk.amp.emitter`）。
 *
 * `emitSync()` 同步追加写 NDJSON；`emit()` 异步写入、不阻塞事件循环，
 * 同一文件上的异步写入按调用顺序串行。
 *
 * 签名支持（可选）：传入 signer 后，每条日志在写入前自动完成 Ed25519 / RSA 签名，
 * 签名结果以 integrity 字段随记录持久化。未传 signer 时日志不含签名。
 */

import { getLogger } from "../internal/logger.js";
import { utcNowIso } from "../internal/time.js";
import {
  type RecordEmitOptions,
  appendLine,
  appendLineSync,
  recordTailFields,
} from "./emitterSupport.js";
import { canonicalize } from "./jcs.js";
import {
  AUDIT_LOG_RECORD_DUMP,
  type AuditBodyInput,
  type AuditLogRecord,
  AuditLogRecordSchema,
  modelDump,
  newLogId,
} from "./models.js";
import { pythonJsonDumps } from "../internal/pyCompat.js";
import type { AuditSigner } from "./signer.js";

const logger = getLogger("acps_sdk.amp.emitter");

export interface AuditEmitterOptions {
  /**
   * 可选签名器；传入后每条日志自动附加 integrity 字段。
   * 未传时日志不含签名，monitor-server 将以 missing_public_key 标记入库。
   */
  signer?: AuditSigner | null;
}

/**
 * 把一条 AuditLogRecord 序列化为 NDJSON 行（不含换行符；内部使用，供跨语言对照测试复用）。
 *
 * 签名流程（仅当 signer 已配置）：
 *   1. 将 record 序列化为对象（不含 integrity）
 *   2. JCS（RFC 8785）规范化
 *   3. 签名 → Base64url 编码（无填充）
 *   4. 将 integrity 字段追加到对象末尾
 * 签名异常时降级为不含 integrity 写入，保证日志不丢失。
 *
 * @internal
 */
export function formatAuditLine(record: AuditLogRecord, signer: AuditSigner | null): string {
  const recordDict = modelDump(record, AUDIT_LOG_RECORD_DUMP);
  if (signer !== null) {
    try {
      const canonical = canonicalize(recordDict);
      const signature: unknown = signer.sign(canonical);
      if (!(signature instanceof Uint8Array)) {
        // Python 版 base64 编码非 bytes 时抛 TypeError，同样降级为未签名
        throw new TypeError(`signer.sign() must return a Uint8Array, got ${typeof signature}`);
      }
      recordDict["integrity"] = {
        alg: signer.alg,
        kid: signer.kid,
        sig: Buffer.from(signature).toString("base64url"),
      };
    } catch (error) {
      logger.warn("AuditEmitter: 签名失败，记录将不含 integrity 字段", error);
    }
  }
  return pythonJsonDumps(recordDict);
}

/**
 * AMP Audit 日志发射器。
 *
 * 将审计事件以 NDJSON 格式追加写入本地文件，供 Fluent Bit 尾追后转发到 Kafka。
 * 写入失败只记录 WARNING，不抛异常；body / 选项校验失败（ZodError）照常抛出。
 */
export class AuditEmitter {
  private readonly logFile: string;
  private readonly aic: string;
  private readonly signer: AuditSigner | null;

  /**
   * @param logFile 目标 NDJSON 日志文件路径（不存在时自动创建父目录）
   * @param aic 发射方的 Agent Instance Context 标识符
   */
  constructor(logFile: string, aic: string, { signer = null }: AuditEmitterOptions = {}) {
    this.logFile = logFile;
    this.aic = aic;
    this.signer = signer;
  }

  /** 构造并同步写入一条 AuditLogRecord，返回 log_id。 */
  emitSync(body: AuditBodyInput, options: RecordEmitOptions = {}): string {
    const record = this.buildRecord(body, options);
    try {
      appendLineSync(this.logFile, formatAuditLine(record, this.signer));
    } catch (error) {
      logger.warn("AuditEmitter: 写入失败，跳过", error);
    }
    return record.log_id;
  }

  /** 异步版 emitSync：写入完成后返回 log_id。 */
  async emit(body: AuditBodyInput, options: RecordEmitOptions = {}): Promise<string> {
    const record = this.buildRecord(body, options);
    try {
      await appendLine(this.logFile, formatAuditLine(record, this.signer));
    } catch (error) {
      logger.warn("AuditEmitter: 写入失败，跳过", error);
    }
    return record.log_id;
  }

  private buildRecord(body: AuditBodyInput, options: RecordEmitOptions): AuditLogRecord {
    return AuditLogRecordSchema.parse({
      log_id: newLogId(),
      timestamp: utcNowIso(),
      aic: this.aic,
      body,
      ...recordTailFields(options),
    });
  }
}
