"""acps_sdk.amp 子模块单元测试。

使用同步 pytest（无 pytest-asyncio），emit() 用 asyncio.run() 验证。
"""

import asyncio
import base64
import json
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from acps_sdk.amp import (
    AuditAction,
    AuditActor,
    AuditBody,
    AuditEmitter,
    AuditLogRecord,
    AuditResult,
    AuditTarget,
    Ed25519AuditSigner,
    load_signer_from_keys_json,
)


# ─── 签名测试辅助 ─────────────────────────────────────────────────────────────

def _gen_ed25519_pem() -> tuple[str, str]:
    """生成一对 Ed25519 密钥，返回 (private_pem, public_pem)。"""
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    pub_pem = priv.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    return priv_pem, pub_pem


# ─── 辅助工厂 ────────────────────────────────────────────────────────────────

def _make_body(
    actor_id: str = "user-001",
    actor_type: str = "human",
    actor_name: str | None = None,
    action_name: str = "test_action",
    action_type: str = "test",
    target_type: str = "session",
    target_id: str = "sess-001",
    result_status: str = "success",
    result_reason: str | None = None,
) -> AuditBody:
    return AuditBody(
        actor=AuditActor(id=actor_id, type=actor_type, name=actor_name),
        action=AuditAction(name=action_name, type=action_type),
        target=AuditTarget(type=target_type, id=target_id),
        result=AuditResult(status=result_status, reason=result_reason),  # type: ignore[arg-type]
    )


# ─── AuditLogRecord ──────────────────────────────────────────────────────────

def test_new_log_id_returns_valid_uuid() -> None:
    """new_log_id() 必须返回合法的 UUID 字符串。"""
    log_id = AuditLogRecord.new_log_id()
    parsed = uuid.UUID(log_id)
    assert str(parsed) == log_id


def test_record_serialization_top_level_snake_case_body_nested(tmp_path: Path) -> None:
    """序列化后顶层字段为 snake_case，body 采用嵌套结构（actor / action / target / result）。"""
    record = AuditLogRecord(
        log_id=AuditLogRecord.new_log_id(),
        timestamp="2026-06-10T08:00:00+08:00",
        aic="test-aic",
        body=_make_body(actor_id="u1", actor_name="Alice"),
    )
    data = record.model_dump(mode="json", by_alias=True, exclude_none=True)

    # 顶层：snake_case
    assert "schema_version" in data
    assert "log_id" in data
    assert "log_type" in data
    assert "trace_id" not in data          # None → exclude_none 剔除
    assert "correlation_id" not in data

    # body：嵌套结构
    body = data["body"]
    assert "actor" in body
    assert "action" in body
    assert "target" in body
    assert "result" in body

    actor = body["actor"]
    assert actor["id"] == "u1"
    assert actor["type"] == "human"
    assert actor["name"] == "Alice"

    action = body["action"]
    assert action["name"] == "test_action"
    assert action["type"] == "test"

    result = body["result"]
    assert result["status"] == "success"
    assert "reason" not in result          # None → exclude_none 剔除


def test_record_fixed_fields() -> None:
    """schema_version 固定 1.0，log_type 固定 audit。"""
    record = AuditLogRecord(
        log_id="x",
        timestamp="2026-06-10T00:00:00+00:00",
        aic="aic",
        body=_make_body(),
    )
    assert record.schema_version == "1.0"
    assert record.log_type == "audit"


# ─── AuditEmitter ────────────────────────────────────────────────────────────

def test_emit_sync_writes_valid_record(tmp_path: Path) -> None:
    """emit_sync() 写入的每行都能被 AuditLogRecord.model_validate() 解析。"""
    log_file = tmp_path / "amp_audit.jsonl"
    emitter = AuditEmitter(log_file, aic="1.2.3.test")

    log_id = emitter.emit_sync(_make_body())

    assert log_file.exists()
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    record = AuditLogRecord.model_validate(data)
    assert record.log_id == log_id
    assert record.log_type == "audit"
    assert record.aic == "1.2.3.test"
    assert "+" in record.timestamp or "Z" in record.timestamp  # 含时区


def test_emit_sync_creates_parent_dirs(tmp_path: Path) -> None:
    """emit_sync() 在文件父目录不存在时应自动创建。"""
    log_file = tmp_path / "nested" / "deep" / "audit.jsonl"
    emitter = AuditEmitter(log_file, aic="aic")
    emitter.emit_sync(_make_body())
    assert log_file.exists()


