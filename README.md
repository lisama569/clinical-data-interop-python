# 临床数据互操作接入服务

将多种来源的临床记录（FHIR JSON / HL7 v2）归一到统一的患者、就诊与观测模型，
在重复提交、字段缺失、格式不一致时给出一致且可追溯的结果。被拒记录进入**待处理区**，
检验项目字典热更新后可**重放**，无需上游重发历史报文。

## 支持的交换格式

| 格式 | 探测方式 | 必填字段 |
| --- | --- | --- |
| FHIR 风格 JSON（Bundle 或扁平对象） | 报文以 `{`/`[` 开头 | `Patient.identifier[0].value`（或 `id`）、`Encounter.id`、`Encounter.period.start`、`Observation.code.coding[0].code`、`Observation.valueQuantity.value` |
| HL7 v2 管道分隔（MSH/PID/OBR/OBX 子集） | 报文以 `MSH\|` 开头 | `PID-3` 患者标识、`OBR-2` 医嘱号、`OBR-7` 观测时间、`OBX-3` 检验编码、`OBX-5` 结果值 |

## 编码与兼容规则

- 检验编码采用 LOINC 子集注册表（`app/codes.py`），大小写不敏感、允许首尾空白；
- 支持常见别名归一（如 `GLU` → `2345-7`、`HGB` → `718-7`、`SBP` → `8480-6`）；
- 无法识别的编码不会静默丢弃，记录以 `unknown_code` 原因被拒绝；
- **运行时热更新**：`register_code` / `register_alias`（或 `POST /admin/codes`）可在不重启
  服务的前提下新增编码或别名，注册即时生效、线程安全；同名映射幂等，冲突映射返回 409 且不覆盖。

## 拒绝语义（原因可区分）

| 原因 | 含义 |
| --- | --- |
| `malformed_format` | 格式非法：无法识别的格式、JSON 截断、缺少 MSH 首段等 |
| `missing_required_field` | 必填字段缺失（具体字段见 `detail`） |
| `unknown_code` | 检验项目编码无法识别（条目上保留归一前的 `code_token`，便于定向重放） |

每条拒绝都保留 `source_fragment`：原始报文经**报文感知脱敏**（遮蔽 FHIR `name.text/family/given`、
HL7 `PID-3/PID-5/PID-13`，并对证件号/手机号/长数字串做 HMAC 假名化）后截取前 300 字符，
可用于定位问题，但不含姓名、证件号、联系方式原文。

## 幂等与冲突语义

- **批次幂等**：`POST /ingest/{batch_id}` 中同一 `batch_id` 只处理一次；重复或并发重复提交
  返回 `replayed=true` 与首个批次的结果，不产生重复记录。
- **记录级结果**：`created`（新建）/ `duplicate`（同键同内容，幂等命中）/
  `conflict`（同键不同内容）/ `rejected`（被拒绝）。
- **冲突处理**：同一资源键被并发更新且内容不一致时，保留先到的值、不覆盖，
  后到者得到 `conflict` 结论；并发场景下同一键最多只有一个 `created`。
- 存储层所有操作在单把可重入锁下执行，保证并发安全。

## 被拒记录待处理区（quarantine）

### 进入规则

- 摄取批次中任何一条记录因 `malformed_format` / `missing_required_field` / `unknown_code`
  被拒，即按其失败类别持久进入待处理区（同批次其他记录照常处理）；
- 条目保存：`entry_id`（单调序号）、来源批次/来源位置、失败类别、脱敏依据、脱敏报文片段、
  未知编码令牌 `code_token`、进入时间，以及仅用于重放的原始报文（**不随任何响应/日志输出**）。

### 状态流转与生命周期

```
admitted                 replayed（仍失败）                 replayed（通过）
(pending) ───────────────────────────▶ (failed) ───────────────────────────▶ (resolved)
    │                                     │
    └────────────── 容量上限 FIFO 淘汰 ────┴────────▶ (evicted，墓碑，丢弃原始报文)
```

- `pending` → `failed`：重放后仍不通过，**失败类别与依据更新为最新结论**，继续留在待处理区；
- `pending`/`failed` → `resolved`：重放通过并完成入库；
- 每次进入/重放/淘汰都追加一条不可变历史事件（时间、动作、前后状态、类别、依据、入库结论），
  可回答“何时因何被拒、何时被重放、结果如何”。

### 查阅（有明确权限边界）

- `GET /quarantine` 支持按 `reason`（失败类别）、`status`、`start`/`end`（进入时间，ISO8601）
  过滤，`limit`/`offset` 分页，按进入顺序稳定排序，响应含 `total` 与 `counts` 容量统计；
- `GET /quarantine/{entry_id}` 返回含完整生命周期历史的明细；已淘汰条目返回 **410 Gone**，
  不存在返回 404；
- 权限：`auditor`/`admin` 可查阅，`clinician` 无权；**无权限返回 401/403 明确拒绝，而不是空列表**；
- 列表、明细、分页统计、错误响应与日志中均不出现姓名、证件号、联系方式原文。

### 按最新字典重放（幂等、并发安全）

- `POST /quarantine/replay/{entry_id}`：按**当前最新字典**对单条记录重新解析判定；
- `POST /quarantine/replay`：请求体可选 `{"reason": "unknown_code", "code_token": "GLU"}`，
  对筛选命中的可重放条目批量重放；
