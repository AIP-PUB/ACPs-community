/**
 * ACPs 智能体身份码 (AIC) 工具模块
 *
 * 基于 ACPs-spec-AIC-v02.02 规范实现，与 Python 版 `acps_sdk.aic` 一一对应。
 *
 * AIC 结构（10级，以 '.' 分隔）：
 *     1-4: OID 前缀 (1.2.156.3088)
 *     5:   版本号 (Base36, 1位)
 *     6:   ARSP 注册服务商序号 (Base36, 1-6位)
 *     7:   Vendor 供应商序号 (Base36, 1-6位)
 *     8:   Ontology SN 本体序列号 (Base36, 1-9位)
 *     9:   Instance SN 实例序列号 (Base36, 1-9位, 全0表示本体AIC)
 *     10:  CRC校验码 (Base36, 固定4位)
 *
 * 使用示例：
 *
 * ```ts
 * import { validateAicFormat, parseAic, AICValidator } from "acps-sdk/aic";
 *
 * const [valid] = validateAicFormat(aic);
 * if (valid) {
 *   const info = parseAic(aic);
 *   console.log(info?.arsp, info?.isOntology);
 * }
 *
 * const validator = new AICValidator(new Uint8Array([0x12, 0x34]));
 * if (validator.validate(aic)) console.log("AIC 完全有效（含 CRC 验证）");
 * ```
 */

import { pyRemoveWhitespace, pyStrip } from "../internal/pyCompat.js";

// ============================================================================
// 常量定义
// ============================================================================

/** 由国家 OID 注册中心分配的 AIC 前缀 */
export const AIC_PREFIX = "1.2.156.3088";

/** Base36 字母表（0-9, A-Z） */
export const BASE36_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ";

const BASE36_INDEX = new Map<string, number>(
  [...BASE36_ALPHABET].map((ch, index) => [ch, index] as const),
);

const RE_BASE36 = /^[0-9A-Z]+$/;
const RE_DIGITS = /^[0-9]+$/;

/** AIC 各段的长度约束 */
const SEGMENT_CONSTRAINTS: Readonly<Record<number, readonly [number, number]>> = {
  5: [1, 1], // Version
  6: [1, 6], // ARSP
  7: [1, 6], // Vendor
  8: [1, 9], // Ontology SN
  9: [1, 9], // Instance SN
  10: [4, 4], // CRC
};

// ============================================================================
// 数据类
// ============================================================================

/** AIC 解析结果 */
export class AICInfo {
  constructor(
    /** 原始 AIC 字符串 */
    readonly raw: string,
    /** 规范化后的 AIC（大写，无空白） */
    readonly normalized: string,
    /** OID 前缀 (1-4级) */
    readonly prefix: string,
    /** 版本号 (第5级) */
    readonly version: string,
    /** 注册服务商序号 (第6级) */
    readonly arsp: string,
    /** 供应商序号 (第7级) */
    readonly vendor: string,
    /** 本体序列号 (第8级) */
    readonly ontologySerial: string,
    /** 实例序列号 (第9级) */
    readonly instanceSerial: string,
    /** CRC 校验码 (第10级) */
    readonly checksum: string,
  ) {
    Object.freeze(this);
  }

  /** 是否为本体 AIC（实例序列号全为 0） */
  get isOntology(): boolean {
    return /^0+$/.test(this.instanceSerial);
  }

  /** 是否为实体 AIC（实例序列号非全 0） */
  get isEntity(): boolean {
    return !this.isOntology;
  }

  /** 本体码（1-9级，用于 CRC 计算） */
  get body(): string {
    return [
      this.prefix,
      this.version,
      this.arsp,
      this.vendor,
      this.ontologySerial,
      this.instanceSerial,
    ].join(".");
  }

  /** 本体前缀（1-8级），可用于匹配同一本体的所有实体 */
  get ontologyPrefix(): string {
    return [this.prefix, this.version, this.arsp, this.vendor, this.ontologySerial].join(".");
  }
}

// ============================================================================
// Base36 编解码工具
// ============================================================================

