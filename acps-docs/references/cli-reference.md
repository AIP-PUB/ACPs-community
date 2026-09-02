# acps-cli 命令行使用说明

本文档是 `acps-cli` 的完整命令行参考文档。

## 1. 使用约定

### 1.1 执行方式

- 开发环境常用写法：`uv run acps-cli ...`
- 已安装到虚拟环境或系统环境后可直接执行：`acps-cli ...`

### 1.2 配置文件加载顺序

根命令支持两个公共选项：

- `--config PATH`：显式指定配置文件路径
- `--verbose`：开启更详细的 CLI 日志输出

CLI 会按如下顺序查找配置文件：

1. `--config PATH` 显式指定的文件
2. 当前目录下的 `acps-cli.toml`
3. 用户目录下的 `~/.acps-cli.toml`

找到配置文件后，CLI 会额外加载该配置文件同目录下的 `.env`，且不会覆盖当前 shell 中已经存在的环境变量。如果完全找不到配置文件，CLI 会调用 `python-dotenv` 的默认 `.env` 查找逻辑并返回空 TOML 配置；依赖服务地址、证书路径或 token 文件的命令通常仍需要通过参数或环境显式补齐。

配置值的一般优先级为：命令行选项 > 环境变量或 `.env` > TOML 配置 > 代码默认值。统一入口会拒绝部分旧配置名和旧环境变量，例如 `[registry].server_base_url`、`REGISTRY_SERVER_BASE_URL`、`[ca].server_base_url`、`CA_SERVER_BASE_URL`、`[discovery].server_base_url`、`DISCOVERY_SERVER_BASE_URL`，请使用下文列出的新版名称。

### 1.3 输出与参数风格

- 大部分面向 API 的命令支持 `--json`，用于输出机器可读 JSON。
- Discovery 查询与多数 CA 管理查询命令本身就输出 JSON；这类命令不一定额外提供 `--json`。
- Registry 相关命令普遍支持 `--server-url`，用于临时覆盖主服务地址；`acps-cli entity` 额外支持 `--mtls-url`，用于覆盖 `9002` mTLS 平面地址。
- CA 和 Discovery 相关命令支持 `--server-url`，用于临时覆盖服务地址。
- MQ 管理命令在 `acps-cli admin mq` 组上提供 `--group-api-url` 和 `--auth-api-url`，用于临时覆盖两个服务地址；部分子命令仍支持 `--cert-file` 和 `--key-file` 覆盖证书材料。
- 重复参数会在下文标注为“可重复”。这类参数可以多次传入。
- 成对布尔开关会写成 `--foo/--no-foo`，下文会注明默认状态。

### 1.4 选项位置

组级选项（例如 `--config`、`--verbose`、`--server-url`、`--mtls-url`）可以写在对应子命令前面或后面，CLI 按选项所属的命令解析，不要求固定顺序。下面两种写法等价：

```text
acps-cli entity --mtls-url https://registry.example:8443 derive --ontology-aic <AIC>
acps-cli entity derive --mtls-url https://registry.example:8443 --ontology-aic <AIC>
```

同一选项名被多层命令同时声明时，写在子命令前面的归当前组，写在该子命令后面的归更深层命令。例如 `cert --server-url` 覆盖 CA 地址，`cert eab --server-url` 覆盖 Registry 地址：

```text
acps-cli cert --server-url https://ca.example eab fetch --server-url https://registry.example --aic <AIC> --output eab.json
```

查看帮助时，把 `--help` 放在要查看的那一级命令后面：`acps-cli entity --help` 显示 `entity` 组选项，`acps-cli entity derive --help` 显示 `derive` 的选项。

## 2. 配置节速览

默认配置样例位于项目根目录的 `acps-cli.toml`，主要分为以下几个配置节：

- `[registry]`：Registry 基础地址、`9002` mTLS 平面地址、本体证书目录、服务端 CA 文件、请求超时等
- `[auth]`：普通用户和管理员 token 文件路径
- `[ca]`：CA 服务地址、账户密钥目录、私钥目录、证书目录、CSR 目录、trust bundle 路径
- `[discovery]`：Discovery 服务地址
- `[monitor]`：monitor-server 地址、AMP Query API 前缀、HTTP 超时
- `[mq]`：mq-auth-server 的 Group API/Auth API 地址、Leader 证书、probe 证书、服务端 CA 文件、超时

建议在开始使用前先检查：

- `[registry].base_url`
- `[registry].mtls_base_url`
- `[ca].base_url`
- `[discovery].base_url`
- `[monitor].base_url`
- `[monitor].api_prefix`
- `[monitor].timeout_seconds`
- `[mq].group_api_url`
- `[mq].auth_api_url`

### 2.1 常用环境变量覆盖

以下环境变量会覆盖 TOML 中的同名配置，命令行选项仍具有最高优先级：

