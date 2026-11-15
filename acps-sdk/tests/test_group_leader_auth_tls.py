"""群组 ACL 服务可以单独配一份 TLS（auth_ssl_context），不传时沿用连 broker 的 ssl_context。"""
from __future__ import annotations

import ssl

import pytest

import acps_sdk.aip.aip_group_leader as leader_mod
from acps_sdk.aip.aip_group_leader import GroupLeader

MQ = {"host": "mq.example.com", "port": 5671, "vhost": "acps"}


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []

    class _Acl:
        def __init__(self, **kwargs) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(leader_mod, "GroupAclClient", _Acl)
    return calls


def _ctx() -> ssl.SSLContext:
    return ssl.create_default_context()


def test_auth_ssl_context_is_used_for_acl_client(captured) -> None:
    broker_ctx, auth_ctx = _ctx(), _ctx()
    leader = GroupLeader(
        "leader-aic",
        {**MQ, "auth_service_url": "https://10.0.0.1:9007"},
        ssl_context=broker_ctx,
        auth_ssl_context=auth_ctx,
    )
    assert captured == [{"base_url": "https://10.0.0.1:9007", "ssl_context": auth_ctx}]
    assert leader.ssl_context is broker_ctx
    assert leader.auth_ssl_context is auth_ctx


def test_without_auth_ssl_context_acl_client_keeps_broker_context(captured) -> None:
    broker_ctx = _ctx()
    GroupLeader("leader-aic", {**MQ, "auth_service_url": "https://auth.example.com"}, ssl_context=broker_ctx)
    assert captured == [{"base_url": "https://auth.example.com", "ssl_context": broker_ctx}]


def test_no_acl_client_without_auth_service_url(captured) -> None:
    leader = GroupLeader("leader-aic", MQ, ssl_context=_ctx(), auth_ssl_context=_ctx())
    assert captured == []
    assert leader._group_acl_client is None

