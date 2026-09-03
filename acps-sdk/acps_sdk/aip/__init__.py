"""
AIP v2 SDK 公共导出

本模块导出 AIP v2 协议的所有公共类型和客户端。
"""

import logging

# Set up NullHandler to avoid "No handler found" warnings
logging.getLogger(__name__).addHandler(logging.NullHandler())

# AIP v2 基础类型
from .aip_base_model import (  # noqa: F401
    # 枚举
    TaskState,
    TaskCommandType,
    # 数据项
    DataItem,
    TextDataItem,
    FileDataItem,
    StructuredDataItem,
    # 消息基类
    Message,
    # 任务命令和结果
    TaskCommand,
    TaskResult,
    # 任务状态和产出物
    TaskStatus,
    Product,
    # 命令参数
    GetCommandParams,
    StartCommandParams,
)

from .aip_identity import (  # noqa: F401
    AUTHENTICATION_REQUIRED_CODE,
    AUTHORIZATION_FAILED_CODE,
    AipIdentityBindingConfig,
    AipIdentityError,
    InvalidPeerCertificateError,
    PeerAicMissingError,
    PeerIdentity,
    SenderIdentityMismatchError,
    assert_aic_matches_expected,
    assert_sender_matches_expected,
    assert_sender_matches_peer,
    extract_acps_uri_san,
    extract_common_name,
    extract_peer_aic_from_httpx_response,
    extract_peer_identity,
    identity_error_to_jsonrpc,
    normalize_aic,
    normalize_and_validate_aic,
    normalize_and_validate_expected_aic,
)

from .aip_peer_cert import (  # noqa: F401
    AipPeerCertH11Protocol,
    AipPeerCertificateMiddleware,
    PeerCertificateInfo,
    PeerCertificateRegistry,
    get_request_peer_aic,
)

# RPC 客户端
from .aip_rpc_client import AipRpcClient  # noqa: F401

# -----------------------------------------------------------------------
# 流式传输（Streaming）
# -----------------------------------------------------------------------

# 流式传输模型
from .aip_stream_model import (  # noqa: F401
    SSE_HEADERS,
    SSE_MEDIA_TYPE,
    StreamEventData,
    StreamEventPayload,
    StreamRequest,
    StreamRequestParams,
    StreamResponse,
    TaskStatusUpdateEvent,
    ProductChunkEvent,
    ReStreamCommandParams,
)

# 流式传输服务端
from .aip_stream_server import (  # noqa: F401
    BufferedStreamEvent,
    StreamHandlers,
    StreamHub,
    TaskStreamChannel,
    add_aip_stream_router,
    build_stream_error_response,
    build_stream_response,
    format_sse,
    handle_stream_request,
)

# 流式传输客户端
from .aip_stream_client import AipStreamClient, StreamProtocolError  # noqa: F401

# -----------------------------------------------------------------------
# 通知方式（Notification）
# -----------------------------------------------------------------------

# 通知模型
from .aip_notification_model import (  # noqa: F401
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

# 通知服务端
from .aip_notification_server import (  # noqa: F401
    NotificationConfigStore,
    NotificationDispatcher,
    NotificationHandlers,
    NotificationRegistry,
    NotificationService,
    NotificationSubscription,
    add_aip_notification_router,
)

# 通知客户端
from .aip_notification_client import (  # noqa: F401
    AipNotificationClient,
    NotificationReceiver,
    add_aip_notification_receiver_router,
)

# -----------------------------------------------------------------------
# 群组模式（Group Mode）
# -----------------------------------------------------------------------

# 群组模式类型
from .aip_group_model import (  # noqa: F401
    # 基础信息
    ACSObject,
    GroupInfo,
    GroupInvitationError,
    GroupInvitationErrorData,
    # 枚举
    GroupMgmtCommandType,
    # 群组管理命令和结果
    GroupMgmtCommand,
    GroupMgmtResult,
    GroupMemberStatus,
    InboxGroupInvitation,
    InboxGroupInvitationError,
    # RabbitMQ 配置
    RabbitMQRequest,
    RabbitMQResponse,
    RabbitMQRequestParams,
    RabbitMQServerConfig,
    AMQPConfig,
)
from .aip_group_identity import (  # noqa: F401
    assert_direct_group_request_identity,
    assert_incoming_group_message_identity,
    build_outgoing_amqp_user_id,
    extract_amqp_user_id,
)

# 群组模式客户端
from .aip_group_leader import (  # noqa: F401
    GroupLeaderMqClient,
    GroupLeaderSession,
    GroupLeader,
)
from .aip_group_partner import (  # noqa: F401
    GroupPartnerMqClient,
    PartnerGroupSession,
    PartnerGroupState,
)
