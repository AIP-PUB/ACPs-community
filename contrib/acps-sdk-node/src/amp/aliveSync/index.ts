/**
 * AMP Heartbeat Alive-Delta Consumer 引擎（存储/传输无关）。
 *
 * 对应 Python `acps_sdk.amp.alive_sync`。
 */

export { nextLookbackSeconds, seekTimestampMs } from "./bootstrap.js";
export type { NextLookbackOptions } from "./bootstrap.js";
export {
  AliveSyncEngine,
  DeltaDecision,
  classifyDelta,
  isGap,
  passesSeqGate,
  passesVersion,
} from "./engine.js";
export type { ApplyDeltaOptions } from "./engine.js";
export {
  AliveSyncError,
  GapDetectedError,
  ResyncRequired,
  SnapshotProtocolError,
} from "./errors.js";
export { AliveView } from "./models.js";
export type { AliveOutputDict } from "./models.js";
export { parseMetaLine, parseSnapshotRow, snapshotRowToRecord } from "./snapshot.js";
export type { AliveReader, AliveRecord, AliveSyncStore, ShardCheckpoint } from "./store.js";
