# HTTP 接口

所有接口以 `X-Role` 头携带角色；响应中不出现任何直接标识明文。

## POST `/ingest/{batch_id}?f=fhir-json|csv`

提交导入批次，body 为原始 FHIR JSON 或 CSV 文本。

```bash
curl -XPOST "localhost:8000/ingest/BATCH-001?f=fhir-json" \
  -H "X-Role: ingest" -H "Content-Type: application/json" \
  -d @samples/patient_observation.json
```

成功响应（200）：

```json
{
  "batch_id": "BATCH-001",
  "status": "ACCEPTED",
  "replay": false,
  "accepted_counts": {"Patient": 1, "Encounter": 0, "Observation": 1},
  "rejected_count": 0,
  "conflicts": [],
  "rejections": []
}
```

- 重复/并发同批次：`status="REPLAYED", replay=true`，计数为 0。
- 同 `batch_id` 不同负载：409。
- 拒绝项形如：
  `{"index":1,"locator":"Bundle.entry[1]","reason":"UNKNOWN_CODE",
    "detail":"unrecognized observation code: '9999-9'","masked_preview":"..."}`

## GET `/patients/{source_system}/{source_id}`

脱敏患者明细（`reader/auditor/admin`）。只返回令牌与非标识属性：

```json
{"source_system":"sysA","source_id":"P1","name_token":"ab12...","ident_token":"...",
 "contact_token":"...","birth_date":"1990-01-01","gender":"male","version":1}
```

## GET `/patients/{patient_source_id}/observations`

该患者观测列表（脱敏）。

## GET `/export/observations`

批量导出（`exporter/admin`），返回规范编码与规范单位下的观测 DTO。

## GET `/rejections?batch_id=...`

拒绝清单（`auditor/admin`），仅含 locator/原因/依据/脱敏预览。

## GET `/rejections/{batch_id}/{index}/fragment`

取隔离区原始片段（仅 `auditor`），直接标识在返回前再次脱敏；
无键 CSV 纯值行返回 `<redacted>` 占位。

## PUT `/observations/{source_system}/{source_id}`

更新观测值（仅 `admin`），乐观锁：

```json
{"value": 6.0, "unit": "mmol/L", "expected_version": 1}
```

- 成功：`{"verdict":"NO_CONFLICT","observation":{...,"version":2}}`
- 相同值：`verdict="IDEMPOTENT_REPLAY"`（200）
- 版本过期：409，`detail.verdict="VERSION_CONFLICT"`
- 单位不兼容：422，`detail.reason="UNIT_MISMATCH"`

## 错误码约定

| HTTP | 场景 |
| --- | --- |
| 403 | 角色无权执行该动作（消息仅含动作名） |
| 400 | 未提供/不支持格式，更新请求体非法 |
| 404 | 资源或隔离片段不存在 |
| 409 | 批次内容不一致 / 乐观版本冲突 |
| 422 | 更新值触发 UNIT_MISMATCH 等可区分校验失败 |
