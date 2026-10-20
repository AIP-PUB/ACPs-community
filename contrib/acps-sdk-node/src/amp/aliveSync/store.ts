/**
 * AMP alive-sync 存储抽象层（对应 Python `acps_sdk.amp.alive_sync.store`）。
 *
 * 定义了存储层的数据结构与协议，使引擎（engine.ts）完全不依赖任何具体存储实现。
 * 宿主项目（如 discovery-server）提供 AliveSyncStore 的具体实现。
 */

import type { AliveView } from "./models.js";

/** 写入存储的 alive 状态行（来自 snapshot 行或 delta 事件）。 */
export interface AliveRecord {
  readonly aic: string;
  readonly alive: boolean;
  readonly lastSeenAt: string | null;
  readonly version: number;
  readonly shard: string;
}

/** per-shard 持久化 checkpoint，用于续跑与自举。 */
export interface ShardCheckpoint {
  readonly shard: string;
  readonly lastSeenSeq: number;
  readonly cutoverSeq: number;
  readonly kafkaNextOffset: number | null;
  readonly snapshotGeneratedAt: string | null;
}

/**
 * 只读接口：按 AIC 批量查询 alive 视图。
 *
 * 供查询侧（holder → enrichment）使用，无写能力。
 */
export interface AliveReader {
  /**
   * 批量查询指定 AIC 的 alive 视图。
   *
   * 不存在于存储中的 AIC 不出现在结果中（键缺失 = 未知，非 not-alive）。
   */
  loadAliveViews(aics: readonly string[]): Promise<Record<string, AliveView>>;
}

/**
 * 读写接口：引擎用于持久化 alive 状态与 checkpoint。
 *
 * 继承 AliveReader，实现方只需实现一次 loadAliveViews。
 */
export interface AliveSyncStore extends AliveReader {
  /**
   * 原子替换全量 alive 集合（snapshot 应用）。
   *
   * 须在单事务内：先清空两张表，再流式写入 records 与 checkpoints。
   */
  replaceAliveSet(
    records: AsyncIterable<AliveRecord>,
    checkpoints: ShardCheckpoint[],
  ): Promise<void>;

  /**
   * 应用 enter_alive / refresh_alive delta：写入 alive 行并推进 checkpoint。
   *
   * alive 行与 checkpoint 须在同一事务内提交（C-SYNC-6 §9.3）。
   */
  applyUpsert(
    record: AliveRecord,
    shard: string,
    lastSeenSeq: number,
    kafkaNextOffset: number | null,
  ): Promise<void>;

  /**
   * 应用 leave_alive delta：置 alive=false（保留行与 version），推进 checkpoint。
   *
   * alive 行与 checkpoint 须在同一事务内提交（C-SYNC-5/6）。
   */
  applyDelete(
    aic: string,
    shard: string,
    lastSeenSeq: number,
    kafkaNextOffset: number | null,
    version: number,
  ): Promise<void>;

  /** 读取所有 shard 的持久化 checkpoint（用于 hydrate 续跑）。 */
  loadCheckpoints(): Promise<ShardCheckpoint[]>;

  /** 读取所有 alive 行的 {aic: version} 映射（用于 hydrate 内存 localVersion）。 */
  loadLocalVersions(): Promise<Record<string, number>>;

  /** 单事务清空两张表（agent_alive_status + alive_sync_shard_state）。 */
  reset(): Promise<void>;
}
