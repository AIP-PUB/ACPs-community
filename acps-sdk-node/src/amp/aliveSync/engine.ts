/**
 * AMP alive-sync Consumer 引擎核心（对应 Python `acps_sdk.amp.alive_sync.engine`）。
 *
 * 本模块分三层：
 *   1. 纯判定函数（无状态、无 I/O）—— 最易单测
 *   2. DeltaDecision / classifyDelta —— 纯决策核
 *   3. AliveSyncEngine —— 有状态编排（持 in-memory 水位），持久化委托 AliveSyncStore
 */

import {
  type AliveDeltaEnvelope,
  type AliveSnapshotMeta,
  aicFromObjectId,
  seqFromStr,
} from "../heartbeatSync.js";
import { GapDetectedError, ResyncRequired } from "./errors.js";
import type { AliveRecord, AliveSyncStore, ShardCheckpoint } from "./store.js";

// ── 1. 纯判定函数 ────────────────────────────────────────────────────────────

/** seq 闸门：seq > lastSeenSeq 才放行（§7.5 第 7 条第一道去重）。 */
export function passesSeqGate(seq: number, lastSeenSeq: number): boolean {
  return seq > lastSeenSeq;
}

/** 缺口检测：seq > lastSeenSeq + 1（§7.5 第 7 条；调用前已过 seq 闸门）。 */
export function isGap(seq: number, lastSeenSeq: number): boolean {
  return seq > lastSeenSeq + 1;
}

/** 版本幂等检查（第二道去重）：localVersion 缺失或 version 更大才应用（§7.5 第 2 条）。 */
export function passesVersion(version: number, localVersion: number | null | undefined): boolean {
  return localVersion === null || localVersion === undefined || version > localVersion;
}

// ── 2. DeltaDecision + classifyDelta ───────────────────────────────────────

/** 引擎对单条 delta 的处置决策。 */
export const DeltaDecision = {
  APPLY_UPSERT: "APPLY_UPSERT",
  APPLY_DELETE: "APPLY_DELETE",
  /** seq ≤ lastSeenSeq，重复投递 */
  SKIP_SEQ_GATE: "SKIP_SEQ_GATE",
  /** 通过闸门但 version ≤ localVersion（snapshot 行更新） */
  SKIP_VERSION: "SKIP_VERSION",
  /** seq > lastSeenSeq + 1，需触发重同步 */
  GAP: "GAP",
} as const;
export type DeltaDecision = (typeof DeltaDecision)[keyof typeof DeltaDecision];

/**
 * 纯决策核：根据信封内容与当前水位返回处置决策。
 *
 * 决策流程（§7.5 伪代码落地）：
 *   1. seqFromStr(env.seq) → seq（数值化，C-SYNC 第 5 条禁止字典序）
 *   2. passesSeqGate → false → SKIP_SEQ_GATE
 *   3. isGap → true → GAP
 *   4. passesVersion → false → SKIP_VERSION
 *   5. env.op === "upsert" → APPLY_UPSERT，否则 APPLY_DELETE
 *
 * 所有 seq / version 比较均经 seqFromStr 数值化，禁止字符串字典序。
 */
export function classifyDelta(
  env: AliveDeltaEnvelope,
  lastSeenSeq: number,
  localVersion: number | null | undefined,
): DeltaDecision {
  const seq = seqFromStr(env.seq);
  const version = seqFromStr(env.version);

  if (!passesSeqGate(seq, lastSeenSeq)) {
    return DeltaDecision.SKIP_SEQ_GATE;
  }
  if (isGap(seq, lastSeenSeq)) {
    return DeltaDecision.GAP;
  }
  if (!passesVersion(version, localVersion)) {
    return DeltaDecision.SKIP_VERSION;
  }
  return env.op === "upsert" ? DeltaDecision.APPLY_UPSERT : DeltaDecision.APPLY_DELETE;
}

// ── 3. AliveSyncEngine ───────────────────────────────────────────────────────

export interface ApplyDeltaOptions {
  /** 本条消息之后的 Kafka offset，随 checkpoint 一并持久化。 */
  kafkaNextOffset?: number | null;
}

/**
 * 有状态 alive-sync Consumer 引擎。
 *
 * 内存维护 per-shard lastSeenSeq 与 per-aic localVersion，持久化委托 store。
 * 引擎本身不做 HTTP / Kafka I/O，恢复策略由上层服务决定。
 */
export class AliveSyncEngine {
  private readonly store: AliveSyncStore;
  private lastSeenSeq = new Map<string, number>();
  private localVersion = new Map<string, number>();
  private cutoverSeq = new Map<string, number>();

  constructor(store: AliveSyncStore) {
    this.store = store;
  }

