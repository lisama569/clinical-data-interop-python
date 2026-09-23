"""基于角色的访问控制。

动作（action）粒度的权限表；所有错误信息只包含动作名，
绝不回显任何业务数据或直接标识。
"""

from __future__ import annotations

from app.errors import AccessDenied
from app.models import Role

# 动作 -> 允许的角色集合
_ALL_READ_ROLES = frozenset({Role.READER, Role.AUDITOR, Role.ADMIN})

PERMISSIONS: dict[str, frozenset[Role]] = {
    "ingest:submit": frozenset({Role.INGEST, Role.ADMIN}),
    "patient:read": _ALL_READ_ROLES,
    "observation:read": _ALL_READ_ROLES,
    "rejection:list": frozenset({Role.AUDITOR, Role.ADMIN}),
    "rejection:fragment": frozenset({Role.AUDITOR}),  # 原始片段仅审计角色
    "export:observations": frozenset({Role.EXPORTER, Role.ADMIN}),
    "observation:update": frozenset({Role.ADMIN}),
}


def can(role: Role, action: str) -> bool:
    return role in PERMISSIONS.get(action, frozenset())


def require(role: Role, action: str) -> None:
    """校验角色是否可执行动作；否则抛 :class:`AccessDenied`。"""

    if not can(role, action):
        raise AccessDenied(action)


def role_from_header(value: str | None) -> Role:
    """解析 ``X-Role`` 请求头；缺失或非法一律视为匿名。"""

    if not value:
        return Role.ANONYMOUS
    try:
        return Role(value.strip().lower())
    except ValueError:
        return Role.ANONYMOUS
