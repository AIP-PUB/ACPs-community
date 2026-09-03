"""tests/unit/test_audit_service.py — Audit Query Planner 逻辑单元测试（mock DB）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.audit.exception import AuditTaskNotFoundError, InvalidTimeRangeError, UnsupportedFieldError
from app.audit.service import _committed_at_fence, _decode_cursor, _encode_cursor, _require_time_range
from app.core.amp_api_schema import AMPTimeRange


class TestRequireTimeRange:
    def test_valid_time_range_returns_itself(self) -> None:
        tr = AMPTimeRange(start_at="2026-01-01T00:00:00Z", end_at="2026-02-01T00:00:00Z")
        result = _require_time_range(tr)
        assert result is tr

    def test_none_raises_invalid_time_range(self) -> None:
        with pytest.raises(InvalidTimeRangeError):
            _require_time_range(None)

    def test_invalid_iso_format_raises(self) -> None:
        tr = AMPTimeRange(start_at="not-a-date", end_at="2026-02-01T00:00:00Z")
        with pytest.raises(InvalidTimeRangeError, match="非法"):
            _require_time_range(tr)


class TestCursorEncoding:
    def test_encode_decode_roundtrip(self) -> None:
        ts = "2026-06-01T12:00:00+00:00"
        aid = "aud-001"
        cursor = _encode_cursor(ts, aid)
        decoded_ts, decoded_aid = _decode_cursor(cursor)
        assert decoded_ts == ts
        assert decoded_aid == aid

    def test_cursor_is_url_safe_base64(self) -> None:
        cursor = _encode_cursor("2026-01-01T00:00:00Z", "aud-abc")
        assert "+" not in cursor or cursor.count("+") == 0

    def test_invalid_cursor_raises_cursor_invalid(self) -> None:
        from app.audit.exception import CursorInvalidError

        with pytest.raises(CursorInvalidError):
            _decode_cursor("!!!invalid!!!")


class TestFieldWhitelist:
    def test_supported_filter_field_in_field_map(self) -> None:
        from app.audit.service import _FIELD_MAP

        assert "auditId" in _FIELD_MAP
        assert "body.actor.id" in _FIELD_MAP
        assert "integrity.signatureVerified" in _FIELD_MAP

    def test_unsupported_field_raises(self) -> None:
        from app.audit.service import _validate_filter_fields

        with pytest.raises(UnsupportedFieldError):
            _validate_filter_fields(["body.raw.unsupported"])

    def test_aggregate_group_by_whitelist_not_empty(self) -> None:
        from app.audit.schema import AuditAggregateRequest

        req = AuditAggregateRequest(
            time_range=AMPTimeRange(start_at="2026-01-01T00:00:00Z", end_at="2026-02-01T00:00:00Z"),
            group_by=["body.actor.id"],
        )
        assert len(req.valid_group_by_fields) > 0

    def test_unsupported_group_by_detected(self) -> None:
        from app.audit.schema import AuditAggregateRequest

        req = AuditAggregateRequest(
            time_range=AMPTimeRange(start_at="2026-01-01T00:00:00Z", end_at="2026-02-01T00:00:00Z"),
            group_by=["raw_log.secret"],
        )
        assert "raw_log.secret" not in req.valid_group_by_fields

    def test_keyword_columns_include_actor_id(self) -> None:
        from app.audit.service import _KEYWORD_COLUMNS

        assert "actor_id" in _KEYWORD_COLUMNS
        assert "action_name" in _KEYWORD_COLUMNS

    def test_field_map_maps_api_to_db_column(self) -> None:
        from app.audit.service import _FIELD_MAP

        assert _FIELD_MAP["body.actor.id"] == "actor_id"
        assert _FIELD_MAP["body.result.status"] == "result_status"


class TestCommittedAtFence:
    """§5.3 committed_at 围栏谓词测试。"""

    def test_fence_expands_by_max_event_lag(self) -> None:
        """围栏应在 timeRange 两端各扩展 max_event_lag_hours。"""
        start = datetime(2026, 6, 1, tzinfo=UTC)
        end = datetime(2026, 6, 30, tzinfo=UTC)
        lag_hours = 48

        with patch("app.audit.service.settings") as mock_settings:
            mock_settings.audit_max_event_lag_hours = lag_hours
            s_clause, e_clause, fence_start, fence_end = _committed_at_fence(start, end)

        assert fence_start == start - timedelta(hours=lag_hours)
        assert fence_end == end + timedelta(hours=lag_hours)
        assert "fence_start" in s_clause
        assert "fence_end" in e_clause

    def test_fence_clauses_reference_committed_at(self) -> None:
        start = datetime(2026, 6, 1, tzinfo=UTC)
        end = datetime(2026, 6, 30, tzinfo=UTC)
        with patch("app.audit.service.settings") as mock_settings:
            mock_settings.audit_max_event_lag_hours = 48
            s_clause, e_clause, _, _ = _committed_at_fence(start, end)
        assert "committed_at" in s_clause
        assert "committed_at" in e_clause

    def test_fence_zero_lag_returns_exact_range(self) -> None:
        """极端情况：lag=0 时围栏恰好等于 timeRange。"""
        start = datetime(2026, 6, 1, tzinfo=UTC)
        end = datetime(2026, 6, 30, tzinfo=UTC)
        with patch("app.audit.service.settings") as mock_settings:
            mock_settings.audit_max_event_lag_hours = 0
            _, _, fence_start, fence_end = _committed_at_fence(start, end)
        assert fence_start == start
        assert fence_end == end


class TestGetExportTaskKindFilter:
    """§4.8 §6.7 — get_export_task 只暴露 public 任务。"""

    @pytest.mark.asyncio
    async def test_internal_task_raises_not_found(self) -> None:
        """kind='internal' 的归档任务应按 404 处理，不向外暴露。"""
        import uuid

        from app.audit.service import get_export_task

        internal_task = MagicMock()
        internal_task.kind = "internal"
        internal_task.task_id = uuid.uuid4()

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=internal_task)

        with pytest.raises(AuditTaskNotFoundError):
            await get_export_task(mock_session, str(internal_task.task_id))

    @pytest.mark.asyncio
    async def test_public_task_returns_view(self) -> None:
        """kind='public' 的任务正常返回。"""
        import uuid

        from app.audit.service import get_export_task

        task_id = uuid.uuid4()
        now = datetime.now(tz=UTC)
        public_task = MagicMock()
        public_task.kind = "public"
        public_task.task_id = task_id
        public_task.status = "pending"
        public_task.created_at = now
        public_task.finished_at = None
        public_task.record_count = None
        public_task.artifact_sha256 = None
        public_task.manifest_hash = None
        public_task.error = None

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=public_task)

        view = await get_export_task(mock_session, str(task_id))
        assert view.task_id == str(task_id)
        assert view.status == "pending"

    @pytest.mark.asyncio
    async def test_missing_task_raises_not_found(self) -> None:
        import uuid

        from app.audit.service import get_export_task

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=None)

        with pytest.raises(AuditTaskNotFoundError):
            await get_export_task(mock_session, str(uuid.uuid4()))


class TestSubmitExportSetsKindPublic:
    """§4.8 — submit_export 创建的任务 kind 必须为 'public'。"""

    @pytest.mark.asyncio
    async def test_created_task_has_kind_public(self) -> None:
        from app.audit.schema import AuditExportRequest
        from app.audit.service import submit_export

        request = AuditExportRequest(
            time_range=AMPTimeRange(start_at="2026-01-01T00:00:00Z", end_at="2026-02-01T00:00:00Z"),
            format="ndjson",
            include_raw=False,
        )

        added_tasks: list[MagicMock] = []
        mock_session = AsyncMock()
        mock_session.add = MagicMock(side_effect=added_tasks.append)
        mock_session.commit = AsyncMock()

        await submit_export(mock_session, request)

        assert len(added_tasks) == 1
        assert added_tasks[0].kind == "public"