| 域 | 环境变量 |
| -- | -------- |
| Registry | `REGISTRY_BASE_URL`, `REGISTRY_MTLS_BASE_URL`, `REGISTRY_TIMEOUT_SECONDS`, `REGISTRY_ONTOLOGY_MTLS_MATERIALS_DIR`, `REGISTRY_MTLS_SERVER_CA_FILE` |
| Registry 认证 | `AUTH_USER_TOKEN_FILE`, `AUTH_ADMIN_TOKEN_FILE`, `REGISTRY_USER_USERNAME`, `REGISTRY_USER_PASSWORD`, `REGISTRY_USER_NAME`, `REGISTRY_USER_ORG_NAME`, `REGISTRY_ADMIN_USERNAME`, `REGISTRY_ADMIN_PASSWORD` |
| CA | `CA_BASE_URL`, `CA_SERVER_ADMIN_API_TOKEN`, `CA_ACCOUNT_KEYS_DIR`, `CA_PRIVATE_KEYS_DIR`, `CA_CERTS_DIR`, `CA_CSR_DIR`, `CA_TRUST_BUNDLE_PATH` |
| Discovery | `DISCOVERY_BASE_URL` |
| Monitor | `MONITOR_BASE_URL`, `MONITOR_API_PREFIX`, `MONITOR_TIMEOUT_SECONDS` |
| MQ | `MQ_GROUP_API_URL`, `MQ_AUTH_API_URL`, `MQ_GROUP_CERT_FILE`, `MQ_GROUP_KEY_FILE`, `MQ_PROBE_CERT_FILE`, `MQ_PROBE_KEY_FILE`, `MQ_CA_FILE`, `MQ_TIMEOUT_SECONDS` |

## 3. 完整命令树

```text
acps-cli
├── auth
│   ├── login
│   ├── logout
│   ├── change-password
│   └── whoami
├── agent
│   ├── list
│   ├── save
│   ├── submit
│   ├── check
│   ├── sync
│   └── delete
├── entity
│   └── derive
├── cert
│   ├── eab
│   │   └── fetch
│   ├── issue
│   ├── renew
│   ├── revoke
│   ├── status
│   ├── account-key
│   │   └── rollover
│   ├── trust-bundle
│   │   └── update
│   ├── crl
│   │   ├── download
│   │   ├── info
│   │   └── detail
│   └── ocsp
│       ├── check
│       └── cert-status
├── discover
│   ├── status
│   └── query
├── monitor
│   ├── status
│   ├── heartbeat
│   │   ├── summary
│   │   ├── liveness
│   │   └── query
│   ├── metrics
│   │   ├── snapshots
│   │   ├── series
│   │   └── rankings
│   ├── access
│   │   ├── events
│   │   ├── operations
│   │   ├── traces
│   │   ├── trace
│   │   ├── slow
│   │   ├── errors
│   │   └── topology
│   ├── message
│   │   ├── events
│   │   ├── lifecycles
│   │   ├── lifecycle
│   │   ├── deadletters
│   │   ├── destinations
│   │   └── throughput
│   ├── system
│   │   └── events
│   └── audit
│       ├── records
│       ├── record
│       ├── anchors
│       ├── verify
│       └── verify-task
└── admin
    ├── auth
    │   ├── login
    │   ├── logout
    │   ├── change-password
    │   └── whoami
    ├── registry
    │   ├── review
    │   │   ├── list
    │   │   ├── approve
    │   │   └── reject
    │   ├── agent
    │       ├── disable
    │       └── enable
    │   └── user
    │       └── reset-password
    ├── ca
    │   ├── crl
    │   │   ├── list
    │   │   └── refresh
    │   └── ocsp
    │       ├── responder-info
    │       └── stats
    ├── discovery
    │   ├── run-sync
    │   └── dsp
    │       ├── status
    │       ├── registry-info
    │       ├── sync
    │       ├── start
    │       ├── stop
    │       ├── reset
    │       ├── hard-reset
    │       └── register-webhook
    └── mq
        ├── health
        ├── group
        │   ├── add-member
        │   ├── remove-member
        │   ├── delete
        │   └── kick
        └── auth-probe
            ├── user
            ├── vhost
            ├── resource
            └── topic
```

## 4. Registry 用户侧命令

### 4.1 共享组选项

以下命令组都继承根级 `--config`、`--verbose`，并额外提供 Registry 组选项：

- `acps-cli auth`
- `acps-cli agent`
- `acps-cli entity`
- `acps-cli cert eab`

Registry 共享组选项如下：

- `--server-url`：覆盖 Registry 服务基础地址

其中 `acps-cli entity` 额外支持：

- `--mtls-url`：覆盖 Registry `9002` mTLS 服务地址

### acps-cli auth login

用途：用户登录；若本地不存在账号，底层逻辑可结合参数自动注册。

参数：

- `--username`：用户名，未提供时通常会进入交互输入
- `--password`：密码，未提供时通常会进入交互输入
- `--name`：自动注册时的显示名称
- `--org-name`：自动注册时的组织名称
- `--json`：以 JSON 输出结果

### acps-cli auth whoami

用途：查看当前 Registry 用户身份。

参数：

- `--json`：以 JSON 输出结果

### acps-cli auth logout

用途：注销当前 Registry 用户，并清理本地保存的用户 token。

参数：

- `--json`：以 JSON 输出结果

