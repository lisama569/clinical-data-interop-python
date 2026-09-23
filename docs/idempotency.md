# 幂等与冲突语义

## 1. 批次幂等

幂等键为 `(batch_id, 指纹)`，其中

```
fingerprint = SHA256(格式标识 + 0x1F + 原始负载字节)
```

| 情形 | 结果 | HTTP |
| --- | --- | --- |
| 首次提交 | `ACCEPTED`（或含拒绝时 `PARTIAL`），写入并登记指纹 | 200 |
| 相同 `batch_id` + 相同负载再次/并发到达 | `REPLAYED`，**零新增写入**，回放原拒绝结论 | 200（`replay=true`） |
| 相同 `batch_id` + 不同负载 | `BatchInconsistent`，原结论保留、拒绝覆盖 | 409 |

并发重复提交时，仓库的"查指纹 + 登记"在同一把锁内原子完成，因此 N 个并发请求
中恰好 1 个为首次写入，其余全部重放，不会产生重复患者/就诊/观测。

## 2. 资源身份键

- Patient / Encounter / Observation 均以 `(source_system, source_id)` 为业务键。
- 观测在批次内再按业务键去重（`DUPLICATE_IN_BATCH`，拒绝并定位）；
  患者在 CSV 同批次重复出现按幂等静默合并（同一患者可携带多条观测）。

## 3. 冲突结论（一致且可区分）

| 结论 | 含义 | 写入方 | HTTP（更新接口） |
| --- | --- | --- | --- |
| `NO_CONFLICT` | 新资源，或显式编辑且版本匹配 | 是 | 200 |
| `IDEMPOTENT_REPLAY` | 内容与现有完全一致 | 否（空操作） | 200（`verdict` 字段） |
| `VERSION_CONFLICT` | 携带的 `expected_version` 已过期 | 否，拒绝覆盖 | 409 |
| `DIVERGENT_CONTENT` | 导入路径（无版本号）遇到同键但内容不同的既有资源 | 否，拒绝静默覆盖 | —（记录在批次 `conflicts`） |

### 显式编辑（乐观锁）

`PUT /observations/{system}/{id}` 请求体携带 `expected_version`：

1. 版本不匹配 → `VERSION_CONFLICT`（409），当前值不动；
2. 版本匹配且归一后值相同 → `IDEMPOTENT_REPLAY`，版本不增长；
3. 版本匹配且值不同 → 接受，`version += 1`，观测 `status` 置为 `amended`。

因此 A、B 同时基于 v1 编辑时，先提交者生成 v2，后提交者收到 `VERSION_CONFLICT`，
必须重新读取最新版本后再决定是否合并——**并发更新不会相互覆盖**。

### 导入期分叉

导入负载不带版本号。当同一业务键的内容与库中既有记录不同（例如另一来源系统
先前导入了不同数值），返回 `DIVERGENT_CONTENT` 并写入批次的 `conflicts` 列表，
保留先到数据，避免"最后一条静默覆盖全部历史"。
