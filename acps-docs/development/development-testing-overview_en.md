[Home](../README.md)

**[English](development-testing-overview_en.md) | [中文](development-testing-overview.md)**

# ACPs Development and Testing Overview

## 1. Purpose and Scope of This Document

This document is aimed at developers working on the various ACPs projects, and explains the unified approach to local development, environment preparation, service startup, test layering, and quality checks. It consolidates the conventions found in each project's `README.md`, `tests/README.md`, and `Justfile`, focusing on explaining the `just`-based command system rather than replacing each repository's own detailed instructions.

If a command differs between this document and a project's `Justfile`, the current project's `just help` and `Justfile` take precedence.

## 2. Applicable Projects

This document primarily covers the following Python projects that expose a unified development and testing entry point:

| Project | Type | Local development entry point | Main external dependencies |
| --- | --- | --- | --- |
| `registry-server` | Core service | `just dev start` | PostgreSQL, local mTLS certificates |
| `ca-server` | Core service | `just dev start` | PostgreSQL, CA certificate material |
| `discovery-server` | Core service | `just dev start` | PostgreSQL / pgvector, embedding / LLM configuration |
| `mq-auth-server` | Core service | `just dev start` | Redis, RabbitMQ, local mTLS certificates |
| `demo-partner` | Sample application | `just dev start` | RabbitMQ, LLM configuration, local mTLS certificates |
| `demo-leader` | Sample application | `just dev start` | RabbitMQ, LLM configuration, `demo-partner` |
| `acps-cli` | CLI / integration testing tool | `just dev bootstrap` | Local dev-infra, optional backend services (no long-running service in this repository) |

`acps-sdk` currently has no unified `Justfile`; development is still based on the usual Python package commands such as `uv sync` and `uv build`.

## 3. First, Remember One Unified Model

Most service-type projects organize their `Justfile` commands around the same set of domains:

| domain | Purpose | Common commands |
| --- | --- | --- |
| `help` | View command documentation | `just`, `just help` |
| `infra` | Manage shared development dependencies | `just infra up postgres`, `just infra status`, `just infra logs rabbitmq --follow` |
| `prep` | Prepare or repair this repository's environment | `just prep env`, `just prep sync`, `just prep hooks`, `just prep certs`, `just prep migrate test` |
| `dev` | Development environment checks and the local application lifecycle | `just dev check`, `just dev start`, `just dev start fg`, `just dev stop` (optionally `just dev bootstrap` to warm up only) |
| `test` | Test entry points | `just test check`, `just test bootstrap`, `just test unit`, `just test integration`, `just test e2e`, `just test` |
| `qa` | Formatting, linting, typing, and auditing | `just qa`, `just qa fmt`, `just qa type`, `just qa lint` |
| `package` | Wheel runtime package builds | `just package check`, `just package bootstrap`, `just package wheel` |

You can think of it as a chain that goes from light to heavy:

```text
infra -> prep -> dev -> test -> qa
```

`infra` prepares shared dependencies, `prep` prepares this repository's environment, `dev check` performs read-only checks, `dev start/stop` manages local services, `test` runs layered verification, and `qa` performs pre-commit quality checks. `package` is used to build runtime packages and is only used during development when you need to verify artifacts.

## 4. What to Do the First Time You Enter a Repository

For service-type projects, run the following from the project root directory:

```bash
cp .env.example .env
# Edit the database, token, certificate, LLM, RabbitMQ, and other settings in .env as described in the README

just dev start
```

`just dev start` automatically completes environment preparation before starting (you can also run `just dev bootstrap` on its own to warm up without starting), which typically includes:

- Starting the shared dependencies this repository needs, such as PostgreSQL, Redis, and RabbitMQ.
- Generating `.env`, and keeping the file if it already exists.
- Installing managed Python 3.14 via `uv` and syncing `.venv/`.
- Installing or updating the pre-commit / commit-msg hooks.
- Preparing local development certificates from `../acps-infra/dev-infra/dev-cert.sh` as needed.
- Running development database migrations for services that have a database.

`acps-cli` also uses the unified `dev` domain, but it has no local service runtime. The first time you enter `acps-cli`, use:

```bash
just dev bootstrap
```

It prepares the CLI's own environment and starts the shared `postgres`, `redis`, and `rabbitmq` dependencies for later use by test fixtures or manual integration testing; for day-to-day checks, use `just dev check` rather than `just dev start`.

## 5. `infra`: Manage Shared Dependencies from a Single Entry Point

Services do not operate the compose files under `acps-infra/dev-infra` directly; instead they go through a proxy in this repository's `Justfile`:

```bash
just infra status
just infra up postgres
just infra up redis rabbitmq
just infra wait redis rabbitmq
just infra logs rabbitmq --follow
just infra down
```