/**
 * 将非负整数编码为 Base36 字符串（大写）。
 *
 * @param num 非负整数
 * @param length 目标长度，不足左侧补 0；0 表示不补齐
 * @throws RangeError num 为负数、不是整数，或超出 `Number.MAX_SAFE_INTEGER`
 *   （Python 为任意精度整数；JS 超出安全整数后除法会丢精度，因此显式报错，与 base36Decode 对称）
 */
export function base36Encode(num: number, length = 0): string {
  if (!Number.isInteger(num) || num < 0) {
    throw new RangeError("num 必须是非负整数");
  }
  if (!Number.isSafeInteger(num)) {
    throw new RangeError("num 超出安全整数范围");
  }
  let encoded: string;
  if (num === 0) {
    encoded = "0";
  } else {
    const digits: string[] = [];
    let rest = num;
    while (rest > 0) {
      const rem = rest % 36;
      digits.push(BASE36_ALPHABET.charAt(rem));
      rest = Math.floor(rest / 36);
    }
    encoded = digits.reverse().join("");
  }
  return length > 0 ? encoded.padStart(length, "0") : encoded;
}

/**
 * 将 Base36 字符串解码为整数。
 *
 * @param s Base36 字符串（大小写不敏感）
 * @throws Error 包含非 Base36 字符
 * @throws RangeError 结果超出 `Number.MAX_SAFE_INTEGER`（Python 为任意精度整数，JS 需显式报错）
 */
export function base36Decode(s: string): number {
  if (!s) {
    return 0;
  }
  let value = 0;
  for (const ch of pyStrip(s).toUpperCase()) {
    const index = BASE36_INDEX.get(ch);
    if (index === undefined) {
      throw new Error(`非 Base36 字符: ${ch}`);
    }
    value = value * 36 + index;
    if (value > Number.MAX_SAFE_INTEGER) {
      throw new RangeError("Base36 解码结果超出安全整数范围");
    }
  }
  return value;
}

// ============================================================================
// 内部工具函数
// ============================================================================

/** 规范化：去除空白并转为大写 */
function normalizeText(text: string | null | undefined): string {
  if (!text) {
    return "";
  }
  return pyRemoveWhitespace(String(text)).toUpperCase();
}

/** 分割 AIC 为各段，规范化后返回 */
function splitAic(aic: string | null | undefined): string[] {
  const normalized = normalizeText(aic);
  if (!normalized) {
    return [];
  }
  const parts = normalized.split(".");
  if (parts.some((part) => part === "")) {
    return [];
  }
  return parts;
}

// ============================================================================
// 格式验证（无需 SALT）
// ============================================================================

export interface AicFormatOptions {
  /** 期望的 OID 前缀，默认为 "1.2.156.3088"；传空字符串表示不校验前缀 */
  expectedPrefix?: string;
}

/**
 * 验证 AIC 格式是否符合规范（不验证 CRC 校验码）。
 *
 * 此函数不需要 SALT，适用于 SDK 端的基础验证。
 *
 * @returns `[isValid, errorMessage]` 元组；有效时 errorMessage 为 null
 */
export function validateAicFormat(
  aic: string | null | undefined,
  { expectedPrefix = AIC_PREFIX }: AicFormatOptions = {},
): readonly [boolean, string | null] {
  if (!aic) {
    return [false, "AIC 不能为空"];
  }

  const parts = splitAic(aic);
  if (parts.length !== 10) {
    return [false, `AIC 必须包含 10 段，当前为 ${parts.length} 段`];
  }

  // 验证前缀（1-4级）
  if (expectedPrefix) {
    const prefixParts = expectedPrefix.split(".");
    const actualPrefix = parts.slice(0, prefixParts.length);
    const matches =
      actualPrefix.length === prefixParts.length &&
      actualPrefix.every((part, index) => part === prefixParts[index]);
    if (!matches) {
      return [false, `前缀不匹配，期望 ${expectedPrefix}，实际 ${actualPrefix.join(".")}`];
    }
  }

  // 验证 1-4 级为纯数字
  for (let i = 0; i < 4; i += 1) {
    const part = parts[i] ?? "";
    if (!RE_DIGITS.test(part)) {
      return [false, `第 ${i + 1} 级必须为纯数字，实际为 '${part}'`];
    }
  }

  // 验证 5-10 级为 Base36 且符合长度约束
  for (let level = 5; level <= 10; level += 1) {
    const seg = parts[level - 1] ?? "";
    const [minLen, maxLen] = SEGMENT_CONSTRAINTS[level] ?? [0, 0];

    if (!RE_BASE36.test(seg)) {
      return [false, `第 ${level} 级必须为 Base36 字符，实际为 '${seg}'`];
    }
    if (seg.length < minLen || seg.length > maxLen) {
      return [false, `第 ${level} 级长度必须在 ${minLen}-${maxLen} 之间，实际为 ${seg.length}`];
    }
  }

  return [true, null];
}

