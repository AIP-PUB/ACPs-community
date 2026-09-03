# 开发模式 Audit 联合验证

**前置**：请先按 [dev-runbook.md](./dev-runbook.md) 完成服务启动（monitor-server、demo-leader、
demo-partner、Fluent Bit）。本文只讲 Audit 链路在开发模式下的验证内容。

链路中的**验签环节**有两种模式，本文均覆盖：

- **Mock 模式**（[第 2–3 章](#2-产生审计日志)）：纯本地，公钥来自本地文件，无需 ca-server，适合日常开发与 CI。
- **CA 联合验签模式**（[第 4 章](#4-进阶ca-联合验签ca-模式)）：以证书序列号向 ca-server 实时验签，面向集成测试 / staging / 生产。

建议首次阅读先跑通第 2–3 章的 Mock 模式基础链路，再按第 4 章升级到 CA 模式。

## 1. 链路概览

### 1.1 数据流与端口

```text
demo-leader / demo-partner
  └─ acps-sdk AuditEmitter
        │ 写入 NDJSON 审计日志
        ▼
  logs/amp_audit.jsonl             (demo-leader，一个文件)
  logs/amp_audit_*.jsonl           (demo-partner，每个 partner 一个文件)
        │
        │ Fluent Bit tail + JSON parse
        ▼
  Kafka  amp.audit  (localhost:19092, Redpanda)
        │
        │ monitor-server Kafka Consumer
        │ 验证签名 → 构建哈希链 → 幂等写入
        ▼
  PostgreSQL  agent_monitor.audit_records  (localhost:5432)
        │
        │ Query API
        ▼
  http://localhost:9009/acps-amp-v1/audit/...
```

涉及组件与端口：

| 组件 | 端口 | 说明 |
|------|------|------|
| acps-infra PostgreSQL | 5432 | 所有 server 的共享数据库 |
| acps-infra Redpanda (Kafka) | 19092 | 审计事件流（宿主机访问） |
| demo-leader API | 9031 | Leader 业务接口（触发审计日志） |
| demo-leader Web UI | 9030 | Leader 静态前端 |
| demo-partner | 9023+ | Partner 服务实例（触发审计日志） |
| monitor-server Query API | 9009 | 审计记录查询接口 |
| ca-server | 9003 | 证书公钥查询服务（**仅 CA 模式需要**） |

### 1.2 两种验签模式

验签发生在 monitor-server 的 Kafka Consumer 中，由 `config/development.toml`
的 `[atr].mock_mode` 一个开关决定：

| 特性 | Mock 模式（`mock_mode = true`，默认） | CA 模式（`mock_mode = false`） |
|------|-------------------------------|-------------------------------|
| 公钥来源 | `config/audit_keys.json`（本地文件） | ca-server HTTP API |
| `integrity.kid` | 任意字符串，需在 `audit_keys.json` 中有对应条目 | X.509 证书序列号（大写十六进制） |
| `integrity.alg` | 由 Agent 签名器决定：`"EdDSA"` 或 `"RS256"` | 由证书密钥类型决定 |
| 依赖服务 | 无（纯本地） | ca-server 在线 |
| 证书吊销感知 | 无 | 有（status 校验） |
| 适用场景 | 纯本地开发、CI 单元测试 | 集成测试、staging、生产 |

> 无论哪种模式，**审计记录都会正常入库**；验签结果只影响 `signature_verified` 与
> `verification_failure_type` 两个字段，审计数据永不因验签失败而丢弃。

## 2. 产生审计日志

### 2.1 通过 demo-leader API 触发

调用 demo-leader 的 `/api/v1/submit` 接口，触发一次完整业务请求。Leader 在处理请求的
关键节点（提交、意图分析、任务调度等）会自动通过 `acps-sdk AuditEmitter`
写入审计日志到 `demo-leader/logs/amp_audit.jsonl`。

> **注意**：`/api/v1/submit` 内部会调用 LLM 接口，在没有配置 LLM 的开发环境中会返回
> `LLM_CALL_ERROR`，但审计日志仍可能部分写入（取决于 Leader 的具体实现）。
> 若 LLM 不可用，可直接用 SDK 写入测试日志（见下方"备用方式"）。

```bash
curl -s -X POST http://localhost:9031/api/v1/submit \
  -H "Content-Type: application/json" \
  -d '{
    "clientRequestId": "audit-test-001",
    "query": "帮我查一下北京到上海的高铁",
    "mode": "direct_rpc"
  }' | python3 -m json.tool
```

### 2.2 备用：SDK 内联写入（无 LLM 环境）

**方式 A — Ed25519 签名（Mock 模式，推荐日常开发）**

```python
cd demo-leader
uv run python - <<'EOF'
import sys
sys.path.insert(0, '../acps-sdk')
from pathlib import Path
from acps_sdk.amp.emitter import AuditEmitter
from acps_sdk.amp.signer import load_signer_from_keys_json
from acps_sdk.amp.models import AuditBody, AuditActor, AuditAction, AuditTarget, AuditResult

aic = '1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ'
signer = load_signer_from_keys_json('../monitor-server/config/audit_keys.json', aic)
print(f'kid={signer.kid}, alg={signer.alg}')  # audit-key-demo_leader / EdDSA

emitter = AuditEmitter(Path('logs/amp_audit.jsonl'), aic=aic, signer=signer)
body = AuditBody(
    actor=AuditActor(id='user-e2e-test', type='user', name='E2E Test'),
    action=AuditAction(name='e2e.verify', type='test'),
    target=AuditTarget(type='audit_log', id='e2e-target-001'),
    result=AuditResult(status='success'),
)
print('emitted log_id:', emitter.emit_sync(body))
EOF
```

**方式 B — 证书签名（CA / Mock 模式，使用 client.pem）**

```python
cd demo-leader
uv run python - <<'EOF'
import sys
sys.path.insert(0, '../acps-sdk')
from pathlib import Path
from acps_sdk.amp.emitter import AuditEmitter
from acps_sdk.amp.signer import CertificateAuditSigner
from acps_sdk.amp.models import AuditBody, AuditActor, AuditAction, AuditTarget, AuditResult

signer = CertificateAuditSigner(
    private_key_pem=Path('leader/atr/client.key').read_text(),
    cert_pem=Path('leader/atr/client.pem').read_text(),
)
print(f'kid={signer.kid}, alg={signer.alg}')  # <cert serial> / RS256

emitter = AuditEmitter(Path('logs/amp_audit.jsonl'),
    aic='1.2.156.3088.1.1.SC64YN.Z5LSGY.1.0NMQ', signer=signer)
body = AuditBody(
    actor=AuditActor(id='user-e2e-test', type='user', name='E2E Test'),
    action=AuditAction(name='e2e.verify', type='test'),
    target=AuditTarget(type='audit_log', id='e2e-target-001'),
    result=AuditResult(status='success'),
)
print('emitted log_id:', emitter.emit_sync(body))
EOF
```

### 2.3 查看已写入的日志文件

```bash
tail -1 demo-leader/logs/amp_audit.jsonl | python3 -m json.tool

if ls demo-partner/logs/amp_audit_*.jsonl >/dev/null 2>&1; then
  for f in demo-partner/logs/amp_audit_*.jsonl; do
    echo "== ${f} =="
    tail -1 "${f}" | python3 -m json.tool
  done
fi
```

## 3. 验证各环节

### 3.1 验证 Kafka（Fluent Bit 已转发）

```bash
docker exec dev-redpanda rpk topic describe amp.audit -p
# 期望：HIGH-WATERMARK 应在每次触发业务后递增

HW=$(docker exec dev-redpanda rpk topic describe amp.audit -p | awk '$1=="0"{print $6}')
OFFSET=$((HW - 1))
docker exec dev-redpanda rpk topic consume amp.audit \
  --partitions=0 --offset="${OFFSET}" --num=1 \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(json.loads(d['value']), indent=2, ensure_ascii=False))"
```

### 3.2 验证 PostgreSQL（monitor-server 已入库）

```bash
psql postgresql://monitor:monitor@localhost:5432/agent_monitor \
  -c "SELECT log_id, action_name, signature_verified, committed_at
      FROM audit_records
      ORDER BY committed_at DESC LIMIT 5;"
```

期望：能看到与上一步 Kafka 消息对应的记录，`committed_at` 为最近时间。

> Mock 模式下，若未在 `MockKeyResolver` 中注入对应公钥，`signature_verified` 可能为 `false`
> 且 `verification_failure_type = missing_public_key`，这是预期行为（见 [§5 排查](#5-故障排查)）。

### 3.3 验证 Query API

```bash
# 查询最近 10 分钟内的所有审计记录（macOS date 语法）
START=$(date -u -v-10M +%Y-%m-%dT%H:%M:%SZ)
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
curl -s -X POST http://localhost:9009/acps-amp-v1/audit/records/query \
  -H "Content-Type: application/json" \
  -d "{
    \"timeRange\": {\"startAt\": \"$START\", \"endAt\": \"$END\"},
    \"page\": {\"limit\": 10}
  }" | python3 -m json.tool
```

期望：`items` 数组包含刚刚触发的审计记录，其中：

- `integrity.signatureVerified` 为 `true`（Mock 模式下需在 `audit_keys.json` 中有对应公钥）
- `integrity.signatureAlg`：Ed25519 密钥时为 `"EdDSA"`；X.509 RSA 证书时为 `"RS256"`

> **关于 `dataFreshnessAt`**：响应 `meta.dataFreshnessAt` 表示 monitor-server
> 已稳定消费并入库的最早事件时间（所有 Kafka 分区水位的最小值）。若某分区长时间
> 无新数据，该值可能看起来较旧，这是正常行为，不代表最新数据未入库。

也可以通过 OpenAPI 文档交互测试：`http://localhost:9009/docs`

### 3.4 全链路一键脚本

`scripts/demo_audit.sh` 在清空本地 audit 日志后，向 demo-leader 发起 `/api/v1/submit`，
等待 Fluent Bit + AuditWriter 传播，再按 `traceId` 断言 monitor Query API 记录数 ≥ 2。

```bash
# 任意当前目录均可（脚本自动解析 acps/ 根）；见 dev-runbook.md §1.2
bash monitor-server/scripts/demo_audit.sh
```

## 4. 进阶：CA 联合验签（CA 模式）

本章在第 2–3 章基础上，将验签从本地 Mock 公钥切换为**以证书序列号（`kid`）向 ca-server 实时查询公钥**，
实现生产级验签链路。

### 4.1 设计与数据流

```text
Agent 的 X.509 证书（由 acps-cli bootstrap 签发）
  │ 序列号 → kid（大写十六进制）
  │ 私钥签名 AMP 日志
  ▼
audit.jsonl  integrity.kid = 证书序列号, integrity.alg = EdDSA / RS256
  │ Fluent Bit → Kafka
  ▼
monitor-server CAKeyResolver
  │ GET /acps-atr-v2/ca/keys/{kid}
  ▼
ca-server（PostgreSQL 内的证书数据库）
  │ 返回 publicKey PEM + status(valid/revoked/expired)
  ▼
monitor-server 完成 JCS + 签名验证
  ▼
audit_records.signature_verified = true
```

### 4.2 证书前置检查与 ca-server 注册

#### 4.2.1 检查 Agent 证书

```bash
# demo-leader
ls -la demo-leader/leader/atr/client.pem demo-leader/leader/atr/client.key
openssl x509 -in demo-leader/leader/atr/client.pem -serial -noout
# serial=5FCB77CA23BBA4A402358721986C16C7626269B
openssl x509 -in demo-leader/leader/atr/client.pem -text -noout | grep "Public Key Algorithm"
# rsaEncryption → alg = "RS256"

# demo-partner
ls demo-partner/partners/online/china_hotel/client.pem
openssl x509 -in demo-partner/partners/online/china_hotel/client.pem -serial -noout
```

#### 4.2.2 验证 ca-server 已注册该证书

```bash
SERIAL=$(openssl x509 -in demo-leader/leader/atr/client.pem -serial -noout | cut -d= -f2 | sed 's/^0*//')
curl -s "http://localhost:9003/acps-atr-v2/ca/keys/${SERIAL}" | python3 -m json.tool
# 期望：status = "valid"，publicKey 非空
```

若返回 404（`CERTIFICATE_NOT_FOUND`），需重新引导（见 §4.2.3）。

#### 4.2.3 证书不在 ca-server 时：重新引导

通过 ACME 流程重新签发证书（会写入新的密钥对与证书；`integrity.kid` 变为新证书序列号）：

```bash
curl -s http://localhost:9001/health  # 确认 registry-server 在线

cd acps-cli
./scripts/bootstrap.sh demo-leader --install-dir ../demo-leader
./scripts/bootstrap.sh demo-partner --install-dir ../demo-partner
```

签发完成后，用 §4.2.2 再查一次 ca-server，确认 `status = "valid"`。

### 4.3 切换配置到 CA 模式

编辑 `monitor-server/config/development.toml`：

```toml
[atr]
mock_mode = false                      # false = 向 ca-server 实时查询公钥
ca_base_url = "http://localhost:9003"  # ca-server 地址
```

| 配置项 | 键 | 默认值 | 说明 |
|--------|-----|--------|------|
| CA 服务地址 | `[atr].ca_base_url` | `http://localhost:9003` | ca-server 根地址 |
| Mock 模式 | `[atr].mock_mode` | `true` | `false` 时使用真实 CA 查询 |

#### Agent 端（无需额外配置）

`demo-leader` 和 `demo-partner` 会**自动**优先选择 CA 证书模式：

- 若 `leader/atr/client.pem` + `leader/atr/client.key` 存在 → `CertificateAuditSigner`
- 否则 → `load_signer_from_keys_json`（mock 模式回退）

### 4.4 增量启动 ca-server

在 [dev-runbook.md](./dev-runbook.md) 各服务基础上，额外启动 ca-server 并使用 CA 模式配置：

```bash
# 启动 ca-server
(cd ca-server && just dev start)
curl http://localhost:9003/health  # 期望：{"status":"healthy"}

# 确认 Agent 证书在 ca-server（见 §4.2.2）

# 配置 monitor-server 使用 CA 模式（见 §4.3），然后重启
(cd monitor-server && just dev restart)
```

### 4.5 CA 模式验证

#### 4.5.1 Agent 端：确认使用证书序列号签名

```bash
tail -1 demo-leader/logs/amp_audit.jsonl | python3 -c "
import json, sys
r = json.load(sys.stdin)
print('kid:', r.get('integrity', {}).get('kid'))   # 证书序列号（大写十六进制）
print('alg:', r.get('integrity', {}).get('alg'))   # RS256 或 EdDSA
print('sig:', (r.get('integrity', {}).get('sig') or '')[:20] + '...')
"
```

#### 4.5.2 ca-server 能返回对应公钥

```bash
KID=$(tail -1 demo-leader/logs/amp_audit.jsonl | python3 -c "import json,sys; print(json.load(sys.stdin)['integrity']['kid'])")
curl -s "http://localhost:9003/acps-atr-v2/ca/keys/${KID}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('status:', d.get('status'))     # 应为 valid
print('publicKey (前60字符):', d.get('publicKey','')[:60])
"
```

#### 4.5.3 PostgreSQL 中 signature_verified = true

```bash
psql postgresql://monitor:monitor@localhost:5432/agent_monitor \
  -c "SELECT log_id, signature_verified, verification_failure_type, committed_at
      FROM audit_records
      ORDER BY committed_at DESC LIMIT 5;"
# 期望：signature_verified = t，verification_failure_type = NULL
```

#### 4.5.4 快速 E2E 验证脚本（不依赖 Kafka/Fluent-Bit）

```bash
cd monitor-server
APP_ENV=development uv run python scripts/smoke_audit.py
# [PASS] CA-based 审计日志签名 + 验签 E2E 验证通过！
```

### 4.6 证书吊销行为

当证书被 ca-server 吊销（`status = revoked` 或 `expired`）后：

1. `CAKeyResolver._fetch_from_ca()` 检查 `status` 字段，若非 `valid` 则抛出 `KeyNotFoundError`。
2. `AuditWriter` 捕获 `KeyNotFoundError`，设置 `verification_failure_type = "missing_public_key"`。
3. 记录仍然正常入库（审计数据永不丢弃），但 `signature_verified = false`。

## 5. 故障排查

### 5.1 Kafka 无消息

参见 [dev-runbook.md §5](./dev-runbook.md) 的通用排查。

确认 Audit topic 存在且水位增长：

```bash
docker exec dev-redpanda rpk topic describe amp.audit -p
```

确认消费组 lag → 0：

```bash
docker exec dev-redpanda rpk group describe amp.audit.writer
```

demo-partner 无日志文件时，确认实例在运行：

```bash
just -f demo-partner/Justfile app status
ls demo-partner/logs/
```

### 5.2 Mock 模式：signature_verified = false（missing_public_key）

**签名模式说明**：

- demo-leader / demo-partner 若存在 `client.pem` + `client.key`，会自动使用
  `CertificateAuditSigner`（`kid` = 证书序列号，`alg` = `"RS256"` 或 `"EdDSA"`）。
- 否则回退到 `load_signer_from_keys_json()`（`alg = "EdDSA"`，`kid` = 人工字符串）。

**Ed25519 签名（回退模式）**：运行脚本生成/更新 `audit_keys.json`：

```bash
cd monitor-server
uv run python scripts/gen_audit_keys.py
```

**RSA 证书签名（CertificateAuditSigner）**：手动将证书公钥添加到 `config/audit_keys.json`：

```bash
# 获取序列号和 SPKI 公钥
openssl x509 -in demo-leader/leader/atr/client.pem -serial -noout
openssl x509 -in demo-leader/leader/atr/client.pem -pubkey -noout
```

```json
{
  "demo_leader_cert": {
    "aic": "<demo-leader 的 AIC>",
    "kid": "<证书序列号，大写十六进制，去掉前导零>",
    "public_key": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n",
    "private_key": ""
  }
}
```

### 5.3 CA 模式：排查要点

**确认配置已生效**：

```bash
cd monitor-server && APP_ENV=development uv run python -c "
from app.core.config import settings
print('mock_mode:', settings.atr_mock_mode)
print('ca_base_url:', settings.atr_ca_base_url)
"
```

**常见原因**：

1. `mock_mode` 仍为 `true`（会走 MockKeyResolver，不查 ca-server）。
2. ca-server 未运行：`curl http://localhost:9003/health`。
3. 证书不在 ca-server：`curl http://localhost:9003/acps-atr-v2/ca/keys/{KID}`。
   若返回 404，执行 §4.2.3 导入步骤。
4. Agent 端未使用 `CertificateAuditSigner`（证书文件不存在）：
   ```bash
   ls demo-leader/leader/atr/client.pem demo-leader/leader/atr/client.key
   ```

**ca-server 不可达（ATRUnavailableError）**：monitor-server 在无法连接时不会丢失审计数据——
记录仍入库，`signature_verified = false`，`verification_failure_type = "missing_public_key"`。
恢复 ca-server 后，新到达的消息会自动重新验签（但已入库记录不会回填）。

**audit_records 无新记录**：确认消费组未积压，如有请查 monitor-server 日志：

```bash
just -f monitor-server/Justfile app logs
```
