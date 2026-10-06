[Home](../README.md)

**[English](cli-reference_en.md) | [中文](cli-reference.md)**

# acps-cli Command-Line Reference

This document is the complete command-line reference for `acps-cli`.

## 1. Usage Conventions

### 1.1 How to Run It

- Common form used in a development environment: `uv run acps-cli ...`
- After installation into a virtual environment or the system environment, it can be executed directly: `acps-cli ...`

### 1.2 Configuration File Load Order

The root command supports two common options:

- `--config PATH`: explicitly specify the configuration file path
- `--verbose`: enable more detailed CLI log output

The CLI looks up the configuration file in the following order:

1. The file explicitly specified by `--config PATH`
2. `acps-cli.toml` in the current directory
3. `~/.acps-cli.toml` in the user directory

Once a configuration file is found, the CLI additionally loads the `.env` in the same directory as that configuration file, and does not overwrite environment variables that already exist in the current shell. If no configuration file can be found at all, the CLI falls back to `python-dotenv`'s default `.env` lookup logic and returns an empty TOML configuration; commands that depend on service addresses, certificate paths or token files usually still need those supplied explicitly through arguments or the environment.

The general precedence of configuration values is: command-line options > environment variables or `.env` > TOML configuration > code defaults. The unified entry point rejects some legacy configuration names and legacy environment variables, for example `[registry].server_base_url`, `REGISTRY_SERVER_BASE_URL`, `[ca].server_base_url`, `CA_SERVER_BASE_URL`, `[discovery].server_base_url`, `DISCOVERY_SERVER_BASE_URL`; use the new names listed below.

### 1.3 Output and Argument Style

- Most API-facing commands support `--json`, used to output machine-readable JSON.
- Discovery queries and most CA admin query commands already output JSON themselves; such commands do not necessarily provide an additional `--json`.
- Registry-related commands generally support `--server-url`, used to temporarily override the primary service address; `acps-cli entity` additionally supports `--mtls-url`, used to override the `9002` mTLS plane address.
- CA- and Discovery-related commands support `--server-url`, used to temporarily override the service address.
- MQ admin commands provide `--group-api-url` and `--auth-api-url` on the `acps-cli admin mq` group, used to temporarily override the two service addresses; some subcommands still support `--cert-file` and `--key-file` to override certificate material.
- Repeatable arguments are marked below as "repeatable". Such arguments may be passed multiple times.
- Paired boolean switches are written as `--foo/--no-foo`, and the default state is noted below.

### 1.4 Option Placement

Group-level options (for example `--config`, `--verbose`, `--server-url`, `--mtls-url`) may be written before or after the corresponding subcommand; the CLI parses them according to the command that owns the option and does not require a fixed order. The following two forms are equivalent:

```text
acps-cli entity --mtls-url https://registry.example:8443 derive --ontology-aic <AIC>
acps-cli entity derive --mtls-url https://registry.example:8443 --ontology-aic <AIC>
```

When the same option name is declared by several levels of commands at once, the one written before the subcommand belongs to the current group, and the one written after that subcommand belongs to the deeper command. For example, `cert --server-url` overrides the CA address, while `cert eab --server-url` overrides the Registry address:

```text
acps-cli cert --server-url https://ca.example eab fetch --server-url https://registry.example --aic <AIC> --output eab.json
```

When viewing help, put `--help` after the level of command you want to inspect: `acps-cli entity --help` shows the `entity` group options, and `acps-cli entity derive --help` shows the options of `derive`.

## 2. Configuration Sections at a Glance

The default configuration sample is located in `acps-cli.toml` in the project root directory and is mainly divided into the following configuration sections:

- `[registry]`: Registry base address, `9002` mTLS plane address, ontology certificate directory, server-side CA file, request timeout, etc.
- `[auth]`: token file paths for regular users and administrators
- `[ca]`: CA service address, account key directory, private key directory, certificate directory, CSR directory, trust bundle path
- `[discovery]`: Discovery service address
- `[monitor]`: monitor-server address, AMP Query API prefix, HTTP timeout
- `[mq]`: mq-auth-server Group API/Auth API addresses, Leader certificate, probe certificate, server-side CA file, timeout

It is recommended to check the following before you start using it:

- `[registry].base_url`
- `[registry].mtls_base_url`
- `[ca].base_url`
- `[discovery].base_url`
- `[monitor].base_url`
- `[monitor].api_prefix`
- `[monitor].timeout_seconds`
- `[mq].group_api_url`
- `[mq].auth_api_url`

### 2.1 Common Environment-Variable Overrides

