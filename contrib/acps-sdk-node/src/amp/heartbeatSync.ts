/**
 * AMP Heartbeat alive-delta 同步子协议线缆模型（spec §6.2.1）与编码约定（设计 §4.1、§4.3）。
 *
 * 对应 Python `acps_sdk.amp.heartbeat_sync`。Provider 写 Kafka/NDJSON、Consumer 读，双方共享本模块。
 * 字段名为线上 camelCase 名；序列化：解析后的对象直接 `JSON.stringify`。
 */

import { z } from "zod";

import { optional } from "../internal/schema.js";
import { isAsciiDigits } from "../internal/pyCompat.js";

// ── 常量 ──────────────────────────────────────────────────────────────────────

export const ALIVE_DELTA_TYPE = "amp-alive-delta";
export const ALIVE_DELTA_SCHEMA_VERSION = "1";
export const SNAPSHOT_META_RECORD_TYPE = "snapshot-meta";
export const SNAPSHOT_CONTENT_TYPE = "application/x-ndjson";
export const ALIVE_OBJECT_ID_PREFIX = "urn:amp:alive:";
export const HEARTBEAT_TOPIC_DEFAULT = "amp.heartbeat";
export const ALIVE_DELTA_TOPIC_DEFAULT = "amp.heartbeat.alive-delta";

// ── 类型别名 ──────────────────────────────────────────────────────────────────

export const DeltaOpSchema = z.enum(["upsert", "delete"]);
export type DeltaOp = z.output<typeof DeltaOpSchema>;

export const DeltaKindSchema = z.enum(["snapshot", "enter_alive", "refresh_alive", "leave_alive"]);
export type DeltaKind = z.output<typeof DeltaKindSchema>;

// ── 编码工具（线缆两侧必须一致，故入 SDK） ────────────────────────────────────

/**
 * 将 AIC 编码为 alive object id（§7.1）。
 *
 * @returns "urn:amp:alive:<aic>" 格式的 object id
 */
export function aliveObjectId(aic: string): string {
  return `${ALIVE_OBJECT_ID_PREFIX}${aic}`;
}

/**
 * 从 alive object id 反解 AIC。
 *
 * @throws Error 前缀不匹配
 */
export function aicFromObjectId(objectId: string): string {
  if (!objectId.startsWith(ALIVE_OBJECT_ID_PREFIX)) {
    throw new Error(`object_id 前缀不匹配，期望 '${ALIVE_OBJECT_ID_PREFIX}'，实际 '${objectId}'`);
  }
  return objectId.slice(ALIVE_OBJECT_ID_PREFIX.length);
}

/** Python `f"{value:0{width}d}"`：符号在前、数字部分补零。 */
function zeroPadInt(value: number, width: number): string {
  const digits = String(Math.abs(value));
  return value < 0 ? `-${digits.padStart(width - 1, "0")}` : digits.padStart(width, "0");
}

/**
 * 将分片索引编码为线缆 shard id 字符串（§4.1）。
 *
 * 宽度 = max(3, len(str(shardCount - 1)))，即 ≤1000 分片固定 3 位（hb-000），
 * 超过自动加宽（如 shardCount=10000 → 4 位 hb-0000）。
 *
 * @param shardIndex 分片索引（0-based）
 * @param shardCount 总分片数
 * @throws TypeError 参数不是整数（Python 的 `d` 格式只接受 int）
 */
export function shardId(shardIndex: number, shardCount: number): string {
  if (!Number.isInteger(shardIndex) || !Number.isInteger(shardCount)) {
    throw new TypeError("shard_index 与 shard_count 必须是整数");
  }
  const width = Math.max(3, String(shardCount - 1).length);
  return `hb-${zeroPadInt(shardIndex, width)}`;
}

/** Python `int(str)`（仅 ASCII 十进制）：超出安全整数范围时报错，避免静默丢精度。 */
function parseDecimal(digits: string): number {
  const value = Number(digits);
  if (!Number.isSafeInteger(value)) {
    throw new RangeError(`整数超出安全整数范围: ${digits}`);
  }
  return value;
}

/**
 * 从 shard id 字符串反解分片索引。
 *
 * @param shard "hb-NNN" 格式的 shard id
 * @throws Error 格式非法
 */