### acps-cli auth change-password

用途：交互式修改当前登录用户密码。

参数：

- `--json`：以 JSON 输出结果

说明：命令会依次提示输入当前密码、新密码，并要求再次确认新密码。成功后会额外提示“密码已修改，建议重新登录”。

### acps-cli agent list

用途：列出当前用户名下的 Agent 草稿或已提交记录。

参数：

- `--page`：页码，默认 `1`
- `--page-size`：每页条数，默认 `20`
- `--status`：按状态过滤，可重复传入
- `--json`：以 JSON 输出结果

### acps-cli agent save

用途：创建或更新 Agent 草稿。

参数：

- `--logo-url`：Agent logo 地址
- `--acs-file`：必填，ACS JSON 文件路径
- `--ontology/--no-ontology`：是否将该 Agent 标记为 ontology，默认 `--no-ontology`
- `--json`：以 JSON 输出结果

### acps-cli agent submit

用途：提交 Agent 草稿进入审核流程。

参数：

- `--agent-id`：必填，待提交草稿的 Agent UUID
- `--json`：以 JSON 输出结果

### acps-cli agent check

用途：根据本地 ACS 文件检查对应 Agent 的审核状态。

参数：

- `--acs-file`：必填，ACS JSON 文件路径
- `--json`：以 JSON 输出结果

### acps-cli agent sync

用途：把服务端最新 ACS 状态同步回本地文件。

参数：

- `--acs-file`：必填，ACS JSON 文件路径
- `--json`：以 JSON 输出结果

### acps-cli agent delete

用途：删除当前用户拥有的 Agent 草稿。

参数：

- `--acs-file`：必填，ACS JSON 文件路径
- `--json`：以 JSON 输出结果

### acps-cli entity derive

用途：基于已经审核通过的本体（ontology）AIC 派生并注册实体。

参数：

- `--mtls-url`：覆盖 Registry `9002` mTLS 服务地址。该选项定义在 `entity` 组上，可以写在 `derive` 前面或后面
- `--ontology-aic`：必填，已审核通过的本体（ontology）AIC
- `--payload-file`：可选，派生实体 payload 的 UTF-8 JSON 文件路径
- `--mtls-cert-file`：覆盖本体 mTLS 证书路径
- `--mtls-key-file`：覆盖本体 mTLS 私钥路径
- `--mtls-server-ca-file`：覆盖用于校验 Registry `9002` 服务端证书的 CA 文件
- `--json`：以 JSON 输出结果

说明：该命令依赖 Registry 的 mTLS 平面，通常需要 `[registry].mtls_base_url` 和对应证书材料都已正确配置。

`--payload-file` 可省略。若提供，必须是 JSON 对象，允许的字段为：

- `endPoints`：实体自己的服务端点列表。省略时沿用本体端点
- `entityUserId`：绑定终端用户 ID
- `entityMeta`：实体自定义元数据
- `certificate`：实体证书签发配置，写入实体 ACS，供后续申请服务端证书时读取 SAN 和期望有效期。不继承本体的 `certificate`

`ontologyAic` 不写在文件里，由 `--ontology-aic` 传入。其它字段会被拒绝。

示例：

```json
{
  "entityUserId": "user-001",
  "entityMeta": { "scenario": "production" },
  "endPoints": [
    {
      "url": "https://entity.example.com/callback",
      "transport": "JSONRPC",
      "security": []
    }
  ],
  "certificate": {
    "altNames": {
      "dns": ["entity.example.com"],
      "ip": ["10.0.1.50"]
    },
    "requestedValidity": 365
  }
}
```

### acps-cli cert eab fetch

用途：为后续证书申请获取 EAB 凭证。

参数：

- `--aic`：必填，目标 Agent AIC
- `--output`：必填，EAB JSON 输出路径
- `--json`：以 JSON 输出结果

说明：命令路径位于 `cert eab` 下，但实际访问的是 Registry 能力。

注意：`cert` 组自身的 `--server-url` 覆盖 CA 服务地址；EAB 使用 Registry 地址，因此 Registry 覆盖项应写在 `cert eab` 之后，例如 `acps-cli cert eab --server-url http://localhost:9001 fetch ...`。

## 5. CA 用户侧命令

### 5.1 共享组选项

以下命令组都继承根级 `--config`、`--verbose`，并额外提供：

- `acps-cli cert --server-url`

含义：覆盖 CA 服务基础地址。

### acps-cli cert issue

用途：为 Agent 申请新证书。

参数：

- `--aic, -a`：必填，Agent Identity Code
- `--eab-file`：必填，EAB JSON 文件路径
- `--usage, -u`：必填，证书用途，取值 `clientAuth` 或 `serverAuth`
- `--key-type, -k`：密钥类型，取值 `ec` 或 `rsa`，默认 `ec`
- `--reuse-key`：若已存在本地私钥则复用
- `--key-path`：输出私钥路径
- `--cert-path`：输出证书链路径
- `--trust-bundle-path`：输出 trust bundle 路径

