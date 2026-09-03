"""AMP alive-sync Consumer 引擎层异常。

SDK 引擎层只抛这里的异常；HTTP/Kafka 传输层的应用错误由各宿主项目自行定义。
"""


class AliveSyncError(Exception):
    """引擎层根异常。所有 alive-sync 引擎内部错误均继承本类。"""


class SnapshotProtocolError(AliveSyncError):
    """NDJSON snapshot 协议违规（meta 行格式非法、行字段缺失等）。"""


class GapDetectedError(AliveSyncError):
    """检测到序号缺口（seq > lastSeenSeq + 1），需触发重同步。"""

    def __init__(self, shard: str, expected_seq: int, got_seq: int) -> None:
        self.shard = shard
        self.expected_seq = expected_seq
        self.got_seq = got_seq
        super().__init__(
            f"Gap on shard {shard!r}: expected seq {expected_seq}, got {got_seq}"
        )


class ResyncRequired(AliveSyncError):
    """无法从当前状态续跑，需从头重同步（如 checkpoint 丢失、offset 越界等）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Resync required: {reason}")