- 判定通过则走与单条提交**完全相同**的入库路径，因此入库结论（`created`/`duplicate`/
  `conflict`）与直接提交一致、可复现；命中既有冲突保护时返回 `conflict` 且不覆盖已有值；
- 重放结果可区分：`resolved`（本次通过并入库）/ `still_failed`（仍失败，已更新最新结论）/
  `already_resolved`（幂等命中，不重复处理）/ `not_processed`（批量重放未被筛选命中）/
  `not_found`（不存在或已淘汰）；
- 所有重放由一把重放总锁串行化：同一条目被重复或并发触发时**只有一次真正解决**，
  不产生重复记录、不覆盖已解决结论，多个触发之间不相互覆盖；
- 重放权限：仅 `admin`。

### 容量与淘汰策略

- 容量上限可配置（`IngestService(..., quarantine_capacity=N)`，默认 1000），容量只统计
  `pending`/`failed` 条目；
- 达到上限再收入新条目时，按**确定且可解释的 FIFO 顺序**淘汰：进入序号（`entry_id`）最老的
  未解决条目先淘汰，与失败类别、重放次数无关；
- **已解决（resolved）记录永不淘汰**；淘汰不改变任何既有记录的失败类别（类别与依据保留在墓碑
  历史中）；被淘汰条目丢弃原始报文、转为 `evicted` 墓碑供审计，不可再重放。

## 脱敏与访问控制

- 姓名、证件号、联系方式在解析阶段即被 HMAC-SHA256 假名化（`anon:<hash>`），
  持久化与所有读取路径只出现脱敏值；
- 角色通过 `X-Role` 头传递：

| 动作 | clinician | auditor | admin |
| --- | --- | --- | --- |
| `read_detail` 明细查询 `GET /patients/{key}` | ✅ | – | ✅ |
| `export` 导出汇总 `GET /export` | – | ✅ | ✅ |
| `read_quarantine` 待处理区查阅 | – | ✅ | ✅ |
| `replay_quarantine` 重放 / 字典更新 | – | – | ✅ |

- 未认证返回 401，越权返回 403，错误信息与日志中均不含原始敏感值（有测试保证）。

## 接口

```
POST   /ingest/{batch_id}            {"records": ["<报文字符串>", ...]}
GET    /patients/{patient_key}       需 X-Role: clinician|admin
GET    /export                       需 X-Role: auditor|admin
GET    /quarantine                   需 X-Role: auditor|admin；支持 reason/status/start/end/limit/offset
GET    /quarantine/{entry_id}        需 X-Role: auditor|admin
POST   /quarantine/replay/{entry_id} 需 X-Role: admin
POST   /quarantine/replay            需 X-Role: admin；body 可选 {"reason": ..., "code_token": ...}
POST   /admin/codes                  需 X-Role: admin
                                     {"code": "1988-5", "display": "CRP", "unit": "mg/L"}
                                     或 {"alias": "CRP", "code": "1988-5"}
```

### 典型闭环（curl）

```bash
# 1) 含未知编码的记录被拒并进入待处理区
curl -s -X POST localhost:8000/ingest/batch-1 \
  -H 'Content-Type: application/json' \
  -d '{"records": ["{\"patient\":{\"id\":\"p1\"},\"encounter\":{\"id\":\"e1\",\"period\":{\"start\":\"2026-01-01\"}},\"observations\":[{\"id\":\"o1\",\"code\":{\"coding\":[{\"code\":\"NEW-9\"}]},\"valueQuantity\":{\"value\":1}}]}"]}'

# 2) 字典热更新（不重启）
curl -s -X POST localhost:8000/admin/codes -H 'X-Role: admin' -H 'Content-Type: application/json' \
  -d '{"code": "NEW-9", "display": "New analyte", "unit": "mg/L"}'

# 3) 重放待处理区中相关记录
curl -s -X POST localhost:8000/quarantine/replay -H 'X-Role: admin' -H 'Content-Type: application/json' \
  -d '{"reason": "unknown_code"}'
```

## 兼容范围

- 待处理区、重放与运行时字典均为**进程内**实现（与现有存储一致）：重启后内存状态清空；
  持久化落盘不在当前范围。
- 重放只重新执行“解析归一 + 入库判定”，不会修改或重发原批次的其他记录，也不改变批次幂等结论。
- 字典注册只增不改：已存在编码重复注册为幂等 no-op；别名改映射会被拒绝（409），
  避免历史结论被静默翻转。

## 验证方法

```bash
uv run pytest -q                          # 全部测试（46 项）
uv run pytest tests/test_parsers.py -q        # 畸形记录、缺字段、未知编码
uv run pytest tests/test_idempotency.py -q    # 重复/并发提交、冲突（原有语义不回归）
uv run pytest tests/test_access_control.py -q # 越权访问、脱敏不外泄
uv run pytest tests/test_masking.py -q        # 姓名/证件/电话片段脱敏（含截断）
uv run pytest tests/test_quarantine.py -q     # 待处理区、字典更新后重放、重复/并发重放、淘汰、生命周期
uv run pytest tests/test_quarantine_api.py -q # 无权限查阅、API 脱敏、404/410、热更新+重放闭环
uv run uvicorn main:app --reload              # 启动服务
```

测试日志（`logging`）会打印每条用例的输入摘要与判定依据，可用
`uv run pytest --log-cli-level=INFO` 查看。