说明：`--eab-file` 指向的 JSON 必须包含 `keyId`、`macKey` 和与 `--aic` 一致的 `aic` 字段。证书签发成功后会同时更新 trust bundle。

### acps-cli cert renew

用途：续期现有证书。

参数：

- `--aic, -a`：必填，Agent Identity Code
- `--eab-file`：必填，EAB JSON 文件路径
- `--usage, -u`：必填，证书用途，取值 `clientAuth` 或 `serverAuth`
- `--force, -f`：强制续期，即使证书还未接近过期
- `--key-path`：输出私钥路径
- `--cert-path`：输出证书链路径
- `--trust-bundle-path`：输出 trust bundle 路径

说明：未加 `--force` 时，若本地证书仍有超过 30 天有效期，命令会拒绝续期。续期会复用本地 Agent 私钥。

### acps-cli cert revoke

用途：吊销证书。

参数：

- `--aic, -a`：必填，Agent Identity Code
- `--reason, -r`：吊销原因，默认 `unspecified`

说明：当前实现识别 `unspecified`、`keyCompromise`、`cACompromise`、`affiliationChanged`、`superseded`、`cessationOfOperation`；其它字符串会按 `unspecified` 处理。

### acps-cli cert status

用途：查询 Agent 当前证书状态。

参数：

- `--aic, -a`：必填，Agent Identity Code
- `--cert-path`：本地证书文件路径
- `--check-ocsp/--no-check-ocsp`：是否执行 OCSP 检查，默认开启，即 `--check-ocsp`

### acps-cli cert account-key rollover

用途：轮转 ACME account key。

参数：

- `--aic, -a`：必填，Agent Identity Code
- `--new-key, -n`：新 key 文件路径；可指向预生成 key，也可作为自动生成输出路径
- `--key-type, -k`：自动生成时的 key 类型，取值 `ec` 或 `rsa`，默认 `ec`
- `--backup/--no-backup`：是否先备份旧账户密钥，默认开启，即 `--backup`

说明：如果 `--new-key` 指向已存在文件，命令会读取该预生成密钥；如果路径不存在，则会生成新密钥并在轮转成功后写入该路径。未提供 `--new-key` 时只更新当前 AIC 的 account key 文件。

### acps-cli cert trust-bundle update

用途：更新本地 trust bundle 文件。

参数：

- `--output, -o`：输出路径

### acps-cli cert crl download

用途：下载 CRL 文件。

参数：

- `--output, -o`：输出文件路径
- `--format, -f`：CRL 格式，取值 `der` 或 `pem`，默认 `der`
- `--version`：下载历史 CRL 版本，仅适用于 DER 下载路径

说明：未提供 `--output` 时，当前 DER CRL 默认保存到 CA 证书目录的 `ca.crl`，PEM 保存到 `ca.pem`，历史版本保存到 `ca-<version>.crl`。

### acps-cli cert crl info

用途：查看当前 CRL 元数据。

参数：无专属参数。

### acps-cli cert crl detail

用途：查看当前 CRL 中的吊销条目详情。

参数：无专属参数。

### acps-cli cert ocsp check

用途：通过 OCSP 检查证书状态。

参数：

- `--aic, -a`：Agent Identity Code
- `--cert, -c`：证书文件路径
- `--issuer, -i`：签发者证书或 trust bundle 文件路径
- `--request-method`：OCSP 请求方法，取值 `post` 或 `get`，默认 `post`
- `--json`：以 JSON 输出结果

说明：常见用法是传入 AIC，或直接传入证书文件进行检查。

### acps-cli cert ocsp cert-status

用途：调用简化 OCSP 状态查询端点。

参数：

- `--serial-number`：证书序列号
- `--aic`：Agent Identity Code
- `--cert-path`：用于提取序列号的证书文件路径

说明：三种输入方式可按实际环境任选其一。

## 6. Discovery 用户侧命令

### 6.1 共享组选项

`acps-cli discover` 继承根级 `--config`、`--verbose`，并额外提供：

- `--server-url`：覆盖 Discovery 服务基础地址

### acps-cli discover status

用途：检查 Discovery 服务状态。

参数：无专属参数。

### acps-cli discover query

用途：执行 Discovery 查询，既支持自然语言查询，也支持结构化请求体。

参数：

- `QUERY_STR`：可选位置参数，自然语言查询文本
- `--type`：显式指定 Discovery 请求类型；未提供且请求体中没有 `type` 时默认 `explicit`
- `--limit`：最大返回条数，范围 `1-50`；未提供且请求体中没有 `limit` 时默认 `5`
- `--request-json`：内联 DiscoveryRequest JSON；不能与 `--request-file` 同时使用
- `--request-file`：DiscoveryRequest JSON 文件路径；不能与 `--request-json` 同时使用
- `--filter-json`：内联 DiscoveryFilter JSON；不能与 `--filter-file` 同时使用
- `--filter-file`：DiscoveryFilter JSON 文件路径；不能与 `--filter-json` 同时使用
- `--context-json`：内联查询上下文 JSON；不能与 `--context-file` 同时使用
- `--context-file`：查询上下文 JSON 文件路径；不能与 `--context-json` 同时使用
- `--forward-depth-limit`：转发深度限制，范围 `1-5`
- `--forward-fanout-limit`：转发扇出限制，范围 `1-5`
- `--forward-fanout-remaining`：剩余扇出额度，范围 `0-5`
- `--forward-chain`：追加 `forwardChain`，可重复
- `--forward-trusted-server`：追加 `forwardTrustedServers`，可重复
- `--forward-signature`：追加 `forwardSignatures`，可重复
- `--forward-each-timeout-ms`：单跳转发超时（毫秒），必须大于等于 `1`
- `--forward-total-timeout-ms`：总转发超时（毫秒），必须大于等于 `1`

