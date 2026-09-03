"""tests/unit/test_generic_runner_message.py — GenericRunner Message 发射单元测试（EM5）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from acps_sdk.aip.aip_group_partner import GroupPartnerMqClient

from partners.generic_runner import GenericRunner
from partners.group_handler import GroupHandler


def test_different_agent_names_have_different_message_files(mock_generic_runner: GenericRunner) -> None:
    base_dir = mock_generic_runner.base_dir
    with patch("partners.generic_runner.AsyncOpenAI"):
        runner_a = GenericRunner("agent_alpha", base_dir)
        runner_b = GenericRunner("agent_beta", base_dir)

    assert runner_a._message_emitter._log_file != runner_b._message_emitter._log_file
    assert "agent_alpha" in runner_a._message_emitter._log_file.name
    assert "agent_beta" in runner_b._message_emitter._log_file.name


def test_create_group_client_injects_message_emitter(mock_generic_runner: GenericRunner) -> None:
    handler = GroupHandler(
        agent_name=mock_generic_runner.agent_name,
        runner=mock_generic_runner,
        rabbitmq_config={"host": "localhost", "port": 5671, "vhost": "acps"},
        identity_binding_enabled=False,
    )

    with patch.object(GroupPartnerMqClient, "__init__", return_value=None) as mock_init:
        handler._create_group_client(use_shared_connection=False)
        kwargs = mock_init.call_args.kwargs
        assert kwargs["message_emitter"] is mock_generic_runner._message_emitter
        assert kwargs["message_system"] == "rabbitmq"
        assert kwargs["identity_binding_enabled"] is False
        assert "trace_context_provider" not in kwargs


@pytest.mark.asyncio
async def test_inbox_client_does_not_inject_message_emitter(mock_generic_runner: GenericRunner) -> None:
    from unittest.mock import AsyncMock

    handler = GroupHandler(
        agent_name=mock_generic_runner.agent_name,
        runner=mock_generic_runner,
        rabbitmq_config={"host": "localhost", "port": 5671, "vhost": "acps"},
        identity_binding_enabled=False,
    )

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.start_inbox_consuming = AsyncMock()

    with patch("partners.group_handler.GroupPartnerMqClient", return_value=mock_client) as mock_cls:
        started = await handler._start_shared_inbox_consumer()

    assert started is True
    kwargs = mock_cls.call_args.kwargs
    assert "message_emitter" not in kwargs
    assert kwargs["identity_binding_enabled"] is False
