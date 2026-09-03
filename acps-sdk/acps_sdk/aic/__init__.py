"""
ACPs 智能体身份码 (AIC) 工具模块

基于 ACPs-spec-AIC-v02.02 规范实现。

AIC 结构（10级，以 '.' 分隔）：
    1-4: OID 前缀 (1.2.156.3088)
    5:   版本号 (Base36, 1位)
    6:   ARSP 注册服务商序号 (Base36, 1-6位)
    7:   Vendor 供应商序号 (Base36, 1-6位)
    8:   Ontology SN 本体序列号 (Base36, 1-9位)
    9:   Instance SN 实例序列号 (Base36, 1-9位, 全0表示本体AIC)
    10:  CRC校验码 (Base36, 固定4位)

使用示例：
    # 基础格式验证（无需 SALT）
    from acps_sdk.aic import validate_aic_format, parse_aic

    if validate_aic_format(aic):
        info = parse_aic(aic)
        print(f"ARSP: {info.arsp}, 是否本体: {info.is_ontology}")

    # 完整验证（需要 SALT）
    from acps_sdk.aic import AICValidator

    validator = AICValidator(salt=b'\\x12\\x34')
    if validator.validate(aic):
        print("AIC 完全有效（含 CRC 验证）")
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Union, List

__all__ = [
    # 常量
    "AIC_PREFIX",
    "BASE36_ALPHABET",
    # 数据类
    "AICInfo",
    # 格式验证（无需 SALT）
    "validate_aic_format",
    "is_valid_aic_format",
    # 解析
    "parse_aic",
    "get_aic_segment",
    # 本体/实体判断
    "is_ontology_aic",
    "is_entity_aic",
    "get_ontology_prefix_from_aic",
    # 完整验证器（需要 SALT）
    "AICValidator",
    # 工具函数
    "base36_encode",
    "base36_decode",
]

# ============================================================================
# 常量定义
# ============================================================================

# 由国家 OID 注册中心分配的 AIC 前缀
AIC_PREFIX = "1.2.156.3088"

# Base36 字母表（0-9, A-Z）
BASE36_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_BASE36_INDEX = {ch: i for i, ch in enumerate(BASE36_ALPHABET)}

# 正则表达式
_RE_BASE36 = re.compile(r"^[0-9A-Z]+$")
_RE_BASE36_4 = re.compile(r"^[0-9A-Z]{4}$")
_RE_DIGITS = re.compile(r"^[0-9]+$")

# AIC 各段的长度约束
_SEGMENT_CONSTRAINTS = {
    5: (1, 1),  # Version
    6: (1, 6),  # ARSP
    7: (1, 6),  # Vendor
    8: (1, 9),  # Ontology SN
    9: (1, 9),  # Instance SN
    10: (4, 4),  # CRC
}


# ============================================================================
# 数据类
# ============================================================================


@dataclass(frozen=True)
class AICInfo:
    """AIC 解析结果"""

    raw: str
    """原始 AIC 字符串"""

    normalized: str
    """规范化后的 AIC（大写，无空白）"""

    prefix: str
    """OID 前缀 (1-4级)"""

    version: str
    """版本号 (第5级)"""

    arsp: str
    """注册服务商序号 (第6级)"""

    vendor: str
    """供应商序号 (第7级)"""

    ontology_serial: str
    """本体序列号 (第8级)"""

    instance_serial: str
    """实例序列号 (第9级)"""

    checksum: str
    """CRC 校验码 (第10级)"""

    @property
    def is_ontology(self) -> bool:
        """是否为本体 AIC（实例序列号全为 0）"""
        return set(self.instance_serial) == {"0"}

    @property
    def is_entity(self) -> bool:
        """是否为实体 AIC（实例序列号非全 0）"""
        return not self.is_ontology

    @property
    def body(self) -> str:
        """本体码（1-9级，用于 CRC 计算）"""
        return ".".join(
            [
                self.prefix,
                self.version,
                self.arsp,
                self.vendor,
                self.ontology_serial,
                self.instance_serial,
            ]
        )

    @property
    def ontology_prefix(self) -> str:
        """本体前缀（1-8级），可用于匹配同一本体的所有实体"""
        return ".".join(
            [
                self.prefix,
                self.version,
                self.arsp,
                self.vendor,
                self.ontology_serial,
            ]
        )


# ============================================================================
# Base36 编解码工具
# ============================================================================


def base36_encode(num: int, length: int = 0) -> str:
    """
    将非负整数编码为 Base36 字符串（大写）。

    Args:
        num: 非负整数
        length: 目标长度，不足左侧补 0；0 表示不补齐

    Returns:
        Base36 编码字符串

    Raises:
        ValueError: num 为负数
    """
    if num < 0:
        raise ValueError("num 必须是非负整数")
    if num == 0:
        encoded = "0"
    else:
        digits = []
        while num > 0:
            num, rem = divmod(num, 36)
            digits.append(BASE36_ALPHABET[rem])
        encoded = "".join(reversed(digits))

    if length > 0:
        encoded = encoded.rjust(length, "0")
    return encoded


def base36_decode(s: str) -> int:
    """
    将 Base36 字符串解码为整数。

    Args:
        s: Base36 字符串（大小写不敏感）

    Returns:
        解码后的整数

    Raises:
        ValueError: 包含非 Base36 字符
    """
    if not s:
        return 0
    val = 0
    for ch in s.strip().upper():
        if ch not in _BASE36_INDEX:
            raise ValueError(f"非 Base36 字符: {ch}")
        val = val * 36 + _BASE36_INDEX[ch]
    return val


# ============================================================================
# 内部工具函数
# ============================================================================


def _normalize(text: str) -> str:
    """规范化：去除空白并转为大写"""
    if not text:
        return ""
    return re.sub(r"\s+", "", str(text)).upper()


def _split_aic(aic: str) -> List[str]:
    """分割 AIC 为各段，规范化后返回"""
    normalized = _normalize(aic)
    if not normalized:
        return []
    parts = normalized.split(".")
    # 不允许空段
    if any(p == "" for p in parts):
        return []
    return parts


# ============================================================================
# 格式验证（无需 SALT）
# ============================================================================


def validate_aic_format(
    aic: str,
    *,
    expected_prefix: str = AIC_PREFIX,
) -> tuple[bool, Optional[str]]:
    """
    验证 AIC 格式是否符合规范（不验证 CRC 校验码）。

    此函数不需要 SALT，适用于 SDK 端的基础验证。

    Args:
        aic: 待验证的 AIC 字符串
        expected_prefix: 期望的 OID 前缀，默认为 "1.2.156.3088"

    Returns:
        (is_valid, error_message) 元组
        - is_valid: 格式是否有效
        - error_message: 如果无效，返回错误描述；有效则为 None
    """
    if not aic:
        return False, "AIC 不能为空"

    parts = _split_aic(aic)
    if len(parts) != 10:
        return False, f"AIC 必须包含 10 段，当前为 {len(parts)} 段"

    # 验证前缀（1-4级）
    if expected_prefix:
        prefix_parts = expected_prefix.split(".")
        actual_prefix = parts[: len(prefix_parts)]
        if actual_prefix != prefix_parts:
            return (
                False,
                f"前缀不匹配，期望 {expected_prefix}，实际 {'.'.join(actual_prefix)}",
            )

    # 验证 1-4 级为纯数字
    for i in range(4):
        if not _RE_DIGITS.fullmatch(parts[i]):
            return False, f"第 {i + 1} 级必须为纯数字，实际为 '{parts[i]}'"

    # 验证 5-10 级为 Base36 且符合长度约束
    for level in range(5, 11):
        seg = parts[level - 1]
        min_len, max_len = _SEGMENT_CONSTRAINTS[level]

        if not _RE_BASE36.fullmatch(seg):
            return False, f"第 {level} 级必须为 Base36 字符，实际为 '{seg}'"

        if not (min_len <= len(seg) <= max_len):
            return (
                False,
                f"第 {level} 级长度必须在 {min_len}-{max_len} 之间，实际为 {len(seg)}",
            )

    return True, None


def is_valid_aic_format(aic: str, *, expected_prefix: str = AIC_PREFIX) -> bool:
    """
    简化版格式验证，仅返回布尔值。

    Args:
        aic: 待验证的 AIC 字符串
        expected_prefix: 期望的 OID 前缀

    Returns:
        格式是否有效
    """
    valid, _ = validate_aic_format(aic, expected_prefix=expected_prefix)
    return valid


# ============================================================================
# AIC 解析
# ============================================================================


def parse_aic(aic: str) -> Optional[AICInfo]:
    """
    解析 AIC 字符串，返回结构化信息。

    注意：此函数不验证 CRC 校验码，仅做格式解析。

    Args:
        aic: AIC 字符串

    Returns:
        AICInfo 对象，解析失败返回 None
    """
    if not is_valid_aic_format(aic):
        return None

    parts = _split_aic(aic)
    return AICInfo(
        raw=aic,
        normalized=".".join(parts),
        prefix=".".join(parts[:4]),
        version=parts[4],
        arsp=parts[5],
        vendor=parts[6],
        ontology_serial=parts[7],
        instance_serial=parts[8],
        checksum=parts[9],
    )


def get_aic_segment(aic: str, level: int) -> Optional[str]:
    """
    获取 AIC 指定级别的内容。

    Args:
        aic: AIC 字符串
        level: 级别（1-10）

    Returns:
        指定级别的内容，失败返回 None
    """
    if not 1 <= level <= 10:
        return None
    parts = _split_aic(aic)
    if len(parts) != 10:
        return None
    return parts[level - 1]


# ============================================================================
# 本体/实体判断
# ============================================================================


def is_ontology_aic(aic: str) -> bool:
    """
    判断是否为本体 AIC。

    本体 AIC 的第 9 级（实例序列号）全为 0。

    Args:
        aic: AIC 字符串

    Returns:
        是否为本体 AIC
    """
    info = parse_aic(aic)
    return info is not None and info.is_ontology


def is_entity_aic(aic: str) -> bool:
    """
    判断是否为实体 AIC。

    实体 AIC 的第 9 级（实例序列号）非全 0。

    Args:
        aic: AIC 字符串

    Returns:
        是否为实体 AIC
    """
    info = parse_aic(aic)
    return info is not None and info.is_entity


def get_ontology_prefix_from_aic(aic: str) -> Optional[str]:
    """
    从 AIC 提取本体前缀（1-8级）。

    可用于数据库 LIKE 查询，匹配同一本体的所有实体。

    Args:
        aic: AIC 字符串

    Returns:
        本体前缀字符串，失败返回 None
    """
    info = parse_aic(aic)
    return info.ontology_prefix if info else None


# ============================================================================
# 完整验证器（需要 SALT）
# ============================================================================


class AICValidator:
    """
    AIC 完整验证器（包含 CRC 校验）。

    CRC 校验需要 SALT，SALT 由各 ARSP 内部维护。

    使用示例：
        validator = AICValidator(salt=b'\\x12\\x34')
        if validator.validate(aic):
            print("AIC 有效")
        else:
            print(f"AIC 无效: {validator.last_error}")
    """

    def __init__(self, salt: Optional[Union[bytes, str]] = None):
        """
        初始化验证器。

        Args:
            salt: CRC 计算的盐值
                - bytes: 直接使用
                - str: 视为十六进制字符串（支持 "0x" 前缀）
                - None: 不验证 CRC，仅做格式验证
        """
        self._salt: Optional[bytes] = None
        self._last_error: Optional[str] = None

        if salt is not None:
            if isinstance(salt, bytes):
                self._salt = salt
            elif isinstance(salt, str):
                self._salt = self._parse_hex_salt(salt)
            else:
                raise TypeError(f"salt 必须是 bytes 或 str，实际为 {type(salt)}")

    @staticmethod
    def _parse_hex_salt(hex_str: str) -> bytes:
        """解析十六进制字符串为字节"""
        hex_str = hex_str.strip()
        if hex_str.lower().startswith("0x"):
            hex_str = hex_str[2:]
        if len(hex_str) % 2 != 0:
            hex_str = "0" + hex_str
        return bytes.fromhex(hex_str)

    @property
    def has_salt(self) -> bool:
        """是否配置了 SALT"""
        return self._salt is not None

    @property
    def last_error(self) -> Optional[str]:
        """最后一次验证的错误信息"""
        return self._last_error

    def calculate_checksum(self, body_1_9: str) -> Optional[str]:
        """
        计算 CRC 校验码。

        Args:
            body_1_9: AIC 的 1-9 级（本体码）

        Returns:
            4 位 Base36 校验码，未配置 SALT 时返回 None
        """
        if self._salt is None:
            return None

        normalized = _normalize(body_1_9)
        data = normalized.encode("ascii") + self._salt
        crc = self._crc16_ccitt_false(data)
        return base36_encode(crc, 4)

    @staticmethod
    def _crc16_ccitt_false(data: bytes) -> int:
        """
        CRC-16/CCITT-FALSE 算法。

        参数: poly=0x1021, init=0xFFFF, refin/refout=False, xorout=0x0000
        """
        crc = 0xFFFF
        for b in data:
            crc ^= (b << 8) & 0xFFFF
            for _ in range(8):
                if crc & 0x8000:
                    crc = ((crc << 1) ^ 0x1021) & 0xFFFF
                else:
                    crc = (crc << 1) & 0xFFFF
        return crc & 0xFFFF

    def validate(
        self,
        aic: str,
        *,
        expected_prefix: str = AIC_PREFIX,
        require_crc: bool = False,
    ) -> bool:
        """
        验证 AIC。

        Args:
            aic: 待验证的 AIC 字符串
            expected_prefix: 期望的 OID 前缀
            require_crc: 是否必须验证 CRC
                - True: 必须验证 CRC（需要配置 SALT）
                - False: 如果有 SALT 则验证 CRC，否则仅验证格式

        Returns:
            AIC 是否有效
        """
        self._last_error = None

        # 格式验证
        valid, error = validate_aic_format(aic, expected_prefix=expected_prefix)
        if not valid:
            self._last_error = error
            return False

        # CRC 验证
        if self._salt is None:
            if require_crc:
                self._last_error = "未配置 SALT，无法验证 CRC"
                return False
            # 无 SALT 且不要求 CRC，格式正确即通过
            return True

        # 有 SALT，验证 CRC
        info = parse_aic(aic)
        if info is None:
            self._last_error = "解析 AIC 失败"
            return False

        expected_crc = self.calculate_checksum(info.body)
        if expected_crc != info.checksum:
            self._last_error = (
                f"CRC 校验失败，期望 {expected_crc}，实际 {info.checksum}"
            )
            return False

        return True

    def validate_with_detail(
        self,
        aic: str,
        *,
        expected_prefix: str = AIC_PREFIX,
    ) -> tuple[bool, Optional[str], Optional[AICInfo]]:
        """
        验证 AIC 并返回详细信息。

        Args:
            aic: 待验证的 AIC 字符串
            expected_prefix: 期望的 OID 前缀

        Returns:
            (is_valid, error_message, aic_info) 元组
        """
        is_valid = self.validate(aic, expected_prefix=expected_prefix)
        info = parse_aic(aic) if is_valid or self._last_error else None
        return is_valid, self._last_error, info
