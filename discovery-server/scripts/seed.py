#!/usr/bin/env python3
"""导入本地 demo ACS 样本到 discovery-server 指定数据库。"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import click
from dotenv import dotenv_values

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
ENV_FILE = REPO_ROOT / ".env"
PARTNER_SEED_DIR = WORKSPACE_ROOT / "demo-partner" / "partners" / "online"
LEADER_SEED_PATH = WORKSPACE_ROOT / "demo-leader" / "leader" / "atr" / "acs.json"


@dataclass(frozen=True)
class SeedSource:
    """一个 ACS 样本来源。"""

    kind: str
    name: str
    path: Path


@dataclass(frozen=True)
class SeedSummary:
    """seed 执行摘要。"""

    target: str
    source_count: int
    imported_agents: int
    imported_skills: int
    reset_applied: bool
    reset_deleted_agents: int
    include_leader: bool
    dry_run: bool


@dataclass(frozen=True)
class SeedTargetContext:
    """seed 目标数据库上下文。"""

    target: str
    app_env: str
    database_url: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="向 discovery-server 指定数据库导入本地 demo ACS 样本")
    parser.add_argument(
        "target",
        nargs="?",
        choices=("app", "test"),
        default="app",
        help="目标数据库，app=开发库，test=测试库",
    )
    parser.add_argument(
        "--include-leader",
        action="store_true",
        help="额外导入 demo-leader 的 ACS（默认仅导入 partner ACS）",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="导入前先清空本地 Agent/Skill 数据",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要导入的样本摘要，不执行数据库写入",
    )
    return parser.parse_args(argv)


def _resolve_env_value(key: str) -> str:
    """优先环境变量，其次 .env，解析指定键。"""

    value = os.getenv(key, "").strip()
    if value:
        return value

    if not ENV_FILE.is_file():
        return ""

    dotenv_value = dotenv_values(ENV_FILE).get(key)
    if isinstance(dotenv_value, str):
        return dotenv_value.strip()
    return ""


def configure_target_environment(target: str) -> SeedTargetContext:
    """根据 app/test 目标切换运行环境。"""

    if target == "test":
        database_url = _resolve_env_value("TEST_DATABASE_URL")
        if not database_url:
            raise ValueError("未配置 TEST_DATABASE_URL，无法向测试数据库导入样本")

        os.environ["APP_ENV"] = "testing"
        os.environ["DATABASE_URL"] = database_url
        os.environ["TEST_DATABASE_URL"] = database_url
        return SeedTargetContext(target="test", app_env="testing", database_url=database_url)

    database_url = _resolve_env_value("DATABASE_URL")
    if not database_url:
        raise ValueError("未配置 DATABASE_URL，无法向开发数据库导入样本")

    app_env = _resolve_env_value("APP_ENV") or "development"
    if app_env == "testing":
        app_env = "development"

    os.environ["APP_ENV"] = app_env
    os.environ["DATABASE_URL"] = database_url
    return SeedTargetContext(target="app", app_env=app_env, database_url=database_url)


def resolve_seed_sources(include_leader: bool = False) -> list[SeedSource]:
    """解析默认 demo ACS 来源。"""

    partner_paths = sorted(PARTNER_SEED_DIR.glob("*/acs.json"), key=lambda path: path.parent.name)
    if not partner_paths:
        raise FileNotFoundError(f"未找到 partner ACS 样本目录: {PARTNER_SEED_DIR}")

    sources = [SeedSource(kind="partner", name=path.parent.name, path=path) for path in partner_paths]

    if include_leader:
        if not LEADER_SEED_PATH.is_file():
            raise FileNotFoundError(f"未找到 leader ACS 样本文件: {LEADER_SEED_PATH}")
        sources.append(SeedSource(kind="leader", name="leader", path=LEADER_SEED_PATH))

    return sources


def load_seed_payload(source: SeedSource) -> dict[str, Any]:
    """读取并校验一个 ACS 样本。"""

    payload = json.loads(source.path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"ACS 样本必须是 JSON object: {source.path}")

    aic = str(payload.get("aic") or "").strip()
    if not aic:
        raise ValueError(f"ACS 样本缺少 aic 字段: {source.path}")

    return payload


def build_seed_records(include_leader: bool = False) -> list[tuple[SeedSource, dict[str, Any]]]:
    """构建待导入的 ACS 记录。"""

    return [(source, load_seed_payload(source)) for source in resolve_seed_sources(include_leader=include_leader)]


def count_seed_skills(records: list[tuple[SeedSource, dict[str, Any]]]) -> int:
    """统计样本中的技能数量。"""

    total = 0
    for _, payload in records:
        skills = payload.get("skills")
        if isinstance(skills, list):
            total += len(skills)
    return total


def build_seed_summary(
    records: list[tuple[SeedSource, dict[str, Any]]],
    *,
    target: str,
    include_leader: bool,
    dry_run: bool,
    reset_applied: bool,
    reset_deleted_agents: int = 0,
) -> SeedSummary:
    """根据 ACS 记录生成摘要。"""

    return SeedSummary(
        target=target,
        source_count=len(records),
        imported_agents=len(records),
        imported_skills=count_seed_skills(records),
        reset_applied=reset_applied,
        reset_deleted_agents=reset_deleted_agents,
        include_leader=include_leader,
        dry_run=dry_run,
    )


def build_seed_envelopes(records: list[tuple[SeedSource, dict[str, Any]]]) -> list[Any]:
    """将 ACS 记录转换为合成 DSP Envelope。"""

    from app.sync.model import Envelope, OperationType

    envelopes = []
    for _, payload in records:
        aic = str(payload["aic"]).strip()
        envelopes.append(
            Envelope(
                seq=0,
                ts=None,
                op=OperationType.UPSERT,
                type="acs",
                id=aic,
                version=1,
                payload=copy.deepcopy(payload),
            )
        )
    return envelopes


def create_semantic_matcher() -> Any | None:
    """按当前 discovery 配置初始化 matcher。"""

    from app.core.config import settings
    from app.discovery.semantic_matcher import SemanticAgentMatcher

    mode = (settings.DISCOVERY_MODE or "cpu").strip().lower()
    if mode == "cpu":
        if not settings.EMBEDDING_BASE_URL.strip():
            click.echo("[WARN] 未配置 EMBEDDING_BASE_URL，seed 期间跳过语义索引初始化。")
            return None
        return SemanticAgentMatcher(
            mode=mode,
            api_key=settings.EMBEDDING_API_KEY,
            base_url=settings.EMBEDDING_BASE_URL,
            model_name=settings.EMBEDDING_MODEL_NAME,
            batch_size=settings.BGE_BATCH_SIZE,
            max_wait_time=settings.BGE_MAX_WAIT_TIME,
        )

    if not settings.EMBEDDING_MODEL_PATH.strip():
        click.echo("[WARN] 未配置 EMBEDDING_MODEL_PATH，seed 期间跳过语义索引初始化。")
        return None

    matcher = SemanticAgentMatcher(
        mode=mode,
        model_path=settings.EMBEDDING_MODEL_PATH,
        devices=settings.embedding_devices_list,
        reranker_url=settings.RERANKER_URL,
        batch_size=settings.BGE_BATCH_SIZE,
        max_wait_time=settings.BGE_MAX_WAIT_TIME,
    )
    matcher.start_workers()
    return matcher


async def reset_seed_data() -> int:
    """清空本地 Agent 数据。"""

    from sqlalchemy import delete, text

    from app.core.database import get_async_session_context
    from app.sync.model import Agent

    async with get_async_session_context() as session, session.begin():
        await session.execute(text("TRUNCATE TABLE available_agents_runtime"))
        result = cast("CursorResult[Any]", await session.execute(delete(Agent)))
        return result.rowcount or 0


async def refresh_skills_without_embeddings(agent_data: dict[str, Any]) -> None:
    """在缺少 embedding 运行时配置时，仅回填技能元数据。"""

    from sqlalchemy import delete

    from app.core.config import settings
    from app.core.database import get_async_session_context
    from app.discovery.singleton import AgentDiscovery
    from app.sync.model import Skill

    agent_aic = str(agent_data.get("aic") or agent_data.get("AIC") or "").strip()
    if not agent_aic:
        return

    skill_candidates = AgentDiscovery._expand_agents_to_skills([agent_data])
    zero_embedding = [0.0] * settings.EMBEDDING_DIM

    async with get_async_session_context() as session, session.begin():
        await session.execute(delete(Skill).where(cast("Any", Skill.aic) == agent_aic))
        for skill_candidate in skill_candidates:
            session.add(
                Skill(
                    aic=agent_aic,
                    skill_id=str(skill_candidate.get("skillid") or ""),
                    description=str(skill_candidate.get("description") or ""),
                    embedding=list(zero_embedding),
                    sparse_embedding=None,
                )
            )


async def ensure_seed_skills_present(agent_data: dict[str, Any]) -> None:
    """确保 seed 导入后的技能元数据已经落库。"""

    from sqlalchemy import func, select

    from app.core.database import get_async_session_context
    from app.sync.model import Skill

    agent_aic = str(agent_data.get("aic") or agent_data.get("AIC") or "").strip()
    if not agent_aic:
        return

    async with get_async_session_context() as session:
        skill_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Skill).where(cast("Any", Skill.aic) == agent_aic)
                )
            ).scalar_one()
        )

    if skill_count > 0:
        return

    await refresh_skills_without_embeddings(agent_data)


async def import_seed_records(
    records: list[tuple[SeedSource, dict[str, Any]]], *, target: str, reset: bool
) -> SeedSummary:
    """执行本地样本导入。"""

    from app.core.config import settings
    from app.core.database import close_db
    from app.discovery.semantic_matcher_holder import set_matcher
    from app.sync.client import DSPClient

    matcher = create_semantic_matcher()
    set_matcher(matcher)
    client = DSPClient(
        registry_base_url=settings.DSP_BASE_URL,
        sync_interval=settings.DSP_CHANGES_PULL_INTERVAL,
        target_types=["acs"],
    )

    reset_deleted_agents = 0
    try:
        if reset:
            reset_deleted_agents = await reset_seed_data()

        for envelope in build_seed_envelopes(records):
            payload = cast("dict[str, Any]", envelope.payload)
            if "aic" not in payload:
                payload["aic"] = envelope.id

            await client._apply_to_database(envelope)
            if matcher is None:
                await refresh_skills_without_embeddings(payload)
            else:
                await client.update_search_index(envelope)
                await ensure_seed_skills_present(payload)
    finally:
        await client.close()
        if matcher is not None and getattr(matcher, "mode", "") != "cpu":
            await matcher.stop_workers()
        set_matcher(None)
        await close_db()

    return build_seed_summary(
        records,
        target=target,
        include_leader=any(source.kind == "leader" for source, _ in records),
        dry_run=False,
        reset_applied=reset,
        reset_deleted_agents=reset_deleted_agents,
    )


def print_summary(summary: SeedSummary) -> None:
    """打印摘要信息。"""

    mode_label = "dry-run" if summary.dry_run else "import"
    click.echo(
        f"[INFO] seed {mode_label}({summary.target}): 来源 {summary.source_count} 个，"
        f"Agent {summary.imported_agents} 个，Skill {summary.imported_skills} 个"
    )
    if summary.include_leader:
        click.echo("[INFO] 已包含 demo-leader ACS 样本")
    if summary.reset_applied:
        click.echo(f"[INFO] 已在导入前清空本地 Agent 数据：{summary.reset_deleted_agents} 条")
    click.echo("[INFO] 已使用合成 Envelope(seq=0) 导入，后续真实 DSP 仍会走完整快照同步")


async def async_main(args: argparse.Namespace) -> int:
    """脚本异步入口。"""

    records = build_seed_records(include_leader=args.include_leader)
    if args.dry_run:
        print_summary(
            build_seed_summary(
                records,
                target=args.target,
                include_leader=args.include_leader,
                dry_run=True,
                reset_applied=args.reset,
            )
        )
        return 0

    configure_target_environment(args.target)
    summary = await import_seed_records(records, target=args.target, reset=args.reset)
    print_summary(summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    """脚本同步入口。"""

    args = parse_args(argv)
    try:
        return asyncio.run(async_main(args))
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"[ERROR] {exc}", err=True)
        return 1
    except KeyboardInterrupt:
        click.echo("[ERROR] 用户取消 seed 导入。", err=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
