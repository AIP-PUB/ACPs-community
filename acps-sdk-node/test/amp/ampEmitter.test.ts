/**
 * 对应 Python tests/test_amp_emitter.py：AuditLogRecord / AuditEmitter / Ed25519AuditSigner / loadSignerFromKeysJson。
 */

import { createPublicKey, generateKeyPairSync, verify } from "node:crypto";
import { existsSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  type AuditBodyInput,
  AuditEmitter,
  AuditLogRecord,
  AuditLogRecordSchema,
  Ed25519AuditSigner,
  loadSignerFromKeysJson,
} from "../../src/amp/index.js";
import { canonicalize } from "../../src/amp/jcs.js";
import { AUDIT_LOG_RECORD_DUMP, modelDump } from "../../src/amp/models.js";
import {
  UUID_RE,
  readLines,
  readSingleRecord,
  unwritablePath,
  useLogCapture,
  useTmpPath,
} from "./helpers.js";

// ─── 签名测试辅助 ─────────────────────────────────────────────────────────────

/** 生成一对 Ed25519 密钥，返回 [privatePem, publicPem]。 */
function genEd25519Pem(): [string, string] {
  const { privateKey, publicKey } = generateKeyPairSync("ed25519");
  return [
    privateKey.export({ format: "pem", type: "pkcs8" }).toString(),
    publicKey.export({ format: "pem", type: "spki" }).toString(),
  ];
}

// ─── 辅助工厂 ────────────────────────────────────────────────────────────────

function makeBody({
  actorId = "user-001",
  actorType = "human",
  actorName = null as string | null,
  actionName = "test_action",
  actionType = "test",
  targetType = "session",
  targetId = "sess-001",
  resultStatus = "success" as "success" | "failure" | "unknown",
  resultReason = null as string | null,
} = {}): AuditBodyInput {
  return {
    actor: { id: actorId, type: actorType, name: actorName },
    action: { name: actionName, type: actionType },
    target: { type: targetType, id: targetId },
    result: { status: resultStatus, reason: resultReason },
  };
}

describe("AuditLogRecord", () => {
  it("test_new_log_id_returns_valid_uuid", () => {
    expect(AuditLogRecord.newLogId()).toMatch(UUID_RE);
  });

  it("test_record_serialization_top_level_snake_case_body_nested", () => {
    const record = AuditLogRecordSchema.parse({
      log_id: AuditLogRecord.newLogId(),
      timestamp: "2026-06-10T08:00:00+08:00",
      aic: "test-aic",
      body: makeBody({ actorId: "u1", actorName: "Alice" }),
    });
    const data = JSON.parse(JSON.stringify(record)) as Record<string, unknown>;

    // 顶层：snake_case
    expect(data).toHaveProperty("schema_version");
    expect(data).toHaveProperty("log_id");
    expect(data).toHaveProperty("log_type");
    expect(data).not.toHaveProperty("trace_id"); // None → exclude_none 剔除
    expect(data).not.toHaveProperty("correlation_id");

    // body：嵌套结构
    const body = data["body"] as Record<string, Record<string, unknown>>;
    expect(Object.keys(body)).toEqual(["actor", "action", "target", "result"]);
    expect(body["actor"]).toEqual({ id: "u1", type: "human", name: "Alice" });
    expect(body["action"]).toEqual({ name: "test_action", type: "test" });
    expect(body["result"]?.["status"]).toBe("success");
    expect(body["result"]).not.toHaveProperty("reason"); // None → exclude_none 剔除
  });

  it("test_record_fixed_fields", () => {
    const record = AuditLogRecordSchema.parse({
      log_id: "x",
      timestamp: "2026-06-10T00:00:00+00:00",
      aic: "aic",
      body: makeBody(),
    });
    expect(record.schema_version).toBe("1.0");
    expect(record.log_type).toBe("audit");
  });
});

