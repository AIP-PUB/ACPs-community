/**
 * 与 Python 内建行为逐字对齐的小工具（SDK 内部共用，不从任何包入口导出）。
 *
 * 各模块的 Python 版在错误信息、规范化与日志序列化里依赖 CPython 内建的具体行为，
 * 为了让 Node 版的输出与 Python 版逐字一致，这里按 CPython 的规则实现：
 *
 * - 字符串：`str.strip()` / `str.split()` / `str.isspace()` 的空白集合（与 JS `trim()` / `\s` 不同：
 *   含 U+001C–U+001F、U+0085，不含 U+FEFF）、`repr(str)`（CPython `unicode_repr` 的引号与转义规则）、
 *   `sorted()` 的码点序；
 * - 数值：`repr(float)`（`12.0`、`1e-07`、`1e+16`）、`round(x, n)` 与 `format(x, ".Nf")` 的
 *   「精确十进制 + 居中取偶」语义、`json.dumps(ensure_ascii=False)` 的输出格式；
 * - 取值：`str()` / `repr()` / `type().__name__`、`isinstance(x, dict | Iterable)`、`dict.get()`。
 *
 * JS 不区分 int 与 float：除非经 {@link PyFloat} 显式标记，绝对值小于 1e21 的整数值一律视为
 * Python `int`（写全部数字），其余数值视为 `float`。
 */

// ============================================================================
// 空白、strip、split
// ============================================================================

/** Python `str.isspace()` 为真的全部字符。 */
const PY_WHITESPACE_CLASS =
  "\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const PY_STRIP = new RegExp(`^[${PY_WHITESPACE_CLASS}]+|[${PY_WHITESPACE_CLASS}]+$`, "g");
const PY_SPLIT = new RegExp(`[${PY_WHITESPACE_CLASS}]+`);
const PY_BLANK = new RegExp(`^[${PY_WHITESPACE_CLASS}]*$`);

const PY_WHITESPACE_RUN = new RegExp(`[${PY_WHITESPACE_CLASS}]+`, "g");

/** Python `re.sub(r"\s+", "", value)`：删除所有 Python 空白。 */
export function pyRemoveWhitespace(value: string): string {
  return value.replace(PY_WHITESPACE_RUN, "");
}

/** Python `str.strip()`（无参数）：去除两端 Python 空白。 */
export function pyStrip(value: string): string {
  return value.replace(PY_STRIP, "");
}

/** Python `str.strip(chars)`：去除两端属于 `chars` 的字符（按码点）。 */
export function pyStripChars(value: string, chars: string): string {
  const strip = new Set(chars);
  const codePoints = Array.from(value);
  let start = 0;
  let end = codePoints.length;
  while (start < end && strip.has(codePoints[start] ?? "")) start += 1;
  while (end > start && strip.has(codePoints[end - 1] ?? "")) end -= 1;
  return codePoints.slice(start, end).join("");
}

/** Python `str.rstrip("/")`。 */
export function pyRstripSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

/** Python `str.split()`（无参数）：按连续空白切分并丢弃空段。 */
export function pySplitWhitespace(value: string): string[] {
  return value.split(PY_SPLIT).filter((part) => part.length > 0);
}

/** 等价于 Python `not s or not s.strip()`：空字符串或只含空白。 */
export function isBlank(value: string): boolean {
  return PY_BLANK.test(value);
}

/** Python `str.isdigit()` 的 ASCII 子集：非空且全部为 0-9。 */
export function isAsciiDigits(value: string): boolean {
  return /^[0-9]+$/.test(value);
}

/**
 * 按 Unicode 码点比较字符串，等价于 Python 对 `str` 的 `<`（`sorted()` 的默认顺序）。
 * JS 默认的 `sort()` 按 UTF-16 码元比较，遇到 BMP 后段字符与代理对时顺序不同。
 */
export function compareCodePoints(a: string, b: string): number {
  const left = a[Symbol.iterator]();
  const right = b[Symbol.iterator]();
  for (;;) {
    const x = left.next();
    const y = right.next();
    if (x.done || y.done) {
      return x.done && y.done ? 0 : x.done ? -1 : 1;
    }
    const cx = x.value.codePointAt(0) ?? 0;
    const cy = y.value.codePointAt(0) ?? 0;
    if (cx !== cy) {
      return cx < cy ? -1 : 1;
    }
  }
}

