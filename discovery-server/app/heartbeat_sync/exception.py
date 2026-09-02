"""discovery-server alive-sync 传输层异常（与 SDK 引擎层异常解耦）。

命名风格对齐 app/sync/exception.py（StrEnum 错误码 + AppBaseError 继承）。
注意：SDK 引擎层有 acps_sdk.amp.alive_sync.errors.AliveSyncError，
     本模块的 AliveSyncError 继承 AppBaseError（discovery 应用错误根类），
     在同一文件 import 时须用别名区分（如 from acps_sdk … import AliveSyncError as _EngineError）。
"""

from enum import StrEnum
from typing import Any

from app.core.base_exception import AppBaseError


class AliveSyncErrorCode(StrEnum):
    """alive-sync 传输/装配层错误码枚举。"""

    CONNECTION_FAIL = "connection_fail"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    SNAPSHOT_FAIL = "snapshot_fail"
    SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"
    DELTA_LOG_UNHEALTHY = "delta_log_unhealthy"
    INVALID_RESPONSE = "invalid_response"
    CLIENT_CONFIG_ERROR = "client_config_error"
    KAFKA_ERROR = "kafka_error"
    DATABASE_ERROR = "database_error"
    RESYNC_REQUIRED = "resync_required"


class AliveSyncError(AppBaseError):
    """alive-sync 传输/装配层根异常。继承自 AppBaseError，error_group 固定为 'alive_sync'。"""

    def __init__(
        self,
        code: AliveSyncErrorCode | str,
        message: str,
        status_code: int = 500,
        input_params: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code,
            error_group="alive_sync",
            error_name=str(code),
            error_msg=message,
            input_params=input_params,
        )