describe("AuditEmitter", () => {
  const tmpPath = useTmpPath();
  const logs = useLogCapture();

  it("test_emit_sync_writes_valid_record", () => {
    const logFile = join(tmpPath(), "amp_audit.jsonl");
    const emitter = new AuditEmitter(logFile, "1.2.3.test");

    const logId = emitter.emitSync(makeBody());

    expect(existsSync(logFile)).toBe(true);
    const lines = readLines(logFile);
    expect(lines).toHaveLength(1);
    const record = AuditLogRecordSchema.parse(JSON.parse(lines[0]!));
    expect(record.log_id).toBe(logId);
    expect(record.log_type).toBe("audit");
    expect(record.aic).toBe("1.2.3.test");
    expect(record.timestamp).toMatch(/\+00:00$/); // 含时区（isoformat 形式）
  });

  it("test_emit_sync_creates_parent_dirs", () => {
    const logFile = join(tmpPath(), "nested", "deep", "audit.jsonl");
    new AuditEmitter(logFile, "aic").emitSync(makeBody());
    expect(existsSync(logFile)).toBe(true);
  });

  it("test_emit_sync_appends_multiple_lines", () => {
    const logFile = join(tmpPath(), "audit.jsonl");
    const emitter = new AuditEmitter(logFile, "aic");
    emitter.emitSync(makeBody({ actionName: "first" }));
    emitter.emitSync(makeBody({ actionName: "second" }));
    const lines = readLines(logFile);
    expect(lines).toHaveLength(2);
    const actionName = (line: string): unknown =>
      (JSON.parse(line) as { body: { action: { name: string } } }).body.action.name;
    expect(actionName(lines[0]!)).toBe("first");
    expect(actionName(lines[1]!)).toBe("second");
  });

  it("test_emit_sync_failure_does_not_raise", () => {
    const emitter = new AuditEmitter(tmpPath(), "aic"); // tmpPath 是目录，写文件会失败
    expect(() => emitter.emitSync(makeBody())).not.toThrow(); // 应该静默吞掉异常
    expect(logs.some((r) => r.level === "warn" && r.message.includes("AuditEmitter"))).toBe(true);
  });

  it("test_emit_async_returns_same_log_id", async () => {
    const logFile = join(tmpPath(), "audit.jsonl");
    const emitter = new AuditEmitter(logFile, "aic");

    const logId = await emitter.emit(makeBody());

    expect(logId).toMatch(UUID_RE);
    const lines = readLines(logFile);
    expect(lines).toHaveLength(1);
    expect((JSON.parse(lines[0]!) as { log_id: string }).log_id).toBe(logId);
  });

  it("test_emit_sync_with_optional_fields", () => {
    const logFile = join(tmpPath(), "audit.jsonl");
    const emitter = new AuditEmitter(logFile, "aic");
    emitter.emitSync(makeBody(), { traceId: "sess-abc", correlationId: "task-xyz" });

    const data = readSingleRecord(logFile);
    expect(data["trace_id"]).toBe("sess-abc");
    expect(data["correlation_id"]).toBe("task-xyz");
  });

  it("test_audit_body_actor_user_agent_serialized_as_camel_case", () => {
    const logFile = join(tmpPath(), "audit.jsonl");
    const emitter = new AuditEmitter(logFile, "aic");
    emitter.emitSync({
      actor: { id: "u1", type: "human", userAgent: "curl/7.64.1" },
      action: { name: "login", type: "auth" },
      target: { type: "session", id: "sess-1" },
      result: { status: "success" },
    });

    const body = readSingleRecord(logFile)["body"] as Record<string, Record<string, unknown>>;
    const actor = body["actor"];
    expect(actor?.["userAgent"]).toBe("curl/7.64.1");
    expect(actor).not.toHaveProperty("user_agent");
  });

  it("write failure is logged and the log_id is still returned (unwritable parent)", async () => {
    const emitter = new AuditEmitter(unwritablePath(tmpPath(), "a.jsonl"), "aic");
    expect(emitter.emitSync(makeBody())).toMatch(UUID_RE);
    await expect(emitter.emit(makeBody())).resolves.toMatch(UUID_RE);
    expect(
      logs.filter((r) => r.level === "warn" && r.message === "AuditEmitter: 写入失败，跳过"),
    ).toHaveLength(2);
  });

  it("invalid body raises like pydantic ValidationError (not swallowed)", async () => {
    const emitter = new AuditEmitter(join(tmpPath(), "audit.jsonl"), "aic");
    const bad = { ...makeBody(), result: { status: "maybe" } } as unknown as AuditBodyInput;
    expect(() => emitter.emitSync(bad)).toThrow();
    await expect(emitter.emit(bad)).rejects.toThrow();
  });
});

