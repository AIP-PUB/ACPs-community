"""Heartbeat Relay — outbox → Kafka alive-delta 发布链路（§3.6 / §5.4）。

职责：
1. 监听 Redis Stream outbox（每 shard 独立消费组 XREADGROUP）
2. 逐条构造 AliveDeltaEnvelope 并 send_and_wait 到 Kafka alive-delta（C-RELAY-2）
3. 每条发布后调用 relay_ack（epoch 守卫 XACK，C-RELAY-1）
4. 每批 / 追平时调用 relay_commit_published_seq（C-RELAY-3）
5. 周期调用 relay_trim（epoch 守卫 XTRIM，C-RELAY-1）
6. 重启时 XAUTOCLAIM 接管旧 PEL 重发（C-RELAY-5）
7. _reset_published_seq 按 §5.4 三分支重置 published_seq
8. 积压超阈且 XREADGROUP 连续空读时重置消费组自愈（避免 last-delivered 僵局）

C-RELAY-1：relay_ack / relay_trim / relay_commit 三函数均有 epoch fencing。
C-RELAY-2：enable_idempotence=True, acks="all", max_in_flight=1, send_and_wait 串行。
C-RELAY-3：每批 + 追平即推进 published_seq。
C-RELAY-5：_recover_pel 通过 XAUTOCLAIM 接管任意旧 consumer PEL。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from acps_sdk.amp.heartbeat_sync import (
    ALIVE_DELTA_TYPE,
    AliveDeltaEnvelope,
    AliveSetEntry,
    DeltaKind,
    DeltaOp,
    alive_object_id,
    shard_index_from_id,
)
from aiokafka import AIOKafkaProducer
from redis.asyncio import Redis

from app.core.config import settings
from app.heartbeat.exception import HeartbeatConfigError
from app.heartbeat.functions import (
    relay_ack,
    relay_commit_published_seq,
    relay_trim,
)
from app.heartbeat.metrics import metrics
from app.heartbeat.redis_keys import (
    delta_outbox_key,
    relay_epoch_key,
    relay_lock_key,
)
from app.heartbeat.sharding import all_shard_ids
from app.heartbeat.store import outbox_publish_lag_ms

OUTBOX_CONSUMER_GROUP: Final = "amp-hb-relay"

_LOCK_TTL_SECONDS: Final = 30
_POLL_BLOCK_MS: Final = 500
_XREADGROUP_COUNT: Final = 100
_TRIM_INTERVAL: Final = 500  # XTRIM 触发间隔（XACK 条数）
# 积压超阈且 XREADGROUP 连续空读时，重置消费组以自愈（≈ 10 * 500ms）
_HEAL_EMPTY_POLLS: Final = 10
_IDLE_CONSUMER_MS: Final = 60_000

# Lua 脚本：原子 CAS 续锁（A-5：防止 epoch 切换后误续期）
# 返回 1 表示续期成功；0 表示 owner 不匹配或 key 不存在。
_RELAY_LOCK_RENEW_SCRIPT: Final = (
    "local cur = redis.call('GET', KEYS[1]) "
    "if cur == false or cur ~= ARGV[1] then return 0 end "
    "redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]), 'XX') "
    "return 1"
)

logger = structlog.get_logger(__name__)


def _ms_to_iso(ms: int) -> str:
    """将毫秒时间戳转为 ISO 8601 UTC 字符串（毫秒精度，Z 后缀）。

    Args:
        ms: UTC 毫秒时间戳。

    Returns:
        形如 "2023-11-14T22:13:20.000Z" 的字符串（与 Lua observed_at_iso 一致）。
    """
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class HeartbeatRelay:
    """outbox → Kafka alive-delta 发布器（§3.6 / §5.4）。

    每个 shard 独立一个 _shard_loop asyncio task，持有 relay_lock（NX EX）
    并通过 relay_epoch 实现 epoch fencing（防止僵尸 relay 的 XACK / XTRIM 污染）。
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._node_id = uuid.uuid4().hex
        self._consumer_name = f"relay-{self._node_id}"
        self._producer: AIOKafkaProducer | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False

        # per-shard mutable state
        self._last_acked_entry_id: dict[str, str] = {}
        self._acked_since_trim: dict[str, int] = {}
        self._expected_seq: dict[str, int] = {}
        # 积压超阈时连续空读计数（触发消费组重置自愈）
        self._empty_lag_polls: dict[str, int] = {}

        # 指标（内存 counter，已接入 HeartbeatMetrics，B-6）
        self._truncated_total: int = 0
        self._published_total: int = 0

    # ── 生命周期 ───────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动 Relay：创建 Producer，校验分区数（C-CONF-1），初始化消费组。

        Raises:
            HeartbeatConfigError: shard_count 与 delta_topic 分区数不符（C-CONF-1）。
        """
        delta_topic = settings.heartbeat_delta_topic
        self._producer = AIOKafkaProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            enable_idempotence=True,
            acks="all",
            # C-RELAY-2 严格串行由 send_and_wait 保证；aiokafka 0.14 不支持 max_in_flight 参数
        )
        await self._producer.start()

        # C-CONF-1: 校验 shard_count == partition count
        partitions = await self._producer.partitions_for(delta_topic)
        expected = settings.heartbeat_heartbeat_shard_count
        actual = len(partitions) if partitions else 0
        if actual != expected:
            await self._producer.stop()
            raise HeartbeatConfigError(
                f"heartbeat_shard_count ({expected}) != partition count of {delta_topic} ({actual})"
            )

        # XGROUP CREATE MKSTREAM（幂等；BUSYGROUP 表示 group 已存在，忽略）
        for shard in all_shard_ids():
            outbox_key = delta_outbox_key(shard)
            try:
                await self._redis.xgroup_create(outbox_key, OUTBOX_CONSUMER_GROUP, "0", mkstream=True)
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    raise

        self._running = True

    async def run(self) -> None:
        """为每个 shard 启动独立 shard_loop task，等待所有 task 完成。"""
        for shard in all_shard_ids():
            task: asyncio.Task[None] = asyncio.create_task(self._shard_loop(shard))
            self._tasks.append(task)
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        """停止所有 shard loop，停止 producer。"""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    # ── shard 主循环 ──────────────────────────────────────────────────────────

    async def _shard_loop(self, shard: str) -> None:
        """单 shard 主循环：抢锁 → PEL 恢复 → published_seq 重置 → 发布循环。"""
        while self._running:
            epoch = await self._acquire_shard(shard)
            if epoch is None:
                await asyncio.sleep(5)
                continue
            try:
                await self._prune_idle_consumers(shard)
                await self._recover_pel(shard, epoch)
                await self._reset_published_seq(shard, epoch)

                while self._running:
                    ok = await self._publish_batch(shard, epoch)
                    if not ok:
                        # epoch 过期，放弃本届
                        break
                    # A-5: CAS 续锁（Lua 原子检查 owner 相符后才 SET XX EX）
                    renewed = await self._redis.eval(
                        _RELAY_LOCK_RENEW_SCRIPT,
                        1,
                        relay_lock_key(shard),
                        self._node_id,
                        str(_LOCK_TTL_SECONDS),
                    )
                    if not renewed:
                        logger.warning(
                            "relay_lock CAS 续期失败（owner 已变更），退出本届循环",
                            shard=shard,
                            node_id=self._node_id,
                        )
                        break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("shard_loop unexpected error, will retry", shard=shard)
            finally:
                # 尽力释放锁（仅当持有者为本节点时）
                try:
                    val = await self._redis.get(relay_lock_key(shard))
                    if val is not None and str(val) == self._node_id:
                        await self._redis.delete(relay_lock_key(shard))
                except Exception:
                    logger.debug("_shard_loop: failed to release relay_lock", shard=shard)

    async def _acquire_shard(self, shard: str) -> int | None:
        """尝试抢占 relay_lock，成功后 INCR relay_epoch 返回本届 epoch。

        Returns:
            本届 epoch 整数；抢占失败返回 None。
        """
        ok = await self._redis.set(relay_lock_key(shard), self._node_id, nx=True, ex=_LOCK_TTL_SECONDS)
        if not ok:
            return None
        epoch: int = await self._redis.incr(relay_epoch_key(shard))
        return epoch

    # ── PEL 恢复（C-RELAY-5） ─────────────────────────────────────────────────

    async def _recover_pel(self, shard: str, epoch: int) -> None:
        """XAUTOCLAIM 接管任意旧 consumer 的 PEL，按序 republish + relay_ack。

        A-4 修复：只有在 send_and_wait 成功后才调用 relay_ack；
        发布失败时跳过 ack，保留 PEL 条目以便下次重试。

        XAUTOCLAIM(start_id="0", min_idle_time=0) 接管全部 PEL（不限 consumer name）。
        返回 next_start_id = "0-0" 时表示 PEL 已遍历完毕。
        """
        outbox_key = delta_outbox_key(shard)
        while True:
            result: Any = await self._redis.xautoclaim(
                outbox_key,
                OUTBOX_CONSUMER_GROUP,
                self._consumer_name,
                min_idle_time=0,
                start_id="0",
            )
            # result = (next_start_id, [(entry_id, fields), ...], deleted_ids)
            next_start, entries, _deleted = result[0], result[1], result[2]
            for entry_id_raw, fields_raw in entries:
                entry_id = str(entry_id_raw)
                fields = {str(k): str(v) for k, v in fields_raw.items()}
                published = False
                try:
                    envelope = self._build_envelope(shard, fields)
                    partition = shard_index_from_id(shard)
                    if self._producer is not None:
                        await self._producer.send_and_wait(
                            settings.heartbeat_delta_topic,
                            value=json.dumps(
                                envelope.model_dump(mode="json", by_alias=True, exclude_none=True),
                                ensure_ascii=False,
                            ).encode(),
                            partition=partition,
                        )
                        published = True
                        self._published_total += 1
                        metrics.inc("amp_heartbeat_relay_published_total")
                except Exception:
                    logger.exception(
                        "_recover_pel: republish failed, skipping ack to prevent data loss",
                        shard=shard,
                        entry_id=entry_id,
                    )

                # A-4: 仅在发布成功后才 XACK（防止丢失条目）
                if not published:
                    continue

                ok = await relay_ack(self._redis, shard=shard, epoch=epoch, entry_id=entry_id)
                if not ok:
                    return

            # XAUTOCLAIM returns "0-0" when all PEL entries have been iterated
            if str(next_start) in ("0-0", "0"):
                break

    # ── published_seq 重置（§5.4 三分支） ────────────────────────────────────

    async def _reset_published_seq(self, shard: str, epoch: int) -> None:
        """按 §5.4 PEL 算法三分支重置 published_seq（via relay_commit_published_seq）。

        分支优先级：
        1. PEL 非空 → XPENDING 取最旧条目 seq → published = seq-1
        2. PEL 为空且 stream 有已投递条目 → last-delivered-id seq → published = seq
        3. stream 空或无 group → 保持原值（不调用 relay_commit）
        """
        outbox_key = delta_outbox_key(shard)
        group_name = OUTBOX_CONSUMER_GROUP

        # 分支 1：PEL 非空
        try:
            pending = await self._redis.xpending_range(outbox_key, group_name, min="-", max="+", count=1)
            if pending:
                min_pending_id = str(pending[0]["message_id"])
                entries = await self._redis.xrange(outbox_key, min_pending_id, min_pending_id)
                if entries:
                    _, fields_raw = entries[0]
                    fields = {str(k): str(v) for k, v in fields_raw.items()}  # type: ignore[union-attr]
                    seq = int(fields.get("seq", "1"))
                    published_seq = max(0, seq - 1)
                    await relay_commit_published_seq(self._redis, shard=shard, epoch=epoch, seq=published_seq)
                return
        except Exception:
            logger.debug("_reset_published_seq: PEL branch failed", shard=shard)

        # 分支 2：PEL 为空，检查 last-delivered-id
        try:
            groups_info = await self._redis.xinfo_groups(outbox_key)
            for group_info in groups_info:
                gname = group_info.get("name", "")
                if gname != group_name:
                    continue
                last_delivered = str(group_info.get("last-delivered-id", "0-0"))
                if last_delivered not in ("0-0", "0"):
                    entries = await self._redis.xrange(outbox_key, last_delivered, last_delivered)
                    if entries:
                        _, fields_raw = entries[0]
                        fields = {str(k): str(v) for k, v in fields_raw.items()}  # type: ignore[union-attr]
                        seq = int(fields.get("seq", "0"))
                        await relay_commit_published_seq(self._redis, shard=shard, epoch=epoch, seq=seq)
                return
        except Exception:
            logger.debug("_reset_published_seq: XINFO GROUPS branch failed", shard=shard)

        # 分支 3：stream 空或 group 不存在 → 保持原 published_seq

    # ── 发布批次 ─────────────────────────────────────────────────────────────

    async def _publish_batch(self, shard: str, epoch: int) -> bool:
        """XREADGROUP 取一批，逐条 Kafka 发布 + relay_ack + 批量/追平 commit。

        Returns:
            True 表示本轮正常完成；False 表示 epoch 过期需退出 _shard_loop。
        """
        outbox_key = delta_outbox_key(shard)
        delta_topic = settings.heartbeat_delta_topic
        partition = shard_index_from_id(shard)
        batch_size = settings.heartbeat_relay_published_seq_batch_size

        entries: Any = await self._redis.xreadgroup(
            OUTBOX_CONSUMER_GROUP,
            self._consumer_name,
            count=_XREADGROUP_COUNT,
            block=_POLL_BLOCK_MS,
            streams={outbox_key: ">"},
        )
        if not entries:
            await self._maybe_heal_undelivered_lag(shard)
            return True

        self._empty_lag_polls[shard] = 0
        stream_entries = entries[0][1]  # [(entry_id, fields), ...]
        last_published_seq: int | None = None
        published_in_batch = 0

        for entry_id_raw, fields_raw in stream_entries:
            entry_id = str(entry_id_raw)
            fields = {str(k): str(v) for k, v in fields_raw.items()}

            seq = int(fields.get("seq", "0"))
            self._detect_truncation(shard, seq)

            envelope = self._build_envelope(shard, fields)
            value = json.dumps(
                envelope.model_dump(mode="json", by_alias=True, exclude_none=True),
                ensure_ascii=False,
            ).encode()

            # 串行 send_and_wait（in-flight=1，C-RELAY-2）
            if self._producer is not None:
                await self._producer.send_and_wait(delta_topic, value=value, partition=partition)
                self._published_total += 1
                metrics.inc("amp_heartbeat_relay_published_total")  # B-6

            # XACK with epoch fencing（C-RELAY-1）
            ok = await relay_ack(self._redis, shard=shard, epoch=epoch, entry_id=entry_id)
            if not ok:
                return False

            self._last_acked_entry_id[shard] = entry_id
            last_published_seq = seq
            published_in_batch += 1
            self._acked_since_trim[shard] = self._acked_since_trim.get(shard, 0) + 1

            # 批量提交（C-RELAY-3）
            if published_in_batch % batch_size == 0:
                ok = await relay_commit_published_seq(self._redis, shard=shard, epoch=epoch, seq=seq)
                if not ok:
                    return False

        # 追平即推进（C-RELAY-3）
        if last_published_seq is not None and published_in_batch % batch_size != 0:
            ok = await relay_commit_published_seq(self._redis, shard=shard, epoch=epoch, seq=last_published_seq)
            if not ok:
                return False

        # 周期 XTRIM（C-RELAY-1：relay_trim 有 epoch 守卫）
        last_acked = self._last_acked_entry_id.get(shard)
        if last_acked and self._acked_since_trim.get(shard, 0) >= _TRIM_INTERVAL:
            ok = await relay_trim(self._redis, shard=shard, epoch=epoch, min_entry_id=last_acked)
            if not ok:
                return False
            self._acked_since_trim[shard] = 0

        return True

    # ── 自愈（空读 + 积压） ───────────────────────────────────────────────────

    async def _prune_idle_consumers(self, shard: str) -> None:
        """删除同组内长时间空闲的异名 consumer，避免历史 consumer 占用 PEL/游标异常。

        Args:
            shard: 分片 id。
        """
        outbox_key = delta_outbox_key(shard)
        try:
            consumers = await self._redis.xinfo_consumers(outbox_key, OUTBOX_CONSUMER_GROUP)
        except Exception:
            logger.debug("_prune_idle_consumers: XINFO CONSUMERS failed", shard=shard)
            return
        for consumer in consumers:
            name = str(consumer.get("name", ""))
            idle_ms = int(consumer.get("idle", 0) or 0)
            if not name or name == self._consumer_name:
                continue
            if idle_ms < _IDLE_CONSUMER_MS:
                continue
            try:
                await self._redis.xgroup_delconsumer(outbox_key, OUTBOX_CONSUMER_GROUP, name)
                logger.info(
                    "pruned idle amp-hb-relay consumer",
                    shard=shard,
                    consumer=name,
                    idle_ms=idle_ms,
                )
            except Exception:
                logger.debug(
                    "_prune_idle_consumers: XGROUP DELCONSUMER failed",
                    shard=shard,
                    consumer=name,
                )

    async def _maybe_heal_undelivered_lag(self, shard: str) -> None:
        """XREADGROUP 空读时若未投递积压超阈，累计后重置消费组以重新投递。

        验证中曾出现 last-delivered 停滞、delta_seq 前进、Kafka 侧 snapshot 空的
        僵局；DESTROY + CREATE(0) 会重放 outbox，alive-delta 按 seq/version 幂等。

        Args:
            shard: 分片 id。
        """
        lag = await outbox_publish_lag_ms(self._redis, shard)
        max_lag_ms = settings.heartbeat_relay_max_publish_lag_seconds * 1000
        if lag is None or lag <= max_lag_ms:
            self._empty_lag_polls[shard] = 0
            return

        polls = self._empty_lag_polls.get(shard, 0) + 1
        self._empty_lag_polls[shard] = polls
        if polls < _HEAL_EMPTY_POLLS:
            return

        await self._reset_consumer_group(shard)
        self._empty_lag_polls[shard] = 0

    async def _reset_consumer_group(self, shard: str) -> None:
        """DESTROY 后以 id=0 重建 amp-hb-relay，使后续 XREADGROUP '>' 重读未确认积压。

        Args:
            shard: 分片 id。
        """
        outbox_key = delta_outbox_key(shard)
        try:
            await self._redis.xgroup_destroy(outbox_key, OUTBOX_CONSUMER_GROUP)
        except Exception as e:
            logger.debug(
                "_reset_consumer_group: xgroup_destroy failed",
                shard=shard,
                error=str(e),
            )
        await self._redis.xgroup_create(outbox_key, OUTBOX_CONSUMER_GROUP, "0", mkstream=True)
        metrics.inc("amp_heartbeat_relay_group_reset_total")
        logger.warning(
            "reset amp-hb-relay consumer group due to undelivered lag with empty XREADGROUP",
            shard=shard,
        )

    # ── 辅助方法 ──────────────────────────────────────────────────────────────

    def _detect_truncation(self, shard: str, seq: int) -> None:
        """检测 seq 跳跃（outbox 被 XTRIM 截断过），更新 _truncated_total 指标。

        Args:
            shard: 分片 id。
            seq: 当前条目的 seq 数值。
        """
        expected = self._expected_seq.get(shard)
        if expected is not None and seq != expected:
            self._truncated_total += 1
            metrics.inc("amp_heartbeat_relay_truncated_total")  # B-6
            logger.warning(
                "outbox seq gap detected (possible truncation)",
                shard=shard,
                expected=expected,
                actual=seq,
            )
        self._expected_seq[shard] = seq + 1

    def _build_envelope(self, shard: str, fields: dict[str, str]) -> AliveDeltaEnvelope:
        """从 outbox stream 条目字段构造 AliveDeltaEnvelope。

        §3.6 第 2 条：id=alive_object_id(aic)、version=seq、type=ALIVE_DELTA_TYPE、
        ms → ISO 格式化 payload.lastSeenAt / sourceTimestamp；
        leave_alive(op=delete) 省略 payload；reason 不进信封（§4.2 / 9-11）。

        Args:
            shard: 分片 id（写入 envelope.shard）。
            fields: Redis Stream 条目中的 dict[str, str]。

        Returns:
            构造好的 AliveDeltaEnvelope。
        """
        aic = fields["aic"]
        seq = fields["seq"]
        op: DeltaOp = fields["op"]  # type: ignore[assignment]
        kind: DeltaKind = fields["kind"]  # type: ignore[assignment]

        payload: AliveSetEntry | None = None
        if op == "upsert":
            last_seen_at_iso = _ms_to_iso(int(fields["last_seen_at_ms"]))
            source_ts: str | None = None
            if "source_timestamp_ms" in fields:
                source_ts = _ms_to_iso(int(fields["source_timestamp_ms"]))
            payload = AliveSetEntry(
                aic=aic,
                last_seen_at=last_seen_at_iso,
                source_timestamp=source_ts,
            )

        return AliveDeltaEnvelope(
            shard=shard,
            seq=seq,
            type=ALIVE_DELTA_TYPE,
            id=alive_object_id(aic),
            version=seq,
            op=op,
            kind=kind,
            payload=payload,
        )

    # ── 指标 ──────────────────────────────────────────────────────────────────

    async def publish_lag_seconds(self) -> float:
        """返回所有 shard 中最大的发布延迟（秒）。

        无积压时返回 0.0。基于 outbox_publish_lag_ms（覆盖 PEL + 未投递两种积压，P1-2）。

        Returns:
            最大延迟秒数（float），无积压时为 0.0。
        """
        max_lag_ms = 0
        for shard in all_shard_ids():
            lag = await outbox_publish_lag_ms(self._redis, shard)
            if lag is not None and lag > max_lag_ms:
                max_lag_ms = lag
        return max_lag_ms / 1000.0