export function shardIndexFromId(shard: string): number {
  if (!shard.startsWith("hb-")) {
    throw new Error(`shard id 格式非法，期望 'hb-NNN'，实际 '${shard}'`);
  }
  const numericPart = shard.slice(3);
  if (!isAsciiDigits(numericPart)) {
    throw new Error(`shard id 数字部分非法：'${numericPart}'`);
  }
  return parseDecimal(numericPart);
}

/** 将 seq 数值编码为线缆字符串（十进制整数字符串，§4.3）。 */
export function seqToStr(seq: number): string {
  return String(seq);
}

/**
 * 将线缆 seq 字符串解码为数值。
 *
 * 跨进程 seq 比较必须经由本函数解码后按数值比较，禁止字符串字典序（§4.3、C-SYNC-3）。
 *
 * 只接受 ASCII 数字（Python 的 `str.isdigit()` 还接受其它 Unicode 数字字符）；
 * 超出 `Number.MAX_SAFE_INTEGER` 时抛 RangeError（Python 为任意精度整数）。
 *
 * @throws Error 非数字、负数或空串
 */
export function seqFromStr(value: string): number {
  if (!value) {
    throw new Error("seq 字符串不能为空");
  }
  const unsigned = value.replace(/^-+/, "");
  if (!isAsciiDigits(unsigned)) {
    throw new Error(`seq 字符串非整数：'${value}'`);
  }
  const signs = value.length - unsigned.length;
  if (signs > 1) {
    // Python int("--5") 抛 ValueError
    throw new Error(`invalid literal for int() with base 10: '${value}'`);
  }
  const magnitude = parseDecimal(unsigned);
  if (signs === 1 && magnitude !== 0) {
    throw new Error(`seq 不能为负数：-${magnitude}`);
  }
  return magnitude;
}

// ── 模型 ──────────────────────────────────────────────────────────────────────

/** 单个 alive AIC 的 Sync Profile 线缆表示（spec §6.2.1）。 */
export const AliveSetEntrySchema = z.object({
  aic: z.string(),
  /** ISO 8601 UTC 时间戳 */
  lastSeenAt: z.string(),
  sourceTimestamp: optional(z.string()),
});
export type AliveSetEntry = z.output<typeof AliveSetEntrySchema>;

/** alive-delta Kafka 消息信封（spec §6.2.1）。 */
export const AliveDeltaEnvelopeSchema = z.object({
  shard: z.string(),
  /** 十进制整数字符串，数值比较用 seqFromStr() */
  seq: z.string(),
  type: z.literal(ALIVE_DELTA_TYPE),
  /** urn:amp:alive:<aic> */
  id: z.string(),
  /** 与 seq 同值 */
  version: z.string(),
  op: DeltaOpSchema,
  kind: DeltaKindSchema,
  /** op=delete 可省略 */
  payload: optional(AliveSetEntrySchema),
});
export type AliveDeltaEnvelope = z.output<typeof AliveDeltaEnvelopeSchema>;

/** alive-delta Sync Profile 元数据端点响应（/sync/info，spec §6.2.1）。 */
export const HeartbeatSyncInfoSchema = z.object({
  type: z.literal(ALIVE_DELTA_TYPE),
  schemaVersion: z.string(),
  snapshotContentType: z.literal(SNAPSHOT_CONTENT_TYPE),
  kafkaTopic: z.string(),
  shardCount: z.number().int(),
  refreshEmitIntervalSeconds: z.number().int(),
  deltaRetentionHours: z.number().int(),
  /** shard id → seq 字符串；Consumer 用 seqFromStr() 按数值比较 */
  currentPublishedSeqByShard: z.record(z.string(), z.string()),
});
export type HeartbeatSyncInfo = z.output<typeof HeartbeatSyncInfoSchema>;

/** 全量快照 NDJSON 首行元数据（snapshot-meta，spec §6.2.1）。 */
export const AliveSnapshotMetaSchema = z.object({
  recordType: z.literal(SNAPSHOT_META_RECORD_TYPE),
  type: z.literal(ALIVE_DELTA_TYPE),
  /** Consumer 从此 seq 之后订阅 delta，shard id → seq 字符串 */
  cutoverSeqByShard: z.record(z.string(), z.string()),
  /** ISO 8601 UTC 快照生成时间 */
  generatedAt: z.string(),
});
export type AliveSnapshotMeta = z.output<typeof AliveSnapshotMetaSchema>;
