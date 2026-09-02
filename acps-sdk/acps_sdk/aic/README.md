# ACPs 智能体身份码 (AIC) 工具模块

基于 **ACPs-spec-AIC-v02.02** 规范实现的 AIC 验证与解析工具。

## AIC 结构

AIC 由 10 级组成，以 `.` 分隔：

```
1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4
└─────┬────┘ └┘ └┬┘ └─┬─┘ └──┬──┘ └──┬──┘ └─┬┘
    前缀      版本 ARSP Vendor  本体SN  实体SN   CRC
   (1-4级)    (5级)(6级) (7级)  (8级)   (9级)  (10级)
```

| 级别 | 名称        | 格式            | 说明                                    |
| ---- | ----------- | --------------- | --------------------------------------- |
| 1-4  | OID 前缀    | 纯数字          | `1.2.156.3088`（国家 OID 注册中心分配） |
| 5    | Version     | Base36, 1位     | AIC 版本号                              |
| 6    | ARSP        | Base36, 1-6位   | 智能体注册服务商序号                    |
| 7    | Vendor      | Base36, 1-6位   | 智能体供应商序号                        |
| 8    | Ontology SN | Base36, 1-9位   | 智能体本体序列号                        |
| 9    | Instance SN | Base36, 1-9位   | 智能体实例序列号（全 0 = 本体 AIC）     |
| 10   | CRC         | Base36, 固定4位 | CRC-16 校验码                           |

## 快速开始

### 基础格式验证（无需 SALT）

```python
from acps_sdk.aic import validate_aic_format, is_valid_aic_format

aic = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"

# 简单验证
if is_valid_aic_format(aic):
    print("格式正确")

# 带错误信息的验证。注意：这里只验证格式，不校验 CRC。
valid, error = validate_aic_format(aic)
if not valid:
    print(f"格式错误: {error}")
```

### 解析 AIC

```python
from acps_sdk.aic import parse_aic

aic = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
info = parse_aic(aic)

if info:
    print(f"原始值: {info.raw}")
    print(f"规范化: {info.normalized}")
    print(f"前缀: {info.prefix}")          # 1.2.156.3088
    print(f"版本: {info.version}")         # 1
    print(f"ARSP: {info.arsp}")            # 1
    print(f"Vendor: {info.vendor}")        # 34C2
    print(f"本体序列号: {info.ontology_serial}")  # 478BDF
    print(f"实例序列号: {info.instance_serial}")  # 3GF546
    print(f"校验码: {info.checksum}")      # 0JU4
    print(f"是否本体: {info.is_ontology}") # False
    print(f"是否实体: {info.is_entity}")   # True
    print(f"正文(1-9级): {info.body}")    # 1.2.156.3088.1.1.34C2.478BDF.3GF546
    print(f"本体前缀: {info.ontology_prefix}")  # 1.2.156.3088.1.1.34C2.478BDF
```

### 本体/实体判断

```python
from acps_sdk.aic import is_ontology_aic, is_entity_aic, get_ontology_prefix_from_aic

# 本体 AIC: 第 9 级（实例序列号）全为 0
ontology_aic = "1.2.156.3088.1.1.34C2.478BDF.000000.0SV9"

# 实体 AIC: 第 9 级非全 0
entity_aic = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"

print(is_ontology_aic(ontology_aic))  # True
print(is_entity_aic(entity_aic))      # True

# 获取本体前缀（用于数据库 LIKE 查询）
prefix = get_ontology_prefix_from_aic(entity_aic)
# 结果: "1.2.156.3088.1.1.34C2.478BDF"
# SQL: WHERE aic LIKE '1.2.156.3088.1.1.34C2.478BDF.%'
```

### 获取指定级别内容

```python
from acps_sdk.aic import get_aic_segment

aic = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"

print(get_aic_segment(aic, 5))   # "1" (Version)
print(get_aic_segment(aic, 8))   # "478BDF" (本体序列号)
print(get_aic_segment(aic, 10))  # "0JU4" (CRC)
```

## 完整验证（需要 SALT）

CRC 校验码的验证需要 SALT。SALT 由各 ARSP（智能体注册服务商）内部维护，外部无法获知。

```python
from acps_sdk.aic import AICValidator

# 方式 1: bytes 格式
validator = AICValidator(salt=b'\x12\x34')

# 方式 2: 十六进制字符串
validator = AICValidator(salt="0x1234")
validator = AICValidator(salt="1234")

# 验证
aic = "1.2.156.3088.1.1.34C2.478BDF.3GF546.0JU4"
if validator.validate(aic):
    print("AIC 完全有效（含 CRC 验证）")
else:
    print(f"验证失败: {validator.last_error}")

# 强制要求 CRC 验证
if validator.validate(aic, require_crc=True):
    print("CRC 验证通过")

# 获取详细信息
is_valid, error, info = validator.validate_with_detail(aic)
if is_valid and info:
    print(f"ARSP: {info.arsp}")
```

### 无 SALT 时的行为

```python
# 不提供 SALT
validator = AICValidator()

# 默认行为：仅验证格式，不验证 CRC
validator.validate(aic)  # True（格式正确即通过）

# 强制要求 CRC：会失败
validator.validate(aic, require_crc=True)  # False
print(validator.last_error)  # "未配置 SALT，无法验证 CRC"
```

### 计算校验码

```python
validator = AICValidator(salt=b'\x12\x34')

# 使用 salt=0x1234 计算 1-9 级的 CRC 校验码
body = "1.2.156.3088.1.1.34C2.478BDF.3GF546"
checksum = validator.calculate_checksum(body)
print(checksum)  # "0JU4"
```

## Base36 工具

```python
from acps_sdk.aic import base36_encode, base36_decode

# 编码
print(base36_encode(255))        # "73"
print(base36_encode(255, 4))     # "0073" (固定 4 位，左侧补 0)
print(base36_encode(0x8FCF, 4))  # "0SEN"

# 解码
print(base36_decode("0SEN"))     # 36815 (0x8FCF)
print(base36_decode("73"))       # 255
```

## 功能对照表

| 功能              | 函数/类                             | 需要 SALT |
| ----------------- | ----------------------------------- | :-------: |
| 格式验证          | `validate_aic_format()`             |    ❌     |
| 简单格式验证      | `is_valid_aic_format()`             |    ❌     |
| 解析 AIC          | `parse_aic()`                       |    ❌     |
| 获取指定级别      | `get_aic_segment()`                 |    ❌     |
| 判断本体 AIC      | `is_ontology_aic()`                 |    ❌     |
| 判断实体 AIC      | `is_entity_aic()`                   |    ❌     |
| 获取本体前缀      | `get_ontology_prefix_from_aic()`    |    ❌     |
| CRC 验证          | `AICValidator.validate()`（配置 SALT 时） |    ✅     |
| **计算校验码**    | `AICValidator.calculate_checksum()` |    ✅     |
| Base36 编码       | `base36_encode()`                   |    ❌     |
| Base36 解码       | `base36_decode()`                   |    ❌     |

## 参考

- [ACPs-spec-AIC-v02.02](../../../acps-specs/02-ACPs-spec-AIC/ACPs-spec-AIC.md) - 智能体身份码规范
