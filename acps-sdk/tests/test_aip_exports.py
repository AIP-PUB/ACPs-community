"""
X0 测试：acps_sdk.aip 公共导出完整性

确保所有预期的类、函数和常量均可从顶层包导入。
"""
from __future__ import annotations

import inspect

import pytest


# ---------------------------------------------------------------------------
# 基础类型
# ---------------------------------------------------------------------------


def test_base_model_exports():
    from acps_sdk.aip import (
        AipIdentityBindingConfig,
        AipIdentityError,
        AipPeerCertH11Protocol,
        AipPeerCertificateMiddleware,
        DataItem,
        FileDataItem,
        GetCommandParams,
        InvalidPeerCertificateError,
        InboxGroupInvitation,
        InboxGroupInvitationError,
        Message,
        PeerAicMissingError,
        PeerCertificateInfo,
        PeerCertificateRegistry,
        PeerIdentity,
        Product,
        SenderIdentityMismatchError,
        StartCommandParams,
        StructuredDataItem,
        TaskCommand,
        TaskCommandType,
        TaskResult,
        TaskState,
        TaskStatus,
        TextDataItem,
    )

    assert inspect.isclass(TaskState)
    assert inspect.isclass(TaskCommand)
    assert inspect.isclass(TaskResult)
    assert inspect.isclass(TaskStatus)
    assert inspect.isclass(TextDataItem)
    assert inspect.isclass(PeerIdentity)
    assert inspect.isclass(PeerCertificateRegistry)
    assert inspect.isclass(AipPeerCertH11Protocol)
    assert inspect.isclass(AipPeerCertificateMiddleware)
    assert inspect.isclass(AipIdentityBindingConfig)
    assert inspect.isclass(AipIdentityError)
    assert inspect.isclass(PeerAicMissingError)
    assert inspect.isclass(SenderIdentityMismatchError)
    assert inspect.isclass(InvalidPeerCertificateError)
    assert inspect.isclass(PeerCertificateInfo)
    assert inspect.isclass(InboxGroupInvitation)
    assert inspect.isclass(InboxGroupInvitationError)


def test_identity_helper_exports():
    from acps_sdk.aip import (
        AUTHENTICATION_REQUIRED_CODE,
        AUTHORIZATION_FAILED_CODE,
        assert_aic_matches_expected,
        assert_direct_group_request_identity,
        assert_incoming_group_message_identity,
        assert_sender_matches_expected,
        assert_sender_matches_peer,
        build_outgoing_amqp_user_id,
        extract_acps_uri_san,
        extract_amqp_user_id,
        extract_common_name,
        extract_peer_aic_from_httpx_response,
        extract_peer_identity,
        get_request_peer_aic,
        identity_error_to_jsonrpc,
        normalize_aic,
        normalize_and_validate_aic,
        normalize_and_validate_expected_aic,
    )

    assert isinstance(AUTHENTICATION_REQUIRED_CODE, int)
    assert isinstance(AUTHORIZATION_FAILED_CODE, int)
    assert callable(normalize_aic)
    assert callable(normalize_and_validate_aic)
    assert callable(normalize_and_validate_expected_aic)
    assert callable(extract_common_name)
    assert callable(extract_acps_uri_san)
    assert callable(extract_peer_identity)
    assert callable(extract_peer_aic_from_httpx_response)
    assert callable(assert_sender_matches_peer)
    assert callable(assert_sender_matches_expected)
    assert callable(assert_aic_matches_expected)
    assert callable(extract_amqp_user_id)
    assert callable(build_outgoing_amqp_user_id)
    assert callable(assert_direct_group_request_identity)
    assert callable(assert_incoming_group_message_identity)
    assert callable(identity_error_to_jsonrpc)
    assert callable(get_request_peer_aic)


# ---------------------------------------------------------------------------
# RPC 客户端
# ---------------------------------------------------------------------------


def test_rpc_client_export():
    from acps_sdk.aip import AipRpcClient

    assert inspect.isclass(AipRpcClient)