// ============================================================================
// repr / str / type
// ============================================================================

/** 不可打印字符：与 CPython `str.isprintable()` 的判定一致（空格除外）。 */
const NON_PRINTABLE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]/u;

function hex(value: number, width: number): string {
  return value.toString(16).padStart(width, "0");
}

/**
 * Python `repr(str)`（CPython `unicode_repr` 规则），用于还原 `f"{value!r}"` 形式的错误信息：
 * 默认单引号，字符串含 `'` 且不含 `"` 时改用双引号；转义反斜杠与所用引号，`\t` `\n` `\r` 用短转义，
 * 其它不可打印字符用 `\xhh` / `\uhhhh` / `\Uhhhhhhhh`；可打印的非 ASCII 字符（如中文）原样输出。
 */
export function reprStr(value: string): string {
  const quote = value.includes("'") && !value.includes('"') ? '"' : "'";
  let out = quote;
  for (const ch of value) {
    const cp = ch.codePointAt(0) ?? 0;
    if (ch === quote || ch === "\\") {
      out += `\\${ch}`;
    } else if (ch === "\t") {
      out += "\\t";
    } else if (ch === "\n") {
      out += "\\n";
    } else if (ch === "\r") {
      out += "\\r";
    } else if (cp < 0x20 || cp === 0x7f) {
      out += `\\x${hex(cp, 2)}`;
    } else if (cp < 0x7f || !NON_PRINTABLE.test(ch)) {
      out += ch;
    } else if (cp <= 0xff) {
      out += `\\x${hex(cp, 2)}`;
    } else if (cp <= 0xffff) {
      out += `\\u${hex(cp, 4)}`;
    } else {
      out += `\\U${hex(cp, 8)}`;
    }
  }
  return out + quote;
}

/** Python `repr(list[str])`，例如 `['a', 'b']`。 */
export function reprStrList(values: readonly string[]): string {
  return `[${values.map(reprStr).join(", ")}]`;
}

/** Python `repr(tuple[str, ...])`，例如 `('a', 'b')`；单元素时带尾逗号 `('a',)`。 */
export function reprStrTuple(values: readonly string[]): string {
  if (values.length === 1) {
    return `(${reprStr(values[0] ?? "")},)`;
  }
  return `(${values.map(reprStr).join(", ")})`;
}