The following environment variables override the configuration of the same name in TOML; command-line options still take the highest precedence:

| Domain | Environment variable |
| -- | -------- |
| Registry | `REGISTRY_BASE_URL`, `REGISTRY_MTLS_BASE_URL`, `REGISTRY_TIMEOUT_SECONDS`, `REGISTRY_ONTOLOGY_MTLS_MATERIALS_DIR`, `REGISTRY_MTLS_SERVER_CA_FILE` |
| Registry auth | `AUTH_USER_TOKEN_FILE`, `AUTH_ADMIN_TOKEN_FILE`, `REGISTRY_USER_USERNAME`, `REGISTRY_USER_PASSWORD`, `REGISTRY_USER_NAME`, `REGISTRY_USER_ORG_NAME`, `REGISTRY_ADMIN_USERNAME`, `REGISTRY_ADMIN_PASSWORD` |
| CA | `CA_BASE_URL`, `CA_SERVER_ADMIN_API_TOKEN`, `CA_ACCOUNT_KEYS_DIR`, `CA_PRIVATE_KEYS_DIR`, `CA_CERTS_DIR`, `CA_CSR_DIR`, `CA_TRUST_BUNDLE_PATH` |
| Discovery | `DISCOVERY_BASE_URL` |
| Monitor | `MONITOR_BASE_URL`, `MONITOR_API_PREFIX`, `MONITOR_TIMEOUT_SECONDS` |
| MQ | `MQ_GROUP_API_URL`, `MQ_AUTH_API_URL`, `MQ_GROUP_CERT_FILE`, `MQ_GROUP_KEY_FILE`, `MQ_PROBE_CERT_FILE`, `MQ_PROBE_KEY_FILE`, `MQ_CA_FILE`, `MQ_TIMEOUT_SECONDS` |

## 3. Complete Command Tree

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

## 4. Registry User-Side Commands

### 4.1 Shared Group Options

The following command groups all inherit the root-level `--config`, `--verbose`, and additionally provide Registry group options:

- `acps-cli auth`
- `acps-cli agent`
- `acps-cli entity`
- `acps-cli cert eab`

The Registry shared group options are as follows:

- `--server-url`: override the Registry service base address

Among them, `acps-cli entity` additionally supports:

- `--mtls-url`: override the Registry `9002` mTLS service address

### acps-cli auth login

Purpose: user login; if no local account exists, the underlying logic can automatically register using the arguments.

Arguments:

- `--username`: username; when not provided, it usually falls back to interactive input
- `--password`: password; when not provided, it usually falls back to interactive input
- `--name`: display name used for automatic registration
- `--org-name`: organization name used for automatic registration
- `--json`: output the result as JSON

### acps-cli auth whoami

Purpose: view the current Registry user identity.

Arguments:

- `--json`: output the result as JSON

### acps-cli auth logout

Purpose: log out the current Registry user and clear the locally stored user token.

Arguments:

- `--json`: output the result as JSON

### acps-cli auth change-password

Purpose: interactively change the password of the currently logged-in user.

Arguments:

- `--json`: output the result as JSON

Description: the command prompts in turn for the current password and the new password, and requires the new password to be confirmed again. On success it additionally prints "password changed, re-login recommended".

### acps-cli agent list

Purpose: list the Agent drafts or submitted records under the current user.

Arguments:

- `--page`: page number, default `1`
- `--page-size`: number of entries per page, default `20`
- `--status`: filter by status; repeatable
- `--json`: output the result as JSON

### acps-cli agent save

Purpose: create or update an Agent draft.

Arguments:

- `--logo-url`: Agent logo URL
- `--acs-file`: required, path to the ACS JSON file
- `--ontology/--no-ontology`: whether to mark this Agent as ontology, default `--no-ontology`
- `--json`: output the result as JSON

### acps-cli agent submit

Purpose: submit an Agent draft into the review process.

Arguments:

- `--agent-id`: required, UUID of the draft to submit
- `--json`: output the result as JSON

### acps-cli agent check

Purpose: check the review status of the corresponding Agent based on a local ACS file.

Arguments:

- `--acs-file`: required, path to the ACS JSON file
- `--json`: output the result as JSON

### acps-cli agent sync

Purpose: sync the latest ACS state from the server back to a local file.

Arguments:

- `--acs-file`: required, path to the ACS JSON file
- `--json`: output the result as JSON

### acps-cli agent delete

Purpose: delete an Agent draft owned by the current user.

Arguments:

- `--acs-file`: required, path to the ACS JSON file
- `--json`: output the result as JSON

