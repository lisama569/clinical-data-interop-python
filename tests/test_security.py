"""访问控制测试。"""

from __future__ import annotations

import logging

import pytest

from app.errors import AccessDenied
from app.models import Role
from app.security import can, require, role_from_header

logger = logging.getLogger("clinical.interop.test")


def test_role_resolution() -> None:
    logger.info("输入摘要: header=(None,'admin','hacker') 判定: 缺失/非法->anonymous")
    assert role_from_header(None) is Role.ANONYMOUS
    assert role_from_header("ADMIN") is Role.ADMIN
    assert role_from_header("hacker") is Role.ANONYMOUS


def test_permission_matrix() -> None:
    logger.info("输入摘要: role=reader action=export 判定: reader 无导出权限")
    assert can(Role.EXPORTER, "export:observations")
    assert not can(Role.READER, "export:observations")
    assert not can(Role.ANONYMOUS, "patient:read")
    assert can(Role.AUDITOR, "rejection:fragment")
    assert not can(Role.EXPORTER, "rejection:fragment")


def test_require_raises_without_leaking() -> None:
    logger.info("输入摘要: role=anonymous action=patient:read 判定: AccessDenied 且消息无数据")
    with pytest.raises(AccessDenied) as exc:
        require(Role.ANONYMOUS, "patient:read")
    assert "patient:read" in str(exc.value)
