# 临床数据互操作接入服务（clinical-data-interop）

把多来源、多格式的临床记录接入并归一到统一的**患者 / 就诊 / 观测**模型，
在重复提交、字段缺失、格式不一致的情况下仍给出**一致且可追溯**的结果，
并保证直接标识信息在任何路径都不外泄。

## 能力概览

- **多格式解析与归一**：支持简化 FHIR Bundle（JSON）与 CSV，两种格式都归一到
  `app/models.py` 的 `Patient / Encounter / Observation`；检验编码与单位经
  编码目录（`app/codes.py`）归一。
- **坏记录不静默丢弃**：每条失败记录都按原因拒绝并保留**可定位的原始片段**
  （locator 如 `Bundle.entry[3]`、`csv:row=4`）。原因可区分：
  `MALFORMED / MISSING_FIELD / UNKNOWN_CODE / UNIT_MISMATCH / INVALID_VALUE /
  UNSUPPORTED_FORMAT / DUPLICATE_IN_BATCH`。
- **批次幂等与并发安全**：`batch_id` + 负载指纹保证同批次重复/并发提交不产生
  重复记录；资源带版本号，乐观锁给出 `NO_CONFLICT / IDEMPOTENT_REPLAY /
  VERSION_CONFLICT / DIVERGENT_CONTENT` 四种一致且可区分的结论，并发更新不互相覆盖。
- **脱敏与访问控制**：姓名、证件号、联系方式在解析阶段即被 HMAC 令牌化，
  仓库不存明文；读取/导出仅返回令牌或脱敏值；越权返回 403；
  日志经兜底过滤器脱敏，错误信息与日志不回显原始值。

## 文档

- [docs/architecture.md](docs/architecture.md)：总体流程与模块职责
- [docs/compatibility.md](docs/compatibility.md)：支持的格式、编码目录与兼容规则
- [docs/idempotency.md](docs/idempotency.md)：幂等与冲突语义
- [docs/security.md](docs/security.md)：脱敏、令牌化与 RBAC
- [docs/api.md](docs/api.md)：HTTP 接口说明
- [docs/verification.md](docs/verification.md)：验证方法与场景清单

## 快速开始

```bash
# 运行测试（单测日志会打印输入摘要与判定依据）
python -m pytest

# 启动服务
fastapi dev main.py     # 或 uvicorn main:app --reload
```

最小导入示例：

```bash
curl -s -XPOST "localhost:8000/ingest/BATCH-001?f=fhir-json" \
  -H "X-Role: ingest" -H "Content-Type: application/json" \
  -d @samples/patient_observation.json
```

> 演示版通过 `X-Role` 请求头传递角色（`ingest/reader/exporter/auditor/admin`），
> 生产环境应替换为真实鉴权中间件。
