"""tests: AgentAliveStatus / AliveSyncShardState SQLModel 字段校验（Step 7）。"""

from app.heartbeat_sync.model import AgentAliveStatus, AliveSyncShardState


class TestAgentAliveStatus:
    def test_table_name(self) -> None:
        assert AgentAliveStatus.__tablename__ == "agent_alive_status"

    def test_create_instance_defaults(self) -> None:
        row = AgentAliveStatus(aic="AIC-001", alive=True, version=10, shard="hb-000")
        assert row.aic == "AIC-001"
        assert row.alive is True
        assert row.version == 10
        assert row.shard == "hb-000"
        assert row.last_seen_at is None
        assert row.id is None  # DB 自增

    def test_last_seen_at_optional(self) -> None:
        row = AgentAliveStatus(aic="AIC-002", alive=False, version=5, shard="hb-000")
        assert row.last_seen_at is None

    def test_last_seen_at_set(self) -> None:
        row = AgentAliveStatus(
            aic="AIC-003",
            alive=True,
            version=7,
            shard="hb-000",
            last_seen_at="2026-06-13T01:20:00Z",
        )
        assert row.last_seen_at == "2026-06-13T01:20:00Z"


class TestAliveSyncShardState:
    def test_table_name(self) -> None:
        assert AliveSyncShardState.__tablename__ == "alive_sync_shard_state"

    def test_create_instance_defaults(self) -> None:
        row = AliveSyncShardState(shard="hb-000", last_seen_seq=42, cutover_seq=40)
        assert row.shard == "hb-000"
        assert row.last_seen_seq == 42
        assert row.cutover_seq == 40
        assert row.kafka_next_offset is None
        assert row.snapshot_generated_at is None

    def test_full_fields(self) -> None:
        row = AliveSyncShardState(
            shard="hb-001",
            last_seen_seq=100,
            cutover_seq=90,
            kafka_next_offset=55,
            snapshot_generated_at="2026-06-13T00:00:00Z",
        )
        assert row.kafka_next_offset == 55
        assert row.snapshot_generated_at == "2026-06-13T00:00:00Z"
