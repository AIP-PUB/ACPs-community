"""acps_sdk.amp.alive_sync — AMP Heartbeat Alive-Delta Consumer 引擎（存储/传输无关）。"""

from acps_sdk.amp.alive_sync.bootstrap import next_lookback_seconds, seek_timestamp_ms
from acps_sdk.amp.alive_sync.engine import (
    DeltaDecision,
    AliveSyncEngine,
    classify_delta,
    is_gap,
    passes_seq_gate,
    passes_version,
)
from acps_sdk.amp.alive_sync.errors import (
    AliveSyncError,
    GapDetectedError,
    ResyncRequired,
    SnapshotProtocolError,
)
from acps_sdk.amp.alive_sync.models import AliveView
from acps_sdk.amp.alive_sync.snapshot import parse_meta_line, parse_snapshot_row, snapshot_row_to_record
from acps_sdk.amp.alive_sync.store import AliveReader, AliveRecord, AliveSyncStore, ShardCheckpoint

__all__ = [
    "AliveSyncEngine",
    "AliveSyncStore",
    "AliveReader",
    "AliveRecord",
    "ShardCheckpoint",
    "AliveView",
    "DeltaDecision",
    "classify_delta",
    "passes_seq_gate",
    "is_gap",
    "passes_version",
    "parse_meta_line",
    "parse_snapshot_row",
    "snapshot_row_to_record",
    "seek_timestamp_ms",
    "next_lookback_seconds",
    "AliveSyncError",
    "GapDetectedError",
    "ResyncRequired",
    "SnapshotProtocolError",
]
