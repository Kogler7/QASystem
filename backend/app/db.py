import logging
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base

from .config import settings

logger = logging.getLogger(__name__)

# 配置数据库连接池参数
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
    pool_size=10,  # 连接池大小
    max_overflow=20,  # 最大溢出连接数
    pool_pre_ping=True,  # 连接前检查连接是否有效
    pool_recycle=3600,  # 连接回收时间（秒）
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    class_=AsyncSession,
    autocommit=False,
    autoflush=False,
)

Base = declarative_base()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话的依赖注入函数。"""
    logger.debug("创建数据库会话")
    try:
        async with AsyncSessionLocal() as session:
            try:
                logger.debug("数据库会话已创建，yield session")
                yield session
            except Exception as e:
                logger.error(f"数据库会话异常: {e}", exc_info=True)
                await session.rollback()
                raise
    except Exception as e:
        logger.error(f"创建数据库会话失败: {e}", exc_info=True)
        raise


async def init_db() -> None:
    """初始化数据库，创建所有表。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