### acps-cli entity derive

Purpose: derive and register an entity based on an ontology AIC that has already passed review.

Arguments:

- `--mtls-url`: override the Registry `9002` mTLS service address. This option is defined on the `entity` group and may be written before or after `derive`
- `--ontology-aic`: required, an ontology AIC that has already passed review
- `--payload-file`: optional, path to a UTF-8 JSON file with the derived entity payload
- `--mtls-cert-file`: override the ontology mTLS certificate path
- `--mtls-key-file`: override the ontology mTLS private key path
- `--mtls-server-ca-file`: override the CA file used to verify the Registry `9002` server certificate
- `--json`: output the result as JSON

Description: this command relies on the Registry mTLS plane, and usually requires `[registry].mtls_base_url` and the corresponding certificate material to be configured correctly.

`--payload-file` may be omitted. If provided, it must be a JSON object, and the allowed fields are:

- `endPoints`: the entity's own list of service endpoints. When omitted, the ontology endpoints are reused
- `entityUserId`: bound end-user ID
- `entityMeta`: entity-defined custom metadata
- `certificate`: certificate issuance configuration for the entity, written into the entity ACS, to be read later when applying for a server-side certificate in order to obtain the SANs and the expected validity period. It does not inherit the ontology's `certificate`

`ontologyAic` is not written in the file; it is passed via `--ontology-aic`. Other fields are rejected.

Example:

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

Purpose: obtain EAB credentials for a subsequent certificate application.

Arguments:

- `--aic`: required, the target Agent AIC
- `--output`: required, output path for the EAB JSON
- `--json`: output the result as JSON

Description: the command path sits under `cert eab`, but it actually accesses a Registry capability.

Note: the `cert` group's own `--server-url` overrides the CA service address; EAB uses the Registry address, so the Registry override should be written after `cert eab`, for example `acps-cli cert eab --server-url http://localhost:9001 fetch ...`.

## 5. CA User-Side Commands

### 5.1 Shared Group Options

The following command groups all inherit the root-level `--config`, `--verbose`, and additionally provide:

- `acps-cli cert --server-url`

Meaning: override the CA service base address.

### acps-cli cert issue

Purpose: apply for a new certificate for an Agent.

Arguments:

- `--aic, -a`: required, Agent Identity Code
- `--eab-file`: required, path to the EAB JSON file
- `--usage, -u`: required, certificate usage, value `clientAuth` or `serverAuth`
- `--key-type, -k`: key type, value `ec` or `rsa`, default `ec`
- `--reuse-key`: reuse the local private key if one already exists
- `--key-path`: output path for the private key
- `--cert-path`: output path for the certificate chain
- `--trust-bundle-path`: output path for the trust bundle

Description: the JSON pointed to by `--eab-file` must contain the `keyId`, `macKey` and `aic` fields, with `aic` consistent with `--aic`. After the certificate is issued successfully, the trust bundle is updated at the same time.

### acps-cli cert renew

Purpose: renew an existing certificate.

Arguments:

- `--aic, -a`: required, Agent Identity Code
- `--eab-file`: required, path to the EAB JSON file
- `--usage, -u`: required, certificate usage, value `clientAuth` or `serverAuth`
- `--force, -f`: force renewal even if the certificate is not yet near expiry
- `--key-path`: output path for the private key
- `--cert-path`: output path for the certificate chain
- `--trust-bundle-path`: output path for the trust bundle

Description: without `--force`, if the local certificate still has more than 30 days of validity, the command refuses to renew. Renewal reuses the local Agent private key.

### acps-cli cert revoke

Purpose: revoke a certificate.

Arguments:

- `--aic, -a`: required, Agent Identity Code
- `--reason, -r`: revocation reason, default `unspecified`

Description: the current implementation recognises `unspecified`, `keyCompromise`, `cACompromise`, `affiliationChanged`, `superseded`, `cessationOfOperation`; any other string is treated as `unspecified`.

### acps-cli cert status

Purpose: query the current certificate status of an Agent.

Arguments:

- `--aic, -a`: required, Agent Identity Code
- `--cert-path`: local certificate file path
- `--check-ocsp/--no-check-ocsp`: whether to perform an OCSP check, enabled by default, i.e. `--check-ocsp`

### acps-cli cert account-key rollover

Purpose: roll over the ACME account key.

Arguments:

- `--aic, -a`: required, Agent Identity Code
- `--new-key, -n`: new key file path; it may point to a pre-generated key, or serve as the output path for automatic generation
- `--key-type, -k`: key type when generating automatically, value `ec` or `rsa`, default `ec`
- `--backup/--no-backup`: whether to back up the old account key first, enabled by default, i.e. `--backup`

