/**
 * JSON Canonicalization Scheme（RFC 8785）。
 *
 * 对应 Python 版签名与 log_id 兜底使用的 `jcs.canonicalize()`（PyPI `jcs` 0.2.x）：
 * - 对象键按 UTF-16 码元升序排列，紧凑分隔符（`,` / `:`），无多余空白；
 * - 数值按 ECMAScript `Number::toString` 输出（`-0` → `0`、`1e21` → `1e+21`、`1e-7` → `1e-7`），
 *   Python 版先把 int 转成 float 再格式化，因此超出 2^53 的整数两边同样丢精度；
 * - 字符串转义同 `JSON.stringify`（短转义 `\b \t \n \f \r`，其余控制字符 `\u00xx` 小写）。
 *
 * 以下输入与 Python 一样直接报错：NaN / ±Infinity、含孤立代理项的字符串（Python 在 UTF-8 编码时失败）、
 * 函数 / symbol 等非 JSON 值、循环引用。对象中值为 `undefined` 的键视为不存在，数组中的 `undefined` 写 `null`
 * （与 `JSON.stringify` 一致）；带 `toJSON()` 的对象先取 `toJSON()` 的结果。
 */

const LONE_SURROGATE = /\p{Cs}/u;

function serializeNumber(value: number): string {
  if (!Number.isFinite(value)) {
    const text = Number.isNaN(value) ? "nan" : value > 0 ? "inf" : "-inf";
    throw new Error(`Invalid JSON number: ${text}`);
  }
  return JSON.stringify(value);
}

function serializeString(value: string): string {
  if (LONE_SURROGATE.test(value)) {
    throw new Error("JCS: string contains a lone surrogate and cannot be encoded as UTF-8");
  }
  return JSON.stringify(value);
}

/** RFC 8785 规范化，返回字符串。 */
export function canonicalizeToString(value: unknown): string {
  const active = new Set<object>();

  const serialize = (current: unknown): string => {
    if (current === null || current === undefined) {
      return "null";
    }
    switch (typeof current) {
      case "boolean":
        return current ? "true" : "false";
      case "number":
        return serializeNumber(current);
      case "bigint":
        return serializeNumber(Number(current));
      case "string":
        return serializeString(current);
      case "object":
        break;
      default:
        throw new TypeError(`Object of type '${typeof current}' is not JSON serializable`);
    }
    const object = current as object;
    const toJSON = (object as { toJSON?: unknown }).toJSON;
    if (typeof toJSON === "function") {
      return serialize((toJSON as () => unknown).call(object));
    }
    if (active.has(object)) {
      throw new Error("Circular reference detected");
    }
    active.add(object);
    try {
      if (Array.isArray(object)) {
        return `[${object.map((item) => serialize(item)).join(",")}]`;
      }
      const record = object as Record<string, unknown>;
      // Array#sort 的默认比较按 UTF-16 码元，正是 RFC 8785 §3.2.3 要求的顺序。
      const keys = Object.keys(record)
        .filter((key) => record[key] !== undefined)
        .sort();
      return `{${keys.map((key) => `${serializeString(key)}:${serialize(record[key])}`).join(",")}}`;
    } finally {
      active.delete(object);
    }
  };

  return serialize(value);
}

/** RFC 8785 规范化，返回 UTF-8 字节（对应 Python `jcs.canonicalize()` 返回的 bytes）。 */
export function canonicalize(value: unknown): Buffer {
  return Buffer.from(canonicalizeToString(value), "utf-8");
}