/** 简化版格式验证，仅返回布尔值。 */
export function isValidAicFormat(
  aic: string | null | undefined,
  options: AicFormatOptions = {},
): boolean {
  const [valid] = validateAicFormat(aic, options);
  return valid;
}

// ============================================================================
// AIC 解析
// ============================================================================

/**
 * 解析 AIC 字符串，返回结构化信息。
 *
 * 注意：此函数不验证 CRC 校验码，仅做格式解析。
 *
 * @returns AICInfo 对象，解析失败返回 null
 */
export function parseAic(aic: string | null | undefined): AICInfo | null {
  if (!isValidAicFormat(aic)) {
    return null;
  }
  const parts = splitAic(aic);
  const at = (index: number): string => parts[index] ?? "";
  return new AICInfo(
    aic as string,
    parts.join("."),
    parts.slice(0, 4).join("."),
    at(4),
    at(5),
    at(6),
    at(7),
    at(8),
    at(9),
  );
}

/**
 * 获取 AIC 指定级别的内容。
 *
 * @param level 级别（1-10）
 * @returns 指定级别的内容，失败返回 null
 */
export function getAicSegment(aic: string | null | undefined, level: number): string | null {
  if (!Number.isInteger(level) || level < 1 || level > 10) {
    return null;
  }
  const parts = splitAic(aic);
  if (parts.length !== 10) {
    return null;
  }
  return parts[level - 1] ?? null;
}

// ============================================================================
// 本体/实体判断
// ============================================================================

/** 判断是否为本体 AIC（第 9 级实例序列号全为 0）。 */
export function isOntologyAic(aic: string | null | undefined): boolean {
  const info = parseAic(aic);
  return info !== null && info.isOntology;
}

/** 判断是否为实体 AIC（第 9 级实例序列号非全 0）。 */
export function isEntityAic(aic: string | null | undefined): boolean {
  const info = parseAic(aic);
  return info !== null && info.isEntity;
}

/**
 * 从 AIC 提取本体前缀（1-8级）。
 *
 * 可用于数据库 LIKE 查询，匹配同一本体的所有实体。
 */
export function getOntologyPrefixFromAic(aic: string | null | undefined): string | null {
  const info = parseAic(aic);
  return info ? info.ontologyPrefix : null;
}

// ============================================================================
// 完整验证器（需要 SALT）
// ============================================================================

export interface AicValidateOptions extends AicFormatOptions {
  /**
   * 是否必须验证 CRC
   * - true: 必须验证 CRC（需要配置 SALT）
   * - false: 如果有 SALT 则验证 CRC，否则仅验证格式
   */
  requireCrc?: boolean;
}

/**
 * AIC 完整验证器（包含 CRC 校验）。
 *
 * CRC 校验需要 SALT，SALT 由各 ARSP 内部维护。
 */
export class AICValidator {
  private readonly salt: Uint8Array | null;
  private _lastError: string | null = null;

  /**
   * @param salt CRC 计算的盐值
   *   - Uint8Array / Buffer: 直接使用
   *   - string: 视为十六进制字符串（支持 "0x" 前缀）
   *   - null / undefined: 不验证 CRC，仅做格式验证
   */
  constructor(salt?: Uint8Array | string | null) {
    if (salt === undefined || salt === null) {
      this.salt = null;
    } else if (salt instanceof Uint8Array) {
      this.salt = Uint8Array.from(salt);
    } else if (typeof salt === "string") {
      this.salt = AICValidator.parseHexSalt(salt);
    } else {
      throw new TypeError(`salt 必须是 Uint8Array 或 string，实际为 ${typeof salt}`);
    }
  }