Description: if `--new-key` points to a file that already exists, the command reads that pre-generated key; if the path does not exist, a new key is generated and written to that path after a successful rollover. When `--new-key` is not provided, only the account key file of the current AIC is updated.

### acps-cli cert trust-bundle update

Purpose: update the local trust bundle file.

Arguments:

- `--output, -o`: output path

### acps-cli cert crl download

Purpose: download a CRL file.

Arguments:

- `--output, -o`: output file path
- `--format, -f`: CRL format, value `der` or `pem`, default `der`
- `--version`: download a historical CRL version, applicable only to the DER download path

Description: when `--output` is not provided, the current DER CRL is saved by default to `ca.crl` in the CA certificate directory, the PEM one to `ca.pem`, and historical versions to `ca-<version>.crl`.

### acps-cli cert crl info

Purpose: view the metadata of the current CRL.

Arguments: no dedicated arguments.

### acps-cli cert crl detail

Purpose: view the details of the revocation entries in the current CRL.

Arguments: no dedicated arguments.

### acps-cli cert ocsp check

Purpose: check certificate status through OCSP.

Arguments:

- `--aic, -a`: Agent Identity Code
- `--cert, -c`: certificate file path
- `--issuer, -i`: issuer certificate or trust bundle file path
- `--request-method`: OCSP request method, value `post` or `get`, default `post`
- `--json`: output the result as JSON

Description: the common usage is to pass an AIC, or to pass a certificate file directly for the check.

### acps-cli cert ocsp cert-status

Purpose: call the simplified OCSP status query endpoint.

Arguments:

- `--serial-number`: certificate serial number
- `--aic`: Agent Identity Code
- `--cert-path`: certificate file path used to extract the serial number

Description: choose any one of the three input methods according to the actual environment.

## 6. Discovery User-Side Commands

### 6.1 Shared Group Options

`acps-cli discover` inherits the root-level `--config`, `--verbose`, and additionally provides:

- `--server-url`: override the Discovery service base address

### acps-cli discover status

Purpose: check the Discovery service status.

Arguments: no dedicated arguments.

### acps-cli discover query

Purpose: execute a Discovery query, supporting both natural-language queries and a structured request body.

Arguments:

- `QUERY_STR`: optional positional argument, the natural-language query text
- `--type`: explicitly specify the Discovery request type; when not provided and the request body has no `type`, it defaults to `explicit`
- `--limit`: maximum number of returned entries, range `1-50`; when not provided and the request body has no `limit`, it defaults to `5`
- `--request-json`: inline DiscoveryRequest JSON; cannot be used together with `--request-file`
- `--request-file`: DiscoveryRequest JSON file path; cannot be used together with `--request-json`
- `--filter-json`: inline DiscoveryFilter JSON; cannot be used together with `--filter-file`
- `--filter-file`: DiscoveryFilter JSON file path; cannot be used together with `--filter-json`
- `--context-json`: inline query context JSON; cannot be used together with `--context-file`
- `--context-file`: query context JSON file path; cannot be used together with `--context-json`
- `--forward-depth-limit`: forwarding depth limit, range `1-5`
- `--forward-fanout-limit`: forwarding fan-out limit, range `1-5`
- `--forward-fanout-remaining`: remaining fan-out budget, range `0-5`
- `--forward-chain`: append to `forwardChain`, repeatable
- `--forward-trusted-server`: append to `forwardTrustedServers`, repeatable
- `--forward-signature`: append to `forwardSignatures`, repeatable
- `--forward-each-timeout-ms`: per-hop forwarding timeout (milliseconds), must be greater than or equal to `1`
- `--forward-total-timeout-ms`: total forwarding timeout (milliseconds), must be greater than or equal to `1`

Description:

- For simple queries, `QUERY_STR` alone may be passed
- When precise control over the request structure is needed, prefer `--request-json` or `--request-file`
- When `--request-json` or `--request-file` is used, the `QUERY_STR`, `--limit`, forward and other options on the command line override the fields of the same name in the request body
- The forward-related parameters are mainly used for multi-hop forwarding and chain-testing scenarios

## 7. Monitor User-Side Commands

### 7.1 Shared Group Options

`acps-cli monitor` inherits the root-level `--config`, `--verbose`, and additionally provides:

- `--server-url`: override the monitor-server root address
- `--api-prefix`: override the AMP Query API prefix, default is `/acps-amp-v1`

