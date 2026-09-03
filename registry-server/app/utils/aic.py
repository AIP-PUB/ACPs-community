from __future__ import annotations

import hashlib
import re
import secrets

from .utils import get_beijing_time

# 规范来源：ACPs-spec-AIC-v02.02
# AIC 形如：
#   1.2.156.3088.<VER>.<ARSP>.<VENDOR>.<ONTOLOGY_SN>.<INSTANCE_SN>.<CRC16>
# 其中 CRC16 = CRC-16/CCITT-FALSE(0x1021, init=0xFFFF, refin/refout=false, xorout=0x0000)
# 本实现支持对 CRC 计算加入盐：将环境变量 AIC_CRC_SALT（十六进制字符串）解析为字节后，
# 追加到 body_1_9 的 ASCII 字节序列末尾参与 CRC 计算。
# 盐值每次从 Settings 读取，避免在模块导入时缓存；单测可 patch 本模块的 AIC_CRC_SALT。
AIC_CRC_SALT: str | None = None

# 由国家OID注册中心分配的前缀
AIC_PREFIX = "1.2.156.3088"

# 第 5 级：AIC 版本号（1~Z，Base36）
PROTOCOL_VERSION = "1"

# 默认序列号长度（规范允许 1～9 位；未在 TOML 配置时默认生成 6 位）
# 本体 AIC 第9级固定为字符 "0"（判断仍接受任意长度的全 0，以兼容历史 AIC）
MIN_AIC_SERIAL_LEN = 1
MAX_AIC_SERIAL_LEN = 9
DEFAULT_ONTOLOGY_SERIAL_LEN = 6
DEFAULT_INSTANCE_SERIAL_LEN = 6
ONTOLOGY_INSTANCE_SERIAL = "0"

# Base36 字母表（0-9, A-Z）
BASE36_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
BASE36_INDEX = {ch: i for i, ch in enumerate(BASE36_ALPHABET)}

_RE_BASE36 = re.compile(r"^[0-9A-Z]+$")
_RE_BASE36_4 = re.compile(r"^[0-9A-Z]{4}$")
_RE_AIC_LEVEL_CODE = re.compile(r"^[0-9A-Z]{1,6}$")
_RE_AIC_PROTOCOL_VERSION = re.compile(r"^[1-9A-Z]$")
_RE_DIGITS = re.compile(r"^[0-9]+$")


def _base36_encode(num: int, length: int) -> str:
    """将非负整数编码为固定长度的 Base36 字符串（大写，左侧以 0 补齐）。"""
    if num < 0:
        raise ValueError("num 必须是非负整数")
    if length <= 0:
        raise ValueError("length 必须为正数")
    if num == 0:
        return "0".rjust(length, "0")
    digits = []
    base = 36
    while num > 0:
        num, rem = divmod(num, base)
        digits.append(BASE36_ALPHABET[rem])
    encoded = "".join(reversed(digits))
    if len(encoded) > length:
        # 超长则截断右侧（低位），保持固定长度
        encoded = encoded[-length:]
    return encoded.rjust(length, "0")


def _base36_decode(s: str) -> int:
    """将 Base36 字符串解码为整数。允许小写输入与空格。"""
    if not s:
        return 0
    val = 0
    for ch in s.strip().upper():
        if ch == " ":
            continue
        if ch not in BASE36_INDEX:
            raise ValueError(f"非 Base36 字符: {ch}")
        val = val * 36 + BASE36_INDEX[ch]
    return val


def _get_ms_of_year(now_beijing: float | None = None) -> int:
    """获取北京时间当年内的毫秒数（去掉年份影响）。

    为避免闰年边界误差，这里精确计算：从当年 01-01 00:00:00.000 到当前时间的毫秒差。
    """
    # 当前北京时间
    dt = get_beijing_time()
    # 当年起点（北京时间）
    year_start = dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    # 差值（毫秒）
    delta_ms = int((dt - year_start).total_seconds() * 1000)
    return max(delta_ms, 0)


def _serial_from_ms_with_salt(ms_in_year: int, salt: bytes, kind: bytes, length: int) -> str:
    """基于年内毫秒数 + 随机盐，生成指定长度的 Base36 序列。

    为了避免 Base36 非 2 的幂导致的位操作复杂性，采用 BLAKE2b 哈希将
    (kind || ms_in_year || salt) 映射为高熵字节，再转换为大整数后以 Base36 编码，
    取所需长度，不足左侧以 '0' 补齐。不同 kind（b'OBJ'/b'INS'）保证两段序列不同。
    """
    # 组装消息：kind + ms(8B big-endian) + salt(>=8B)
    ms_bytes = ms_in_year.to_bytes(8, byteorder="big", signed=False)
    h = hashlib.blake2b(digest_size=16)
    h.update(kind)
    h.update(ms_bytes)
    h.update(salt)
    digest = h.digest()  # 128-bit
    val = int.from_bytes(digest, "big")
    s36 = _base36_encode(val, length)
    # 使用末尾 length 位，确保不同长度时后缀分布稳定
    if len(s36) > length:
        s36 = s36[-length:]
    return s36