# ---------------------------------------------------------------------------
# 流式传输 — 模型
# ---------------------------------------------------------------------------


def test_stream_model_exports():
    from acps_sdk.aip import (
        SSE_HEADERS,
        SSE_MEDIA_TYPE,
        ProductChunkEvent,
        ReStreamCommandParams,
        StreamEventData,
        StreamEventPayload,
        StreamRequest,
        StreamRequestParams,
        StreamResponse,
        TaskStatusUpdateEvent,
    )

    assert isinstance(SSE_MEDIA_TYPE, str)
    assert isinstance(SSE_HEADERS, dict)
    assert inspect.isclass(StreamResponse)
    assert inspect.isclass(StreamEventData)


# ---------------------------------------------------------------------------
# 流式传输 — 服务端
# ---------------------------------------------------------------------------


def test_stream_server_exports():
    from acps_sdk.aip import (
        BufferedStreamEvent,
        StreamHub,
        TaskStreamChannel,
        add_aip_stream_router,
        build_stream_error_response,
        build_stream_response,
        format_sse,
    )

    assert inspect.isclass(StreamHub)
    assert inspect.isclass(TaskStreamChannel)
    assert callable(add_aip_stream_router)
    assert callable(format_sse)


# ---------------------------------------------------------------------------
# 流式传输 — 客户端（含 S3 新增）
# ---------------------------------------------------------------------------


def test_stream_client_exports():
    from acps_sdk.aip import AipStreamClient, StreamProtocolError

    assert inspect.isclass(AipStreamClient)
    assert inspect.isclass(StreamProtocolError)
    assert issubclass(StreamProtocolError, Exception)


def test_stream_protocol_error_attrs():
    """StreamProtocolError 应有 code / message / data 属性。"""
    from acps_sdk.aip import StreamProtocolError

    err = StreamProtocolError(code=-32000, message="buffer expired", data={"seq": 5})
    assert err.code == -32000
    assert err.message == "buffer expired"
    assert err.data == {"seq": 5}


# ---------------------------------------------------------------------------
# 通知方式 — 模型
# ---------------------------------------------------------------------------


def test_notification_model_exports():
    from acps_sdk.aip import (
        NOTIFICATION_TOKEN_HEADER,
        NotificationConfig,
        NotificationDeleteRequest,
        NotificationDeleteResponse,
        NotificationDeleteResult,
        NotificationGetRequest,
        NotificationGetResponse,
        NotificationIdParams,
        NotificationRequest,
        NotificationResponse,
        NotificationStartParams,
        NotificationStartRequest,
        NotificationStartRequestParams,
    )

    assert isinstance(NOTIFICATION_TOKEN_HEADER, str)
    assert inspect.isclass(NotificationConfig)


# ---------------------------------------------------------------------------
# 通知方式 — 服务端（含 N2 新增 NotificationHandlers）
# ---------------------------------------------------------------------------


def test_notification_server_exports():
    from acps_sdk.aip import (
        NotificationConfigStore,
        NotificationDispatcher,
        NotificationHandlers,
        NotificationRegistry,
        NotificationService,
        NotificationSubscription,
        add_aip_notification_router,
    )

    assert inspect.isclass(NotificationConfigStore)
    assert inspect.isclass(NotificationDispatcher)
    assert inspect.isclass(NotificationHandlers)
    assert inspect.isclass(NotificationService)
    assert callable(add_aip_notification_router)


def test_notification_handlers_optional_fields():
    """NotificationHandlers 的所有字段均为可选（dataclass，缺省 None）。"""
    from acps_sdk.aip import NotificationHandlers

    h = NotificationHandlers()
    assert h.on_notification_start is None


# ---------------------------------------------------------------------------
# 通知方式 — 客户端
# ---------------------------------------------------------------------------


def test_notification_client_exports():
    from acps_sdk.aip import AipNotificationClient, NotificationReceiver

    assert inspect.isclass(AipNotificationClient)
    assert inspect.isclass(NotificationReceiver)
