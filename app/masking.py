"""敏感信息脱敏与不可逆令牌化。

设计约定：
- 直接标识（姓名、证件号、联系方式）永不以明文进入持久化的读取路径；
- ``tokenize`` 生成带盐 HMAC-SHA256 令牌，供跨记录身份匹配，不可逆推；
- ``mask_record`` / ``mask_value`` 用于所有对外展示、日志与预览；
- 盐从环境变量 ``CLINICAL_MASKING_SALT`` 读取；未设置时使用开发默认盐
  （生产部署必须显式设置，见 docs/security.md）。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import unicodedata
from typing import Any

# 归一化时视为直接标识的字段名（小写匹配，兼容 FHIR 与 CSV 列名）
_DIRECT_ID_KEYS = frozenset(
    {
        "name",
        "family",
        "given",
        "text",  # HumanName.text
        "id_number",
        "id_no",
        "identity",
        "passport",
        "certificate",
        "ssn",
        "phone",
        "mobile",
        "tel",
        "telecom",
        "email",
        "mail",
        "contact",
        "contact_name",
        "address",
        "line",
        "postal_code",
        "zip",
    }
)

# 允许以列名后缀出现的直接标识（如 patient_name、home_phone）
_DIRECT_ID_SUFFIXES = (
    "_name",
    "_family",
    "_given",
    "_id_number",
    "_id_no",
    "_passport",
    "_ssn",
    "_phone",
    "_mobile",
    "_tel",
    "_telecom",
    "_email",
    "_mail",
    "_contact_name",
    "_address",
    "_postal_code",
    "_zip",
)

_WS_RE = re.compile(r"\s+")
_MASK = "***"


def _salt() -> bytes:
    return os.environ.get("CLINICAL_MASKING_SALT", "dev-salt-change-me").encode("utf-8")


def _strip_punctuation(text: str) -> str:
    """删除 Unicode 标点类别字符（电话号码连字符、证件中的 x 分隔等保留字母数字与 CJK）。"""

    return "".join(ch for ch in text if not unicodedata.category(ch).startswith("P"))


def _normalize(value: str) -> str:
    """删除全部空白与标点并小写化。

    姓名/证件号/联系方式中的空白与分隔符不具语义，统一剔除，
    保证不同来源格式不一致时令牌仍可匹配（如 张三 == 张 三，
    138-0000-0000 == 13800000000）。
    """

    no_space = _WS_RE.sub("", value.strip())
    return _strip_punctuation(no_space).casefold()


def tokenize(value: str) -> str:
    """对直接标识做带盐 HMAC-SHA256，返回 64 位十六进制不可逆令牌。"""

    if not isinstance(value, str):
        raise TypeError("tokenize expects str")
    digest = hmac.new(_salt(), _normalize(value).encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


def mask_value(value: str, *, keep: int = 0) -> str:
    """将明文替换为脱敏展示。

    keep=0 时完全遮蔽；keep>0 时保留末尾固定位数（用于尾号核对）。
    """

    text = str(value)
    if keep <= 0 or len(text) <= keep:
        return _MASK
    return "*" * (len(text) - keep) + text[-keep:]


def is_direct_identifier_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _DIRECT_ID_KEYS:
        return True
    return lowered.endswith(_DIRECT_ID_SUFFIXES)


def _mask_leaves(node: Any) -> Any:
    """遮蔽子树内所有字符串叶子（用于直接标识字段的整棵子树）。"""

    if isinstance(node, str):
        return mask_value(node)
    if isinstance(node, list):
        return [_mask_leaves(item) for item in node]
    if isinstance(node, dict):
        return {k: _mask_leaves(v) for k, v in node.items()}
    return node


def mask_record(data: Any) -> Any:
    """递归脱敏 dict/list 中的直接标识字段，其余字段保持不变。

    - dict 中命中直接标识名的键，其整棵子树的字符串叶子都被遮蔽；
    - 普通键继续递归处理，保证深层（如 telecom[].value）也被覆盖；
    - 标量原样返回。
    """

    if isinstance(data, dict):
        result: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(key, str) and is_direct_identifier_key(key):
                result[key] = _mask_leaves(value)
            else:
                result[key] = mask_record(value)
        return result
    if isinstance(data, list):
        return [mask_record(item) for item in data]
    return data