/** Python `repr(value)`，覆盖 JSON 能表达的取值（None / bool / int / float / str / list / dict）。 */
export function pyRepr(value: unknown): string {
  if (value === null || value === undefined) {
    return "None";
  }
  if (typeof value === "boolean") {
    return value ? "True" : "False";
  }
  if (typeof value === "number") {
    return pyNumberStr(value, pyFloatRepr);
  }
  if (typeof value === "bigint") {
    return value.toString();
  }
  if (typeof value === "string") {
    return reprStr(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => pyRepr(item)).join(", ")}]`;
  }
  if (isPlainObject(value)) {
    // 值为 undefined 的键视为不存在（与 pythonJsonDumps / pydantic exclude_none 的约定一致）
    const entries = Object.entries(value)
      .filter(([, item]) => item !== undefined)
      .map(([key, item]) => `${reprStr(key)}: ${pyRepr(item)}`);
    return `{${entries.join(", ")}}`;
  }
  return String(value);
}

/** Python `str(value)`：字符串原样，其余同 {@link pyRepr}（如 pydantic 校验器里的 `str(item)`）。 */
export function pyStr(value: unknown): string {
  return typeof value === "string" ? value : pyRepr(value);
}

/** Python `type(value).__name__`（JSON 取值范围），用于还原 `TypeError` 文本，例如 `'NoneType'`。 */
export function pyTypeName(value: unknown): string {
  if (value === null || value === undefined) {
    return "NoneType";
  }
  if (typeof value === "boolean") {
    return "bool";
  }
  if (typeof value === "number") {
    return isPyIntLike(value) ? "int" : "float";
  }
  if (typeof value === "string") {
    return "str";
  }
  if (Array.isArray(value)) {
    return "list";
  }
  return isPlainObject(value) ? "dict" : typeof value;
}

/** Python 真值判断（JSON 取值范围）：None / False / 0 / 空字符串 / 空 list / 空 dict 为假（NaN 为真）。 */
export function pyTruthy(value: unknown): boolean {
  if (value === null || value === undefined || value === false || value === 0 || value === "") {
    return false;
  }
  if (Array.isArray(value)) {
    return value.length > 0;
  }
  if (typeof value === "object") {
    return Object.keys(value).length > 0;
  }
  return true;
}

/** Python 内建 `ValueError` 的对应物（`TypeError` 直接用 JS 的 `TypeError`）。 */
export class PyValueError extends Error {
  override name = "ValueError";
}

// ============================================================================
// dict / Iterable
// ============================================================================

/** Python `isinstance(value, dict)`：普通对象（非数组、非 null）。 */
export function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const proto: unknown = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

/** Python `dict.get(key)`：只读自有属性，避免命中 `constructor`、`toString` 等原型属性。 */
export function getOwn(record: Record<string, unknown>, key: string): unknown {
  return Object.hasOwn(record, key) ? record[key] : undefined;
}

function isBytesLike(value: unknown): boolean {
  return value instanceof Uint8Array || value instanceof ArrayBuffer;
}

/**
 * Python `isinstance(value, Iterable)` 后的迭代结果（字符串需由调用方先处理）。
 *
 * - 数组、Set 等可迭代对象：逐个元素；
 * - 普通对象与 Map 对应 Python `dict`：迭代键；
 * - 字节串对应 Python `bytes`：逐个整数；
 * - 其它值：返回 `null`（非 Iterable）。
 */
export function iterPython(value: unknown): unknown[] | null {
  if (value instanceof Map) return Array.from(value.keys());
  if (isBytesLike(value)) return Array.from(new Uint8Array(value as ArrayBufferLike));
  if (isPlainObject(value)) return Object.keys(value);
  if (typeof value === "object" && value !== null && Symbol.iterator in value) {
    return Array.from(value as Iterable<unknown>);
  }
  return null;
}

/**
 * keycloak 辅助函数使用的更严格判断：
 * `isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, str, dict))`。
 */
export function iterNonMappingSequence(value: unknown): unknown[] | null {
  if (typeof value === "string" || value instanceof Map || isBytesLike(value)) return null;
  if (isPlainObject(value)) return null;
  if (typeof value === "object" && value !== null && Symbol.iterator in value) {
    return Array.from(value as Iterable<unknown>);
  }
  return null;
}

/** 去重并规范化字符串序列：strip、丢弃空串、保留首次出现顺序。 */
export function normalizeDistinctStrings(values: Iterable<string>): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    const item = pyStrip(value);
    if (!item || seen.has(item)) {
      continue;
    }
    seen.add(item);
    result.push(item);
  }
  return result;
}

// ============================================================================
// float repr、json.dumps、round / format
// ============================================================================

/**
 * pydantic `float` 字段取值的标记。
 *
 * JS 无法区分 `12` 与 `12.0`，而 pydantic 会把 `float` 字段里的整数转成 float，
 * `json.dumps` 随后写出 `12.0`。模型序列化时把这类字段包成 `PyFloat`，
 * {@link pythonJsonDumps} 据此按 float repr 输出；JCS 等其它序列化器经 `toJSON` 取回原始数值。
 */
export class PyFloat {
  constructor(readonly value: number) {}

  toJSON(): number {
    return this.value;
  }
}

/** 正有限数的最短往返十进制表示：`value = 0.d1d2d3… × 10^(exponent + 1)`。 */
function shortestDigits(abs: number): { digits: string; exponent: number } {
  // Number#toString 规定了「最短且最接近」的数字串（ECMA-262 Number::toString），
  // 与 CPython repr 使用的 David Gay 最短模式结果相同。
  const text = String(abs);
  const [mantissa = "", expPart] = text.split("e");
  const [intPart = "", fracPart = ""] = mantissa.split(".");
  let digits = intPart + fracPart;
  let pointPos = intPart.length + (expPart === undefined ? 0 : Number(expPart));
  const leadingZeros = digits.length - digits.replace(/^0+/, "").length;
  digits = digits.slice(leadingZeros).replace(/0+$/, "");
  pointPos -= leadingZeros;
  return { digits, exponent: pointPos - 1 };
}

/** Python `repr(float)`：`nan` / `inf` / `-inf` / `0.0` / `-0.0` / `1e-07` / `1e+16` / `12.0`。 */
export function pyFloatRepr(value: number): string {
  if (Number.isNaN(value)) {
    return "nan";
  }
  if (!Number.isFinite(value)) {
    return value > 0 ? "inf" : "-inf";
  }
  if (value === 0) {
    return Object.is(value, -0) ? "-0.0" : "0.0";
  }
  const sign = value < 0 ? "-" : "";
  const { digits, exponent } = shortestDigits(Math.abs(value));
  // CPython float_repr_style='short'：小数点位置 decpt = exponent + 1，
  // decpt <= -4 或 decpt > 16 时用科学计数法，指数至少两位。
  if (exponent < -4 || exponent >= 16) {
    const mantissa = digits.length > 1 ? `${digits.charAt(0)}.${digits.slice(1)}` : digits;
    const expSign = exponent < 0 ? "-" : "+";
    return `${sign}${mantissa}e${expSign}${String(Math.abs(exponent)).padStart(2, "0")}`;
  }
  if (exponent < 0) {
    return `${sign}0.${"0".repeat(-exponent - 1)}${digits}`;
  }
  const intLen = exponent + 1;
  if (digits.length > intLen) {
    return `${sign}${digits.slice(0, intLen)}.${digits.slice(intLen)}`;
  }
  return `${sign}${digits.padEnd(intLen, "0")}.0`;
}

/** 视为 Python `int` 的 JS number：整数值且绝对值小于 1e21。 */
function isPyIntLike(value: number): boolean {
  return Number.isInteger(value) && Math.abs(value) < 1e21;
}

/**
 * JS number → Python 数值文本。
 *
 * JS 不区分 int 与 float：绝对值小于 1e21 的整数值（含 `-0` → `0`、2^53 以上的大整数如纳秒时间戳）视为
 * Python `int`，写出全部数字；其余（非整数、≥ 1e21 的值）视为 `float`（如 `1e21` → `1e+21`）。
 * 需要保留 `12.0` / `-0.0` 形式的 float 字段经 {@link PyFloat} 输出。
 */
function pyNumberStr(value: number, floatStr: (value: number) => string): string {
  if (!isPyIntLike(value)) {
    return floatStr(value);
  }
  // 2^53 以上 String() 只给最短往返数字（2^60 → 1152921504606847000），BigInt 才是该 double 的精确整数值
  return Number.isSafeInteger(value) ? String(value) : BigInt(value).toString();
}

/** `json.dumps` 的 floatstr：非有限值写 `NaN` / `Infinity` / `-Infinity`（allow_nan=True）。 */
function jsonFloatStr(value: number): string {
  if (Number.isNaN(value)) {
    return "NaN";
  }
  if (!Number.isFinite(value)) {
    return value > 0 ? "Infinity" : "-Infinity";
  }
  return pyFloatRepr(value);
}

function isPlainRecord(value: object): value is Record<string, unknown> {
  return !Array.isArray(value);
}

/**
 * 等价于 Python `json.dumps(value, ensure_ascii=False)`（默认分隔符 `", "` / `": "`）。
 *
 * 数值规则：{@link PyFloat} 按 float repr；其余 JS number 中绝对值小于 1e21 的整数值视为 Python `int`，
 * 其它视为 `float`（见 pyNumberStr）；`bigint` 视为 `int`。对象中值为 `undefined` 的键视为不存在
 * （对应 pydantic `exclude_none` 之后的结果），数组中的 `undefined` 写 `null`。
 * 带 `toJSON()` 的对象（如 Date）先取 `toJSON()` 的结果。
 *
 * 字符串转义与 `JSON.stringify` 相同；唯一差异是孤立代理项：JS 写成 `\udxxx` 转义，
 * Python 原样输出后在 UTF-8 写文件时报错。
 */
export function pythonJsonDumps(value: unknown): string {
  const active = new Set<object>();

  const encode = (current: unknown): string => {
    if (current instanceof PyFloat) {
      return jsonFloatStr(current.value);
    }
    if (current === null || current === undefined) {
      return "null";
    }
    switch (typeof current) {
      case "boolean":
        return current ? "true" : "false";
      case "number":
        return pyNumberStr(current, jsonFloatStr);
      case "bigint":
        return current.toString();
      case "string":
        return JSON.stringify(current);
      case "object":
        break;
      default:
        throw new TypeError(`Object of type ${typeof current} is not JSON serializable`);
    }
    const object = current as object;
    const toJSON = (object as { toJSON?: unknown }).toJSON;
    if (typeof toJSON === "function") {
      return encode((toJSON as () => unknown).call(object));
    }
    if (active.has(object)) {
      throw new Error("Circular reference detected");
    }
    active.add(object);
    try {
      if (Array.isArray(object)) {
        return object.length === 0 ? "[]" : `[${object.map((item) => encode(item)).join(", ")}]`;
      }
      if (!isPlainRecord(object)) {
        throw new TypeError("Object is not JSON serializable");
      }
      const parts: string[] = [];
      for (const [key, item] of Object.entries(object)) {
        if (item !== undefined) {
          parts.push(`${JSON.stringify(key)}: ${encode(item)}`);
        }
      }
      return parts.length === 0 ? "{}" : `{${parts.join(", ")}}`;
    } finally {
      active.delete(object);
    }
  };

  return encode(value);
}

/** 把正有限 double 精确分解为 `mantissa × 2^exponent`。 */
function decomposeDouble(abs: number): { mantissa: bigint; exponent: number } {
  const view = new DataView(new ArrayBuffer(8));
  view.setFloat64(0, abs);
  const high = view.getUint32(0);
  const low = view.getUint32(4);
  const biased = (high >>> 20) & 0x7ff;
  const fraction = (BigInt(high & 0xfffff) << 32n) | BigInt(low);
  if (biased === 0) {
    return { mantissa: fraction, exponent: -1074 };
  }
  return { mantissa: fraction | (1n << 52n), exponent: biased - 1075 };
}

/** 对 `abs × 10^digits` 的精确值做 round-half-even，返回整数。 */
function scaledRoundHalfEven(abs: number, digits: number): bigint {
  const { mantissa, exponent } = decomposeDouble(abs);
  let numerator = mantissa;
  let denominator = 1n;
  if (exponent >= 0) {
    numerator <<= BigInt(exponent);
  } else {
    denominator <<= BigInt(-exponent);
  }
  if (digits >= 0) {
    numerator *= 10n ** BigInt(digits);
  } else {
    denominator *= 10n ** BigInt(-digits);
  }
  const quotient = numerator / denominator;
  const twiceRemainder = (numerator % denominator) * 2n;
  if (twiceRemainder > denominator || (twiceRemainder === denominator && (quotient & 1n) === 1n)) {
    return quotient + 1n;
  }
  return quotient;
}

/**
 * Python `round(x, ndigits)`（x 为 float）：按 x 的精确二进制值舍入到 ndigits 位小数，
 * 恰好居中时取偶，再按正确舍入转回 double。与 `Math.round` / `toFixed` 的结果在居中时不同。
 */
export function pyRound(value: number, ndigits = 0): number {
  if (!Number.isFinite(value) || value === 0) {
    return value;
  }
  const scaled = scaledRoundHalfEven(Math.abs(value), ndigits);
  const result = Number(`${scaled}e${-ndigits}`);
  return value < 0 ? -result : result;
}

/** Python `format(x, ".{digits}f")`（x 为 float）：精确十进制、居中取偶。 */
export function pyFormatFixed(value: number, digits: number): string {
  if (Number.isNaN(value)) {
    return "nan";
  }
  if (!Number.isFinite(value)) {
    return value > 0 ? "inf" : "-inf";
  }
  const negative = value < 0 || Object.is(value, -0);
  const scaled = scaledRoundHalfEven(Math.abs(value), digits)
    .toString()
    .padStart(digits + 1, "0");
  const intPart = scaled.slice(0, scaled.length - digits);
  const fracPart = digits > 0 ? `.${scaled.slice(scaled.length - digits)}` : "";
  return `${negative ? "-" : ""}${intPart}${fracPart}`;
}