def validate_aic_serial_len(value: object, *, name: str = "serial_len") -> int:
    """校验 AIC 第8/9 级自动生成长度（规范允许 1～9）。

    Args:
        value: 待校验的长度。
        name: 用于错误信息的字段名。

    Returns:
        合法的长度整数。

    Raises:
        ValueError: 非整数、布尔值，或不在 1～9。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是 1～{MAX_AIC_SERIAL_LEN} 的整数")
    if not (MIN_AIC_SERIAL_LEN <= value <= MAX_AIC_SERIAL_LEN):
        raise ValueError(f"{name} 必须在 {MIN_AIC_SERIAL_LEN}～{MAX_AIC_SERIAL_LEN} 之间")
    return value


def _resolve_protocol_version(value: str | None = None) -> str:
    """解析第5级版本号：显式参数优先，否则读 Settings。"""
    if value is not None:
        return normalize_aic_protocol_version(value)
    from app.core.config import settings

    return settings.aic_protocol_version


def _resolve_ontology_serial_len(value: int | None = None) -> int:
    """解析第8级自动生成长度：显式参数优先，否则读 Settings。"""
    if value is not None:
        return validate_aic_serial_len(value, name="ontology_serial_len")
    from app.core.config import settings

    return settings.aic_ontology_serial_len


def _resolve_instance_serial_len(value: int | None = None) -> int:
    """解析第9级自动生成长度：显式参数优先，否则读 Settings。"""
    if value is not None:
        return validate_aic_serial_len(value, name="instance_serial_len")
    from app.core.config import settings

    return settings.aic_instance_serial_len


def _resolve_aic_crc_salt() -> str:
    """解析 CRC 盐。优先使用测试 patch 的模块属性，否则每次读取 Settings。"""
    module_salt = globals().get("AIC_CRC_SALT")
    if isinstance(module_salt, str):
        return module_salt
    from app.core.config import settings

    return settings.aic_crc_salt


def _normalize_aic_text(text: str) -> str:
    """规范化：去除空白字符并转为大写（保留 '.' 分隔符）。"""
    if text is None:
        return ""
    # 去除所有空白（含\t/\n等）
    return re.sub(r"\s+", "", str(text)).upper()


def normalize_aic_protocol_version(value: str) -> str:
    """规范化 AIC 第5级版本号。

    去首尾空白、转大写，要求恰好 1 位 Base36，取值 1～Z（禁止 ``0``）。

    Args:
        value: 原始版本号。

    Returns:
        规范化后的大写版本号。

    Raises:
        ValueError: 空值、非 1 位，或含非法字符（含 ``0``）。
    """
    normalized = value.strip().upper()
    if not _RE_AIC_PROTOCOL_VERSION.fullmatch(normalized):
        raise ValueError("AIC 第5级版本号必须是 1 位 Base36（1-9/A-Z）")
    return normalized


def normalize_aic_level_code(value: str) -> str:
    """规范化 AIC 第6/7 级序号。

    去首尾空白、转大写，要求 1～6 位 Base36（0-9/A-Z）。允许前导零，不自动补齐也不剥离前导零。
    全 0（每一位都是 ``0``）非法。

    Args:
        value: 原始序号字符串。

    Returns:
        规范化后的大写序号。

    Raises:
        ValueError: 空值、超长、含非法字符或全 0。
    """
    normalized = value.strip().upper()
    if not _RE_AIC_LEVEL_CODE.fullmatch(normalized):
        raise ValueError("AIC 第6/7 级序号必须是 1～6 位 Base36（0-9/A-Z）")
    if set(normalized) == {"0"}:
        raise ValueError("AIC 第6/7 级序号不能为全 0")
    return normalized


def _split_aic(aic_text: str) -> list[str]:
    aic_text = _normalize_aic_text(aic_text)
    if not aic_text:
        return []
    parts = aic_text.split(".")
    # 不允许空段
    if any(p == "" for p in parts):
        return []
    return parts


def _crc16_ccitt_false_with_salt(data: bytes, salt: bytes) -> int:
    """计算 CRC-16/CCITT-FALSE。

    参数为 poly=0x1021、init=0xFFFF、refin/refout=False、xorout=0x0000，
    并在计算前将 salt 追加到原始数据末尾。
    """
    salted_data = data + salt
    crc = 0xFFFF
    for b in salted_data:
        crc ^= (b << 8) & 0xFFFF
        for _ in range(8):
            crc = (crc << 1 ^ 4129) & 65535 if crc & 32768 else crc << 1 & 65535
    return crc & 0xFFFF


def calculate_aic_checksum(body_1_9: str) -> str:
    """计算第 10 级 CRC 校验码（固定 4 位 Base36，0-9/A-Z，大写）。

    CRC 输入为：normalize(body_1_9).encode('ascii') + salt_bytes
    其中 salt_bytes 由 AIC_CRC_SALT（十六进制字符串）解析得到。
    """
    normalized = _normalize_aic_text(body_1_9)
    crc_salt = _resolve_aic_crc_salt()
    # 将十六进制字符串形式的 salt 解析为字节
    try:
        salt_hex = crc_salt[2:] if crc_salt.lower().startswith("0x") else crc_salt
        if len(salt_hex) % 2 != 0:
            salt_hex = "0" + salt_hex
        salt_bytes = bytes.fromhex(salt_hex)
    except Exception:
        salt_bytes = b"\xff\xff"

    crc = _crc16_ccitt_false_with_salt(normalized.encode("ascii"), salt_bytes)
    return _base36_encode(crc, 4)


def validate_aic(aic: str, *, expected_prefix: str = AIC_PREFIX) -> bool:
    """验证 ACPs-spec-AIC-v02.02 AIC。

    规则：
    - 10 段（以 '.' 分隔）；
    - 1~4 段为数字（OID 前缀）；
    - 5 段为 Base36 单字符（版本号）；
    - 6~9 段为 Base36（0-9,A-Z），长度分别为 1~6,1~6,1~9,1~9；
    - 10 段为固定 4 位 Base36；
        - CRC16 对 1~9 段拼接字符串（含 '.'）计算，大小写不敏感；
            本实现会在该字符串 ASCII 字节末尾追加 salt_bytes 后再计算 CRC。
    """
    parts = _split_aic(aic)
    if len(parts) != 10:
        return False

    prefix_parts = expected_prefix.split(".") if expected_prefix else []
    if prefix_parts and parts[: len(prefix_parts)] != prefix_parts:
        return False

    if not all(_RE_DIGITS.fullmatch(p) for p in parts[:4]):
        return False

    seg5, seg6, seg7, seg8, seg9, seg10 = (
        parts[4],
        parts[5],
        parts[6],
        parts[7],
        parts[8],
        parts[9],
    )

    if not (_RE_BASE36.fullmatch(seg5) and len(seg5) == 1):
        return False
    if not (_RE_BASE36.fullmatch(seg6) and 1 <= len(seg6) <= 6):
        return False
    if not (_RE_BASE36.fullmatch(seg7) and 1 <= len(seg7) <= 6):
        return False
    if not (_RE_BASE36.fullmatch(seg8) and 1 <= len(seg8) <= 9):
        return False
    if not (_RE_BASE36.fullmatch(seg9) and 1 <= len(seg9) <= 9):
        return False
    if not _RE_BASE36_4.fullmatch(seg10):
        return False

    body_1_9 = ".".join(parts[:9])
    expected_crc = calculate_aic_checksum(body_1_9)
    return expected_crc == seg10


def _validate_base36_segment(name: str, value: str, *, min_len: int, max_len: int) -> str:
    v = _normalize_aic_text(value)
    if not v:
        raise ValueError(f"{name} 不能为空")
    if not _RE_BASE36.fullmatch(v):
        raise ValueError(f"{name} 必须仅包含 0-9 与 A-Z")
    if not (min_len <= len(v) <= max_len):
        raise ValueError(f"{name} 长度必须在 {min_len}~{max_len} 之间")
    return v


def _generate_nonzero_base36(kind: bytes, length: int) -> str:
    ms_in_year = _get_ms_of_year()
    salt = secrets.token_bytes(8)
    serial = _serial_from_ms_with_salt(ms_in_year, salt, kind, length)
    # 避免全 0
    while set(serial) == {"0"}:
        salt = secrets.token_bytes(8)
        serial = _serial_from_ms_with_salt(ms_in_year, salt, kind, length)
    return serial


def generate_aic_provider_code(*, length: int = 6) -> str:
    """随机生成 AIC 第7级供应商序号（固定长度 Base36，首位非 0）。

    管理员预置的自定义序号仍允许前导零；本函数只约束自动分配的随机串。
    """
    if not (1 <= length <= 6):
        raise ValueError("length 必须在 1～6 之间")
    # 随机分配的第7级首位不得为 0；其余位仍用完整 Base36。
    first = secrets.choice(BASE36_ALPHABET[1:])
    rest = "".join(secrets.choice(BASE36_ALPHABET) for _ in range(length - 1))
    return normalize_aic_level_code(first + rest)


def generate_aic(
    protocol_version: str | None = None,
    *,
    manager_code: str,
    provider_code: str,
    ontology_serial_len: int | None = None,
    instance_serial_len: int | None = None,
) -> str:
    """生成实体 AIC（第 9 级实例序列号不为全 0）。

    第5级默认来自 TOML ``[aic].protocol_version``；
    第8、9 级长度默认来自 ``ontology_serial_len`` / ``instance_serial_len``。
    """
    ver = _resolve_protocol_version(protocol_version)
    arsp = _validate_base36_segment("manager_code", manager_code, min_len=1, max_len=6)
    vendor = _validate_base36_segment("provider_code", provider_code, min_len=1, max_len=6)

    ontology_serial = _generate_nonzero_base36(b"ONT", _resolve_ontology_serial_len(ontology_serial_len))
    instance_serial = _generate_nonzero_base36(b"INS", _resolve_instance_serial_len(instance_serial_len))

    body_1_9 = f"{AIC_PREFIX}.{ver}.{arsp}.{vendor}.{ontology_serial}.{instance_serial}"
    crc = calculate_aic_checksum(body_1_9)
    return f"{body_1_9}.{crc}"


def get_instance_serial(aic: str) -> str | None:
    """提取第 9 级实例序列号（失败返回 None）。"""
    parts = _split_aic(aic)
    if len(parts) != 10:
        return None
    return parts[8]


def is_ontology_aic(aic: str) -> bool:
    """
    判断 AIC 是否为本体 AIC（Ontology AIC）。

    规则：第 9 级实例序列号全为 0。
    """
    if not validate_aic(aic):
        return False
    instance_serial = get_instance_serial(aic)
    if instance_serial is None:
        return False
    return bool(instance_serial) and set(instance_serial) == {"0"}


def is_entity_aic(aic: str) -> bool:
    """
    判断 AIC 是否为实体 AIC（Entity AIC）。

    规则：第 9 级实例序列号非全 0。
    """
    return not is_ontology_aic(aic)


def get_ontology_aic_from_entity(entity_aic: str) -> str | None:
    """
    从实体 AIC 提取对应的本体 AIC：将第 9 级替换为字符 ``0`` 并重算 CRC。
    """
    if not validate_aic(entity_aic):
        return None
    parts = _split_aic(entity_aic)
    parts[8] = ONTOLOGY_INSTANCE_SERIAL
    body_1_9 = ".".join(parts[:9])
    parts[9] = calculate_aic_checksum(body_1_9)
    return ".".join(parts)


def generate_entity_aic_from_ontology(
    ontology_aic: str,
    *,
    instance_serial_len: int | None = None,
) -> str | None:
    """
    基于本体 AIC 生成新的实体 AIC：保留 1~8 级，重生成第 9 级并重算 CRC。

    第9级长度取 TOML ``[aic].instance_serial_len``（或显式参数），不沿用本体的 ``0`` 位数。
    """
    if not is_ontology_aic(ontology_aic):
        return None
    parts = _split_aic(ontology_aic)
    parts[8] = _generate_nonzero_base36(b"ENT", _resolve_instance_serial_len(instance_serial_len))
    body_1_9 = ".".join(parts[:9])
    parts[9] = calculate_aic_checksum(body_1_9)
    return ".".join(parts)


def get_derived_entity_like_prefix(ontology_aic: str) -> str | None:
    """用于 DB like 查询的派生实体前缀：'<1..8>.'（失败返回 None）。"""
    if not validate_aic(ontology_aic):
        return None
    parts = _split_aic(ontology_aic)
    return ".".join(parts[:8]) + "."


def generate_ontology_aic(
    protocol_version: str | None = None,
    *,
    manager_code: str,
    provider_code: str,
    ontology_serial_len: int | None = None,
    instance_serial_len: int | None = None,
) -> str:
    """生成本体 AIC（第 9 级实例序列号为字符 ``0``）。

    第5级默认来自 TOML ``[aic].protocol_version``；
    第8级长度默认来自 ``ontology_serial_len``。
    第9级固定为 ``0``，不按 ``instance_serial_len`` 补齐。
    """
    ver = _resolve_protocol_version(protocol_version)
    arsp = _validate_base36_segment("manager_code", manager_code, min_len=1, max_len=6)
    vendor = _validate_base36_segment("provider_code", provider_code, min_len=1, max_len=6)
    if instance_serial_len is not None:
        validate_aic_serial_len(instance_serial_len, name="instance_serial_len")

    ontology_serial = _generate_nonzero_base36(b"ONT", _resolve_ontology_serial_len(ontology_serial_len))
    instance_serial = ONTOLOGY_INSTANCE_SERIAL

    body_1_9 = f"{AIC_PREFIX}.{ver}.{arsp}.{vendor}.{ontology_serial}.{instance_serial}"
    crc = calculate_aic_checksum(body_1_9)
    return f"{body_1_9}.{crc}"