说明：

- 简单查询时可以只传 `QUERY_STR`
- 需要精确控制请求结构时，优先使用 `--request-json` 或 `--request-file`
- 使用 `--request-json` 或 `--request-file` 时，命令行中的 `QUERY_STR`、`--limit`、forward 等选项会覆盖请求体中的同名字段
- Forward 相关参数主要用于多跳转发和链路测试场景

## 7. Monitor 用户侧命令

### 7.1 共享组选项

`acps-cli monitor` 继承根级 `--config`、`--verbose`，并额外提供：

- `--server-url`：覆盖 monitor-server 根地址
- `--api-prefix`：覆盖 AMP Query API 前缀，默认是 `/acps-amp-v1`

默认配置节如下：

```toml
[monitor]
base_url = "http://localhost:9009"
api_prefix = "/acps-amp-v1"
timeout_seconds = 15
```

说明：

- `monitor` 配置优先级遵循：命令行选项 > `MONITOR_*` 环境变量 > `[monitor]` > 默认值
- `monitor status` 访问 `GET /health`，不会拼接 `api_prefix`
- 其余业务命令访问 `{base_url}{api_prefix}/...`
- `monitor` 查询命令默认输出 pretty JSON
- 所有 POST 查询命令都支持 `--request-json` 或 `--request-file`
- `--request-json` 与 `--request-file` 互斥，内容必须是 JSON object
- 当同时提供 request 与快捷参数时，CLI 会用快捷参数覆盖请求体中的同名标量字段、`timeRange` 和 `page`
- 快捷 filter 只会合并到 `logic=and` 且不含 `groups` 的简单 `filter`；复杂过滤请直接写入完整 request
- 一旦提供完整 request，CLI 不再强制要求那些只用于“快捷构造 payload”的本地必填参数
- payload-only 命令不会自动构造空请求体；必须显式提供 request
- 本命令组只负责 Query API 查询与排障，不包含 smoke、Kafka 写入或 AMP LogRecord 构造；写入链路验证仍使用 `monitor-server/scripts/smoke_*.py`

### acps-cli monitor status

用途：检查 monitor-server 健康状态。

参数：无专属参数。

### acps-cli monitor heartbeat

用途：查询 Heartbeat 读模型。

子命令：

- `summary`：查询全局 Heartbeat 汇总
- `liveness <AIC>`：点查单个 AIC 的 liveness
- `query`：批量查询 liveness 快照

`heartbeat query` 参数：

- `--aic`：追加 `aic eq` 过滤
- `--limit`：写入 `page.limit`
- `--cursor`：写入 `page.cursor`
- `--request-json` / `--request-file`：提交完整请求体

说明：

- 未提供 request 时，`heartbeat query` 必须提供 `--aic`
- Heartbeat liveness query 不支持快捷 `--start` / `--end` 时间范围构造

### acps-cli monitor metrics

用途：查询 Metrics 读模型。

子命令：

- `snapshots`：查询最新指标快照
- `series`：查询指标时序
- `rankings`：查询指标排行

`metrics snapshots` 参数：

- `--aic`：追加 `aic eq` 过滤
- `--limit` / `--cursor`：写入 `page`
- `--request-json` / `--request-file`：提交完整请求体

`metrics series` 参数：

- `--metric`：写入顶层 `metric`
- `--aic`：追加 `aic eq` 过滤
- `--start`、`--end`：写入 `timeRange.startAt` / `timeRange.endAt`
- `--step`：写入顶层 `step`
- `--request-json` / `--request-file`：提交完整请求体

`metrics rankings` 参数：

- `--metric`：写入顶层 `metric`
- `--start`、`--end`：写入 `timeRange`
- `--top-n`：写入顶层 `topN`
- `--request-json` / `--request-file`：提交完整请求体

说明：

- 未提供 request 时，`metrics series` 和 `metrics rankings` 都必须提供 `--metric`、`--start`、`--end`
- 如果服务端未启用 Analytics Profile，`rankings` 会直接透出服务端返回的 `404/422/503`

### acps-cli monitor access

用途：查询 Access 事件、Trace 和分析读模型。

子命令：

- `events`
- `operations`
- `traces`
- `trace <TRACE_ID>`
- `slow`
- `errors`
- `topology`

`access events` 参数：

- `--aic`：追加 `aic eq` 过滤
- `--trace-id`：追加 `traceId eq` 过滤
- `--start`、`--end`：写入 `timeRange`
- `--limit` / `--cursor`：写入 `page`
- `--request-json` / `--request-file`：提交完整请求体

