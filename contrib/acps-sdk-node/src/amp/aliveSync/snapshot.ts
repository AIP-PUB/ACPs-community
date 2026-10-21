/**
 * AMP alive-sync NDJSON snapshot 解析（存储/传输无关，由宿主负责流式 HTTP）。
 *
 * 对应 Python `acps_sdk.amp.alive_sync.snapshot`。snapshot 格式：
 *   行 0：snapshot-meta（AliveSnapshotMeta）
 *   行 1+：kind="snapshot" op="upsert" 的 AliveDeltaEnvelope（存活 AIC 一行）
 */

import { formatZodError } from "../../internal/schema.js";
import {
  ALIVE_DELTA_TYPE,
  type AliveDeltaEnvelope,
  AliveDeltaEnvelopeSchema,
  type AliveSnapshotMeta,
  AliveSnapshotMetaSchema,
  SNAPSHOT_META_RECORD_TYPE,
  aicFromObjectId,
  seqFromStr,
} from "../heartbeatSync.js";
import { pyStr, pyTruthy, pyTypeName } from "../../internal/pyCompat.js";
import { SnapshotProtocolError } from "./errors.js";
import type { AliveRecord } from "./store.js";

const utf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

/**
 * 字节按 UTF-8 严格解码，再解析为 JSON 对象。
 *
 * 与 Python 版的差异（有意为之）：非法 UTF-8 与「JSON 合法但不是对象」（数组、数字、null 等）
 * 在 Python 里分别抛 UnicodeDecodeError / AttributeError；Node 版统一包装为 SnapshotProtocolError，
 * 调用方只需处理一种协议错误。
 */
function parseJsonObject(raw: string | Uint8Array, what: string): Record<string, unknown> {
  let text: string;
  try {
    text = typeof raw === "string" ? raw : utf8.decode(raw);
  } catch (error) {
    throw new SnapshotProtocolError(`${what} UTF-8 解码失败: ${String(error)}`, { cause: error });
  }
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch (error) {
    throw new SnapshotProtocolError(`${what} JSON 解析失败: ${String(error)}`, { cause: error });
  }
  if (data === null || typeof data !== "object" || Array.isArray(data)) {
    throw new SnapshotProtocolError(`${what} 不是 JSON 对象: 实际为 ${pyTypeName(data)}`);
  }
  return data as Record<string, unknown>;
}

/**
 * 解析 snapshot 首行（snapshot-meta）。
 *
 * @param raw NDJSON 首行文本或字节
 * @throws SnapshotProtocolError UTF-8 / JSON 解析失败、不是 JSON 对象、recordType 或 type 字段不符合契约、字段校验失败
 */
export function parseMetaLine(raw: string | Uint8Array): AliveSnapshotMeta {
  const data = parseJsonObject(raw, "snapshot meta 行");

  if (data["recordType"] !== SNAPSHOT_META_RECORD_TYPE) {
    throw new SnapshotProtocolError(
      `snapshot meta 行 recordType 非法: 期望 '${SNAPSHOT_META_RECORD_TYPE}'，` +
        `实际 '${pyStr(data["recordType"])}'`,
    );
  }
  if (data["type"] !== ALIVE_DELTA_TYPE) {
    throw new SnapshotProtocolError(
      `snapshot meta 行 type 非法: 期望 '${ALIVE_DELTA_TYPE}'，实际 '${pyStr(data["type"])}'`,
    );
  }

  const parsed = AliveSnapshotMetaSchema.safeParse(data);
  if (!parsed.success) {
    throw new SnapshotProtocolError(
      `snapshot meta 行字段校验失败: ${formatZodError(parsed.error)}`,
      {
        cause: parsed.error,
      },
    );
  }
  return parsed.data;
}

/**
 * 解析 snapshot 数据行（kind="snapshot" op="upsert"）。
 *
 * @param raw NDJSON 数据行文本或字节
 * @throws SnapshotProtocolError UTF-8 / JSON 解析失败、不是 JSON 对象、kind/op/payload 不符合契约、字段校验失败
 */
export function parseSnapshotRow(raw: string | Uint8Array): AliveDeltaEnvelope {
  const data = parseJsonObject(raw, "snapshot 数据行");

  if (data["kind"] !== "snapshot") {
    throw new SnapshotProtocolError(
      `snapshot 数据行 kind 非法: 期望 'snapshot'，实际 '${pyStr(data["kind"])}'`,
    );
  }
  if (data["op"] !== "upsert") {
    throw new SnapshotProtocolError(
      `snapshot 数据行 op 非法: 期望 'upsert'，实际 '${pyStr(data["op"])}'`,
    );
  }
  if (!pyTruthy(data["payload"])) {
    throw new SnapshotProtocolError("snapshot 数据行 payload 为空");
  }

  const parsed = AliveDeltaEnvelopeSchema.safeParse(data);
  if (!parsed.success) {
    throw new SnapshotProtocolError(
      `snapshot 数据行字段校验失败: ${formatZodError(parsed.error)}`,
      {
        cause: parsed.error,
      },
    );
  }
  return parsed.data;
}

/**
 * 将 snapshot upsert 行转换为存储 AliveRecord（alive=true，version=seqFromStr(env.version)）。
 *
 * @param env 已通过 parseSnapshotRow 校验的 AliveDeltaEnvelope
 */
export function snapshotRowToRecord(env: AliveDeltaEnvelope): AliveRecord {
  return {
    aic: aicFromObjectId(env.id),
    alive: true,
    lastSeenAt: env.payload ? env.payload.lastSeenAt : null,
    version: seqFromStr(env.version),
    shard: env.shard,
  };
}
