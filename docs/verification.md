# 验证方法

## 运行

```bash
python -m pytest                       # 全部测试（含单测日志：输入摘要 + 判定依据）
python -m pytest tests/test_api.py     # 仅 HTTP 端到端
python -m compileall app main.py       # 编译检查
python -m pyflakes app tests main.py   # 静态检查（无未用导入/未定义名）
```

测试按层组织：

| 测试文件 | 覆盖重点 |
| --- | --- |
| `tests/test_masking.py` | 令牌化确定性/归一化、脱敏键与后缀、深层遮蔽 |
| `tests/test_codes.py` | 编码别名、单位换算、UNKNOWN_CODE vs UNIT_MISMATCH 可区分 |
| `tests/test_security.py` | 角色解析、权限矩阵、越权错误不含数据 |
| `tests/test_repository.py` | 16 线程并发重复插入、并发分叉不覆盖、乐观版本冲突、批次指纹 |
| `tests/test_fhir_parser.py` | FHIR 畸形/缺字段/坏值/坏码/坏单位/同批重复/脱敏预览 |
| `tests/test_csv_parser.py` | CSV 缺列/列数不符/缺值/三类可区分原因/同患者幂等/预览无明文 |
| `tests/test_service.py` | 批次重放、批次不一致、并发提交、RBAC、脱敏视图、隔离片段、更新四结论 |
| `tests/test_api.py` | 全链路 HTTP：越权 403、冲突 409、重放、并发、CSV/FHIR 导入 |
| `tests/test_logging_redaction.py` | 日志对手机/证件/邮箱/键值模式的兜底遮蔽 |

## 关键场景与预期

### 1. 畸形记录不丢失、原因可区分

- 非 JSON 负载 -> MALFORMED，批次仍返回 200 且拒绝项中含 locator；
- FHIR 非 Bundle -> UNSUPPORTED_FORMAT；
- 必填为空（如 CSV phone 列空）-> MISSING_FIELD，detail 指名字段；
- 未知检验编码 9999-9 -> UNKNOWN_CODE；单位 parsec -> UNIT_MISMATCH；
  数值 xyz -> INVALID_VALUE；三类在同一批次中同时出现时分别列出；
- 同一批次内重复观测 -> DUPLICATE_IN_BATCH，首条保留、后续拒绝并定位。

### 2. 重复与并发

- 相同 batch_id + 相同负载提交两次：第一次计数为 1，第二次 REPLAYED、
  零新增；导出仍只有 1 条观测。
- 12 线程并发提交同一批次：恰好 1 个 ACCEPTED，其余 REPLAYED。
- 相同 batch_id 携带不同负载：409，首批次结论保留。
- 导入期同键不同内容：DIVERGENT_CONTENT 进入冲突列表，先到值不被覆盖。

### 3. 并发更新不互相覆盖

A、B 都基于 expected_version=1 发起 PUT：A 成功（version 增长为 2），B 得
VERSION_CONFLICT（409），资源保持 A 的值；用相同值更新得到
IDEMPOTENT_REPLAY 且版本不增长。

### 4. 越权与脱敏

- 无 X-Role 提交/读取 -> 403；reader 导出/列拒绝清单/更新 -> 403；
  exporter 取隔离片段 -> 403；只有 auditor 可取片段。
- reader 的患者视图不含 name/phone/id_number 键，也不含姓名/手机/证件明文；
- 导出 DTO、拒绝清单 masked_preview、审计片段、错误体、日志输出中
  均检索不到 13800000000、Zhang San、张三 等原始值
  （相关断言已写入测试）。

## 手工冒烟

```bash
fastapi dev main.py
curl -s -XPOST "localhost:8000/ingest/B1?f=fhir-json" \
  -H "X-Role: ingest" -H "Content-Type: application/json" \
  --data-binary @samples/patient_observation.json
curl -s "localhost:8000/export/observations" -H "X-Role: exporter"
curl -s "localhost:8000/patients/sysA/P1" -H "X-Role: reader"
```
