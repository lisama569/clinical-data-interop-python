"""脱敏与令牌化测试。每个用例打印输入摘要与判定依据。"""

from __future__ import annotations

import logging

from app.masking import is_direct_identifier_key, mask_record, mask_value, tokenize

logger = logging.getLogger("clinical.interop.test")


def test_tokenize_deterministic_and_normalized() -> None:
    cases = [
        ("张三", " 张  三 "),
        ("ZHANG SAN", "zhang   san"),
        ("13800000000", "138-0000-0000"),
    ]
    for raw_a, raw_b in cases:
        logger.info("输入摘要: pair=(len_a=%d,len_b=%d) 判定: 归一化后令牌须一致", len(raw_a), len(raw_b))
        assert tokenize(raw_a) == tokenize(raw_b)
    assert tokenize("张三") != tokenize("李四")  # 不同值令牌不同
    assert len(tokenize("张三")) == 64  # sha256 hex


def test_tokenize_irreversible_shape() -> None:
    token = tokenize("110101199001011234")
    logger.info("输入摘要: id_number 长度=18 判定: 令牌不含明文子串且定长64")
    assert "110101" not in token
    assert len(token) == 64


def test_mask_value() -> None:
    logger.info("输入摘要: value='13800000000' 判定: 默认全遮蔽, keep=4 仅留尾4位")
    assert mask_value("13800000000") == "***"
    assert mask_value("13800000000", keep=4) == "*******0000"


def test_mask_record_fhir_like() -> None:
    record = {
        "resourceType": "Patient",
        "id": "P1",
        "name": [{"family": "Zhang", "given": ["San"]}],
        "telecom": [{"system": "phone", "value": "13800000000"}],
        "gender": "male",
    }
    masked = mask_record(record)
    logger.info("输入摘要: keys=%s 判定: name/telecom 子树遮蔽, gender/id 保留", list(record))
    assert masked["name"][0]["family"] == "***"
    assert masked["name"][0]["given"] == ["***"]
    assert masked["telecom"][0]["value"] == "***"
    assert masked["gender"] == "male"
    assert masked["id"] == "P1"


def test_mask_record_csv_row() -> None:
    row = {"patient_id": "P1", "patient_name": "张三", "id_number": "110101199001011234", "phone": "13800000000"}
    masked = mask_record(row)
    logger.info("输入摘要: columns=%s 判定: 直接标识列遮蔽, 业务ID保留", list(row))
    assert masked["patient_name"] == "***"
    assert masked["id_number"] == "***"
    assert masked["phone"] == "***"
    assert masked["patient_id"] == "P1"


def test_direct_identifier_keys() -> None:
    assert is_direct_identifier_key("phone")
    assert is_direct_identifier_key("Email")  # 大小写不敏感
    assert not is_direct_identifier_key("gender")
