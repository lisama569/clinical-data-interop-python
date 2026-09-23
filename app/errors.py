"""摄取过程中的错误类型。"""
from __future__ import annotations

from app.models import Rejection


class RecordRejectedError(Exception):
    """单条记录被拒绝（携带原因与原始片段）。"""

    def __init__(self, rejection: Rejection) -> None:
        super().__init__(f"{rejection.reason.value}: {rejection.detail}")
        self.rejection = rejection


class PermissionDeniedError(Exception):
    """访问控制拒绝：错误信息中不得包含任何原始敏感值。"""
