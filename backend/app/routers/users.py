from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from ..db import get_session
from ..deps.auth import get_current_user
from ..models.user import User
from ..services.auth import create_access_token, get_password_hash, verify_password

router = APIRouter(prefix="/api", tags=["users", "auth"])


class UserCreate(BaseModel):
    username: str
    password: str


class UserRead(BaseModel):
    id: int
    username: str
    role: str
    created_at: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/auth/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(
    body: UserCreate,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> UserRead:
    stmt = select(User).where(User.username == body.username)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")

    user = User(
        username=body.username,
        hashed_password=get_password_hash(body.password),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return UserRead(
        id=user.id,
        username=user.username,
        role=user.role,
        created_at=user.created_at.isoformat(),
    )


@router.post("/auth/login", response_model=Token)
async def login(
    body: UserCreate,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Token:
    stmt = select(User).where(User.username == body.username)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    token = create_access_token({"sub": user.id})
    return Token(access_token=token)


@router.get("/users/me", response_model=UserRead)
async def read_current_user(
    current_user: Annotated[User, Depends(get_current_user)],
) -> UserRead:
    return UserRead(
        id=current_user.id,
        username=current_user.username,
        role=current_user.role,
        created_at=current_user.created_at.isoformat(),
    )

