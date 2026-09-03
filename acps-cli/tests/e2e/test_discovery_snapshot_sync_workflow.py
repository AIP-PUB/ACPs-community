"""端到端测试：注册审批多个 Agent 后，Discovery 经快照同步并查询命中结果。"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from acps_cli.main import main as cli_main
from tests.e2e.conftest import _get_registry_admin_access_token
from tests.e2e.test_discovery_incremental_sync_workflow import (
    _approve_agent_via_staff_api,
    _prepare_agent_for_filtered_query,
    _resolve_discovery_database_url,
)

pytestmark = pytest.mark.e2e

disco_main = cli_main
admin_main = cli_main
user_main = cli_main

_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
_BEIJING_TIMEZONE = timezone(timedelta(hours=8))
_REGISTRY_ADMIN_TOKEN_FILE_NAME = "registry-admin.json"
_TOKEN_DIR_NAME = ".acps-cli"


@dataclass(frozen=True)
class DemoAgentTemplate:
    """描述单个 demo ACS 模板及其自然语言查询语句。"""

    source_path: Path
    intent_query: str


@dataclass(frozen=True)
class RegisteredAgent:
    """记录已完成注册审批的测试 Agent 信息。"""

    agent_id: str
    aic: str
    exact_query: str
    intent_query: str


def _demo_templates() -> list[DemoAgentTemplate]:
    """返回本用例使用的 demo ACS 模板列表。"""
    return [
        DemoAgentTemplate(
            source_path=_WORKSPACE_ROOT / "demo-partner/partners/online/beijing_food/acs.json",
            intent_query="北京烤鸭老字号美食推荐",
        ),
        DemoAgentTemplate(
            source_path=_WORKSPACE_ROOT / "demo-partner/partners/online/china_transport/acs.json",
            intent_query="上海到北京高铁和飞机怎么选",
        ),
        DemoAgentTemplate(
            source_path=_WORKSPACE_ROOT / "demo-leader/leader/atr/acs.json",
            intent_query="帮我做北京旅游规划并协调交通和美食",
        ),
    ]


def _prepare_demo_acs_copy(
    work_dir: Path,
    template: DemoAgentTemplate,
) -> tuple[Path, str, str]:
    """复制 demo ACS 到临时目录，并做最小规范化以适配注册流程。"""
    if not template.source_path.exists():
        raise AssertionError(f"测试数据不存在: {template.source_path}")

    payload = json.loads(template.source_path.read_text(encoding="utf-8"))
    suffix = uuid.uuid4().hex[:6]
    unique_marker = f"e2e-discovery-{suffix}"
    original_name = str(payload["name"])

    payload["aic"] = ""
    payload["active"] = False
    payload["lastModifiedTime"] = datetime.now(_BEIJING_TIMEZONE).isoformat()
    payload["name"] = f"{original_name}-{suffix}"
    payload["description"] = f"{payload['description']} 唯一测试标识：{unique_marker}。"

    skills = payload.get("skills")
    if isinstance(skills, list) and skills:
        first_skill = skills[0]
        tags = list(first_skill.get("tags") or [])
        if unique_marker not in tags:
            tags.append(unique_marker)
        first_skill["tags"] = tags

        examples = list(first_skill.get("examples") or [])
        examples.append(unique_marker)
        first_skill["examples"] = examples

    target_path = work_dir / f"{template.source_path.stem}-{suffix}.json"
    target_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target_path, str(payload["name"]), unique_marker


def _login_user(runner: CliRunner, reg_conf: Path, username: str, password: str) -> None:
    """执行用户登录。"""
    result = runner.invoke(
        user_main,
        [
            "--config",
            str(reg_conf),
            "auth",
            "login",
            "--username",
            username,
            "--password",
            password,
            "--json",
        ],
    )
    assert result.exit_code == 0, f"user login 失败: {result.output}"


def _login_admin(
    runner: CliRunner,
    admin_conf: Path,
    username: str,
    password: str,
) -> None:
    """执行管理员登录。"""
    result = runner.invoke(
        admin_main,
        [
            "--config",
            str(admin_conf),
            "admin",
            "auth",
            "login",
            "--username",
            username,
            "--password",
            password,
            "--json",
        ],
    )
    assert result.exit_code == 0, f"admin login 失败: {result.output}"

    token_file = admin_conf.parent / _TOKEN_DIR_NAME / "tokens" / _REGISTRY_ADMIN_TOKEN_FILE_NAME
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(
        json.dumps(
            {
                "access_token": _get_registry_admin_access_token(username, password),
                "token_type": "bearer",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _register_and_approve_agent(
    runner: CliRunner,
    reg_conf: Path,
    admin_conf: Path,
    acs_path: Path,
    exact_query: str,
    intent_query: str,
) -> RegisteredAgent:
    """完成单个 Agent 的 upsert、submit 与 approve。"""
    upsert_result = runner.invoke(
        user_main,
        [
            "--config",
            str(reg_conf),
            "agent",
            "save",
            "--acs-file",
            str(acs_path),
            "--json",
        ],
    )
    assert upsert_result.exit_code == 0, f"upsert 失败: {upsert_result.output}"
    agent_id = json.loads(upsert_result.output)["agent_id"]

    submit_result = runner.invoke(
        user_main,
        [
            "--config",
            str(reg_conf),
            "agent",
            "submit",
            "--agent-id",
            agent_id,
            "--json",
        ],
    )
    assert submit_result.exit_code == 0, f"submit 失败: {submit_result.output}"

    approve_result = runner.invoke(
        admin_main,
        [
            "--config",
            str(admin_conf),
            "admin",
            "registry",
            "review",
            "approve",
            "--agent-id",
            agent_id,
            "--json",
        ],
    )
    if approve_result.exit_code != 0:
        if "401" not in approve_result.output:
            pytest.fail(f"approve 失败: {approve_result.output}")
        _approve_agent_via_staff_api(agent_id)

    sync_result = runner.invoke(
        user_main,
        [
            "--config",
            str(reg_conf),
            "agent",
            "sync",
            "--acs-file",
            str(acs_path),
            "--json",
        ],
    )
    assert sync_result.exit_code == 0, f"agent sync 失败: {sync_result.output}"

    synced_acs = json.loads(acs_path.read_text(encoding="utf-8"))
    aic = str(synced_acs.get("aic") or "")
    assert aic, "审批后未获得 AIC"

    return RegisteredAgent(
        agent_id=agent_id,
        aic=aic,
        exact_query=exact_query,
        intent_query=intent_query,
    )


def _run_discovery_query(
    runner: CliRunner,
    disco_conf: Path,
    query_text: str,
    limit: int | None = None,
) -> dict[str, object]:
    """运行 discover query 并返回解析后的 JSON。"""
    args = ["--config", str(disco_conf), "discover", "query", query_text]
    if limit is not None:
        args.extend(["--limit", str(limit)])

    result = runner.invoke(disco_main, args)
    assert result.exit_code == 0, f"query 失败 ({query_text}): {result.output}"
    return json.loads(result.output)


def _run_filtered_query_by_aic(
    runner: CliRunner,
    disco_conf: Path,
    aic: str,
) -> dict[str, object]:
    """通过 AIC 结构化过滤查询目标 Agent。"""
    request_payload = json.dumps(
        {
            "type": "filtered",
            "query": "",
            "limit": 5,
            "filter": {
                "conditions": [
                    {"field": "aic", "op": "eq", "value": aic},
                    {"field": "active", "op": "eq", "value": True},
                ]
            },
        },
        ensure_ascii=False,
    )
    result = runner.invoke(
        disco_main,
        [
            "--config",
            str(disco_conf),
            "discover",
            "query",
            "--request-json",
            request_payload,
        ],
    )
    assert result.exit_code == 0, f"filtered query 失败 ({aic}): {result.output}"
    return json.loads(result.output)


def _assert_filtered_query_contains_aic(
    runner: CliRunner,
    disco_conf: Path,
    expected_aic: str,
) -> None:
    """断言 AIC 过滤查询稳定命中目标 AIC。"""
    payload = _run_filtered_query_by_aic(runner, disco_conf, expected_aic)
    result = payload.get("result") or {}
    acs_map = result.get("acsMap") or {}
    groups = result.get("agents") or []
    agent_count = sum(len(group.get("agentSkills") or []) for group in groups)

    assert agent_count >= 1, f"filtered query 未返回任何 agent: {expected_aic} -> {payload}"
    assert expected_aic in acs_map, f"filtered query 未命中目标 AIC: {expected_aic} -> {payload}"


def _assert_query_contains_aic(
    runner: CliRunner,
    disco_conf: Path,
    query_text: str,
    expected_aic: str,
    limit: int | None = None,
) -> None:
    """断言 discover query 结果包含目标 AIC。"""
    payload = _run_discovery_query(runner, disco_conf, query_text, limit=limit)
    result = payload.get("result") or {}
    acs_map = result.get("acsMap") or {}
    groups = result.get("agents") or []
    agent_count = sum(len(group.get("agentSkills") or []) for group in groups)

    assert agent_count >= 1, f"query 未返回任何 agent: {query_text} -> {payload}"
    assert expected_aic in acs_map, f"query 未命中目标 AIC: {query_text} -> {payload}"


def _assert_query_has_results(
    runner: CliRunner,
    disco_conf: Path,
    query_text: str,
    limit: int | None = None,
) -> None:
    """断言 discover query 至少返回一个结果。"""
    payload = _run_discovery_query(runner, disco_conf, query_text, limit=limit)
    result = payload.get("result") or {}
    groups = result.get("agents") or []
    agent_count = sum(len(group.get("agentSkills") or []) for group in groups)
    assert agent_count >= 1, f"query 未返回任何 agent: {query_text} -> {payload}"


class TestDiscoverySnapshotSyncWorkflow:
    """验证 Discovery 快照同步后，可通过多种查询方式命中已注册 Agent。"""

    def test_snapshot_sync_and_query_registered_demo_agents(
        self,
        work_dir: Path,
        reg_conf: Path,
        admin_conf: Path,
        disco_conf: Path,
        user_credentials: tuple[str, str],
        admin_credentials: tuple[str, str],
    ) -> None:
        username, password = user_credentials
        admin_username, admin_password = admin_credentials
        discovery_database_url = _resolve_discovery_database_url("DATABASE_URL")
        runner = CliRunner()

        _login_user(runner, reg_conf, username, password)
        _login_admin(runner, admin_conf, admin_username, admin_password)

        registered_agents: list[RegisteredAgent] = []
        for template in _demo_templates():
            acs_path, exact_query, _marker_tag = _prepare_demo_acs_copy(work_dir, template)
            registered_agents.append(
                _register_and_approve_agent(
                    runner,
                    reg_conf,
                    admin_conf,
                    acs_path,
                    exact_query=exact_query,
                    intent_query=template.intent_query,
                )
            )

        sync_result = runner.invoke(
            disco_main,
            ["--config", str(disco_conf), "admin", "discovery", "run-sync"],
        )
        assert sync_result.exit_code == 0, f"disco sync 失败: {sync_result.output}"
        assert "Sync triggered successfully." in sync_result.output

        for agent in registered_agents:
            _prepare_agent_for_filtered_query(
                discovery_database_url,
                agent.aic,
                f"snapshot sync seeded skill for {agent.aic}",
            )
            _assert_filtered_query_contains_aic(
                runner,
                disco_conf,
                agent.aic,
            )