The default configuration section is as follows:

```toml
[monitor]
base_url = "http://localhost:9009"
api_prefix = "/acps-amp-v1"
timeout_seconds = 15
```

Description:

- `monitor` configuration precedence follows: command-line options > `MONITOR_*` environment variables > `[monitor]` > defaults
- `monitor status` accesses `GET /health` and does not append `api_prefix`
- The remaining business commands access `{base_url}{api_prefix}/...`
- `monitor` query commands output pretty JSON by default
- All POST query commands support `--request-json` or `--request-file`
- `--request-json` and `--request-file` are mutually exclusive, and the content must be a JSON object
- When a request and shortcut arguments are provided at the same time, the CLI uses the shortcut arguments to override the fields of the same name in the request body, including scalar fields, `timeRange` and `page`
- Shortcut filters are only merged into a simple `filter` that has `logic=and` and no `groups`; for complex filtering, write the complete request directly
- Once a complete request is provided, the CLI no longer requires those local required arguments that are only used for "shortcut payload construction"
- payload-only commands do not automatically construct an empty request body; the request must be provided explicitly
- This command group is only responsible for Query API queries and troubleshooting, and does not include smoke tests, Kafka writes or AMP LogRecord construction; write-path verification still uses `monitor-server/scripts/smoke_*.py`

### acps-cli monitor status

Purpose: check the monitor-server health status.

Arguments: no dedicated arguments.

### acps-cli monitor heartbeat

Purpose: query the Heartbeat read model.

Subcommands:

- `summary`: query the global Heartbeat summary
- `liveness <AIC>`: point-query the liveness of a single AIC
- `query`: batch-query liveness snapshots

`heartbeat query` arguments:

- `--aic`: append an `aic eq` filter
- `--limit`: write to `page.limit`
- `--cursor`: write to `page.cursor`
- `--request-json` / `--request-file`: submit the complete request body

Description:

- When no request is provided, `heartbeat query` must provide `--aic`
- Heartbeat liveness query does not support shortcut `--start` / `--end` time range construction

### acps-cli monitor metrics

Purpose: query the Metrics read model.

Subcommands:

- `snapshots`: query the latest metric snapshots
- `series`: query metric time series
- `rankings`: query metric rankings

`metrics snapshots` arguments:

- `--aic`: append an `aic eq` filter
- `--limit` / `--cursor`: write to `page`
- `--request-json` / `--request-file`: submit the complete request body

`metrics series` arguments:

- `--metric`: write to the top-level `metric`
- `--aic`: append an `aic eq` filter
- `--start`, `--end`: write to `timeRange.startAt` / `timeRange.endAt`
- `--step`: write to the top-level `step`
- `--request-json` / `--request-file`: submit the complete request body

`metrics rankings` arguments:

- `--metric`: write to the top-level `metric`
- `--start`, `--end`: write to `timeRange`
- `--top-n`: write to the top-level `topN`
- `--request-json` / `--request-file`: submit the complete request body

Description:

- When no request is provided, both `metrics series` and `metrics rankings` must provide `--metric`, `--start` and `--end`
- If the server has not enabled the Analytics Profile, `rankings` directly passes through the `404/422/503` returned by the server

### acps-cli monitor access

Purpose: query Access events, Traces and analytics read models.

Subcommands:

- `events`
- `operations`
- `traces`
- `trace <TRACE_ID>`
- `slow`
- `errors`
- `topology`

`access events` arguments:

- `--aic`: append an `aic eq` filter
- `--trace-id`: append a `traceId eq` filter
- `--start`, `--end`: write to `timeRange`
- `--limit` / `--cursor`: write to `page`
- `--request-json` / `--request-file`: submit the complete request body

`access trace <TRACE_ID>` arguments:

- `--include-events`: mapped to the GET query parameter `include_events=true`

payload-only subcommands:

- `operations`
- `traces`
- `slow`
- `errors`
- `topology`

Description:

- When no request is provided, `access events` must provide `--start` and `--end`
- The payload-only subcommands above must be given `--request-json` or `--request-file` explicitly

### acps-cli monitor message

Purpose: query Message events, lifecycles, dead letters and destination read models.

Subcommands:

- `events`
- `lifecycles`
- `lifecycle <MESSAGE_ID>`
- `deadletters`
- `destinations`
- `throughput`

`message events` arguments:

- `--message-id`: append a `messageId eq` filter
- `--trace-id`: append a `traceId eq` filter
- `--start`, `--end`: write to `timeRange`
- `--limit` / `--cursor`: write to `page`
- `--request-json` / `--request-file`: submit the complete request body

