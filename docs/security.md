# 脱敏、令牌化与访问控制

## 1. 直接标识处理

直接标识包括：姓名、证件号（身份证/护照/SSN）、联系方式（电话/邮箱/电信地址）、
邮政地址等（键名集合见 `app/masking.py`）。

处理原则：

1. **不入库明文**：解析 Patient 时即对姓名/证件/联系方式做 HMAC-SHA256 令牌化，
   仓库 `Patient` 只有 `name_token / ident_token / contact_token`，没有明文字段。
2. **令牌可匹配、不可逆**：令牌用于跨来源识别同一患者。归一化规则为
   删除全部空白与标点、大小写折叠后再加盐 HMAC，因此
   `张三` = `张 三`、`138-0000-0000` = `13800000000`、`ZHANG SAN` = `zhang san`。
3. **盐**：环境变量 `CLINICAL_MASKING_SALT`；未设置时使用开发默认值
   `dev-salt-change-me`。**生产必须显式设置独立盐值并妥善保管。**
4. **所有读取路径脱敏**：
   - 患者明细只返回令牌与非标识属性（性别、出生日期），不存在 name/phone 键；
   - 观测/导出 DTO 不含任何直接标识；
   - 拒绝清单只给 `masked_preview`（结构化遮蔽后截断）；
   - 隔离区原始片段仅 `auditor` 可取，取回时仍执行结构化脱敏；
     无键的 CSV 纯值行无法可靠对齐表头时，返回 `<redacted>` 占位而非原文。

## 2. 日志与错误信息

- 业务日志只记录 `locator / reason / 字段名 / 编码 / 单位 / 批次ID`，
  不记录直接标识值；单测日志显式打印"输入摘要 + 判定依据"。
- `RedactingFilter` 作为兜底，在输出前遮蔽身份证号（15/18 位）、手机号、
  邮箱以及 `name=... / phone: ...` 等键值模式。
- 越权错误体仅含动作名（如 `access denied for action: export:observations`），
  冲突错误仅含资源定位（`Observation/O1`）与结论码，均不含业务数据。

## 3. 角色权限矩阵（RBAC）

角色通过 `X-Role` 请求头传入；缺失或无法识别按 `anonymous` 处理。

| 动作 | ingest | reader | exporter | auditor | admin | anonymous |
| --- | :---: | :---: | :---: | :---: | :---: | :---: |
| `ingest:submit` 提交批次 | ✅ | – | – | – | ✅ | – |
| `patient:read` 患者明细(脱敏) | – | ✅ | – | ✅ | ✅ | – |
| `observation:read` 观测列表(脱敏) | – | ✅ | – | ✅ | ✅ | – |
| `rejection:list` 拒绝清单 | – | – | – | ✅ | ✅ | – |
| `rejection:fragment` 隔离区片段(仍脱敏) | – | – | – | ✅ | – | – |
| `export:observations` 批量导出(脱敏) | – | – | ✅ | – | ✅ | – |
| `observation:update` 更新观测 | – | – | – | – | ✅ | – |

越权统一返回 `403`。注意即使有权限，所有返回数据依旧是脱敏后的；
权限控制"能不能访问"，脱敏保证"访问到的也不含明文标识"。

> `X-Role` 仅用于在脱离真实 IAM 的环境中演示授权边界；生产部署应替换为
> 认证中间件签发的、不可被客户端伪造的身份与角色声明。