`access trace <TRACE_ID>` 参数：

- `--include-events`：映射为 GET 查询参数 `include_events=true`

payload-only 子命令：

- `operations`
- `traces`
- `slow`
- `errors`
- `topology`

说明：

- 未提供 request 时，`access events` 必须提供 `--start` 和 `--end`
- 上述 payload-only 子命令必须显式提供 `--request-json` 或 `--request-file`

### acps-cli monitor message

用途：查询 Message 事件、生命周期、死信和目的地读模型。

子命令：

- `events`
- `lifecycles`
- `lifecycle <MESSAGE_ID>`
- `deadletters`
- `destinations`
- `throughput`

`message events` 参数：

- `--message-id`：追加 `messageId eq` 过滤
- `--trace-id`：追加 `traceId eq` 过滤
- `--start`、`--end`：写入 `timeRange`
- `--limit` / `--cursor`：写入 `page`
- `--request-json` / `--request-file`：提交完整请求体

`message lifecycle <MESSAGE_ID>` 参数：

- `--system`：映射为查询参数 `system`
- `--destination-name`：映射为查询参数 `destinationName`
- `--destination-kind`：映射为查询参数 `destinationKind`
- `--virtual-host`：映射为查询参数 `virtualHost`

payload-only 子命令：

- `lifecycles`
- `deadletters`
- `destinations`
- `throughput`

说明：

- 未提供 request 时，`message events` 必须提供 `--start` 和 `--end`
- 上述 payload-only 子命令必须显式提供 `--request-json` 或 `--request-file`
- Reliability / Destination Profile 是否启用由服务端决定，CLI 只负责请求与错误透传

### acps-cli monitor system

用途：查询 System 日志事件。

子命令：

- `events`

`system events` 参数：

- `--aic`：追加 `aic eq` 过滤
- `--correlation-id`：追加 `correlationId eq` 过滤
- `--severity-min`：追加 `severityNumber gte` 过滤
- `--start`、`--end`：写入 `timeRange`
- `--limit` / `--cursor`：写入 `page`
- `--request-json` / `--request-file`：提交完整请求体

说明：

- 未提供 request 时，`system events` 必须提供 `--start` 和 `--end`

### acps-cli monitor audit

用途：查询 Audit 记录、链锚点和完整性校验任务。

子命令：

- `records`
- `record <AUDIT_ID>`
- `anchors`
- `verify`
- `verify-task <TASK_ID>`

`audit records` 参数：

- `--aic`：追加 `aic eq` 过滤
- `--keyword`：写入顶层 `keyword`
- `--start`、`--end`：写入 `timeRange`
- `--limit` / `--cursor`：写入 `page`
- `--request-json` / `--request-file`：提交完整请求体

`audit anchors` 参数：

- `--chain-id`：映射为 GET 查询参数 `chain_id`

`audit verify` 参数：

- `--request-json` / `--request-file`：提交完整校验请求体

说明：

- 未提供 request 时，`audit records` 必须提供 `--start` 和 `--end`
- `audit verify` 是 payload-only 命令，必须显式提供 request
- `audit verify` 只提交服务端校验任务，不在本地做签名、公钥解析或哈希链计算

## 8. Registry 管理侧命令

### 8.1 共享组选项

以下管理命令组继承根级 `--config`、`--verbose`，并额外提供：

- `acps-cli admin auth`
- `acps-cli admin registry`

共享组选项：

- `--server-url`：覆盖 Registry 服务基础地址

### acps-cli admin auth login

用途：Registry 管理员登录。

参数：

- `--username`：管理员用户名
- `--password`：管理员密码
- `--json`：以 JSON 输出结果

### acps-cli admin auth whoami

用途：查看当前 Registry 管理员身份。

参数：

- `--json`：以 JSON 输出结果

### acps-cli admin auth logout

用途：注销当前 Registry 管理员，并清理本地保存的管理员 token。

参数：

- `--json`：以 JSON 输出结果

### acps-cli admin auth change-password

用途：交互式修改当前登录管理员密码。

参数：

- `--json`：以 JSON 输出结果

说明：命令会依次提示输入当前密码、新密码，并要求再次确认新密码。成功后会额外提示“密码已修改，建议重新登录”。

### acps-cli admin registry review list

用途：列出待审核或指定状态的 Agent 审核单。

参数：

- `--page`：页码，默认 `1`
- `--page-size`：每页条数，默认 `20`
- `--status`：按状态过滤，可重复
- `--json`：以 JSON 输出结果

### acps-cli admin registry review approve

用途：批准指定 Agent 的审核。

参数：

- `--agent-id`：必填，Agent UUID
- `--comments`：可选审核备注
- `--json`：以 JSON 输出结果

### acps-cli admin registry review reject

用途：拒绝指定 Agent 的审核。

参数：

- `--agent-id`：必填，Agent UUID
- `--comments`：必填，拒绝原因
- `--json`：以 JSON 输出结果

### acps-cli admin registry agent disable

用途：禁用已存在的 Agent。