  /**
   * 解析十六进制字符串为字节。
   *
   * 与 Python `bytes.fromhex` 一致：字节之间的 ASCII 空白被跳过，但一个字节的两位之间不能有空白。
   */
  private static parseHexSalt(hexInput: string): Uint8Array {
    let hex = pyStrip(hexInput);
    if (hex.toLowerCase().startsWith("0x")) {
      hex = hex.slice(2);
    }
    if (hex.length % 2 !== 0) {
      hex = `0${hex}`;
    }
    const bytes: number[] = [];
    let index = 0;
    while (index < hex.length) {
      if (" \t\n\v\f\r".includes(hex.charAt(index))) {
        index += 1;
        continue;
      }
      const pair = hex.slice(index, index + 2);
      if (!/^[0-9a-fA-F]{2}$/.test(pair)) {
        throw new Error(`非法的十六进制盐值: ${hexInput}`);
      }
      bytes.push(Number.parseInt(pair, 16));
      index += 2;
    }
    return Uint8Array.from(bytes);
  }

  /** 是否配置了 SALT */
  get hasSalt(): boolean {
    return this.salt !== null;
  }

  /** 最后一次验证的错误信息 */
  get lastError(): string | null {
    return this._lastError;
  }

  /**
   * 计算 CRC 校验码。
   *
   * @param body19 AIC 的 1-9 级（本体码）
   * @returns 4 位 Base36 校验码，未配置 SALT 时返回 null
   */
  calculateChecksum(body19: string): string | null {
    if (this.salt === null) {
      return null;
    }
    const normalized = normalizeText(body19);
    // Python `str.encode("ascii")` 遇到非 ASCII 字符抛 UnicodeEncodeError；Buffer 的 "ascii" 编码会静默截断
    if (/[\u0080-\uffff]/.test(normalized)) {
      throw new Error(`'ascii' codec can't encode body: ${normalized}`);
    }
    const ascii = Buffer.from(normalized, "ascii");
    const data = new Uint8Array(ascii.length + this.salt.length);
    data.set(ascii, 0);
    data.set(this.salt, ascii.length);
    return base36Encode(AICValidator.crc16CcittFalse(data), 4);
  }

  /**
   * CRC-16/CCITT-FALSE 算法。
   *
   * 参数: poly=0x1021, init=0xFFFF, refin/refout=False, xorout=0x0000
   */
  static crc16CcittFalse(data: Uint8Array): number {
    let crc = 0xffff;
    for (const byte of data) {
      crc ^= (byte << 8) & 0xffff;
      for (let i = 0; i < 8; i += 1) {
        crc = crc & 0x8000 ? ((crc << 1) ^ 0x1021) & 0xffff : (crc << 1) & 0xffff;
      }
    }
    return crc & 0xffff;
  }

  /** 验证 AIC。 */
  validate(
    aic: string | null | undefined,
    { expectedPrefix = AIC_PREFIX, requireCrc = false }: AicValidateOptions = {},
  ): boolean {
    this._lastError = null;

    const [valid, error] = validateAicFormat(aic, { expectedPrefix });
    if (!valid) {
      this._lastError = error;
      return false;
    }

    if (this.salt === null) {
      if (requireCrc) {
        this._lastError = "未配置 SALT，无法验证 CRC";
        return false;
      }
      return true;
    }

    const info = parseAic(aic);
    if (info === null) {
      this._lastError = "解析 AIC 失败";
      return false;
    }

    const expectedCrc = this.calculateChecksum(info.body);
    if (expectedCrc !== info.checksum) {
      this._lastError = `CRC 校验失败，期望 ${expectedCrc}，实际 ${info.checksum}`;
      return false;
    }
    return true;
  }

  /**
   * 验证 AIC 并返回详细信息。
   *
   * @returns `[isValid, errorMessage, aicInfo]` 元组
   */
  validateWithDetail(
    aic: string | null | undefined,
    { expectedPrefix = AIC_PREFIX }: AicFormatOptions = {},
  ): readonly [boolean, string | null, AICInfo | null] {
    const isValid = this.validate(aic, { expectedPrefix });
    const info = isValid || this._lastError ? parseAic(aic) : null;
    return [isValid, this._lastError, info];
  }
}