describe("Ed25519AuditSigner", () => {
  it("test_ed25519_signer_alg_and_kid", () => {
    const [privPem] = genEd25519Pem();
    const signer = new Ed25519AuditSigner(privPem, "test-key-001");
    expect(signer.alg).toBe("EdDSA");
    expect(signer.kid).toBe("test-key-001");
  });

  it("test_ed25519_signer_produces_64_byte_signature", () => {
    const [privPem] = genEd25519Pem();
    const signer = new Ed25519AuditSigner(privPem, "k");
    expect(signer.sign(Buffer.from("hello world"))).toHaveLength(64);
  });

  it("test_ed25519_signer_rejects_rsa_key", () => {
    const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
    const rsaPem = privateKey.export({ format: "pem", type: "pkcs8" }).toString();
    expect(() => new Ed25519AuditSigner(rsaPem, "k")).toThrow(TypeError);
    expect(() => new Ed25519AuditSigner(rsaPem, "k")).toThrow(/Ed25519PrivateKey/);
  });
});

describe("AuditEmitter + signer", () => {
  const tmpPath = useTmpPath();
  const logs = useLogCapture();

  it("test_emit_sync_with_signer_writes_integrity_field", () => {
    const [privPem] = genEd25519Pem();
    const signer = new Ed25519AuditSigner(privPem, "audit-key-test");
    const logFile = join(tmpPath(), "signed.jsonl");
    new AuditEmitter(logFile, "1.2.3.test", { signer }).emitSync(makeBody());

    const data = readSingleRecord(logFile);
    expect(data).toHaveProperty("integrity");
    const integrity = data["integrity"] as Record<string, unknown>;
    expect(integrity["alg"]).toBe("EdDSA");
    expect(integrity["kid"]).toBe("audit-key-test");
    expect(typeof integrity["sig"]).toBe("string");
    expect((integrity["sig"] as string).length).toBeGreaterThan(0);
    // integrity 位于记录末尾（Python 在 model_dump 之后追加）
    expect(Object.keys(data).at(-1)).toBe("integrity");
  });

  it("test_emit_sync_with_signer_signature_is_verifiable", () => {
    const [privPem, pubPem] = genEd25519Pem();
    const signer = new Ed25519AuditSigner(privPem, "k");
    const logFile = join(tmpPath(), "signed.jsonl");
    new AuditEmitter(logFile, "test-aic", { signer }).emitSync(makeBody());

    const data = readSingleRecord(logFile);
    const integrity = data["integrity"] as { sig: string };
    delete data["integrity"];

    // 重新构造签名内容：去掉 integrity 后 JCS 规范化（与 writer.py 一致）
    const canonical = canonicalize(data);
    const sigBytes = Buffer.from(integrity.sig, "base64url");
    expect(verify(null, canonical, createPublicKey(pubPem), sigBytes)).toBe(true);
  });

  it("test_emit_sync_without_signer_no_integrity_field", () => {
    const logFile = join(tmpPath(), "unsigned.jsonl");
    new AuditEmitter(logFile, "aic").emitSync(makeBody());
    expect(readSingleRecord(logFile)).not.toHaveProperty("integrity");
  });

  it("test_emit_sync_integrity_not_in_signed_payload", () => {
    const [privPem] = genEd25519Pem();
    const signer = new Ed25519AuditSigner(privPem, "k");
    const logFile = join(tmpPath(), "signed.jsonl");
    new AuditEmitter(logFile, "aic", { signer }).emitSync(makeBody());

    const data = readSingleRecord(logFile);
    expect(data).toHaveProperty("integrity");
    const { integrity, ...payloadWithoutIntegrity } = data;
    // 再次签名应得到相同结果（确认 integrity 未被包含进 JCS 输入）
    const sig2 = Buffer.from(signer.sign(canonicalize(payloadWithoutIntegrity))).toString(
      "base64url",
    );
    expect(sig2).toBe((integrity as { sig: string }).sig);
  });

  it("signing failure degrades to an unsigned record with a warning", () => {
    const logFile = join(tmpPath(), "signed.jsonl");
    const failing = {
      kid: "k",
      alg: "EdDSA",
      sign(): Uint8Array {
        throw new Error("hsm offline");
      },
    };
    new AuditEmitter(logFile, "aic", { signer: failing }).emitSync(makeBody());
    expect(readSingleRecord(logFile)).not.toHaveProperty("integrity");
    const warning = logs.find(
      (r) => r.message === "AuditEmitter: 签名失败，记录将不含 integrity 字段",
    );
    expect(warning?.level).toBe("warn");
    expect((warning?.args[0] as Error).message).toBe("hsm offline");
  });

  it("a signer returning a non-Uint8Array degrades to an unsigned record (Python TypeError path)", () => {
    const logFile = join(tmpPath(), "signed.jsonl");
    const bogus = { kid: "k", alg: "EdDSA", sign: () => "not-bytes" as unknown as Uint8Array };
    new AuditEmitter(logFile, "aic", { signer: bogus }).emitSync(makeBody());
    expect(readSingleRecord(logFile)).not.toHaveProperty("integrity");
    const warning = logs.find((r) => r.message.startsWith("AuditEmitter: 签名失败"));
    expect(warning?.args[0]).toBeInstanceOf(TypeError);
  });

  it("the signed payload is exactly the model dump", () => {
    const [privPem, pubPem] = genEd25519Pem();
    const signer = new Ed25519AuditSigner(privPem, "k");
    const record = AuditLogRecordSchema.parse({
      log_id: "x",
      timestamp: "2026-06-10T00:00:00+00:00",
      aic: "aic",
      body: makeBody(),
    });
    const sig = signer.sign(canonicalize(modelDump(record, AUDIT_LOG_RECORD_DUMP)));
    const reparsed = JSON.parse(JSON.stringify(record)) as Record<string, unknown>;
    expect(verify(null, canonicalize(reparsed), createPublicKey(pubPem), sig)).toBe(true);
  });
});

