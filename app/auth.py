"""基于角色的访问控制。

权限矩阵：
- read_detail（明细查询）：clinician / admin
- export（导出）：auditor / admin
所有拒绝只说明动作与角色，不包含任何原始敏感值。
"""
from __future__ import annotations

from enum import Enum

from app.errors import PermissionDeniedError


class Role(str, Enum):
    CLINICIAN = "clinician"
    AUDITOR = "auditor"
    ADMIN = "admin"


_PERMISSIONS: dict[str, set[Role]] = {
    "read_detail": {Role.CLINICIAN, Role.ADMIN},
    "export": {Role.AUDITOR, Role.ADMIN},
}


def require(role: Role, action: str) -> None:
    """无权限时抛出 PermissionDeniedError（不含敏感值）。"""
    allowed = _PERMISSIONS.get(action, set())
    if role not in allowed:
        raise PermissionDeniedError(
            f"role={role.value} 无权执行 action={action}"
        )
