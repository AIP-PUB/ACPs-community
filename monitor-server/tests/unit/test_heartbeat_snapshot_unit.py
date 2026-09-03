"""Unit tests for app/heartbeat/snapshot.py — 全部 mock store layer（TDD red phase）。

运行：just test unit -k heartbeat_snapshot_unit
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_snapshot_row(
    aic: str,
    last_delta_seq: int | None = 1,
    alive_membership_state: str = "alive",
    last_seen_at: str = "2025-01-01T00:00:00+00:00",
    source_timestamp_ms: int | None = None,
) -> MagicMock:
    row = MagicMock()
    row.aic = aic
    row.last_delta_seq = last_delta_seq
    row.alive_membership_state = alive_membership_state
    row.last_seen_at = last_seen_at
    row.source_timestamp_ms = source_timestamp_ms
    return row


def _reset_singleton() -> None:
    import app.heartbeat.snapshot as snap_mod

    snap_mod._exporter_instance = None


# ── Singleton ─────────────────────────────────────────────────────────────────


class TestGetSnapshotExporter:
    def test_returns_same_instance_on_repeated_calls(self) -> None:
        """P1-3: 多次调用 get_snapshot_exporter() 返回同一对象。"""
        from app.heartbeat.snapshot import SnapshotExporter, get_snapshot_exporter

        _reset_singleton()
        e1 = get_snapshot_exporter()
        e2 = get_snapshot_exporter()
        assert e1 is e2
        assert isinstance(e1, SnapshotExporter)

    def test_singleton_survives_multiple_imports(self) -> None:
        """重新 import 模块后仍返回同一对象（模块缓存保障）。"""
        from app.heartbeat.snapshot import get_snapshot_exporter

        _reset_singleton()
        e1 = get_snapshot_exporter()
        import importlib

        import app.heartbeat.snapshot as snap_mod

        importlib.reload(snap_mod)
        snap_mod._exporter_instance = e1
        e2 = snap_mod.get_snapshot_exporter()
        assert e2 is e1


# ── _enumerate_shard_alive ────────────────────────────────────────────────────


class TestEnumerateShardAlive:
    @pytest.fixture
    def exporter(self) -> object:
        from app.heartbeat.snapshot import SnapshotExporter

        return SnapshotExporter()

    async def test_empty_shard_returns_empty_list(self, exporter: object) -> None:
        """空分片返回空列表，且不调用 mget_snapshot_fields。"""
        mock_redis = AsyncMock()
        with (
            patch(
                "app.heartbeat.snapshot.store.zrange_by_score",
                AsyncMock(return_value=[]),
            ),
        ):
            result = await exporter._enumerate_shard_alive(  # type: ignore[attr-defined]
                mock_redis, "hb-000", now_ms=10_000, cutover_seq=0
            )
        assert result == []

    async def test_normal_termination_when_chunk_smaller_than_size(self, exporter: object) -> None:
        """chunk < chunk_size → 判定 is_last_page，加 tie-safe 后停止。"""
        mock_redis = AsyncMock()
        entries = [("aic-001", 1000), ("aic-002", 2000)]
        rows = [
            _make_snapshot_row("aic-001", last_delta_seq=1),
            _make_snapshot_row("aic-002", last_delta_seq=2),
        ]

        with (
            patch(
                "app.heartbeat.snapshot.store.zrange_by_score",
                AsyncMock(return_value=entries),
            ),
            patch(
                "app.heartbeat.snapshot.store.zrange_score_group",
                AsyncMock(return_value=entries),
            ),
            patch(
                "app.heartbeat.snapshot.store.mget_snapshot_fields",
                AsyncMock(return_value=rows),
            ),
        ):
            result = await exporter._enumerate_shard_alive(  # type: ignore[attr-defined]
                mock_redis, "hb-000", now_ms=100_000, cutover_seq=0
            )

        assert len(result) == 2
        aics_in_result = {e.payload.aic for e in result}
        assert aics_in_result == {"aic-001", "aic-002"}

    async def test_skips_left_alive_entries(self, exporter: object) -> None:
        """8-5: alive_membership_state=left_alive 的条目被跳过（弱快照不变式）。"""
        mock_redis = AsyncMock()
        entries = [("aic-alive", 1000), ("aic-left", 2000)]
        rows = [
            _make_snapshot_row("aic-alive", alive_membership_state="alive"),
            _make_snapshot_row("aic-left", alive_membership_state="left_alive"),
        ]

        with (
            patch(
                "app.heartbeat.snapshot.store.zrange_by_score",
                AsyncMock(return_value=entries),
            ),
            patch(
                "app.heartbeat.snapshot.store.zrange_score_group",
                AsyncMock(return_value=entries),
            ),
            patch(
                "app.heartbeat.snapshot.store.mget_snapshot_fields",
                AsyncMock(return_value=rows),
            ),
        ):
            result = await exporter._enumerate_shard_alive(  # type: ignore[attr-defined]
                mock_redis, "hb-000", now_ms=100_000, cutover_seq=0
            )

        ids = [e.id for e in result]
        assert all("aic-alive" in i for i in ids)
        assert not any("aic-left" in i for i in ids)

    async def test_skips_nil_row(self, exporter: object) -> None:
        """mget_snapshot_fields 返回 None 的行被跳过。"""
        mock_redis = AsyncMock()
        entries = [("aic-001", 1000), ("aic-missing", 2000)]
        rows: list = [
            _make_snapshot_row("aic-001", alive_membership_state="alive"),
            None,
        ]

        with (
            patch(
                "app.heartbeat.snapshot.store.zrange_by_score",
                AsyncMock(return_value=entries),
            ),
            patch(
                "app.heartbeat.snapshot.store.zrange_score_group",
                AsyncMock(return_value=entries),
            ),
            patch(
                "app.heartbeat.snapshot.store.mget_snapshot_fields",
                AsyncMock(return_value=rows),
            ),
        ):
            result = await exporter._enumerate_shard_alive(  # type: ignore[attr-defined]
                mock_redis, "hb-000", now_ms=100_000, cutover_seq=0
            )

        assert len(result) == 1
        assert "aic-001" in result[0].id

    async def test_tie_safe_adds_extra_entries_in_last_page(self, exporter: object) -> None:
        """tie-safe：末尾同 score 的额外条目被并入（C-SYNC-2）。"""
        mock_redis = AsyncMock()
        # chunk 返回 2 条（< chunk_size），最后 score=2000 还有额外条目
        chunk_entries = [("aic-001", 1000), ("aic-002", 2000)]
        tie_entries = [("aic-002", 2000), ("aic-003", 2000)]  # aic-003 is extra
        all_rows = [
            _make_snapshot_row("aic-001"),
            _make_snapshot_row("aic-002"),
            _make_snapshot_row("aic-003"),
        ]

        with (
            patch(
                "app.heartbeat.snapshot.store.zrange_by_score",
                AsyncMock(return_value=chunk_entries),
            ),
            patch(
                "app.heartbeat.snapshot.store.zrange_score_group",
                AsyncMock(return_value=tie_entries),
            ),
            patch(
                "app.heartbeat.snapshot.store.mget_snapshot_fields",
                AsyncMock(return_value=all_rows),
            ),
        ):
            result = await exporter._enumerate_shard_alive(  # type: ignore[attr-defined]
                mock_redis, "hb-000", now_ms=100_000, cutover_seq=0
            )

        aics = {e.payload.aic for e in result}
        assert "aic-003" in aics  # tie-safe extra entry is included

    async def test_version_equals_last_delta_seq(self, exporter: object) -> None:
        """envelope.version 与 last_delta_seq 一致（集成测试 8 的前置验证）。"""
        mock_redis = AsyncMock()
        entries = [("aic-001", 1000)]
        rows = [_make_snapshot_row("aic-001", last_delta_seq=42)]

        with (
            patch(
                "app.heartbeat.snapshot.store.zrange_by_score",
                AsyncMock(return_value=entries),
            ),
            patch(
                "app.heartbeat.snapshot.store.zrange_score_group",
                AsyncMock(return_value=entries),
            ),
            patch(
                "app.heartbeat.snapshot.store.mget_snapshot_fields",
                AsyncMock(return_value=rows),
            ),
        ):
            result = await exporter._enumerate_shard_alive(  # type: ignore[attr-defined]
                mock_redis, "hb-000", now_ms=100_000, cutover_seq=0
            )

        assert result[0].version == "42"
        assert result[0].seq == "42"

    async def test_uses_cutover_seq_when_last_delta_seq_is_none(self, exporter: object) -> None:
        """last_delta_seq=None 时 seq 退回 cutover_seq（0）。"""
        mock_redis = AsyncMock()
        entries = [("aic-001", 1000)]
        rows = [_make_snapshot_row("aic-001", last_delta_seq=None)]

        with (
            patch(
                "app.heartbeat.snapshot.store.zrange_by_score",
                AsyncMock(return_value=entries),
            ),
            patch(
                "app.heartbeat.snapshot.store.zrange_score_group",
                AsyncMock(return_value=entries),
            ),
            patch(
                "app.heartbeat.snapshot.store.mget_snapshot_fields",
                AsyncMock(return_value=rows),
            ),
        ):
            result = await exporter._enumerate_shard_alive(  # type: ignore[attr-defined]
                mock_redis, "hb-000", now_ms=100_000, cutover_seq=7
            )

        assert result[0].seq == "7"

    async def test_timeout_truncates_enumeration(self, exporter: object) -> None:
        """超时后截断枚举（不无限循环）。"""
        from app.core.config import settings

        mock_redis = AsyncMock()
        chunk_size = settings.heartbeat_snapshot_chunk_size
        big_chunk = [(f"aic-{i}", i * 1000) for i in range(chunk_size)]
        big_rows = [_make_snapshot_row(f"aic-{i}") for i in range(chunk_size)]

        call_count = 0

        async def fake_zrange_by_score(*args: object, **kwargs: object) -> list:
            nonlocal call_count
            call_count += 1
            return big_chunk

        async def fake_zrange_score_group(*args: object, **kwargs: object) -> list:
            return big_chunk

        async def fake_mget(*args: object, **kwargs: object) -> list:
            return big_rows

        # First call: t=0.0, second loop iteration: t=999.0 → timeout
        with (
            patch("app.heartbeat.snapshot.store.zrange_by_score", side_effect=fake_zrange_by_score),
            patch("app.heartbeat.snapshot.store.zrange_score_group", side_effect=fake_zrange_score_group),
            patch("app.heartbeat.snapshot.store.mget_snapshot_fields", side_effect=fake_mget),
            patch("app.heartbeat.snapshot.time.monotonic", side_effect=[0.0, 0.0, 9999.0]),
        ):
            result = await exporter._enumerate_shard_alive(  # type: ignore[attr-defined]
                mock_redis, "hb-000", now_ms=100_000, cutover_seq=0
            )

        # Should have truncated after first full page (timeout on second iteration)
        assert call_count == 1
        assert len(result) == chunk_size

    async def test_envelope_fields(self, exporter: object) -> None:
        """envelope 字段：type, op, kind, id 格式正确。"""
        mock_redis = AsyncMock()
        entries = [("my-aic-007", 1000)]
        rows = [_make_snapshot_row("my-aic-007", last_delta_seq=5)]

        with (
            patch(
                "app.heartbeat.snapshot.store.zrange_by_score",
                AsyncMock(return_value=entries),
            ),
            patch(
                "app.heartbeat.snapshot.store.zrange_score_group",
                AsyncMock(return_value=entries),
            ),
            patch(
                "app.heartbeat.snapshot.store.mget_snapshot_fields",
                AsyncMock(return_value=rows),
            ),
        ):
            result = await exporter._enumerate_shard_alive(  # type: ignore[attr-defined]
                mock_redis, "hb-000", now_ms=100_000, cutover_seq=0
            )

        env = result[0]
        assert env.type == "amp-alive-delta"
        assert env.op == "upsert"
        assert env.kind == "snapshot"
        assert env.id == "urn:amp:alive:my-aic-007"
        assert env.payload is not None


# ── _get_or_materialize ───────────────────────────────────────────────────────


class TestGetOrMaterialize:
    @pytest.fixture
    def exporter(self) -> object:
        from app.heartbeat.snapshot import SnapshotExporter

        return SnapshotExporter()

    async def test_first_call_materializes_and_caches(self, exporter: object) -> None:
        """首次调用：生成快照并缓存。"""
        mock_redis = AsyncMock()

        with (
            patch("app.heartbeat.snapshot.store.redis_now_ms", AsyncMock(return_value=50_000)),
            patch("app.heartbeat.snapshot.store.read_all_published_seq", AsyncMock(return_value={"hb-000": 5})),
            patch.object(
                exporter,
                "_enumerate_shard_alive",
                AsyncMock(return_value=[]),
            ),
        ):
            snap = await exporter._get_or_materialize(mock_redis)  # type: ignore[attr-defined]

        assert snap is not None
        assert exporter._cached is snap  # type: ignore[attr-defined]

    async def test_second_call_within_share_window_returns_cache(self, exporter: object) -> None:
        """share window 内的第二次调用直接返回缓存（不重新枚举）。

        testing.toml 将 snapshot_share_window_seconds 设为 0（确保 E2E 测试无缓存干扰），
        因此这里用 monkeypatch 临时覆盖为 5s，以触发缓存命中路径。
        """
        from acps_sdk.amp.heartbeat_sync import AliveSnapshotMeta

        from app.core.config import settings
        from app.heartbeat.snapshot import MaterializedSnapshot

        mock_redis = AsyncMock()
        now_ms = 100_000

        meta = AliveSnapshotMeta(
            record_type="snapshot-meta",
            type="amp-alive-delta",
            cutover_seq_by_shard={},
            generated_at="2025-01-01T00:00:00+00:00",
        )
        cached = MaterializedSnapshot(
            meta=meta,
            lines=[b'{"recordType":"snapshot-meta"}\n'],
            materialized_at_ms=now_ms - 100,  # 100ms ago — within share window (5000ms)
        )
        exporter._cached = cached  # type: ignore[attr-defined]

        enumerate_mock = AsyncMock(return_value=[])

        with (
            patch("app.heartbeat.snapshot.store.redis_now_ms", AsyncMock(return_value=now_ms)),
            patch.object(exporter, "_enumerate_shard_alive", enumerate_mock),
            # testing.toml 设为 0；覆盖为 5 触发缓存命中路径
            patch.object(
                type(settings), "heartbeat_snapshot_share_window_seconds", new_callable=lambda: property(lambda self: 5)
            ),
        ):
            snap = await exporter._get_or_materialize(mock_redis)  # type: ignore[attr-defined]

        assert snap is cached
        enumerate_mock.assert_not_called()

    async def test_stale_cache_triggers_rematerialization(self, exporter: object) -> None:
        """过期缓存：超过 share window 触发重新枚举。"""
        from acps_sdk.amp.heartbeat_sync import AliveSnapshotMeta

        from app.heartbeat.snapshot import MaterializedSnapshot

        mock_redis = AsyncMock()
        now_ms = 100_000

        meta = AliveSnapshotMeta(
            record_type="snapshot-meta",
            type="amp-alive-delta",
            cutover_seq_by_shard={},
            generated_at="2025-01-01T00:00:00+00:00",
        )
        old_snap = MaterializedSnapshot(
            meta=meta,
            lines=[b"old\n"],
            materialized_at_ms=0,  # very old
        )
        exporter._cached = old_snap  # type: ignore[attr-defined]

        enumerate_mock = AsyncMock(return_value=[])

        with (
            patch("app.heartbeat.snapshot.store.redis_now_ms", AsyncMock(return_value=now_ms)),
            patch("app.heartbeat.snapshot.store.read_all_published_seq", AsyncMock(return_value={})),
            patch.object(exporter, "_enumerate_shard_alive", enumerate_mock),
        ):
            snap = await exporter._get_or_materialize(mock_redis)  # type: ignore[attr-defined]

        assert snap is not old_snap
        enumerate_mock.assert_called()

    async def test_cutover_happens_before_enumeration(self, exporter: object) -> None:
        """8-6: read_all_published_seq（cutover）先于 _enumerate_shard_alive（C-SYNC-3）。"""
        mock_redis = AsyncMock()
        call_order: list[str] = []

        async def fake_read_published_seq(*a: object, **kw: object) -> dict:
            call_order.append("cutover")
            return {}

        async def fake_enumerate(*a: object, **kw: object) -> list:
            call_order.append("enumerate")
            return []

        with (
            patch("app.heartbeat.snapshot.store.redis_now_ms", AsyncMock(return_value=50_000)),
            patch(
                "app.heartbeat.snapshot.store.read_all_published_seq",
                side_effect=fake_read_published_seq,
            ),
            patch.object(exporter, "_enumerate_shard_alive", side_effect=fake_enumerate),
        ):
            await exporter._get_or_materialize(mock_redis)  # type: ignore[attr-defined]

        assert call_order.index("cutover") < call_order.index("enumerate")

    async def test_ndjson_first_line_is_snapshot_meta(self, exporter: object) -> None:
        """8-9: 生成的 NDJSON 首行包含 recordType=snapshot-meta。"""
        mock_redis = AsyncMock()

        with (
            patch("app.heartbeat.snapshot.store.redis_now_ms", AsyncMock(return_value=50_000)),
            patch("app.heartbeat.snapshot.store.read_all_published_seq", AsyncMock(return_value={})),
            patch.object(exporter, "_enumerate_shard_alive", AsyncMock(return_value=[])),
        ):
            snap = await exporter._get_or_materialize(mock_redis)  # type: ignore[attr-defined]

        import json

        first_line = json.loads(snap.lines[0])
        assert first_line["recordType"] == "snapshot-meta"
        assert first_line["type"] == "amp-alive-delta"