describe("loadSignerFromKeysJson", () => {
  const tmpPath = useTmpPath();
  const logs = useLogCapture();

  function writeKeys(content: unknown): string {
    const keysFile = join(tmpPath(), "audit_keys.json");
    writeFileSync(keysFile, typeof content === "string" ? content : JSON.stringify(content));
    return keysFile;
  }

  it("test_load_signer_returns_none_when_file_missing", () => {
    expect(loadSignerFromKeysJson(join(tmpPath(), "nonexistent.json"), "1.2.3")).toBeNull();
    expect(logs.some((r) => r.message.includes("audit_keys.json 不存在"))).toBe(true);
  });

  it("test_load_signer_returns_none_when_aic_not_found", () => {
    const [privPem, pubPem] = genEd25519Pem();
    const keysFile = writeKeys({
      agent_a: { aic: "1.2.3.A", kid: "k-a", public_key: pubPem, private_key: privPem },
    });
    expect(loadSignerFromKeysJson(keysFile, "1.2.3.UNKNOWN")).toBeNull();
  });

  it("test_load_signer_returns_signer_for_matching_aic", () => {
    const [privPem, pubPem] = genEd25519Pem();
    const keysFile = writeKeys({
      agent_a: { aic: "1.2.3.A", kid: "k-a", public_key: pubPem, private_key: privPem },
    });
    const signer = loadSignerFromKeysJson(keysFile, "1.2.3.A");
    expect(signer).not.toBeNull();
    expect(signer?.kid).toBe("k-a");
    expect(signer?.alg).toBe("EdDSA");
  });

  it("invalid JSON or a broken matching entry returns null with a warning", () => {
    expect(loadSignerFromKeysJson(writeKeys("{not json"), "1.2.3.A")).toBeNull();
    expect(logs.some((r) => r.message.includes("读取 audit_keys.json 失败"))).toBe(true);

    const keysFile = writeKeys({
      broken: { aic: "1.2.3.A", kid: "k" },
      later: { aic: "1.2.3.A", kid: "k2", private_key: genEd25519Pem()[0] },
    });
    // 第一个 aic 匹配的条目即为结果，构造失败也不再继续查找（同 Python）
    expect(loadSignerFromKeysJson(keysFile, "1.2.3.A")).toBeNull();
    expect(logs.some((r) => r.message.includes("构造 Ed25519AuditSigner 失败"))).toBe(true);
  });

  it("non-object JSON raises like Python's AttributeError", () => {
    expect(() => loadSignerFromKeysJson(writeKeys([1, 2]), "x")).toThrow(TypeError);
    expect(() => loadSignerFromKeysJson(writeKeys({ a: "str" }), "x")).toThrow(TypeError);
  });
});