`message lifecycle <MESSAGE_ID>` arguments:

- `--system`: mapped to the query parameter `system`
- `--destination-name`: mapped to the query parameter `destinationName`
- `--destination-kind`: mapped to the query parameter `destinationKind`
- `--virtual-host`: mapped to the query parameter `virtualHost`

payload-only subcommands:

- `lifecycles`
- `deadletters`
- `destinations`
- `throughput`

Description:

- When no request is provided, `message events` must provide `--start` and `--end`
- The payload-only subcommands above must be given `--request-json` or `--request-file` explicitly
- Whether the Reliability / Destination Profile is enabled is decided by the server; the CLI is only responsible for requests and error pass-through

### acps-cli monitor system

Purpose: query System log events.

Subcommands:

- `events`

`system events` arguments:

- `--aic`: append an `aic eq` filter
- `--correlation-id`: append a `correlationId eq` filter
- `--severity-min`: append a `severityNumber gte` filter
- `--start`, `--end`: write to `timeRange`
- `--limit` / `--cursor`: write to `page`
- `--request-json` / `--request-file`: submit the complete request body

Description:

- When no request is provided, `system events` must provide `--start` and `--end`

### acps-cli monitor audit

Purpose: query Audit records, chain anchors and integrity verification tasks.

Subcommands:

- `records`
- `record <AUDIT_ID>`
- `anchors`
- `verify`
- `verify-task <TASK_ID>`

`audit records` arguments:

- `--aic`: append an `aic eq` filter
- `--keyword`: write to the top-level `keyword`
- `--start`, `--end`: write to `timeRange`
- `--limit` / `--cursor`: write to `page`
- `--request-json` / `--request-file`: submit the complete request body

`audit anchors` arguments:

- `--chain-id`: mapped to the GET query parameter `chain_id`

`audit verify` arguments:

- `--request-json` / `--request-file`: submit the complete verification request body

Description:

- When no request is provided, `audit records` must provide `--start` and `--end`
- `audit verify` is a payload-only command and the request must be provided explicitly
- `audit verify` only submits the server-side verification task; it does not perform signing, public key parsing or hash chain computation locally

## 8. Registry Admin-Side Commands

### 8.1 Shared Group Options

The following admin command groups inherit the root-level `--config`, `--verbose`, and additionally provide:

- `acps-cli admin auth`
- `acps-cli admin registry`

Shared group options:

- `--server-url`: override the Registry service base address

### acps-cli admin auth login

Purpose: Registry administrator login.

Arguments:

- `--username`: administrator username
- `--password`: administrator password
- `--json`: output the result as JSON

### acps-cli admin auth whoami

Purpose: view the current Registry administrator identity.

Arguments:

- `--json`: output the result as JSON

### acps-cli admin auth logout

Purpose: log out the current Registry administrator and clear the locally stored administrator token.

Arguments:

- `--json`: output the result as JSON

### acps-cli admin auth change-password

Purpose: interactively change the password of the currently logged-in administrator.

Arguments:

- `--json`: output the result as JSON

Description: the command prompts in turn for the current password and the new password, and requires the new password to be confirmed again. On success it additionally prints "password changed, re-login recommended".

### acps-cli admin registry review list

Purpose: list Agent review requests that are pending or in a specified status.

Arguments:

- `--page`: page number, default `1`
- `--page-size`: number of entries per page, default `20`
- `--status`: filter by status, repeatable
- `--json`: output the result as JSON

### acps-cli admin registry review approve

Purpose: approve the review of the specified Agent.

Arguments:

- `--agent-id`: required, Agent UUID
- `--comments`: optional review comment
- `--json`: output the result as JSON

### acps-cli admin registry review reject

Purpose: reject the review of the specified Agent.

Arguments:

- `--agent-id`: required, Agent UUID
- `--comments`: required, reason for rejection
- `--json`: output the result as JSON

### acps-cli admin registry agent disable

Purpose: disable an existing Agent.

Arguments:

- `--agent-id`: required, Agent UUID
- `--reason`: reason for disabling, default `Staff disable`
- `--json`: output the result as JSON

### acps-cli admin registry agent enable

Purpose: re-enable a disabled Agent.

Arguments:

- `--agent-id`: required, Agent UUID
- `--json`: output the result as JSON

### acps-cli admin registry user reset-password

Purpose: an administrator resets the password of the specified user.

Arguments:

- `--user-id`: required, target user UUID
- `--new-password`: optional, the target user's new password; when not provided, it falls back to interactive input with a second confirmation
- `--json`: output the result as JSON