  /**
   * 启动续跑：从 store 读取 checkpoint 与 localVersion 恢复内存水位。
   *
   * @throws ResyncRequired checkpoint 不存在（reason = "no_checkpoints"）
   */
  async hydrate(): Promise<void> {
    const checkpoints = await this.store.loadCheckpoints();
    if (checkpoints.length === 0) {
      throw new ResyncRequired("no_checkpoints");
    }

    this.lastSeenSeq = new Map(checkpoints.map((cp) => [cp.shard, cp.lastSeenSeq] as const));
    this.cutoverSeq = new Map(checkpoints.map((cp) => [cp.shard, cp.cutoverSeq] as const));
    this.localVersion = new Map(Object.entries(await this.store.loadLocalVersions()));
  }

  /**
   * 原子应用全量 snapshot。
   *
   * 流程：
   *   1. 把 rows 包装成「边转 AliveRecord 边旁记 version」的异步生成器
   *   2. 构造各 shard ShardCheckpoint（lastSeenSeq = cutoverSeq = cutover）
   *   3. 调 store.replaceAliveSet(gen, checkpoints)：单事务原子替换
   *   4. 仅事务提交成功后才重置内存 lastSeenSeq 与 localVersion
   *   （中途异常则内存维持原状，上层下一轮重新 bootstrap）
   */
  async applySnapshot(
    meta: AliveSnapshotMeta,
    rows: AsyncIterable<AliveDeltaEnvelope>,
  ): Promise<void> {
    const cutoverByShard = new Map<string, number>(
      Object.entries(meta.cutoverSeqByShard).map(
        ([shard, seq]) => [shard, seqFromStr(seq)] as const,
      ),
    );

    const newLocalVersions = new Map<string, number>();

    async function* recordGen(): AsyncGenerator<AliveRecord> {
      for await (const env of rows) {
        const aic = aicFromObjectId(env.id);
        const version = seqFromStr(env.version);
        newLocalVersions.set(aic, version);
        yield {
          aic,
          alive: true,
          lastSeenAt: env.payload ? env.payload.lastSeenAt : null,
          version,
          shard: env.shard,
        };
      }
    }

    const checkpoints: ShardCheckpoint[] = [...cutoverByShard].map(([shard, cutover]) => ({
      shard,
      lastSeenSeq: cutover,
      cutoverSeq: cutover,
      kafkaNextOffset: null,
      snapshotGeneratedAt: meta.generatedAt,
    }));

    await this.store.replaceAliveSet(recordGen(), checkpoints);

    // 仅事务提交后才更新内存水位
    this.lastSeenSeq = new Map(cutoverByShard);
    this.cutoverSeq = new Map(cutoverByShard);
    this.localVersion = newLocalVersions;
  }

  /**
   * 应用单条 delta 事件并返回决策。
   *
   * - GAP → 抛 GapDetectedError（由上层决定恢复策略）
   * - APPLY_UPSERT/APPLY_DELETE → 调 store 原子持久化，然后推进内存水位
   * - SKIP_* → 仅推进内存 lastSeenSeq（不写库，持久化滞后是安全方向）
   */
  async applyDelta(
    env: AliveDeltaEnvelope,
    { kafkaNextOffset = null }: ApplyDeltaOptions = {},
  ): Promise<DeltaDecision> {
    const shard = env.shard;
    const lastSeen = this.lastSeenSeq.get(shard) ?? 0;
    const aic = aicFromObjectId(env.id);
    const localVer = this.localVersion.get(aic) ?? null;

    const decision = classifyDelta(env, lastSeen, localVer);
    const seq = seqFromStr(env.seq);
    const version = seqFromStr(env.version);

    if (decision === DeltaDecision.GAP) {
      throw new GapDetectedError(shard, lastSeen + 1, seq);
    }

    if (decision === DeltaDecision.APPLY_UPSERT) {
      const record: AliveRecord = {
        aic,
        alive: true,
        lastSeenAt: env.payload ? env.payload.lastSeenAt : null,
        version,
        shard,
      };
      await this.store.applyUpsert(record, shard, seq, kafkaNextOffset);
      this.lastSeenSeq.set(shard, seq);
      this.localVersion.set(aic, version);
    } else if (decision === DeltaDecision.APPLY_DELETE) {
      await this.store.applyDelete(aic, shard, seq, kafkaNextOffset, version);
      this.lastSeenSeq.set(shard, seq);
      // 保留 localVersion[aic] = version（「保留+自然有界」方案，§5.4）
      this.localVersion.set(aic, version);
    } else {
      // SKIP_SEQ_GATE / SKIP_VERSION：仅推进内存 seq（不写库）
      this.lastSeenSeq.set(shard, Math.max(this.lastSeenSeq.get(shard) ?? 0, seq));
    }

    return decision;
  }

  /** 暴露当前 cutover seq 映射，供 Kafka 自举定位 offset（§7.5 第 8 条）。 */
  cutoverSeqByShard(): Record<string, number> {
    return Object.fromEntries(this.cutoverSeq);
  }
}
