"""异步数据库 session 工厂。

提供 SQLAlchemy async engine 和 session 工厂，以及 FastAPI 依赖注入 fixture。
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

_async_engine = create_async_engine(
    settings.database_url,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_recycle=settings.database_pool_recycle,
    pool_timeout=settings.database_pool_timeout,
    echo=False,
)

async_session_factory = async_sessionmaker(
    _async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_async_session() -> AsyncGenerator[AsyncSession]:
    """FastAPI 依赖注入：提供异步数据库 session。

    Yields:
        AsyncSession: 请求生命周期内的数据库 session。
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_async_engine() -> AsyncEngine:
    """返回全局异步数据库引擎（供 health probe 等内部模块使用）。"""
    return _async_engine


async def close_async_engine() -> None:
    """关闭异步数据库引擎，释放连接池。应在应用 lifespan 结束时调用。"""
    await _async_engine.dispose()