Different projects need different shared dependencies by default:

| Project | Default dependencies for `just dev start` / `bootstrap` |
| --- | --- |
| `registry-server`, `ca-server`, `discovery-server` | `postgres` |
| `mq-auth-server` | `redis rabbitmq` |
| `demo-partner`, `demo-leader` | `rabbitmq` |
| `acps-cli` | `postgres redis rabbitmq` |

If you are only changing pure business logic and running unit tests, you usually do not need to start the shared dependencies first. Only start them as needed once you move on to integration tests, e2e, local service startup, or CLI integration testing.

## 6. `prep`: Partial Preparation and Partial Repair

`prep` is the decomposable entry point for environment preparation, suitable for repairing one broken piece on its own:

| Command | Effect |
| --- | --- |
| `just prep env` | Generate `.env` from `.env.example` if it is missing |
| `just prep sync` | Use `uv` to sync Python, dependencies, and `.venv/` |
| `just prep hooks` | Install or update Git hooks |
| `just prep certs` | Prepare local development certificates or CA material |
| `just prep certs reset` | Currently mainly used for `mq-auth-server`; clean up and re-issue development certificates |
| `just prep migrate app` | Run migrations against the development database |
| `just prep migrate test` | Run migrations against the test database |
| `just prep seed app` / `test` | `discovery-server` only; import demo ACS samples |
| `just prep reseed app` / `test` | `discovery-server` only; clear and re-import samples |
| `just prep sync-embedding-dimension app` / `test` | `discovery-server` only; sync the embedding dimension |

A few points that are easy to confuse:

- If `.env` already exists, `prep env` does not overwrite the existing `.env` file.
- `prep sync` syncs the environment using the versions locked by the project; most projects pin Python 3.14.
- What `prep certs` generates is local development material and should not be committed to Git.
- `mq-auth-server`, `demo-partner`, and `demo-leader` have no business database, so `prep migrate` is a no-op or does not exist for them.
- Services that have a database should distinguish between the development database and the test database; the test database must not point at the development database.

## 7. `dev check`: Check First, Then Start or Integrate

`just dev check` is the read-only check entry point; it usually does not repair the environment. Its value is that it explains up front "why the service will not start and why the tests will not run".

Common checks include:

- Whether `python3`, `uv`, `just`, and `openssl` are available.
- Whether `.env` or `acps-cli.toml` exists.
- Whether `uv sync --check --locked` passes.
- Whether the shared `dev-infra` is available.
- Whether PostgreSQL, Redis, and RabbitMQ are running / healthy.
- Whether the local mTLS certificates and CA material are complete.
- For `acps-cli`, it also checks whether the registry, ca, discovery, and mq target addresses are reachable.

Recommended habit:

```bash
just dev check
```

Run it once before manual integration testing. If it fails, fix the environment according to the output first, rather than going straight into `uv run pytest` or starting services by hand.

## 8. `dev`: Local Service Lifecycle

The common local startup command for service-type projects is:

```bash
just dev start
```

`just dev start` starts in the background by default and automatically prepares the environment as needed. Common actions are as follows:

| Command | Description |
| --- | --- |
| `just dev start` | Start the service in the background, with logs written to `logs/` |
| `just dev start bg` | Explicitly start in the background |
| `just dev start fg` | Start in the foreground, suitable for debugging |
| `just dev stop` | Stop the background instance |
| `just dev status` | Check the status of the background instance |
| `just dev logs` | View recent logs |
| `just dev logs follow` | Continuously follow the logs |
| `just dev restart` | Restart the background instance |

Default ports for each service:

| Project | Default address |
| --- | --- |
| `registry-server` | `http://localhost:9001`, mTLS plane `https://localhost:9002` |
| `ca-server` | `http://localhost:9003` |
| `discovery-server` | `http://localhost:9005` |
| `mq-auth-server` | Group API `https://localhost:9007`, Auth API `https://localhost:9008` |
| `demo-leader` | Web UI `http://localhost:9030`, Leader API `http://localhost:9031` |
| `demo-partner` | Multiple Agent ports, commonly in the default range `9021-9025` |

Foreground startup is good for watching hot reloads and exception stack traces; background startup is good for working with the CLI, e2e, or manual browser-based integration testing.

## 9. `test`: Test Layering and Boundaries

The standard test entry points for each project are:

```bash
just test bootstrap
just test unit
just test integration
just test e2e
just test
```

By default, `just test` runs `all`, that is, the full test suite in the order agreed by the project. For most projects this is:

```text
unit -> integration -> e2e
```

### 9.1. Unit Tests

`unit` is the lightest day-to-day entry point and in principle does not depend on a real database, real network services, or sibling processes.

```bash
just test unit
```

When you change pure functions, configuration parsing, business rules, or command argument parsing, run it first. Most projects also provide a coverage entry point:

```bash
just test coverage
```

### 9.2. Integration Tests

`integration` verifies the integration between this repository's code and the real dependencies this repository needs. For example:

- `registry-server`, `ca-server`, and `discovery-server` use the test database.
- Some integration tests in `mq-auth-server` use a real Redis.
- The integration tests for `demo-partner` and `demo-leader` involve a real LLM or a real Partner service.
- The integration tests for `acps-cli` focus on CLI arguments, configuration, output, and single-service command contracts.

The standard entry point is:

```bash
just test integration
```

If the test environment is not ready, run this first:

```bash
just test bootstrap
```

### 9.3. End-to-End Tests

`e2e` is black-box verification, but the scope of "black box" falls into two categories:

- In server repositories, `e2e` mainly verifies this service's self-contained behavior. Many repositories have the Justfile spin up a temporary test instance of the service and then inject `TEST_E2E_BASE_URL`.
- In `acps-cli`, `e2e` is the main entry point for cross-service integration testing, used to verify the real collaboration path across registry, ca, discovery, mq, and so on.

The standard entry point is:

```bash
just test e2e
```

The most important boundary rule:

> If a test must verify real interactions among multiple sibling services at the same time, it should preferably live in `acps-cli/tests/e2e/` rather than being squeezed further into the `tests/e2e/` of some server repository.

Each server repository's own `integration` / `e2e` should keep this service as self-contained as possible: express external sibling interactions with fake peers, stub transports, contract fixtures, or managed temporary instances.

### 9.4. The Integration Testing Mode of `acps-cli`

`acps-cli` is the main repository for real cross-service integration testing. The recommended flow is:

```bash
cd acps-cli
just test bootstrap
just test e2e
```

If you use the default local addresses and no service is listening on the default ports, the `acps-cli` test fixtures will host and start the required sibling services as needed. If you have already started services on the default ports by hand, the tests will reuse them. If you have changed to custom addresses via environment variables, the tests assume that you manage the target environment yourself and will not automatically fall back to the default ports.


## 10. `qa`: Quality Checks Before Committing

Common entry points:

```bash
just qa
```

For most projects, `just qa` by default runs:

```text
fix -> precommit
```

That is, first format and automatically fix Ruff issues, then run pre-commit. Some services also add `audit` to the default all target, or provide read-only sub-entry points such as `lint` / `type` / `security` for you to combine into gates as needed:

| Command | Description |
| --- | --- |
| `just qa fmt` | Format only |
| `just qa fix` | Format and run `ruff check --fix` |
| `just qa lint` | Read-only lint, provided by some projects |
| `just qa type` | Compatibility entry point for mypy type checking, provided by some projects |
| `just qa type-app` | Check business code |
| `just qa type-tests` | Check test code |
| `just qa security` | Bandit security scan, provided by some projects |
| `just qa audit` | pip-audit dependency audit, provided by some projects |
| `just qa precommit` | Run `pre-commit run --all-files` |

Recommendations for day-to-day development:

- Small changes: run `just test unit`, then `just qa`.
- Changes touching the database, configuration, certificates, or service boundaries: additionally run `just test integration`.
- Changes touching HTTP APIs, process startup, or cross-service collaboration: additionally run `just test e2e`, or switch to `acps-cli` to run integration e2e.

## 11. Per-Project Differences Quick Reference

| Project | Points that need special attention |
| --- | --- |
| `registry-server` | Runs on a dual plane, `9001` public API and `9002` mTLS API; this repository's tests do not carry real cross-service integration testing; for read-only gates, combine `qa lint` / `qa type-app` / `qa type-tests` / `qa security` |
| `ca-server` | Local development certificate material comes from `../acps-infra/dev-infra`; by default it can mock `registry-server`; cross-service certificate chains belong to `acps-cli/tests/e2e/` |
| `discovery-server` | Has CPU / GPU run modes; `prep seed` / `reseed` manage sample ACS; the standard integration / e2e targets run CPU and then GPU in turn when no mode is specified |
| `mq-auth-server` | No database; depends on Redis, RabbitMQ, and mTLS; `prep migrate` is explicitly skipped; e2e uses a temporary local HTTPS listener |
| `demo-partner` | Multiple Agents and multiple ports; integration tests depend on LLM configuration; e2e verifies the running Partner API and task state machine |
| `demo-leader` | Leader API + Web UI, two processes; integration tests usually need `demo-partner` started first; `test integration` uses file-level parallelism by default |
| `acps-cli` | Has no `just dev start`; it is the main entry point for cross-service integration e2e; when services are missing at the default addresses, the test fixtures can automatically host sibling services |
| `acps-sdk` | Currently has no unified `Justfile`; use Python package commands such as `uv sync`, `uv build`, and `uv publish` |
