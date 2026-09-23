# 支持的格式、编码与兼容规则

## 1. 交换格式

| 格式标识（`f=` 参数） | 形态 | 解析器 |
| --- | --- | --- |
| `fhir-json` | 简化 FHIR R4 `Bundle`（JSON） | `app/parsers/fhir.py` |
| `csv` | 首行表头的逗号分隔文本（UTF-8） | `app/parsers/csv_parser.py` |

其他格式值返回 `400 unsupported format`；负载可解析但顶层不是 `Bundle` 返回
记录级 `UNSUPPORTED_FORMAT`。

### 1.1 FHIR 子集

顶层：

```json
{ "resourceType": "Bundle", "type": "transaction", "entry": [ {"resource": {...}} ] }
```

支持的 `resourceType`：`Patient`、`Encounter`、`Observation`；其他类型按
`UNSUPPORTED_FORMAT` 逐条拒绝。

- **Patient**
  - 身份：`identifier[0].system/value`（业务主键）、`identifier` 中
    `type.coding[].code ∈ {IDCARD, SSN, MR}` 的 `value` 作为证件号；
  - 姓名：`name[].text`，或由 `given[] + family` 拼接；
  - 联系方式：`telecom[].value`；
  - 可选：`birthDate`（ISO `yyyy-mm-dd`）、`gender`
    （`male/female/other/unknown`）。
- **Encounter**
  - 主键：`identifier` 或 `id`；`subject.reference`（`Patient/<id>`）必填；
  - `status ∈ {planned, in-progress, finished, cancelled}`；
  - `period.start/end`（ISO8601，可选但格式必须合法）。
- **Observation**
  - 主键：`identifier` 或 `id`；`subject.reference` 必填；
  - `code.coding[].code` 经编码目录归一；
  - `valueQuantity.value`（数值）与 `unit`（或 `code`）必填，按目录换算；
  - `effectiveDateTime/effectiveInstant`（ISO8601）必填；
  - `status ∈ {registered, preliminary, final, amended}`，缺省按 `final`。

### 1.2 CSV 列约定

必填列（缺少任意一列，整批 `MALFORMED` 并指出缺列名）：

```
source_system, patient_id, patient_name, id_number, phone,
observation_id, test_code, value, unit, effective_time
```

可选列：`gender, birth_date, status, encounter_id`。

- 每行产出一个 Patient（按 `(source_system, patient_id)` 在批次内幂等去重）
  和一个 Observation（按 `(source_system, observation_id)` 去重；重复行按
  `DUPLICATE_IN_BATCH` 拒绝）。
- 列数与表头不符的行按 `MALFORMED` 拒绝，其余行继续处理；纯空行跳过。

## 2. 检验编码目录

来源编码大小写、分隔符容错：`LOINC:2345-7`、`loinc2345-7`、`LOINC23457`、
纯数字 `2345-7` 视为同一编码。本地别名（如 `local-glucose`、`HGB`）同样支持。

| 规范编码 | 含义 | 规范单位 | 可接受单位（换算） | 来源编码示例 |
| --- | --- | --- | --- | --- |
| `GLU` | 血糖 | `mmol/L` | `mmol/L`；`mg/dL`（÷18.0156） | `2345-7`, `2339-0`, `GLU`, `local-glucose` |
| `HGB` | 血红蛋白 | `g/L` | `g/L`；`g/dL`（×10） | `718-7`, `20570-8`, `HGB` |
| `CREA` | 肌酐 | `umol/L` | `umol/L`；`mg/dL`（×88.4） | `2160-0`, `38483-4`, `CREA` |
| `WBC` | 白细胞计数 | `10^9/L` | `10^9/L`；`10^3/uL`（=）；`/uL`（×0.001） | `6690-2`, `26464-8`, `WBC` |
| `PLT` | 血小板计数 | `10^9/L` | `10^9/L`；`10^3/uL`（=）；`/uL`（×0.001） | `777-3`, `26515-7`, `PLT` |
| `K` | 血钾 | `mmol/L` | `mmol/L`；`mEq/L`（=） | `2823-3`, `6298-4`, `K` |

换算公式：`canonical_value = value × factor + offset`。

- 编码无法识别 → `UNKNOWN_CODE`；
- 单位不在该编码允许集合内 → `UNIT_MISMATCH`（与未知编码是**不同**原因）；
- 值无法解析为数值 / 日期不合法 → `INVALID_VALUE`。

> 编码目录可在 `app/codes.py` 的 `_CATALOG` 中扩展；新增编码无需改动解析器。

## 3. 拒绝原因对照

| 原因 | 触发场景 | 示例 locator |
| --- | --- | --- |
| `MALFORMED` | JSON 非法、Bundle.entry 非数组、CSV 表头缺列、行列数不符 | `bundle` / `csv:row=4` |
| `MISSING_FIELD` | 必填字段缺失或为空 | `Bundle.entry[2]` |
| `UNKNOWN_CODE` | 检验编码不在目录 | `csv:row=6` |
| `UNIT_MISMATCH` | 单位与编码不兼容 | `Bundle.entry[1]` |
| `INVALID_VALUE` | 数值/日期/枚举格式非法 | `Bundle.entry[0]` |
| `UNSUPPORTED_FORMAT` | 不支持的格式或 resourceType | `bundle` |
| `DUPLICATE_IN_BATCH` | 同一批次内同键记录重复 | `Bundle.entry[5]` |

每条拒绝都包含：`index`、`locator`、`reason`、`detail`（仅字段名/编码/单位，
不含标识值）、`masked_preview`。
