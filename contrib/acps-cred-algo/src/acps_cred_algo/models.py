"""交互凭证（InteractionReceipt）模型 —— 纯标准库实现，零外部依赖。

本文件是 `ACPs-Cred` 主工程中 `cred_core/receipts.py` 的**解耦版**：

- 原实现用 AIP SDK 的 ``TaskState`` 枚举判断终态，这里改为字符串常量，
  保证算法包可以在任何 Python 环境中独立使用；
- 原实现中与 AIP 协议耦合的方法（``from_task_result`` / ``to_data_item``）
  不包含在此独立算法包内，协议适配由调用方实现。

字段与原实现**完全一致**，因此两边产生的凭证可以互认、互验。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

__all__ = [
    "RECEIPT_SCHEMA",
    "SCORE_SCHEMA",
    "SUCCESS_STATES",
    "FAILURE_STATES",
    "NEUTRAL_STATES",
    "canonical_json",
    "sha256_hex",
    "result_digest_of",
    "now_iso",
    "Evidence",
    "Receipt",
]

RECEIPT_SCHEMA = "acps-cred/receipt/1"
SCORE_SCHEMA = "acps-cred/score/1"

#: 计入「成功」的终态（对应 AIP ``TaskState.Completed``）
SUCCESS_STATES = {"completed"}
#: 计入「失败」的终态（对应 AIP ``TaskState.Failed`` / ``Rejected``）
FAILURE_STATES = {"failed", "rejected"}
#: 中性终态：用户主动取消，不反映能力优劣，不计入成功率分母
NEUTRAL_STATES = {"canceled"}


def now_iso() -> str:
    """当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def canonical_json(obj: Any) -> str:
    """JCS 兼容子集的规范化 JSON（递归键排序 + 紧凑分隔符 + UTF-8 安全）。

    用途：保证不同实现针对同一对象生成完全相同的字节串，从而得到相同的摘要。
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(payload: Any) -> str:
    """对任意对象取 SHA-256 十六进制摘要（对象会先做规范化 JSON）。"""
    if not isinstance(payload, (str, bytes, bytearray)):
        payload = canonical_json(payload)
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def result_digest_of(task_id: str, states: List[str]) -> str:
    """由 taskId + 状态序列推导「痕迹摘要」。

    刻意只依赖协议本身的字段（taskId 与 statusHistory 中的 state），
    因此任何对端都能在收到查询命令后复现同一摘要 —— 这是第三方回查的前提。
    """
    return sha256_hex({"taskId": task_id, "states": list(states)})


@dataclass
class Evidence:
    """凭证的证据来源，用于区分「单方观测」与「第三方回查确认」。"""

    source: str = "leader-observation"  # leader-observation | traceback-confirmed
    issuerAic: str = ""
    issuerProvider: str = ""
    peerAic: str = ""
    tracebackVerified: bool = False
    tracebackMemo: str = ""


@dataclass
class Receipt:
    """一次跨智能体协作的交付凭证。

    一条凭证只描述「谁、在什么时候、用了哪个技能、结果是什么、耗时多少」，
    不包含任何业务语义，因此可以跨团队通用。
    """

    schema: str = RECEIPT_SCHEMA
    receiptId: str = field(default_factory=lambda: f"rcpt-{uuid.uuid4()}")
    sessionId: str = ""
    taskId: str = ""
    leaderAic: str = ""
    partnerAic: str = ""
    skillId: str = ""
    acsVersion: str = ""
    protocolVersion: str = ""
    startedAt: str = ""
    finishedAt: str = ""
    durationMs: int = 0
    terminalState: str = ""
    statePath: List[str] = field(default_factory=list)
    resultDigest: str = ""
    productDigest: str = ""
    productCount: int = 0
    commandCount: int = 0
    continueCount: int = 0
    rejectedBeforeStart: bool = False
    probePassed: Optional[bool] = None
    evidence: Evidence = field(default_factory=Evidence)
    note: str = ""

    # ------------------------------------------------------------------ 构造与序列化

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Receipt":
        data = dict(data)
        ev = data.pop("evidence", None)
        receipt = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        if ev:
            receipt.evidence = Evidence(
                **{k: v for k, v in ev.items() if k in Evidence.__dataclass_fields__}
            )
        return receipt

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    # ------------------------------------------------------------------ 派生属性

    @property
    def digest(self) -> str:
        """整条凭证的摘要，用于去重与防篡改。"""
        return sha256_hex(self.to_dict())

    @property
    def is_success(self) -> bool:
        return self.terminalState in SUCCESS_STATES

    @property
    def is_failure(self) -> bool:
        return self.terminalState in FAILURE_STATES

    @property
    def is_neutral(self) -> bool:
        return self.terminalState in NEUTRAL_STATES
