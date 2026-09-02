"""Audit Writer — Kafka Consumer 入库实现。

从 Kafka amp.audit 主题消费 LogRecord，执行以下九步入库流程
（参照 AMP-API-Design-Audit.md §3.1）：

1. log_id 解析：取 LogRecord.log_id；缺省时按 spec §5.1.3 的 JCS 内容哈希兜底
2. 幂等检查：audit_record_identity 是否已存在相同 log_id（重复时也推进水位后返回）
3. CA 公钥解析：根据 integrity.kid（证书序列号）向 CA 服务查询签名公钥（KeyResolver）
4. 源端验签：JCS 规范化 + 验证 integrity.sig
5. 逻辑子链路由：stable_hash(aic) % logical_chain_count → chain_id
6. 子链锁：SELECT ... FOR UPDATE 锁定 audit_chain_head
7. committed_at 取值：SELECT clock_timestamp()（服务端时钟，在持有链头行锁后求值）
8. 链哈希计算：raw_log_hash、chain_seq、current_hash
9. 单事务写入：audit_record_identity + audit_records + audit_chain_head（两表 committed_at 同值）
10. 主事务提交后，独立推进 audit_read_model_watermark
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from acps_sdk.amp import compute_log_id_fallback
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.hashes import SHA256
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.chain import compute_chain_id, compute_current_hash, compute_raw_log_hash
from app.audit.key_resolver import (
    ATRUnavailableError,
    CAKeyResolver,
    KeyNotFoundError,
    KeyResolver,
    MockKeyResolver,
)
from app.audit.model import AuditChainHead, AuditRecord, AuditRecordIdentity
from app.core.amp_schema import LogRecord
from app.core.config import settings
from app.core.db_session import async_session_factory
from app.core.kafka_consumer import BaseLogConsumer

logger = structlog.get_logger(__name__)


def _build_key_resolver() -> KeyResolver:
    """根据配置创建 KeyResolver 实例。

    mock 模式：从 config/audit_keys.json 加载开发用公钥，供本地联调使用。
    生产模式：向 CA 服务（[atr].ca_base_url）按证书序列号实时查询公钥。
    """
    if settings.atr_mock_mode:
        logger.warning("ATR mock 模式已启用，签名验证将使用 audit_keys.json 中的预设密钥")
        keys = _load_mock_keys()
        if not keys:
            logger.warning(
                "audit_keys.json 为空或不存在，MockKeyResolver 将无法验证任何签名；"
                "请运行 uv run python scripts/gen_audit_keys.py 生成开发用密钥"
            )
        return MockKeyResolver(keys)
    logger.info(
        "CA 模式已启用，签名验证将实时查询 CA 服务",
        ca_base_url=settings.atr_ca_base_url,
        key_cache_ttl_seconds=settings.atr_key_cache_ttl_seconds,
    )
    return CAKeyResolver(
        ca_base_url=settings.atr_ca_base_url,
        cache_ttl_seconds=settings.atr_key_cache_ttl_seconds,
    )


def _load_mock_keys() -> dict[str, str]:
    """从 config/audit_keys.json 读取 {kid: public_key_pem} 映射。"""
    import json
    from pathlib import Path

    keys_path = Path(__file__).parent.parent.parent / "config" / "audit_keys.json"
    if not keys_path.exists():
        return {}
    try:
        entries: dict[str, dict[str, Any]] = json.loads(keys_path.read_text(encoding="utf-8"))
        return {
            entry["kid"]: entry["public_key"]
            for entry in entries.values()
            if entry.get("kid") and entry.get("public_key")
        }
    except Exception as exc:
        logger.warning("读取 audit_keys.json 失败，MockKeyResolver 将以空配置运行", error=str(exc))
        return {}


# 传输层元数据字段前缀集合：这些字段由日志转发组件（如 Fluent Bit）在传输过程中注入，
# 不属于原始 AMP 审计日志内容，必须在签名验证前剥离，否则 JCS 规范化结果会与签名时不一致。
# 已知来源：
#   @timestamp — Fluent Bit Kafka 输出插件默认注入（可通过 timestamp_key 更名）
#   _fb_ts     — 本项目 fluent-bit.conf 中重命名后的 Fluent Bit 时间戳字段
_TRANSPORT_METADATA_PREFIXES = ("@", "_fb")


async def _fetch_clock_timestamp(session: AsyncSession) -> datetime:
    """在当前 DB 会话中取服务端 clock_timestamp()。

    必须在持有 audit_chain_head 行锁（SELECT ... FOR UPDATE）之后调用，
    保证 committed_at 在同一链内随 chain_seq 单调不减（§4.3）。
    """
    result = await session.scalar(text("SELECT clock_timestamp()"))
    if isinstance(result, datetime):
        return result
    # 极端情况回退（不应发生）
    return datetime.now(tz=UTC)


def _strip_transport_metadata(raw_log: dict[str, Any]) -> dict[str, Any]:
    """剥离由日志传输组件（如 Fluent Bit）注入的非 AMP 元数据字段。"""
    return {
        k: v for k, v in raw_log.items() if not any(k.startswith(prefix) for prefix in _TRANSPORT_METADATA_PREFIXES)
    }


def _verify_signature(
    pub_key: Ed25519PublicKey | RSAPublicKey,
    alg: str,
    raw_log: dict[str, Any],
    sig_b64: str,
) -> None:
    """对 raw_log 做 JCS 规范化并验证签名。

    Args:
        pub_key: 已加载的公钥对象。
        alg: 签名算法名称（"EdDSA" 或 "RS256"）。
        raw_log: LogRecord 原始 dict（不含 integrity 字段，按规范）。
        sig_b64: Base64url 编码的签名值。

    Raises:
        InvalidSignature: 签名验证失败。
        ValueError: 不支持的算法。
    """
    import jcs

    sig_bytes = base64.urlsafe_b64decode(sig_b64 + "==")
    # 剥离传输层注入字段（如 Fluent Bit 的 @timestamp / _fb_ts），再去掉 integrity，做 JCS
    cleaned = _strip_transport_metadata(raw_log)
    signable = {k: v for k, v in cleaned.items() if k != "integrity"}
    canonical = jcs.canonicalize(signable)

    if alg == "EdDSA" and isinstance(pub_key, Ed25519PublicKey):
        pub_key.verify(sig_bytes, canonical)
    elif alg == "RS256" and isinstance(pub_key, RSAPublicKey):
        pub_key.verify(sig_bytes, canonical, PKCS1v15(), SHA256())
    else:
        raise ValueError(f"不支持的签名算法或密钥类型组合: alg={alg!r}, key={type(pub_key).__name__}")


class AuditWriter(BaseLogConsumer):
    """Audit 日志 Kafka Consumer 与入库处理器。

    继承 BaseLogConsumer，实现 handle_message() 执行完整的七步入库逻辑。
    """

    def __init__(self, key_resolver: KeyResolver | None = None) -> None:
        super().__init__(
            topic=settings.audit_topic,
            dlq_topic=settings.audit_dlq_topic,
            group_id=settings.audit_consumer_group,
            bootstrap_servers=settings.kafka_bootstrap_servers,
            security_protocol=settings.kafka_security_protocol,
            auto_offset_reset=settings.kafka_auto_offset_reset,
            max_poll_records=settings.kafka_max_poll_records,
            session_timeout_ms=settings.kafka_session_timeout_ms,
            heartbeat_interval_ms=settings.kafka_heartbeat_interval_ms,
        )
        self._key_resolver: KeyResolver = key_resolver or _build_key_resolver()
        self._logical_chain_count = settings.audit_logical_chain_count

    async def handle_message(self, message: object) -> None:
        """处理单条 Audit LogRecord Kafka 消息（at-least-once，含幂等保护）。

        Args:
            message: aiokafka ConsumerRecord 对象（value 为 JSON 编码的 LogRecord）。

        Raises:
            Exception: 解析失败或处理错误时抛出，由 BaseLogConsumer 触发重试 + DLQ。
        """
        raw_value: bytes = getattr(message, "value", b"")
        if not raw_value:
            logger.warning("收到空消息，跳过")
            return

        # 提取 Kafka 元数据（用于按分区推进水位）
        partition_key: int = getattr(message, "partition", 0)
        offset: int | None = getattr(message, "offset", None)

        raw_dict: dict[str, Any] = json.loads(raw_value.decode("utf-8"))
        record = LogRecord.model_validate(raw_dict)

        if record.log_type != "audit":
            logger.debug("非 audit 类型消息，跳过", log_type=record.log_type)
            return

        await self._process_audit_record(record, raw_dict, partition_key=partition_key, offset=offset)

    async def _process_audit_record(
        self,
        record: LogRecord,
        raw_dict: dict[str, Any],
        partition_key: int = 0,
        offset: int | None = None,
    ) -> None:
        """执行完整的八步写入流程（含 log_id 兜底）。

        Args:
            record: 已解析的 LogRecord。
            raw_dict: 原始 JSON 反序列化 dict，用于链哈希、验签和 DLQ 载荷。
            partition_key: 消息来自的 Kafka 分区号（默认 0，测试可省略）。
            offset: 消息 Kafka offset（用于水位追踪，可选）。
        """
        # ── Step 1：log_id 解析（spec §5.1.3 C-AUDIT-WRITE-4）─────────────
        # 源端提供的 log_id 直接使用；缺省时按 JCS 内容哈希兜底，保证幂等键唯一且稳定。
        effective_log_id: str = record.log_id or compute_log_id_fallback(raw_dict)
        if record.log_id is None:
            logger.debug(
                "log_id 缺省，使用 JCS 内容哈希兜底",
                effective_log_id=effective_log_id,
                aic=record.aic,
            )

        async with async_session_factory() as session:
            # ── Step 2：幂等检查 ──────────────────────────────────────────
            existing = await session.scalar(
                select(AuditRecordIdentity).where(AuditRecordIdentity.log_id == effective_log_id)  # type: ignore[arg-type]
            )
            if existing is not None:
                logger.debug(
                    "log_id 已存在，幂等跳过",
                    log_id=effective_log_id,
                    audit_id=str(existing.audit_id),
                )
                # 重复路径同样推进水位（§3.1 step 8 设计注释）：
                # 避免崩溃恰好发生在去重路径时水位滞后到下一条新事件才恢复。
                await self._advance_watermark(record.timestamp, partition_key=partition_key, offset=offset)
                return

            # ── Step 3：CA 公钥解析（kid = 证书序列号，向 CA 查询公钥）──────────────────
            sig_verified = False
            failure_type: str | None = None

            if record.integrity is not None:
                try:
                    pub_key = await self._key_resolver.resolve(record.aic, record.integrity.kid)
                    # ── Step 4：源端验签 ──────────────────────────────────
                    try:
                        _verify_signature(
                            pub_key,
                            record.integrity.alg,
                            raw_dict,
                            record.integrity.sig,
                        )
                        sig_verified = True
                    except InvalidSignature, ValueError:
                        logger.warning(
                            "源端签名验证失败",
                            log_id=effective_log_id,
                            kid=record.integrity.kid,
                            alg=record.integrity.alg,
                        )
                        failure_type = "signature"
                except ATRUnavailableError:
                    logger.warning(
                        "ATR 不可达，入库标记 missing_public_key",
                        log_id=effective_log_id,
                        kid=record.integrity.kid,
                    )
                    failure_type = "missing_public_key"
                except KeyNotFoundError:
                    logger.warning(
                        "ATR 未找到公钥，入库标记 missing_public_key",
                        log_id=effective_log_id,
                        kid=record.integrity.kid,
                    )
                    failure_type = "missing_public_key"
            else:
                failure_type = "missing_public_key"

            # ── Step 5：逻辑子链路由 ─────────────────────────────────────
            chain_id = compute_chain_id(record.aic, self._logical_chain_count)

            # ── Step 6：SELECT FOR UPDATE 锁定子链头 ─────────────────────
            chain_head = await session.scalar(
                select(AuditChainHead)
                .where(AuditChainHead.chain_id == chain_id)  # type: ignore[arg-type]
                .with_for_update()
            )
            if chain_head is None:
                raise RuntimeError(f"audit_chain_head 缺少 chain_id={chain_id!r}，请检查初始化迁移")

            # ── Step 7：committed_at 取服务端时钟（clock_timestamp()）──────
            # 必须在持有链头行锁后求值，保证同一链内 committed_at 随 chain_seq 单调不减。
            # 两表（audit_records + audit_record_identity）使用同一个值（§4.3）。
            committed_at = await _fetch_clock_timestamp(session)

            # ── Step 8：链哈希计算 ───────────────────────────────────────
            audit_id = uuid.uuid7()
            raw_log_hash = compute_raw_log_hash(raw_dict)
            chain_seq = chain_head.last_chain_seq + 1
            previous_hash = chain_head.last_current_hash if chain_seq > 0 else None
            current_hash = compute_current_hash(
                audit_id=str(audit_id),
                log_id=effective_log_id,
                timestamp_str=record.timestamp,
                aic=record.aic,
                chain_id=chain_id,
                chain_seq=chain_seq,
                raw_log_hash=raw_log_hash,
                previous_hash=previous_hash,
            )

            now_utc = datetime.now(tz=UTC)  # 用于 signature_checked_at 等元数据时间戳
            sig_alg = record.integrity.alg if record.integrity else ""
            sig_kid = record.integrity.kid if record.integrity else ""
            sig_val = record.integrity.sig if record.integrity else ""

            # 按 AMP Spec §5.6 嵌套结构解析 body 字段
            body: dict[str, Any] = record.body or {}
            actor_obj: dict[str, Any] = body.get("actor", {})
            action_obj: dict[str, Any] = body.get("action", {})
            target_obj: dict[str, Any] = body.get("target", {})
            result_obj: dict[str, Any] = body.get("result", {})

            identity_row = AuditRecordIdentity(
                audit_id=audit_id,
                log_id=effective_log_id,
                timestamp=datetime.fromisoformat(record.timestamp),
                committed_at=committed_at,
                chain_id=chain_id,
                chain_seq=chain_seq,
                created_at=now_utc,
            )

            audit_row = AuditRecord(
                audit_id=audit_id,
                log_id=effective_log_id,
                timestamp=datetime.fromisoformat(record.timestamp),
                committed_at=committed_at,
                aic=record.aic,
                trace_id=record.trace_id,
                correlation_id=record.correlation_id,
                chain_id=chain_id,
                chain_seq=chain_seq,
                actor_id=actor_obj.get("id", ""),
                actor_type=actor_obj.get("type", ""),
                actor_name=actor_obj.get("name"),
                actor_role=actor_obj.get("role"),
                actor_ip=actor_obj.get("ip"),
                actor_user_agent=actor_obj.get("userAgent"),
                action_name=action_obj.get("name", ""),
                action_type=action_obj.get("type", ""),
                action_method=action_obj.get("method"),
                target_type=target_obj.get("type", ""),
                target_id=target_obj.get("id", ""),
                target_name=target_obj.get("name"),
                target_before=target_obj.get("before"),
                target_after=target_obj.get("after"),
                result_status=result_obj.get("status", ""),
                result_reason=result_obj.get("reason"),
                result_error_code=result_obj.get("errorCode"),
                signature_alg=sig_alg,
                signature_kid=sig_kid,
                signature_value=sig_val,
                signature_verified=sig_verified,
                signature_checked_at=now_utc,
                verification_failure_type=failure_type,
                hash_version=1,
                raw_log_hash=raw_log_hash,
                previous_hash=previous_hash,
                current_hash=current_hash,
                chain_verified=None,
                chain_checked_at=None,
                anchor_id=None,
                raw_log=raw_dict,
            )

            # ── Step 8：单事务写入三张表 ─────────────────────────────────
            try:
                session.add(identity_row)
                session.add(audit_row)
                await session.flush()  # 触发约束检查

                await session.execute(
                    update(AuditChainHead)
                    .where(AuditChainHead.chain_id == chain_id)  # type: ignore[arg-type]
                    .values(
                        last_audit_id=audit_id,
                        last_chain_seq=chain_seq,
                        last_current_hash=current_hash,
                        updated_at=now_utc,
                    )
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                logger.warning(
                    "log_id 唯一约束冲突（并发重复投递），幂等回滚",
                    log_id=effective_log_id,
                )
                return

        # ── Step 9：主事务提交后，独立推进分区水位 ──────────────────────
        await self._advance_watermark(record.timestamp, partition_key=partition_key, offset=offset)

        logger.info(
            "Audit 记录入库成功",
            audit_id=str(audit_id),
            log_id=effective_log_id,
            log_id_source="source" if record.log_id else "jcs_fallback",
            chain_id=chain_id,
            chain_seq=chain_seq,
            sig_verified=sig_verified,
        )

    async def _advance_watermark(
        self,
        timestamp_str: str,
        partition_key: int = 0,
        offset: int | None = None,
    ) -> None:
        """按分区推进 audit_read_model_watermark（独立 UPSERT，与主事务解耦）。

        - 分区内用 GREATEST 保证单调推进。
        - 全局 dataFreshnessAt 由查询层对所有分区取 MIN 得出（service._get_watermark）。
        - 使用 INSERT ... ON CONFLICT DO UPDATE 避免依赖预置行，Writer 首次处理某
          分区时自动创建该行（C-AUDIT-WRITE-3）。
        """
        event_time = datetime.fromisoformat(timestamp_str)
        async with async_session_factory() as session:
            await session.execute(
                text("""
                    INSERT INTO audit_read_model_watermark
                        (stream_name, partition_key, partition_watermark, last_offset, updated_at)
                    VALUES
                        ('amp.audit', :pk, :ts, :off, NOW())
                    ON CONFLICT (stream_name, partition_key)
                    DO UPDATE SET
                        partition_watermark = GREATEST(
                            audit_read_model_watermark.partition_watermark, EXCLUDED.partition_watermark
                        ),
                        last_offset = EXCLUDED.last_offset,
                        updated_at  = NOW()
                """),
                {"pk": partition_key, "ts": event_time, "off": offset},
            )
            await session.commit()
