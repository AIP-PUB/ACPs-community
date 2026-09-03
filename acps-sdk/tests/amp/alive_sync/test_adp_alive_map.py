"""tests: DiscoveryResult.alive_map 新字段 + _alive_enrichable PrivateAttr（Step 5）。"""
import pytest

from acps_sdk.adp.models import DiscoveryResult


class TestDiscoveryResultAliveMap:
    def test_alive_map_absent_by_default(self) -> None:
        result = DiscoveryResult()
        assert result.alive_map is None

    def test_alive_map_excluded_none_in_serialization(self) -> None:
        """未启用时 aliveMap 不出现在输出中（exclude_none=True），对既有调用方零破坏。"""
        result = DiscoveryResult()
        d = result.model_dump(by_alias=True, exclude_none=True)
        assert "aliveMap" not in d

    def test_alive_map_present_when_set(self) -> None:
        result = DiscoveryResult()
        result.alive_map = {"AIC-001": {"alive": True, "aliveLastSeenAt": "2026-06-13T01:20:00Z"}}
        d = result.model_dump(by_alias=True, exclude_none=True)
        assert "aliveMap" in d
        assert d["aliveMap"]["AIC-001"]["alive"] is True

    def test_alive_last_seen_at_null_key_present(self) -> None:
        """aliveLastSeenAt 为 null 时键仍存在（区别于键缺失=未知 AIC）。"""
        result = DiscoveryResult()
        result.alive_map = {"AIC-001": {"alive": True, "aliveLastSeenAt": None}}
        # 用 include 而非 exclude_none 以包含 None 值
        d = result.model_dump(by_alias=True)
        assert "aliveMap" in d
        assert "aliveLastSeenAt" in d["aliveMap"]["AIC-001"]
        assert d["aliveMap"]["AIC-001"]["aliveLastSeenAt"] is None

    def test_alive_enrichable_defaults_false(self) -> None:
        result = DiscoveryResult()
        assert result._alive_enrichable is False  # type: ignore[attr-defined]

    def test_alive_enrichable_can_be_set(self) -> None:
        result = DiscoveryResult()
        result._alive_enrichable = True  # type: ignore[attr-defined]
        assert result._alive_enrichable is True  # type: ignore[attr-defined]

    def test_alive_enrichable_not_in_serialization(self) -> None:
        """_alive_enrichable 不进入 model_dump / JSON（线缆零影响）。"""
        result = DiscoveryResult()
        result._alive_enrichable = True  # type: ignore[attr-defined]
        d = result.model_dump(by_alias=True)
        assert "_alive_enrichable" not in d
        assert "aliveEnrichable" not in d


class TestAmpAliveExports:
    def test_alive_sync_engine_importable(self) -> None:
        from acps_sdk.amp import AliveSyncEngine
        assert AliveSyncEngine is not None

    def test_delta_decision_importable(self) -> None:
        from acps_sdk.amp import DeltaDecision
        assert DeltaDecision.APPLY_UPSERT is not None

    def test_alive_reader_importable(self) -> None:
        from acps_sdk.amp import AliveReader
        assert AliveReader is not None
