/** 对应 Python tests/amp/alive_sync/conftest.py：FakeAliveSyncStore — 内存实现 AliveSyncStore 协议。 */

import type {
  AliveRecord,
  AliveSyncStore,
  AliveView,
  ShardCheckpoint,
} from "../../../src/amp/aliveSync/index.js";

/** 基于 Map 的内存实现，满足 AliveSyncStore 协议（结构子类型，编译期检查）。 */
export class FakeAliveSyncStore implements AliveSyncStore {
  rows = new Map<string, AliveRecord>();
  checkpoints = new Map<string, ShardCheckpoint>();

  async loadAliveViews(aics: readonly string[]): Promise<Record<string, AliveView>> {
    const result: Record<string, AliveView> = {};
    for (const aic of aics) {
      const row = this.rows.get(aic);
      if (row !== undefined) {
        result[aic] = { aic: row.aic, alive: row.alive, lastSeenAt: row.lastSeenAt };
      }
    }
    return result;
  }

  async replaceAliveSet(
    records: AsyncIterable<AliveRecord>,
    checkpoints: ShardCheckpoint[],
  ): Promise<void> {
    this.rows.clear();
    this.checkpoints.clear();
    for await (const record of records) {
      this.rows.set(record.aic, record);
    }
    for (const cp of checkpoints) {
      this.checkpoints.set(cp.shard, cp);
    }
  }

  async applyUpsert(
    record: AliveRecord,
    shard: string,
    lastSeenSeq: number,
    kafkaNextOffset: number | null,
  ): Promise<void> {
    this.rows.set(record.aic, record);
    const old = this.checkpoints.get(shard);
    this.checkpoints.set(shard, {
      shard,
      lastSeenSeq,
      cutoverSeq: old ? old.cutoverSeq : 0,
      kafkaNextOffset,
      snapshotGeneratedAt: old ? old.snapshotGeneratedAt : null,
    });
  }

  async applyDelete(
    aic: string,
    shard: string,
    lastSeenSeq: number,
    kafkaNextOffset: number | null,
    version: number,
  ): Promise<void> {
    const oldRow = this.rows.get(aic);
    if (oldRow !== undefined) {
      this.rows.set(aic, {
        aic: oldRow.aic,
        alive: false,
        lastSeenAt: oldRow.lastSeenAt,
        version,
        shard: oldRow.shard,
      });
    }
    const old = this.checkpoints.get(shard);
    this.checkpoints.set(shard, {
      shard,
      lastSeenSeq,
      cutoverSeq: old ? old.cutoverSeq : 0,
      kafkaNextOffset,
      snapshotGeneratedAt: old ? old.snapshotGeneratedAt : null,
    });
  }

  async loadCheckpoints(): Promise<ShardCheckpoint[]> {
    return [...this.checkpoints.values()];
  }

  async loadLocalVersions(): Promise<Record<string, number>> {
    return Object.fromEntries([...this.rows].map(([aic, row]) => [aic, row.version]));
  }

  async reset(): Promise<void> {
    this.rows.clear();
    this.checkpoints.clear();
  }
}

// 对应 Python 的 `assert isinstance(FakeAliveSyncStore(), AliveSyncStore)`：结构子类型由编译期保证。
const _protocolCheck: AliveSyncStore = new FakeAliveSyncStore();
void _protocolCheck;
