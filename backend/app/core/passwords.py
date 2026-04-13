"""
密码策略与 bcrypt 哈希（与 JWT 解耦，供路由与引导脚本复用）。

使用 Pydantic Annotated 类型避免在多个 Router 中重复 field_validator。
"""
from __future__ import annotations

from typing import Annotated, Optional

import bcrypt
from pydantic import AfterValidator

MAX_BCRYPT_PASSWORD_BYTES = 72


def _utf8_byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _reject_if_too_long_for_bcrypt(plain: str) -> str:
    n = _utf8_byte_len(plain)
    if n > MAX_BCRYPT_PASSWORD_BYTES:
        raise ValueError(
            f"密码过长（bcrypt 最多 {MAX_BCRYPT_PASSWORD_BYTES} 字节，当前 {n} 字节），请缩短或使用较短字符"
        )
    return plain


def verify_password(plain_password: str, hashed_password: str) -> bool:
    if not plain_password or not hashed_password:
        return False
    try:
        pw = plain_password.encode("utf-8")
        if len(pw) > MAX_BCRYPT_PASSWORD_BYTES:
            return False
        return bcrypt.checkpw(pw, hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def hash_password(plain: str) -> str:
    _reject_if_too_long_for_bcrypt(plain)
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


# --- 可复用的 Pydantic 类型 ---

PasswordPlain = Annotated[str, AfterValidator(_reject_if_too_long_for_bcrypt)]


def _admin_new_password(p: str) -> str:
    if len(p) < 6:
        raise ValueError("密码至少 6 位")
    if len(p) > 128:
        raise ValueError("密码最多 128 个字符")
    return _reject_if_too_long_for_bcrypt(p)


AdminNewPassword = Annotated[str, AfterValidator(_admin_new_password)]


def _admin_optional_password(p: Optional[str]) -> Optional[str]:
    if p is None:
        return None
    return _admin_new_password(p)


AdminPasswordOptional = Annotated[Optional[str], AfterValidator(_admin_optional_password)]
