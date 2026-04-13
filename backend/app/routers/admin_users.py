from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.roles import ROLE_ADMIN, ROLE_USER, VALID_ROLES
from ..db import get_session
from ..deps.auth import require_admin
from ..models.user import User
from ..core.passwords import AdminNewPassword, AdminPasswordOptional, hash_password
from ..services.bootstrap_users import count_admins

router = APIRouter(prefix="/api/admin", tags=["admin", "users"])


class AdminUserRead(BaseModel):
    id: str
    username: str
    role: str
    created_at: str


class AdminUserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: AdminNewPassword
    role: Literal["user", "admin"] = ROLE_USER


class AdminUserUpdate(BaseModel):
    role: Optional[Literal["user", "admin"]] = None
    password: AdminPasswordOptional = None


@router.get("/users", response_model=list[AdminUserRead])
async def list_users(
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> list[AdminUserRead]:
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()
    return [
        AdminUserRead(
            id=u.id,
            username=u.username,
            role=u.role,
            created_at=u.created_at.isoformat(),
        )
        for u in users
    ]


@router.post("/users", response_model=AdminUserRead, status_code=status.HTTP_201_CREATED)
async def admin_create_user(
    body: AdminUserCreate,
    _: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AdminUserRead:
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="无效的角色")

    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="用户名已存在")

    user = User(
        username=body.username,
        hashed_password=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return AdminUserRead(
        id=user.id,
        username=user.username,
        role=user.role,
        created_at=user.created_at.isoformat(),
    )


@router.patch("/users/{user_id}", response_model=AdminUserRead)
async def admin_update_user(
    user_id: str,
    body: AdminUserUpdate,
    current_admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AdminUserRead:
    if body.role is None and body.password is None:
        raise HTTPException(status_code=400, detail="请提供要更新的字段")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    if body.role is not None:
        if body.role not in VALID_ROLES:
            raise HTTPException(status_code=400, detail="无效的角色")
        if user.role == ROLE_ADMIN and body.role == ROLE_USER:
            admins = await count_admins(db)
            if admins <= 1:
                raise HTTPException(status_code=400, detail="不能移除系统唯一的管理员")
        user.role = body.role

    if body.password is not None:
        user.hashed_password = hash_password(body.password)

    await db.commit()
    await db.refresh(user)
    return AdminUserRead(
        id=user.id,
        username=user.username,
        role=user.role,
        created_at=user.created_at.isoformat(),
    )


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_user(
    user_id: str,
    current_admin: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    if user_id == current_admin.id:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    if user.role == ROLE_ADMIN:
        admins = await count_admins(db)
        if admins <= 1:
            raise HTTPException(status_code=400, detail="不能删除系统唯一的管理员")

    await db.execute(delete(User).where(User.id == user_id))
    await db.commit()
