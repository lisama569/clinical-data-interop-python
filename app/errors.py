"""领域异常定义。"""

from __future__ import annotations

from app.models import ConflictVerdict, RejectReason


class ClinicalInteropError(Exception):
    """所有领域错误的基类（消息中禁止包含原始直接标识）。"""


class RecordRejected(ClinicalInteropError):
    """单条记录被拒绝。"""

    def __init__(self, reason: RejectReason, detail: str, locator: str = "") -> None:
        super().__init__(f"{reason.value}: {detail} @ {locator}")
        self.reason = reason
        self.detail = detail
        self.locator = locator


class ConflictError(ClinicalInteropError):
    """资源并发/重复更新冲突。"""

    def __init__(self, resource_type: str, source_id: str, verdict: ConflictVerdict) -> None:
        super().__init__(f"{verdict.value}: {resource_type}/{source_id}")
        self.resource_type = resource_type
        self.source_id = source_id
        self.verdict = verdict


class AccessDenied(ClinicalInteropError):
    """权限不足。错误信息不得包含原始值。"""

    def __init__(self, action: str) -> None:
        super().__init__(f"access denied for action: {action}")
        self.action = action


class BatchInconsistent(ClinicalInteropError):
    """批次声明与已存储批次不一致（同 batch_id 不同内容）。"""


class UnparseablePayload(ClinicalInteropError):
    """整批负载完全无法解析（格式级失败）。"""