def test_emit_sync_appends_multiple_lines(tmp_path: Path) -> None:
    """多次 emit_sync() 应追加多行，不覆盖。"""
    log_file = tmp_path / "audit.jsonl"
    emitter = AuditEmitter(log_file, aic="aic")
    emitter.emit_sync(_make_body(action_name="first"))
    emitter.emit_sync(_make_body(action_name="second"))
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["body"]["action"]["name"] == "first"
    assert json.loads(lines[1])["body"]["action"]["name"] == "second"


def test_emit_sync_failure_does_not_raise(tmp_path: Path) -> None:
    """写入失败时不应 raise，只 WARNING。"""
    emitter = AuditEmitter(tmp_path, aic="aic")  # tmp_path 是目录，写文件会失败
    emitter.emit_sync(_make_body())               # 应该静默吞掉异常


def test_emit_async_returns_same_log_id(tmp_path: Path) -> None:
    """async emit() 行为与 emit_sync() 一致。"""
    log_file = tmp_path / "audit.jsonl"
    emitter = AuditEmitter(log_file, aic="aic")

    log_id = asyncio.run(emitter.emit(_make_body()))

    assert uuid.UUID(log_id)
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["log_id"] == log_id


def test_emit_sync_with_optional_fields(tmp_path: Path) -> None:
    """trace_id / correlation_id 等可选字段被正确写入。"""
    log_file = tmp_path / "audit.jsonl"
    emitter = AuditEmitter(log_file, aic="aic")
    emitter.emit_sync(_make_body(), trace_id="sess-abc", correlation_id="task-xyz")

    data = json.loads(log_file.read_text().strip())
    assert data["trace_id"] == "sess-abc"
    assert data["correlation_id"] == "task-xyz"


def test_audit_body_actor_user_agent_serialized_as_camel_case(tmp_path: Path) -> None:
    """AuditActor.user_agent 应序列化为 camelCase userAgent。"""
    log_file = tmp_path / "audit.jsonl"
    emitter = AuditEmitter(log_file, aic="aic")
    body = AuditBody(
        actor=AuditActor(id="u1", type="human", user_agent="curl/7.64.1"),
        action=AuditAction(name="login", type="auth"),
        target=AuditTarget(type="session", id="sess-1"),
        result=AuditResult(status="success"),
    )
    emitter.emit_sync(body)

    data = json.loads(log_file.read_text().strip())
    assert data["body"]["actor"]["userAgent"] == "curl/7.64.1"
    assert "user_agent" not in data["body"]["actor"]


# ─── Ed25519AuditSigner ───────────────────────────────────────────────────────

def test_ed25519_signer_alg_and_kid() -> None:
    """Ed25519AuditSigner 应返回正确的 alg 和 kid 属性。"""
    priv_pem, _ = _gen_ed25519_pem()
    signer = Ed25519AuditSigner(private_key_pem=priv_pem, kid="test-key-001")
    assert signer.alg == "EdDSA"
    assert signer.kid == "test-key-001"


def test_ed25519_signer_produces_64_byte_signature() -> None:
    """Ed25519 签名固定为 64 字节。"""
    priv_pem, _ = _gen_ed25519_pem()
    signer = Ed25519AuditSigner(private_key_pem=priv_pem, kid="k")
    sig = signer.sign(b"hello world")
    assert len(sig) == 64


def test_ed25519_signer_rejects_rsa_key() -> None:
    """传入非 Ed25519 私钥时应抛出 TypeError。"""
    from cryptography.hazmat.primitives.asymmetric import rsa
    rsa_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pem = rsa_priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    with pytest.raises(TypeError, match="Ed25519PrivateKey"):
        Ed25519AuditSigner(private_key_pem=rsa_pem, kid="k")


# ─── AuditEmitter + signer ────────────────────────────────────────────────────

def test_emit_sync_with_signer_writes_integrity_field(tmp_path: Path) -> None:
    """传入 signer 时，写出的 JSON 应含 integrity.alg / .kid / .sig 字段。"""
    priv_pem, _ = _gen_ed25519_pem()
    signer = Ed25519AuditSigner(private_key_pem=priv_pem, kid="audit-key-test")
    log_file = tmp_path / "signed.jsonl"
    emitter = AuditEmitter(log_file, aic="1.2.3.test", signer=signer)

    emitter.emit_sync(_make_body())

    data = json.loads(log_file.read_text().strip())
    assert "integrity" in data
    integrity = data["integrity"]
    assert integrity["alg"] == "EdDSA"
    assert integrity["kid"] == "audit-key-test"
    assert isinstance(integrity["sig"], str) and len(integrity["sig"]) > 0


