"""API 鉴权：静态 Token + 三角色（viewer / operator / admin）。

三角色为本轮对细粒度 RBAC 的降级实现：
- ``VIEWER_TOKEN``  只读，可查总览/告警/诊断/知识库/审计；
- ``OPERATOR_TOKEN`` 可读 + 可执行（触发诊断、确认与关闭告警、确认/回滚经验）；
- ``AUTH_TOKEN``    管理员，额外可注册集群与操作 mock 控制面。

三个变量都不配置时视为本地体验模式，全部放行（actor 记为 anonymous）。
只配置其中一个时，该角色拥有其下所有权限。
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import Depends, Header, HTTPException, status

from app.config.settings import Settings, get_settings


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


ROLE_RANK: dict[Role, int] = {Role.VIEWER: 1, Role.OPERATOR: 2, Role.ADMIN: 3}


def role_tokens(settings: Settings | None = None) -> dict[str, Role]:
    """token → 角色映射（同一 token 只映射一个角色，高权限优先）。"""
    cfg = settings or get_settings()
    mapping: dict[str, Role] = {}
    for token, role in (
        (cfg.viewer_token, Role.VIEWER),
        (cfg.operator_token, Role.OPERATOR),
        (cfg.auth_token, Role.ADMIN),
    ):
        if token:
            mapping[token] = role
    return mapping


def extract_token(authorization: str | None, x_auth_token: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if x_auth_token:
        return x_auth_token.strip()
    return ""


def resolve_role(
    authorization: str | None = None, x_auth_token: str | None = None
) -> Role | None:
    """返回请求所属角色；未配置任何 token 时返回 ADMIN（本地体验模式）。"""
    settings = get_settings()
    mapping = role_tokens(settings)
    if not mapping:
        return Role.ADMIN
    return mapping.get(extract_token(authorization, x_auth_token))


async def require_auth(
    authorization: str | None = Header(default=None),
    x_auth_token: str | None = Header(default=None, alias="X-Auth-Token"),
) -> str:
    """校验身份并返回 actor 标识（角色名），用于审计。"""
    role = resolve_role(authorization, x_auth_token)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="鉴权失败：请提供 Authorization: Bearer <TOKEN> 或 X-Auth-Token",
        )
    return "anonymous" if not role_tokens() else role.value


def _require_min(role: Role, current: str) -> str:
    if not get_settings().auth_enabled:  # 本地体验模式：未配置任何 token 时放行
        return current
    mapped = {r.value: r for r in Role}
    rank = ROLE_RANK.get(mapped.get(current, Role.VIEWER), 0)
    if rank < ROLE_RANK[role]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"权限不足：该操作需要 {role.value} 及以上角色",
        )
    return current


async def require_operator(actor: str = Depends(require_auth)) -> str:
    """写操作：需要 operator 及以上。"""
    return _require_min(Role.OPERATOR, actor)


async def require_admin(actor: str = Depends(require_auth)) -> str:
    """管理操作：需要 admin。"""
    return _require_min(Role.ADMIN, actor)


__all__ = [
    "Role",
    "ROLE_RANK",
    "require_admin",
    "require_auth",
    "require_operator",
    "resolve_role",
    "role_tokens",
]
