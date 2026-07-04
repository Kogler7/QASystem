import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..core.roles import ROLE_ADMIN
from ..models.user import User
from ..core.passwords import hash_password

logger = logging.getLogger(__name__)


async def ensure_bootstrap_admin(session: AsyncSession) -> None:
    """若配置了 BOOTSTRAP_ADMIN_*，则保证该账号存在且为管理员。"""
    username = (settings.BOOTSTRAP_ADMIN_USERNAME or "").strip()
    password = settings.BOOTSTRAP_ADMIN_PASSWORD or ""
    if not username or not password:
        return

    try:
        bootstrap_hash = hash_password(password)
    except ValueError as e:
        logger.error("BOOTSTRAP_ADMIN 密码无效，已跳过引导: %s", e)
        return

    result = await session.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if user is None:
        session.add(
            User(
                username=username,
                hashed_password=bootstrap_hash,
                role=ROLE_ADMIN,
            )
        )
        await session.commit()
        logger.info("已创建引导管理员账号: %s", username)
        return

    if user.role != ROLE_ADMIN:
        user.role = ROLE_ADMIN
        user.hashed_password = bootstrap_hash
        await session.commit()
        logger.info("已将引导账号设为管理员并更新密码: %s", username)
        return

    # 已是管理员：按需重置密码（便于运维恢复）
    user.hashed_password = bootstrap_hash
    await session.commit()
    logger.info("已更新引导管理员密码: %s", username)


async def count_admins(session: AsyncSession) -> int:
    r = await session.execute(select(func.count(User.id)).where(User.role == ROLE_ADMIN))
    return int(r.scalar_one() or 0)
