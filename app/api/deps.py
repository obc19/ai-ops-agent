"""API 依赖：静态 Token 鉴权（未配置 AUTH_TOKEN 时开放，仅用于本地体验）。"""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from app.config.settings import get_settings


async def require_auth(
    authorization: str | None = Header(default=None),
    x_auth_token: str | None = Header(default=None, alias="X-Auth-Token"),
) -> str:
    """返回调用方标识，用于审计。"""
    settings = get_settings()
    if not settings.auth_token:
        return "anonymous"

    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_auth_token:
        token = x_auth_token.strip()

    if token != settings.auth_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="鉴权失败：请提供 Authorization: Bearer <AUTH_TOKEN>",
        )
    return "token-user"
