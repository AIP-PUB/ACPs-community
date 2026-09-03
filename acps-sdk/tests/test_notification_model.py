"""
N0 测试：aip_notification_model.py 全套数据模型
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from acps_sdk.aip.aip_base_model import TaskCommand, TaskCommandType, TaskState
from acps_sdk.aip.aip_notification_model import (
    NOTIFICATION_TOKEN_HEADER,
    NotificationConfig,
    NotificationDeleteRequest,
    NotificationDeleteResult,
    NotificationGetRequest,
    NotificationIdParams,
    NotificationRequest,
    NotificationStartParams,
    NotificationStartRequest,
    NotificationStartRequestParams,
)

NOW = datetime.now(timezone.utc).isoformat()


def _make_task_command() -> TaskCommand:
    return TaskCommand(
        id="cmd-1",
        sentAt=NOW,
        senderRole="leader",
        senderId="leader-1",
        sessionId="sess-1",
        command=TaskCommandType.Start,
        taskId="task-1",
        commandParams={"notificationConfigId": "nc-abc"},
    )


# ---------------------------------------------------------------------------
# NotificationConfig
# ---------------------------------------------------------------------------


def test_config_id_optional():
    """id 可不传，创建后 id 为 None。"""
    cfg = NotificationConfig(url="http://leader/cb", token="tok123", taskId="t-1")
    assert cfg.id is None
    assert cfg.url == "http://leader/cb"


def test_config_id_set():
    """id 可传入。"""
    cfg = NotificationConfig(id="nc-1", url="http://leader/cb", token="tok", taskId="t-1")
    assert cfg.id == "nc-1"


# ---------------------------------------------------------------------------
# NotificationRequest (notification/set)
# ---------------------------------------------------------------------------


def test_notification_request_default_method():
    """method 默认为 notification/set。"""
    cfg = NotificationConfig(url="http://x/cb", token="t", taskId="t-1")
    req = NotificationRequest(params=cfg)
    assert req.method == "notification/set"


# ---------------------------------------------------------------------------
# NotificationDeleteRequest / NotificationGetRequest
# ---------------------------------------------------------------------------


def test_delete_request_default_method():
    """method 默认为 notification/delete。"""
    req = NotificationDeleteRequest(params=NotificationIdParams(taskId="t-1"))
    assert req.method == "notification/delete"


def test_get_request_default_method():
    """method 默认为 notification/get。"""
    req = NotificationGetRequest(params=NotificationIdParams(taskId="t-1"))
    assert req.method == "notification/get"


# ---------------------------------------------------------------------------
# NotificationDeleteResult
# ---------------------------------------------------------------------------


def test_delete_result_success():
    """success 默认为 True（Literal[True]）。"""
    result = NotificationDeleteResult()
    assert result.success is True


# ---------------------------------------------------------------------------
# NotificationStartRequest
# ---------------------------------------------------------------------------


def test_start_request_default_method():
    """method 默认为 notification/start。"""
    cmd = _make_task_command()
    params = NotificationStartRequestParams(message=cmd)
    req = NotificationStartRequest(params=params)
    assert req.method == "notification/start"


def test_start_request_parse():
    """用 JSON 做 model_validate，验证 params.message 字段结构。"""
    raw = {
        "jsonrpc": "2.0",
        "method": "notification/start",
        "id": "1",
        "params": {
            "message": {
                "type": "task-command",
                "id": "cmd-1",
                "sentAt": NOW,
                "senderRole": "leader",
                "senderId": "leader-1",
                "sessionId": "sess-1",
                "command": "start",
                "taskId": "task-1",
                "commandParams": {"notificationConfigId": "nc-abc"},
            }
        },
    }
    req = NotificationStartRequest.model_validate(raw)
    assert req.params.message.command == TaskCommandType.Start
    assert req.params.message.commandParams is not None
    assert req.params.message.commandParams["notificationConfigId"] == "nc-abc"


# ---------------------------------------------------------------------------
# NotificationStartParams
# ---------------------------------------------------------------------------


def test_start_params_notify_on_states_optional():
    """notifyOnStates 为可选字段。"""
    p = NotificationStartParams(notificationConfigId="nc-1")
    assert p.notifyOnStates is None

    p2 = NotificationStartParams(
        notificationConfigId="nc-1",
        notifyOnStates=[TaskState.Completed, TaskState.Failed],
    )
    assert TaskState.Completed in p2.notifyOnStates


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


def test_token_header_constant():
    """NOTIFICATION_TOKEN_HEADER 值正确。"""
    assert NOTIFICATION_TOKEN_HEADER == "X-ACPs-AIP-Notification-Token"