## 9. CA Admin-Side Commands

### 9.1 Shared Group Options

`acps-cli admin ca` inherits the root-level `--config`, `--verbose`, and additionally provides:

- `--server-url`: override the CA service base address

### acps-cli admin ca crl list

Purpose: list the CRL history recorded on the CA side.

Arguments:

- `--status`: filter by status, value `current`, `superseded`, `expired`
- `--page`: page number, default `1`
- `--page-size`: number of entries per page, default `20`

### acps-cli admin ca crl refresh

Purpose: refresh the current CRL.

Arguments: no dedicated arguments.

### acps-cli admin ca ocsp responder-info

Purpose: view OCSP responder metadata.

Arguments: no dedicated arguments.

### acps-cli admin ca ocsp stats

Purpose: view OCSP service statistics.

Arguments: no dedicated arguments.

## 10. Discovery Admin-Side Commands

### 10.1 Shared Group Options

`acps-cli admin discovery` inherits the root-level `--config`, `--verbose`, and additionally provides:

- `--server-url`: override the Discovery service base address

### acps-cli admin discovery run-sync

Purpose: trigger a Discovery orchestration-level sync.

Arguments:

- `--hard-reset/--no-hard-reset`: whether to clear Discovery data before syncing, enabled by default, i.e. `--hard-reset`
- `--expect-acs-min`: minimum ACS count required after the sync completes, default `1`
- `--skip-acs-check`: skip the ACS count check

### acps-cli admin discovery dsp status

Purpose: view the current DSP status.

Arguments:

- `--expect-acs-min`: require the number of ACS objects in the returned result to reach at least the specified value

### acps-cli admin discovery dsp registry-info

Purpose: view the currently connected Registry information.

Arguments: no dedicated arguments.

### acps-cli admin discovery dsp sync

Purpose: perform one DSP sync without resetting state.

Arguments: no dedicated arguments.

### acps-cli admin discovery dsp start

Purpose: start DSP background syncing.

Arguments: no dedicated arguments.

### acps-cli admin discovery dsp stop

Purpose: stop DSP background syncing.

Arguments: no dedicated arguments.

### acps-cli admin discovery dsp reset

Purpose: reset the DSP state without clearing already synced data.

Arguments: no dedicated arguments.

### acps-cli admin discovery dsp hard-reset

Purpose: clear already synced data and reset the DSP state.

Arguments: no dedicated arguments.

### acps-cli admin discovery dsp register-webhook

Purpose: register a webhook for DSP push notifications.

Arguments:

- `--url`: required, webhook callback address
- `--secret`: required, shared secret
- `--type`: object type to subscribe to, repeatable
- `--event`: event type to subscribe to, repeatable
- `--description`: optional description

Description: when `--type` is not provided, `acs` is subscribed by default; when `--event` is not provided, `data_change` is subscribed by default. This command requires a Registry administrator token, so usually run `acps-cli admin auth login` first.

## 11. MQ Admin-Side Commands

### 11.1 Prerequisites

`acps-cli admin mq` inherits the root-level `--config`, `--verbose`, and additionally provides:

- `--group-api-url`: override the mq-auth-server Group API address
- `--auth-api-url`: override the mq-auth-server Auth API address

Two kinds of certificates need to be distinguished in particular:

- Group ACL commands: require a Leader client certificate, and the certificate CN must match `--leader-aic`
- Health/Auth Probe commands: require a probe certificate usable for the mTLS handshake, and it does not have to be a Leader certificate

If no certificate path is provided in the configuration, `--cert-file` and `--key-file` on each subcommand can be used to override it temporarily.

### acps-cli admin mq health

Purpose: probe the health status of both the Group API and the Auth API of mq-auth-server.

Arguments:

- `--cert-file`: override the probe client certificate PEM path
- `--key-file`: override the probe client private key PEM path
- `--json`: output the result as JSON

Description: this command is used to report health status; even if an endpoint is unreachable, it still outputs `status: error` with exit code `0`, rather than treating unreachability itself as a CLI call failure.

### acps-cli admin mq group add-member

Purpose: add a member to the specified leader/group.

Arguments:

- `--leader-aic`: required, Leader Agent AIC
- `--group-id`: required, group ID
- `--member-aic`: required, the AIC of the member to add
- `--cert-file`: override the Leader client certificate PEM path
- `--key-file`: override the Leader client private key PEM path
- `--json`: output the result as JSON

### acps-cli admin mq group remove-member

Purpose: remove a member from the specified leader/group.

Arguments:

