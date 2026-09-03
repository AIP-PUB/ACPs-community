from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

from sqlmodel import SQLModel

# 导入所有 SQLModel 模型，确保 metadata 中包含全部表定义
import app.audit.model  # noqa: F401

target_metadata = SQLModel.metadata

from app.core.config import settings


def get_sync_db_url(async_url: str) -> str:
    """将 asyncpg DSN 转换为同步 psycopg DSN，供 Alembic 使用。"""
    if async_url.startswith("postgresql+asyncpg"):
        return async_url.replace("postgresql+asyncpg", "postgresql+psycopg", 1)
    if async_url.startswith("postgresql://"):
        return async_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return async_url


# ConfigParser treats '%' as interpolation; URL-encoded passwords (%21 etc.) need '%%'.
config.set_main_option(
    "sqlalchemy.url",
    get_sync_db_url(settings.database_url).replace("%", "%%"),
)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
