/**
 * AMP alive-sync Consumer 引擎层异常（对应 Python `acps_sdk.amp.alive_sync.errors`）。
 *
 * SDK 引擎层只抛这里的异常；HTTP/Kafka 传输层的应用错误由各宿主项目自行定义。
 */

import { reprStr } from "../../internal/pyCompat.js";

/** 引擎层根异常。所有 alive-sync 引擎内部错误均继承本类。 */
export class AliveSyncError extends Error {
  constructor(message?: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "AliveSyncError";
  }
}

/** NDJSON snapshot 协议违规（meta 行格式非法、行字段缺失等）。 */
export class SnapshotProtocolError extends AliveSyncError {
  constructor(message?: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "SnapshotProtocolError";
  }
}

/** 检测到序号缺口（seq > lastSeenSeq + 1），需触发重同步。 */
export class GapDetectedError extends AliveSyncError {
  readonly shard: string;
  readonly expectedSeq: number;
  readonly gotSeq: number;

  constructor(shard: string, expectedSeq: number, gotSeq: number) {
    super(`Gap on shard ${reprStr(shard)}: expected seq ${expectedSeq}, got ${gotSeq}`);
    this.name = "GapDetectedError";
    this.shard = shard;
    this.expectedSeq = expectedSeq;
    this.gotSeq = gotSeq;
  }
}

/** 无法从当前状态续跑，需从头重同步（如 checkpoint 丢失、offset 越界等）。 */
export class ResyncRequired extends AliveSyncError {
  readonly reason: string;

  constructor(reason: string) {
    super(`Resync required: ${reason}`);
    this.name = "ResyncRequired";
    this.reason = reason;
  }
}