- `--leader-aic`: required, Leader Agent AIC
- `--group-id`: required, group ID
- `--member-aic`: required, the AIC of the member to remove
- `--cert-file`: override the Leader client certificate PEM path
- `--key-file`: override the Leader client private key PEM path
- `--json`: output the result as JSON

### acps-cli admin mq group delete

Purpose: delete the entire group ACL.

Arguments:

- `--leader-aic`: required, Leader Agent AIC
- `--group-id`: required, group ID
- `--yes`: skip the interactive confirmation, suitable for CI or non-TTY environments
- `--cert-file`: override the Leader client certificate PEM path
- `--key-file`: override the Leader client private key PEM path
- `--json`: output the result as JSON

Description: in a non-TTY environment, `--yes` must be passed explicitly, otherwise the command cancels and exits with a failure.

### acps-cli admin mq group kick

Purpose: disconnect the specified member.

Arguments:

- `--leader-aic`: required, Leader Agent AIC
- `--group-id`: required, group ID
- `--member-aic`: required, the AIC of the member to kick
- `--cert-file`: override the Leader client certificate PEM path
- `--key-file`: override the Leader client private key PEM path
- `--json`: output the result as JSON

Description: if mq-auth-server cannot reach the RabbitMQ Management API, the current implementation interprets `502/503` separately as RabbitMQ Management being unreachable, rather than as an ordinary permission failure.

### acps-cli admin mq auth-probe user

Purpose: probe the `/auth/user` authorization decision.

Arguments:

- `--username`: required, the username to probe, usually an AIC
- `--cert-file`: override the probe client certificate PEM path
- `--key-file`: override the probe client private key PEM path
- `--json`: output the result as JSON

### acps-cli admin mq auth-probe vhost

Purpose: probe the `/auth/vhost` authorization decision.

Arguments:

- `--username`: required, username, usually an AIC
- `--vhost`: required, RabbitMQ vhost
- `--cert-file`: override the probe client certificate PEM path
- `--key-file`: override the probe client private key PEM path
- `--json`: output the result as JSON

### acps-cli admin mq auth-probe resource

Purpose: probe the `/auth/resource` authorization decision.

Arguments:

- `--username`: required, username, usually an AIC
- `--vhost`: required, RabbitMQ vhost
- `--resource`: required, resource type, value `exchange` or `queue`
- `--name`: required, resource name
- `--permission`: required, permission type, value `configure`, `write`, `read`
- `--cert-file`: override the probe client certificate PEM path
- `--key-file`: override the probe client private key PEM path
- `--json`: output the result as JSON

### acps-cli admin mq auth-probe topic

Purpose: probe the `/auth/topic` authorization decision.

Arguments:

- `--username`: required, username, usually an AIC
- `--vhost`: required, RabbitMQ vhost
- `--resource`: resource type, default `topic`
- `--name`: required, Exchange name
- `--permission`: required, permission type, value `write` or `read`
- `--routing-key`: required, routing key
- `--cert-file`: override the probe client certificate PEM path
- `--key-file`: override the probe client private key PEM path
- `--json`: output the result as JSON

## 12. Common Command Examples

```bash
uv run acps-cli --config ./acps-cli.toml auth login --username alice --password 'S3cret!'
uv run acps-cli agent save --acs-file ./acs.json --json
uv run acps-cli cert eab fetch --aic <AIC> --output ./private/eab.json
uv run acps-cli cert issue --aic <AIC> --eab-file ./private/eab.json --usage clientAuth
uv run acps-cli discover query "Beijing travel recommendations" --limit 5
uv run acps-cli monitor status
uv run acps-cli monitor metrics series --metric cpu.usage --aic <AIC> --start 2026-06-25T00:00:00Z --end 2026-06-25T01:00:00Z --step 1m
uv run acps-cli admin registry review list --status submitted --json
uv run acps-cli admin discovery run-sync --no-hard-reset --expect-acs-min 1
uv run acps-cli admin mq health --json
```

## 13. Suggested Reading Order

If you are new to this project, it is recommended to read and use it in the following order:

1. First confirm the service addresses and certificate paths in `acps-cli.toml`
2. Then look at the command tree in this document to determine whether you are on the user-side or the admin-side path
3. Finally, look up the specific arguments in the section for the corresponding command

If you still have questions about a command, you can additionally use the following help to view the Click output of the currently running version:

```bash
uv run acps-cli --help
uv run acps-cli cert --help
uv run acps-cli discover query --help
uv run acps-cli monitor --help
uv run acps-cli admin mq group delete --help
```
