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

## 被拒记录待处理区（quarantine）

被拒记录不再只在当次响应中一闪而过，而是按可区分的失败类别**持久进入待处理区**，
待检验项目字典扩充后可重新判定，形成“拒绝 → 待处理 → 重放 → 入库/继续待处理”的闭环。

### 进入规则

- 三类失败（`malformed_format` / `missing_required_field` / `unknown_code`）的记录
  在当次响应返回 `rejected` 的同时进入待处理区，响应中附带 `pending_id` 可直接定位。
- 每条记录保留**足以定位问题且已脱敏**的上下文：
  - `source_fragment`：报文前 300 字符的脱敏片段；
  - `masked_payload`：完整报文的脱敏版本（结构化遮蔽 FHIR `Patient.name` 与
    HL7 `PID-5` 姓名，并假名化证件号/电话/长数字串）；
  - 原始报文仅以私有字段 `payload` 保存供重放，**永不出现在任何列表、明细、
    分页统计、错误响应与日志中**（对外统一走 `PendingRecord.to_view()`）。
- 同一原始报文（以 HMAC 内容指纹判定）再次被拒时**幂等复用**同一条 pending 记录，
  只追加一次“重复进入”历史，不新增记录、不重复计量。

### 退出规则与生命周期

- 状态流转：`pending` →（重放通过）→ `resolved`；容量超限时在区记录可 →
  `evicted`（淘汰档案，见下）。`resolved` 记录保留在区内用于追溯，批量重放时
  作为 `already_resolved` 重复触发命中。
- `history` 完整记录每次事件的时间、动作（`rejected`/`replayed`/`evicted`）、
  当时失败类别、判定依据，以及重放通过时的记录级入库结论，可回答“何时因何被拒、
  何时被重放、结果如何、何时被淘汰”。
- 重放后仍不通过的记录**继续留在待处理区**，其失败类别与依据更新为按**最新字典**
  得到的最新结论（例如编码补齐后，可能从 `unknown_code` 进一步暴露为
  `missing_required_field`）。

### 重放语义（幂等且并发安全）

- 字典在**不重启服务**的前提下扩充：`POST /admin/codes` 新增编码，
  `POST /admin/codes/{code}/aliases` 新增别名（目标编码必须已注册，别名不得静默改指向）。
- 重放走与单条提交**完全一致**的解析与 upsert 路径，因此入库结论可复现：
  `created` / `duplicate` / `conflict`；命中冲突时同样保留先到值、不覆盖，
  冲突保护结论不会被重放抹掉。
- 单条/并发触发的结论可区分：

  | 结论 | 含义 |
  | --- | --- |
  | `resolved` | 本次重放通过并完成入库（附 `ingest_outcome`） |
  | `still_failed` | 仍不通过，留在待处理区并更新最新类别/依据 |
  | `already_resolved` | 幂等命中：此前已解决，不重复处理 |
  | `skipped_busy` | 并发安全：同一刻已有重放在进行，本次跳过 |
  | `evicted` | 记录已因容量淘汰转为档案，不可再重放；明细仍可查（HTTP 200） |
  | `not_found` | 待处理区从无此记录，API 返回 404（`evicted` 与此相区别） |

- 同一条记录同一时刻只允许一个重放（存储层 acquire/release 互斥），并发重复触发
  最多只有一个 `resolved`，其余得到 `skipped_busy`，不产生重复记录、不相互覆盖。

### 容量与淘汰策略

- 容量上限可配置（`QuarantineStore(capacity=...)`，默认 1000；构建应用时可用
  `create_app(..., quarantine_capacity=N)`）。容量只统计**在区**记录
  （pending + resolved）；淘汰档案不占容量。
- **新被拒记录保证入区，绝不静默丢弃**：即使容量已被大量已解决历史占满，也会先
  按规则淘汰一条在区记录腾出位置，再放入新记录；摄取响应里的 `pending_id` 一定
  能查到对应记录。
- 达到上限时按**确定且可解释**的顺序淘汰（`created_at` 升序，进入序号同时刻决胜）：
  1. 优先淘汰最早的、当前未被重放占用的 pending 记录；
  2. 没有空闲 pending 时，才考虑正被占用的 pending（极端竞态兜底）；
  3. 在区全是 resolved、没有任何 pending 时，才淘汰最早的 resolved。
- **淘汰不改变其他在区记录的失败类别**，也不影响其处理历史。

### 淘汰后的可追溯行为

被淘汰记录**不会被物理删除**，而是转为 `evicted`（已淘汰）状态的档案继续保留：

- 仍可用 `GET /quarantine/records/{pending_id}` 直接查到，返回明确的
  `status=evicted` 结论、`evicted_at` 时间，而不是 404 或“从未存在过”；
- 档案保留其**失败类别**（`reason`）、脱敏上下文（`source_fragment` /
  `masked_payload`）与**完整处理历史**：初次 `rejected`、各次 `replayed`
  （含仍失败/通过）以及最后一次 `evicted` 事件，可回答“当初因何被拒、有没有被
  重放过、何时因容量被淘汰”；
- 原始报文（私有 `payload`）在淘汰时清除：淘汰档案不再可重放——对其触发重放会
  得到明确的 `evicted` 结论（HTTP 200，区别于从不存在的 `not_found` / 404）；
- 淘汰档案默认不出现在日常列表（避免淹没待办），显式按状态过滤可查：
  `GET /quarantine/records?status=evicted`；统计接口在 `stats.evicted` 单列计数。
- 已淘汰的同内容报文若日后再次被拒，会作为**全新 pending 记录**入区（幂等只针对
  在区 pending），旧淘汰档案依旧保留。

### 批量重放的四类结论