参数：

- `--agent-id`：必填，Agent UUID
- `--reason`：禁用原因，默认 `Staff disable`
- `--json`：以 JSON 输出结果

### acps-cli admin registry agent enable

用途：重新启用已禁用的 Agent。

参数：

- `--agent-id`：必填，Agent UUID
- `--json`：以 JSON 输出结果

### acps-cli admin registry user reset-password

用途：管理员为指定用户重置密码。

参数：

- `--user-id`：必填，目标用户 UUID
- `--new-password`：可选，目标用户新密码；未提供时进入交互式输入并二次确认
- `--json`：以 JSON 输出结果

## 9. CA 管理侧命令

### 9.1 共享组选项

`acps-cli admin ca` 继承根级 `--config`、`--verbose`，并额外提供：

- `--server-url`：覆盖 CA 服务基础地址

### acps-cli admin ca crl list

用途：列出 CA 端记录的 CRL 历史。

参数：

- `--status`：按状态过滤，取值 `current`、`superseded`、`expired`
- `--page`：页码，默认 `1`
- `--page-size`：每页条数，默认 `20`

### acps-cli admin ca crl refresh

用途：刷新当前 CRL。

参数：无专属参数。

### acps-cli admin ca ocsp responder-info

用途：查看 OCSP responder 元信息。

参数：无专属参数。

### acps-cli admin ca ocsp stats

用途：查看 OCSP 服务统计信息。

参数：无专属参数。

## 10. Discovery 管理侧命令

### 10.1 共享组选项

`acps-cli admin discovery` 继承根级 `--config`、`--verbose`，并额外提供：

- `--server-url`：覆盖 Discovery 服务基础地址

### acps-cli admin discovery run-sync

用途：触发一次 Discovery 编排级同步。

参数：

- `--hard-reset/--no-hard-reset`：同步前是否清空 Discovery 数据，默认开启，即 `--hard-reset`
- `--expect-acs-min`：同步完成后要求最少 ACS 数量，默认 `1`
- `--skip-acs-check`：跳过 ACS 数量校验

### acps-cli admin discovery dsp status

用途：查看当前 DSP 状态。

参数：

- `--expect-acs-min`：要求返回结果中 ACS 对象数量至少达到指定值

### acps-cli admin discovery dsp registry-info

用途：查看当前接入的 Registry 信息。

参数：无专属参数。

### acps-cli admin discovery dsp sync

用途：执行一次 DSP 同步，不重置状态。

参数：无专属参数。

### acps-cli admin discovery dsp start

用途：启动 DSP 后台同步。

参数：无专属参数。

### acps-cli admin discovery dsp stop

用途：停止 DSP 后台同步。

参数：无专属参数。

### acps-cli admin discovery dsp reset

用途：重置 DSP 状态，但不清空已同步数据。

参数：无专属参数。

### acps-cli admin discovery dsp hard-reset

用途：清空已同步数据并重置 DSP 状态。

参数：无专属参数。

### acps-cli admin discovery dsp register-webhook

用途：为 DSP 推送通知注册 webhook。

参数：

- `--url`：必填，webhook 回调地址
- `--secret`：必填，共享密钥
- `--type`：订阅的对象类型，可重复
- `--event`：订阅的事件类型，可重复
- `--description`：可选描述

说明：未提供 `--type` 时默认订阅 `acs`；未提供 `--event` 时默认订阅 `data_change`。该命令需要 Registry 管理员 token，通常先执行 `acps-cli admin auth login`。

## 11. MQ 管理侧命令

### 11.1 使用前提

`acps-cli admin mq` 继承根级 `--config`、`--verbose`，并额外提供：

- `--group-api-url`：覆盖 mq-auth-server Group API 地址
- `--auth-api-url`：覆盖 mq-auth-server Auth API 地址

需要特别区分两类证书：

- Group ACL 命令：需要 Leader 客户端证书，证书 CN 必须与 `--leader-aic` 一致
- Health/Auth Probe 命令：需要可用于 mTLS 握手的 probe 证书，不要求是 Leader 证书

若配置中未提供证书路径，可通过各子命令的 `--cert-file` 与 `--key-file` 临时覆盖。

### acps-cli admin mq health

用途：同时探测 mq-auth-server 的 Group API 和 Auth API 健康状态。

参数：

- `--cert-file`：覆盖 probe 客户端证书 PEM 路径
- `--key-file`：覆盖 probe 客户端私钥 PEM 路径
- `--json`：以 JSON 输出结果

说明：该命令用于报告健康状态；即使某个端点不可达，也会以退出码 `0` 输出 `status: error`，而不是把不可达本身视为 CLI 调用失败。

### acps-cli admin mq group add-member

用途：向指定 leader/group 添加成员。

参数：

- `--leader-aic`：必填，Leader Agent AIC
- `--group-id`：必填，群组 ID
- `--member-aic`：必填，要添加的成员 AIC
- `--cert-file`：覆盖 Leader 客户端证书 PEM 路径
- `--key-file`：覆盖 Leader 客户端私钥 PEM 路径
- `--json`：以 JSON 输出结果

