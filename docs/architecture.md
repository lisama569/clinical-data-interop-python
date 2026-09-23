# 架构与处理流程

## 分层

```
HTTP (app/api.py)
  └─ 角色解析(X-Role) → 参数校验 → 统一错误码(403/404/409/422)
用例编排 (app/service.py)
  ├─ 安全鉴权 (app/security.py, 动作级 RBAC)
  ├─ 批次幂等 (batch_id + 负载指纹)
  ├─ 解析 (app/parsers/)  ── 编码目录 (app/codes.py)  ── 脱敏令牌 (app/masking.py)
  └─ 持久化 (app/repository.py, 锁 + 版本号)
横切：日志兜底脱敏 (app/logging_setup.py)
```

## 批次导入时序

1. **鉴权前置**：`ingest:submit`（角色 `ingest/admin`）。鉴权先于任何参数解析，
   匿名请求一律得到 `403`，而不是参数错误。
2. **幂等判定**：对 `格式标识 + 原始负载` 计算 SHA-256 指纹：
   - `batch_id` 已存在且指纹相同 → 直接返回 `REPLAYED`，**不再做任何写入**，
     并回放该批次已留存的拒绝结论；
   - `batch_id` 已存在但指纹不同 → 抛 `BatchInconsistent`（HTTP 409），
     原批次结论保留，不被覆盖。
3. **解析**：解析器逐条产出 `ParseReport`（接受项 + 拒绝项）。任何单条坏记录
   都被转换成结构化 `Rejection`，整批不回滚、坏记录不影响好记录。
4. **拒绝留存**：被拒记录的定位信息、原因、判定依据、**脱敏预览**写入拒绝清单；
   其原始片段进入**隔离区**，默认读取路径不可见，仅 `auditor` 可按
   `(batch_id, index)` 取用，且取回时直接标识仍被脱敏。
5. **逐条落库**：患者/就诊/观测分别 upsert。导入路径不带版本号，因此：
   - 新资源 `NO_CONFLICT`；
   - 与现有内容完全相同 `IDEMPOTENT_REPLAY`（跳过）；
   - 同键内容不同 `DIVERGENT_CONTENT`（不覆盖，记入冲突结论）。
6. **结果汇总**：返回计数、冲突列表与拒绝清单；状态为
   `ACCEPTED / REPLAYED / PARTIAL`。

## 并发模型

- 仓库所有读写在同一把可重入锁内完成，保证"检查 + 写入"的原子性。
- 资源带单调递增 `version`。显式更新走乐观并发控制：
  `expected_version` 过期 → `VERSION_CONFLICT`，拒绝覆盖。
- 并发重复提交同一批次时，只有一个线程首次写入，其余全部命中幂等重放。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `app/models.py` | 统一领域模型、拒绝原因/冲突结论/角色枚举 |
| `app/masking.py` | HMAC 令牌化、字段脱敏 |
| `app/codes.py` | 检验编码目录、别名识别、单位线性换算 |
| `app/parsers/` | FHIR/CSV 解析与逐条校验 |
| `app/repository.py` | 并发安全内存仓库、版本号、隔离区 |
| `app/security.py` | 动作级 RBAC 权限表 |
| `app/service.py` | 幂等、冲突结论、脱敏 DTO、用例编排 |
| `app/api.py` | HTTP 接口与统一错误码 |
| `app/logging_setup.py` | 日志输出前兜底脱敏 |
