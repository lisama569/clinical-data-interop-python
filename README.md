# 临床数据互操作接入服务

将多种来源的临床记录（FHIR JSON / HL7 v2）归一到统一的患者、就诊与观测模型，
在重复提交、字段缺失、格式不一致时给出一致且可追溯的结果。

## 支持的交换格式

| 格式 | 探测方式 | 必填字段 |
| --- | --- | --- |
| FHIR 风格 JSON（Bundle 或扁平对象） | 报文以 `{`/`[` 开头 | `Patient.identifier[0].value`（或 `id`）、`Encounter.id`、`Encounter.period.start`、`Observation.code.coding[0].code`、`Observation.valueQuantity.value` |
| HL7 v2 管道分隔（MSH/PID/OBR/OBX 子集） | 报文以 `MSH\|` 开头 | `PID-3` 患者标识、`OBR-2` 医嘱号、`OBR-7` 观测时间、`OBX-3` 检验编码、`OBX-5` 结果值 |

## 编码与兼容规则

- 检验编码采用 LOINC 子集注册表（`app/codes.py`），大小写不敏感、允许首尾空白；
- 支持常见别名归一（如 `GLU` → `2345-7`、`HGB` → `718-7`、`SBP` → `8480-6`）；
- 无法识别的编码不会静默丢弃，记录以 `unknown_code` 原因被拒绝。

## 拒绝语义（原因可区分）

| 原因 | 含义 |
| --- | --- |
| `malformed_format` | 格式非法：无法识别的格式、JSON 截断、缺少 MSH 首段等 |
| `missing_required_field` | 必填字段缺失（具体字段见 `detail`） |
| `unknown_code` | 检验项目编码无法识别 |

每条拒绝都保留 `source_fragment`：原始报文前 300 字符经脱敏后的片段，可用于定位问题，
但不含证件号、手机号等直接标识原文。

## 幂等与冲突语义

- **批次幂等**：`POST /ingest/{batch_id}` 中同一 `batch_id` 只处理一次；重复或并发重复提交
  返回 `replayed=true` 与首个批次的结果，不产生重复记录。
- **记录级结果**：`created`（新建）/ `duplicate`（同键同内容，幂等命中）/
  `conflict`（同键不同内容）/ `rejected`（被拒绝）。
- **冲突处理**：同一资源键被并发更新且内容不一致时，保留先到的值、不覆盖，
  后到者得到 `conflict` 结论；并发场景下同一键最多只有一个 `created`。
- 存储层所有操作在单把可重入锁下执行，保证并发安全。

## 脱敏与访问控制

- 姓名、证件号、联系方式在解析阶段即被 HMAC-SHA256 假名化（`anon:<hash>`），
  持久化与所有读取路径只出现脱敏值；
- 角色通过 `X-Role` 头传递：`clinician`/`admin` 可查明细（`GET /patients/{key}`），
  `auditor`/`admin` 可导出汇总（`GET /export`）；
- 未认证返回 401，越权返回 403，错误信息与日志中均不含原始敏感值（有测试保证）。

## 接口

```
POST /ingest/{batch_id}     {"records": ["<报文字符串>", ...]}
GET  /patients/{patient_key}   需 X-Role: clinician|admin
GET  /export                   需 X-Role: auditor|admin
```

## 验证方法

```bash
uv run pytest -q                 # 全部测试（20 项）
uv run pytest tests/test_parsers.py -q        # 畸形记录、缺字段、未知编码
uv run pytest tests/test_idempotency.py -q    # 重复/并发提交、冲突
uv run pytest tests/test_access_control.py -q # 越权访问、脱敏不外泄
uv run uvicorn main:app --reload              # 启动服务
```

测试日志（`logging`）会打印每条用例的输入摘要与判定依据，可用
`uv run pytest --log-cli-level=INFO` 查看。
