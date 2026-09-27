"""报文片段脱敏：姓名、证件号、联系方式在任何截断位置都不得出现原文。"""
import json
import logging

from app.masking import mask_payload_fragment

log = logging.getLogger("test.masking")

SALT = "test-salt"
RAW_NAME = "Zhang San"
RAW_ID = "110101199003077777"
RAW_PHONE = "13812345678"


def _assert_clean(fragment: str):
    log.info("脱敏片段=%s", fragment)
    for raw in (RAW_NAME, "Zhang", "San", RAW_ID, RAW_PHONE):
        assert raw not in fragment, f"片段中仍出现敏感原文: {raw}"


def test_fhir_family_and_given_masked():
    payload = json.dumps({
        "patient": {"id": RAW_ID, "identifier": [{"value": RAW_ID}],
                    "name": [{"family": "Zhang", "given": ["San"], "text": RAW_NAME}],
                    "telecom": [{"value": RAW_PHONE}]},
        "encounter": {"id": "e1"},
    })
    _assert_clean(mask_payload_fragment(payload, SALT))


def test_hl7_pid_fields_masked():
    payload = (
        "MSH|^~\\&|LAB|HOSP\r"
        f"PID|1||{RAW_ID}^^^HOSP||Zhang^San||M|||^^^{RAW_PHONE}\r"
        "OBR|1|O1|||p||20260101\r"
        "OBX|1|NM|XYZ^U||5|mg/dL"
    )
    _assert_clean(mask_payload_fragment(payload, SALT))


def test_fragment_truncated_inside_name_still_clean():
    # 构造一个姓名落在前 300 字符截断点附近的报文
    pad = "x" * 260
    payload = json.dumps({
        "patient": {"id": "p", "name": [{"text": pad + RAW_NAME}],
                    "telecom": [{"value": RAW_PHONE}]},
        "encounter": {"id": "e"},
    })
    fragment = mask_payload_fragment(payload, SALT)
    assert len(fragment) <= 300
    for raw in (RAW_NAME, "Zhang", "San", RAW_PHONE):
        assert raw not in fragment


def test_non_name_text_is_preserved():
    # 非姓名字段的业务文本不应被过度破坏
    payload = json.dumps({"note": "GLU value high", "patient": {"id": "p1"}})
    fragment = mask_payload_fragment(payload, SALT)
    assert "GLU value high" in fragment
