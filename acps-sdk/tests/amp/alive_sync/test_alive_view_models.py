"""tests: AliveView 模型与 to_output_dict()。"""
import pytest

from acps_sdk.amp.alive_sync.models import AliveView


class TestAliveView:
    def test_to_output_dict_alive(self) -> None:
        view = AliveView(aic="AIC-001", alive=True, last_seen_at="2026-06-13T01:20:00Z")
        d = view.to_output_dict()
        assert d["alive"] is True
        assert d["aliveLastSeenAt"] == "2026-06-13T01:20:00Z"

    def test_to_output_dict_not_alive(self) -> None:
        view = AliveView(aic="AIC-001", alive=False, last_seen_at="2026-06-13T01:00:00Z")
        d = view.to_output_dict()
        assert d["alive"] is False

    def test_to_output_dict_last_seen_at_none_key_present(self) -> None:
        """last_seen_at=None 时，键 aliveLastSeenAt 仍存在且值为 None（区别于键缺失）。"""
        view = AliveView(aic="AIC-001", alive=True, last_seen_at=None)
        d = view.to_output_dict()
        assert "aliveLastSeenAt" in d
        assert d["aliveLastSeenAt"] is None

    def test_frozen(self) -> None:
        view = AliveView(aic="AIC-001", alive=True, last_seen_at=None)
        with pytest.raises((AttributeError, TypeError)):
            view.alive = False  # type: ignore[misc]
