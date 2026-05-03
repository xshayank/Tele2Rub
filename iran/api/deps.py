"""Shared FastAPI dependency functions for the Iran VPS service.

Provides:
- ``get_db`` — yields an async SQLAlchemy session.
- ``get_current_user`` — decodes JWT and returns the active user.
- ``require_admin`` — additional guard requiring ``role == 'admin'``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from iran.db.models import User

_bearer = HTTPBearer(auto_error=False)


async def get_db() -> AsyncGenerator["AsyncSession", None]:
    """FastAPI dependency that yields a transactional async DB session."""
    from iran.db.engine import get_async_session

    async with get_async_session() as session:
        yield session


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: "AsyncSession" = Depends(get_db),
) -> "User":
    """Decode and verify the JWT, returning the active ``User``.

    Raises ``401 Unauthorized`` when:
    - No token is supplied.
    - The token is malformed or expired.
    - The referenced user does not exist or is not ``active``.
    """
    from sqlalchemy import select

    from iran.api.auth import decode_access_token
    from iran.db.models import User

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id: str = payload["sub"]
    result = await session.execute(select(User).where(User.id == user_id))
    user: User | None = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account not active",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def require_admin(current_user: "User" = Depends(get_current_user)) -> "User":
    """Dependency that ensures the current user has the ``admin`` role.

    Raises ``403 Forbidden`` when the user does not have the ``admin`` role.
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return current_user