def test_emit_sync_with_signer_signature_is_verifiable(tmp_path: Path) -> None:
    """签名可以用对应公钥成功验证（JCS 规范化 + 无 integrity 字段）。"""
    import jcs
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    priv_pem, pub_pem = _gen_ed25519_pem()
    signer = Ed25519AuditSigner(private_key_pem=priv_pem, kid="k")
    log_file = tmp_path / "signed.jsonl"
    emitter = AuditEmitter(log_file, aic="test-aic", signer=signer)
    emitter.emit_sync(_make_body())

    data = json.loads(log_file.read_text().strip())
    integrity = data.pop("integrity")

    # 重新构造签名内容：去掉 integrity 后 JCS 规范化（与 writer.py 一致）
    canonical = jcs.canonicalize(data)
    sig_bytes = base64.urlsafe_b64decode(integrity["sig"] + "==")

    pub_key = load_pem_public_key(pub_pem.encode())
    assert isinstance(pub_key, Ed25519PublicKey)
    pub_key.verify(sig_bytes, canonical)   # 抛出 InvalidSignature 则测试失败


def test_emit_sync_without_signer_no_integrity_field(tmp_path: Path) -> None:
    """不传 signer 时，写出的 JSON 不含 integrity 字段（向后兼容）。"""
    log_file = tmp_path / "unsigned.jsonl"
    emitter = AuditEmitter(log_file, aic="aic")
    emitter.emit_sync(_make_body())

    data = json.loads(log_file.read_text().strip())
    assert "integrity" not in data


def test_emit_sync_integrity_not_in_signed_payload(tmp_path: Path) -> None:
    """integrity 字段本身不被纳入签名范围（签名只覆盖业务内容）。"""
    import jcs

    priv_pem, _ = _gen_ed25519_pem()
    signer = Ed25519AuditSigner(private_key_pem=priv_pem, kid="k")
    log_file = tmp_path / "signed.jsonl"
    emitter = AuditEmitter(log_file, aic="aic", signer=signer)
    emitter.emit_sync(_make_body())

    data = json.loads(log_file.read_text().strip())
    # 签名时的 payload 不含 integrity
    assert "integrity" in data
    payload_without_integrity = {k: v for k, v in data.items() if k != "integrity"}
    # 再次签名应得到相同结果（确认 integrity 未被包含进 JCS 输入）
    canonical = jcs.canonicalize(payload_without_integrity)
    sig2 = signer.sign(canonical)
    sig_b64 = base64.urlsafe_b64encode(sig2).rstrip(b"=").decode()
    assert sig_b64 == data["integrity"]["sig"]


# ─── load_signer_from_keys_json ───────────────────────────────────────────────

def test_load_signer_returns_none_when_file_missing(tmp_path: Path) -> None:
    """文件不存在时静默返回 None。"""
    result = load_signer_from_keys_json(tmp_path / "nonexistent.json", "1.2.3")
    assert result is None


def test_load_signer_returns_none_when_aic_not_found(tmp_path: Path) -> None:
    """AIC 不在文件中时静默返回 None。"""
    priv_pem, pub_pem = _gen_ed25519_pem()
    keys_file = tmp_path / "audit_keys.json"
    keys_file.write_text(json.dumps({
        "agent_a": {"aic": "1.2.3.A", "kid": "k-a", "public_key": pub_pem, "private_key": priv_pem}
    }))
    result = load_signer_from_keys_json(keys_file, "1.2.3.UNKNOWN")
    assert result is None


def test_load_signer_returns_signer_for_matching_aic(tmp_path: Path) -> None:
    """AIC 匹配时返回正确的 Ed25519AuditSigner。"""
    priv_pem, pub_pem = _gen_ed25519_pem()
    keys_file = tmp_path / "audit_keys.json"
    keys_file.write_text(json.dumps({
        "agent_a": {"aic": "1.2.3.A", "kid": "k-a", "public_key": pub_pem, "private_key": priv_pem}
    }))
    signer = load_signer_from_keys_json(keys_file, "1.2.3.A")
    assert signer is not None
    assert signer.kid == "k-a"
    assert signer.alg == "EdDSA"