`POST /quarantine/replay` 在触发时对**全部在区记录**（pending 与 resolved，可按
`reason` 限定）取快照并逐条判定，响应中 `counts` 为逐条原始结论，`summary` 汇总为
便于跟进的四类（四类之和等于 `triggered`）：

| 汇总分类 | 对应逐条结论 | 含义 |
| --- | --- | --- |
| `resolved` | `resolved` | **本轮**重放新通过并完成入库 |
| `still_failed` | `still_failed` | 本轮处理了但仍失败，继续留在待处理区，需继续跟进 |
| `already_resolved` | `already_resolved` | 此前已解决，本轮属于**重复触发**，未重复处理 |
| `not_reached` | `skipped_busy`（及防御性的 `not_found`/`evicted`） | **本轮未处理到**（如同一刻已有重放在进行被跳过），留待下一轮 |

### 访问控制

- 查阅待处理区（列表/明细）：`auditor` / `admin`（动作 `read_quarantine`）；
  无权限返回可理解的 401/403 拒绝，**不会**用空结果掩盖越权。
- 触发重放与维护字典：仅 `admin`（动作 `manage_quarantine`）。

### 接口

```
POST /ingest/{batch_id}                         {"records": ["<报文字符串>", ...]}
GET  /patients/{patient_key}                    需 X-Role: clinician|admin
GET  /export                                    需 X-Role: auditor|admin

GET  /quarantine/records                        需 X-Role: auditor|admin
        ?status=pending|resolved|evicted&reason=...&start=<ISO>&end=<ISO>&page=1&page_size=50
        （默认只列在区 pending/resolved；?status=evicted 查淘汰档案）
GET  /quarantine/records/{pending_id}           需 X-Role: auditor|admin（evicted 档案也返回 200）
POST /quarantine/records/{pending_id}/replay    需 X-Role: admin（evicted 返回 200+evicted 结论，不存在返回 404）
POST /quarantine/replay                         需 X-Role: admin；body 可省略或 {"reason": null|"..."}；
        响应含 triggered / summary（四类） / counts（逐条） / results

POST /admin/codes                               需 X-Role: admin；{"code","display","unit"}
POST /admin/codes/{code}/aliases                需 X-Role: admin；{"alias","target_code"}
```

## 兼容范围

- FHIR JSON / HL7 v2 的探测、必填字段、默认 LOINC 子集与别名均与既有行为一致；
  原有解析、批次幂等、记录级 `created`/`duplicate`/`conflict` 语义不受影响。
- 默认仍使用进程级全局编码字典（`app.codes.DEFAULT_REGISTRY`）；需要隔离字典或
  重放闭环时，向 `IngestService` 注入自定义 `CodeRegistry` 与 `QuarantineStore` 即可，
  旧的无参/两参构造方式继续可用。
- 运行时字典扩充只影响**之后的解析与重放判定**，不会改写已经入库的观测值，
  也不会改变已 resolved 记录的结论。

## 接口（原有）

```
POST /ingest/{batch_id}     {"records": ["<报文字符串>", ...]}
GET  /patients/{patient_key}   需 X-Role: clinician|admin
GET  /export                   需 X-Role: auditor|admin
```

## 验证方法

```bash
uv run pytest -q                                  # 全部测试（43 项）
uv run pytest tests/test_parsers.py -q            # 畸形记录、缺字段、未知编码（解析不回归）
uv run pytest tests/test_idempotency.py -q        # 重复/并发提交、冲突（幂等与冲突语义不回归）
uv run pytest tests/test_access_control.py -q     # 越权访问、脱敏不外泄
uv run pytest tests/test_quarantine.py -q         # 待处理区、重放、容量淘汰、四类批量结论、权限、脱敏
# 并发/批量用例采用事件同步而非固定 sleep，可重复多轮确认无抖动：
for i in $(seq 1 10); do uv run pytest -q >/dev/null && echo "round $i ok"; done
uv run uvicorn main:app --reload                  # 启动服务
```

本地快速手工验证新行为（建议用小容量应用，或直接在 Python 中构造
`QuarantineStore(capacity=N)`）：

1. **被解决历史占满容量时新被拒记录仍入区**：制造 N 条未知编码记录 → 扩充字典
   批量重放全部 resolved → 再提交一条被拒记录 → 响应的 `pending_id` 用
   `GET /quarantine/records/{id}` 必能查到（此时最旧的一条 resolved 已转为 evicted）。
2. **淘汰可追溯**：`GET /quarantine/records/{被淘汰id}`（auditor/admin）返回
   `status=evicted`、`reason`、`evicted_at` 与含 `rejected/replayed/evicted` 的
   history；`GET /quarantine/records?status=evicted` 列出全部淘汰档案；
   对其 POST replay 返回 200 且 `outcome=evicted`。
3. **批量重放四类结论**：先单独解决一条 → 制造一个并发占用（可选）→
   `POST /quarantine/replay`，响应 `summary` 分别给出 `resolved` /
   `still_failed` / `already_resolved` / `not_reached` 的条数。

待处理区闭环的手动验证顺序：先 `POST /ingest/{batch}` 送入含未知编码的报文拿到
`pending_id` → `GET /quarantine/records/{pending_id}`（auditor/admin）查看脱敏上下文 →
`POST /admin/codes` 与 `/aliases`（admin）扩充字典 →
`POST /quarantine/records/{pending_id}/replay` 观察 `resolved`，再次触发观察
`already_resolved`；也可 `POST /quarantine/replay` 按类别批量重放。

测试日志（`logging`）会打印每条用例的输入摘要与判定依据，可用
`uv run pytest --log-cli-level=INFO` 查看。