### acps-cli admin mq group remove-member

用途：从指定 leader/group 移除成员。

参数：

- `--leader-aic`：必填，Leader Agent AIC
- `--group-id`：必填，群组 ID
- `--member-aic`：必填，要移除的成员 AIC
- `--cert-file`：覆盖 Leader 客户端证书 PEM 路径
- `--key-file`：覆盖 Leader 客户端私钥 PEM 路径
- `--json`：以 JSON 输出结果

### acps-cli admin mq group delete

用途：删除整个群组 ACL。

参数：

- `--leader-aic`：必填，Leader Agent AIC
- `--group-id`：必填，群组 ID
- `--yes`：跳过交互确认，适合 CI 或非 TTY 环境
- `--cert-file`：覆盖 Leader 客户端证书 PEM 路径
- `--key-file`：覆盖 Leader 客户端私钥 PEM 路径
- `--json`：以 JSON 输出结果

说明：非 TTY 环境中必须显式传入 `--yes`，否则命令会取消并以失败退出。

### acps-cli admin mq group kick

用途：断开指定成员的连接。

参数：

- `--leader-aic`：必填，Leader Agent AIC
- `--group-id`：必填，群组 ID
- `--member-aic`：必填，要踢出的成员 AIC
- `--cert-file`：覆盖 Leader 客户端证书 PEM 路径
- `--key-file`：覆盖 Leader 客户端私钥 PEM 路径
- `--json`：以 JSON 输出结果

说明：如果 mq-auth-server 无法访问 RabbitMQ Management API，当前实现会把 `502/503` 单独解释为 RabbitMQ Management 不可达，而不是普通权限失败。

### acps-cli admin mq auth-probe user

用途：探测 `/auth/user` 授权决策。

参数：

- `--username`：必填，待探测用户名，通常为 AIC
- `--cert-file`：覆盖 probe 客户端证书 PEM 路径
- `--key-file`：覆盖 probe 客户端私钥 PEM 路径
- `--json`：以 JSON 输出结果

### acps-cli admin mq auth-probe vhost

用途：探测 `/auth/vhost` 授权决策。

参数：

- `--username`：必填，用户名，通常为 AIC
- `--vhost`：必填，RabbitMQ vhost
- `--cert-file`：覆盖 probe 客户端证书 PEM 路径
- `--key-file`：覆盖 probe 客户端私钥 PEM 路径
- `--json`：以 JSON 输出结果

### acps-cli admin mq auth-probe resource

用途：探测 `/auth/resource` 授权决策。

参数：

- `--username`：必填，用户名，通常为 AIC
- `--vhost`：必填，RabbitMQ vhost
- `--resource`：必填，资源类型，取值 `exchange` 或 `queue`
- `--name`：必填，资源名称
- `--permission`：必填，权限类型，取值 `configure`、`write`、`read`
- `--cert-file`：覆盖 probe 客户端证书 PEM 路径
- `--key-file`：覆盖 probe 客户端私钥 PEM 路径
- `--json`：以 JSON 输出结果

### acps-cli admin mq auth-probe topic

用途：探测 `/auth/topic` 授权决策。

参数：

- `--username`：必填，用户名，通常为 AIC
- `--vhost`：必填，RabbitMQ vhost
- `--resource`：资源类型，默认 `topic`
- `--name`：必填，Exchange 名称
- `--permission`：必填，权限类型，取值 `write` 或 `read`
- `--routing-key`：必填，路由键
- `--cert-file`：覆盖 probe 客户端证书 PEM 路径
- `--key-file`：覆盖 probe 客户端私钥 PEM 路径
- `--json`：以 JSON 输出结果

## 12. 常用命令示例

```bash
uv run acps-cli --config ./acps-cli.toml auth login --username alice --password 'S3cret!'
uv run acps-cli agent save --acs-file ./acs.json --json
uv run acps-cli cert eab fetch --aic <AIC> --output ./private/eab.json
uv run acps-cli cert issue --aic <AIC> --eab-file ./private/eab.json --usage clientAuth
uv run acps-cli discover query "北京旅游推荐" --limit 5
uv run acps-cli monitor status
uv run acps-cli monitor metrics series --metric cpu.usage --aic <AIC> --start 2026-06-25T00:00:00Z --end 2026-06-25T01:00:00Z --step 1m
uv run acps-cli admin registry review list --status submitted --json
uv run acps-cli admin discovery run-sync --no-hard-reset --expect-acs-min 1
uv run acps-cli admin mq health --json
```

## 13. 建议的查阅顺序

如果你是第一次接触本项目，建议按以下顺序阅读和使用：

1. 先确认 `acps-cli.toml` 中的各服务地址与证书路径
2. 再查看本文档中的命令树，确定自己属于用户侧还是管理侧路径
3. 最后根据对应命令的小节查具体参数

如果命令仍有疑问，可再使用以下帮助查看当前运行版本的 Click 输出：

```bash
uv run acps-cli --help
uv run acps-cli cert --help
uv run acps-cli discover query --help
uv run acps-cli monitor --help
uv run acps-cli admin mq group delete --help
```
