/**
 * PyJWT 2.13 解码与 claim 校验行为的移植（内部模块）。
 *
 * Python 版 `OidcTokenValidator` 的错误文本大多直接来自 PyJWT（`str(exc)`），例如
 * `Token is missing the "exp" claim`、`Audience doesn't match`、`Signature has expired`。
 * 为了让两边对同一个 token 给出相同的结论与文本，这里逐条移植了：
 *
 * - `PyJWS._load`：按最后一个 `.` 拆出签名段、按第一个 `.` 拆出 header 段，三段都用
 *   Python `base64.urlsafe_b64decode` 的宽松规则解码（忽略非字母表字符）；
 * - `PyJWS._validate_headers`：`kid` 必须是字符串、`crit` 只支持 `b64`；
 * - `PyJWS.decode_complete` 对 `b64: false` 的处理；
 * - `PyJWT._validate_claims`：required → iat → nbf → exp → iss → aud → sub → jti 的顺序与 leeway 算法，
 *   含 `int(payload["iat"])` 的 Python 转换语义。
 *
 * 签名验证本身由 `jose` 完成（见 `validator.ts`）。
 */

import {
  getOwn,
  isPlainObject,
  pyStrip,
  PyValueError,
  pyTypeName,
  reprStr,
} from "../internal/pyCompat.js";

/** PyJWT `PyJWTError` 家族的对应物；`message` 等于 Python 的 `str(exc)`。 */
export class PyJwtError extends Error {
  override name = "PyJWTError";
}

/** `PyJWS._load` 的结果。 */
export interface LoadedJwt {
  payload: Uint8Array;
  signingInput: Uint8Array;
  header: Record<string, unknown>;
  signature: Uint8Array;
}

const BASE64_TABLE = (() => {
  const table = new Int16Array(256).fill(-1);
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  for (let i = 0; i < alphabet.length; i += 1) {
    table[alphabet.charCodeAt(i)] = i;
  }
  return table;
})();
const PAD = 0x3d; // "="

/**
 * PyJWT `base64url_decode`：按长度补 `=`，再走 `base64.urlsafe_b64decode`
 * （`-_` 映射为 `+/`，随后是 CPython `binascii.a2b_base64` 的非严格模式：忽略非字母表字符，遇到足够的填充即停止）。
 */
export function pyBase64UrlDecode(input: Uint8Array): Uint8Array {
  const rem = input.length % 4;
  const source = new Uint8Array(input.length + (rem > 0 ? 4 - rem : 0)).fill(PAD);
  source.set(input);
  for (let i = 0; i < source.length; i += 1) {
    if (source[i] === 0x2d)
      source[i] = 0x2b; // "-" → "+"
    else if (source[i] === 0x5f) source[i] = 0x2f; // "_" → "/"
  }

  const out: number[] = [];
  let quadPos = 0;
  let leftChar = 0;
  let pads = 0;
  for (const byte of source) {
    if (byte === PAD) {
      if (quadPos >= 2 && quadPos + ++pads >= 4) {
        return Uint8Array.from(out);
      }
      continue;
    }
    const value = BASE64_TABLE[byte] ?? -1;
    if (value < 0) {
      continue;
    }
    pads = 0;
    switch (quadPos) {
      case 0:
        quadPos = 1;
        leftChar = value;
        break;
      case 1:
        quadPos = 2;
        out.push(((leftChar << 2) | (value >> 4)) & 0xff);
        leftChar = value & 0x0f;
        break;
      case 2:
        quadPos = 3;
        out.push(((leftChar << 4) | (value >> 2)) & 0xff);
        leftChar = value & 0x03;
        break;
      default:
        quadPos = 0;
        out.push(((leftChar << 6) | value) & 0xff);
        leftChar = 0;
        break;
    }
  }
  if (quadPos === 1) {
    throw new PyValueError(
      `Invalid base64-encoded string: number of data characters (${Math.floor(out.length / 3) * 4 + 1}) cannot be 1 more than a multiple of 4`,
    );
  }
  if (quadPos !== 0) {
    throw new PyValueError("Incorrect padding");
  }
  return Uint8Array.from(out);
}

const UTF8_DECODER = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false });

/**
 * Python `json.loads(bytes)` 的子集：UTF-8（可带 BOM）+ 标准 JSON。
 * 已知差异：不支持 Python 额外接受的 `NaN`/`Infinity` 字面量与 UTF-16/32 编码的 JSON。
 */
export function pyJsonLoads(data: Uint8Array): unknown {
  return JSON.parse(UTF8_DECODER.decode(data));
}

function lastIndexOfDot(bytes: Uint8Array): number {
  return bytes.lastIndexOf(0x2e);
}

/** `PyJWS._load`。 */
export function loadJwt(token: unknown): LoadedJwt {
  if (typeof token !== "string") {
    throw new PyJwtError("Invalid token type. Token must be a <class 'bytes'>");
  }
  const jwt = new TextEncoder().encode(token);

  const lastDot = lastIndexOfDot(jwt);
  const signingInput = lastDot >= 0 ? jwt.subarray(0, lastDot) : null;
  const firstDot = signingInput === null ? -1 : signingInput.indexOf(0x2e);
  if (signingInput === null || firstDot < 0) {
    throw new PyJwtError("Not enough segments");
  }
  const cryptoSegment = jwt.subarray(lastDot + 1);
  const headerSegment = signingInput.subarray(0, firstDot);
  const payloadSegment = signingInput.subarray(firstDot + 1);

  let headerData: Uint8Array;
  try {
    headerData = pyBase64UrlDecode(headerSegment);
  } catch {
    throw new PyJwtError("Invalid header padding");
  }

  let header: unknown;
  try {
    header = pyJsonLoads(headerData);
  } catch (error) {
    throw new PyJwtError(`Invalid header string: ${(error as Error).message}`);
  }
  if (!isPlainObject(header)) {
    throw new PyJwtError("Invalid header string: must be a json object");
  }

  let payload: Uint8Array;
  if (getOwn(header, "b64") === false) {
    if (payloadSegment.length > 0) {
      throw new PyJwtError("Payload segment must be empty when 'b64' is false.");
    }
    payload = new Uint8Array();
  } else {
    try {
      payload = pyBase64UrlDecode(payloadSegment);
    } catch {
      throw new PyJwtError("Invalid payload padding");
    }
  }

  let signature: Uint8Array;
  try {
    signature = pyBase64UrlDecode(cryptoSegment);
  } catch {
    throw new PyJwtError("Invalid crypto padding");
  }

  return { payload, signingInput: Uint8Array.from(signingInput), header, signature };
}

const SUPPORTED_CRIT = new Set(["b64"]);

/** `PyJWS._validate_headers`（解码方向）。 */
export function validateJwtHeaders(header: Record<string, unknown>): void {
  if (Object.hasOwn(header, "kid") && typeof header.kid !== "string") {
    throw new PyJwtError("Key ID header parameter must be a string");
  }
  if (Object.hasOwn(header, "crit")) {
    const crit = header.crit;
    if (!Array.isArray(crit) || crit.length === 0) {
      throw new PyJwtError("Invalid 'crit' header: must be a non-empty list");
    }
    for (const ext of crit) {
      if (typeof ext !== "string") {
        throw new PyJwtError("Invalid 'crit' header: values must be strings");
      }
      if (!SUPPORTED_CRIT.has(ext)) {
        throw new PyJwtError(`Unsupported critical extension: ${ext}`);
      }
      if (!Object.hasOwn(header, ext)) {
        throw new PyJwtError(`Critical extension '${ext}' is missing from headers`);
      }
    }
  }
}

/** `PyJWS.get_unverified_header`。 */
export function getUnverifiedHeader(token: unknown): Record<string, unknown> {
  const { header } = loadJwt(token);
  validateJwtHeaders(header);
  return header;
}

/**
 * `PyJWS.decode_complete` 中 `b64: false` 的分支：没有 detached payload 时必然失败。
 * 返回前已经由 `loadJwt` 保证 payload 段为空。
 */
export function rejectUnencodedPayload(header: Record<string, unknown>): void {
  if (getOwn(header, "b64") !== false) {
    return;
  }
  const crit = getOwn(header, "crit") ?? [];
  if (!Array.isArray(crit) || !crit.includes("b64")) {
    throw new PyJwtError("The 'b64' header parameter requires 'b64' to be listed in 'crit'.");
  }
  throw new PyJwtError(
    'It is required that you pass in a value for the "detached_payload" argument to decode a message having the b64 header set to false.',
  );
}

/** `PyJWT._decode_payload`。 */
export function decodeJwtPayload(payload: Uint8Array): Record<string, unknown> {
  let decoded: unknown;
  try {
    decoded = pyJsonLoads(payload);
  } catch (error) {
    throw new PyJwtError(`Invalid payload string: ${(error as Error).message}`);
  }
  if (!isPlainObject(decoded)) {
    throw new PyJwtError("Invalid payload string: must be a json object");
  }
  return decoded;
}

const PY_INT_LITERAL = /^[+-]?\d+(?:_\d+)*$/;

/**
 * Python `int(value)` 对 JSON 值的语义：
 * - bool → 0/1，float → 向零截断，str → 十进制解析（允许首尾空白、正负号与数字间单个下划线）；
 * - 无法解析的字符串、NaN → `ValueError`（被 PyJWT 捕获并转成对应 claim 的错误）；
 * - 无穷大 → `OverflowError`、`None`/list/dict → `TypeError`：PyJWT 不捕获，原样抛出。
 *
 * 已知差异：Python 还接受非 ASCII 的 Unicode 十进制数字（如 `"١٢٣"`）。
 */
export function pyInt(value: unknown): number {
  if (typeof value === "boolean") {
    return value ? 1 : 0;
  }
  if (typeof value === "number") {
    if (Number.isNaN(value)) {
      throw new PyValueError("cannot convert float NaN to integer");
    }
    if (!Number.isFinite(value)) {
      throw new RangeError("cannot convert float infinity to integer");
    }
    return Math.trunc(value);
  }
  if (typeof value === "string") {
    const literal = pyStrip(value);
    if (!PY_INT_LITERAL.test(literal)) {
      throw new PyValueError(`invalid literal for int() with base 10: ${reprStr(value)}`);
    }
    return Number(literal.replace(/_/g, ""));
  }
  throw new TypeError(
    `int() argument must be a string, a bytes-like object or a real number, not '${pyTypeName(value)}'`,
  );
}

/** Python 真值判断（仅 JSON 值）。 */
function pyTruthy(value: unknown): boolean {
  if (value === null || value === undefined) return false;
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string" || Array.isArray(value)) return value.length > 0;
  if (isPlainObject(value)) return Object.keys(value).length > 0;
  return true;
}

function intClaim(payload: Record<string, unknown>, key: string, message: string): number {
  try {
    return pyInt(payload[key]);
  } catch (error) {
    if (error instanceof PyValueError) {
      throw new PyJwtError(message);
    }
    throw error;
  }
}

/** `PyJWT._validate_claims` 的输入，对应 Python 版调用 `jwt.decode` 时传入的参数。 */
export interface ValidateClaimsOptions {
  require: readonly string[];
  audience: string;
  issuer: string;
  leeway: number;
  /** 当前时间（Unix 秒，浮点），默认 `Date.now() / 1000` */
  now?: number;
}

/**
 * `PyJWT._validate_claims`，选项等价于 Python 版 validator 传入的
 * `verify_signature/exp/iat/nbf/iss/aud=True` 加 PyJWT 默认的 `verify_sub/verify_jti=True`、`strict_aud=False`。
 */
export function validateJwtClaims(
  payload: Record<string, unknown>,
  options: ValidateClaimsOptions,
): void {
  const { leeway } = options;

  for (const claim of options.require) {
    const value = getOwn(payload, claim);
    if (value === null || value === undefined) {
      throw new PyJwtError(`Token is missing the "${claim}" claim`);
    }
  }

  const now = options.now ?? Date.now() / 1000;

  if (Object.hasOwn(payload, "iat")) {
    const iat = intClaim(payload, "iat", "Issued At claim (iat) must be an integer.");
    if (iat > now + leeway) {
      throw new PyJwtError("The token is not yet valid (iat)");
    }
  }

  if (Object.hasOwn(payload, "nbf")) {
    const nbf = intClaim(payload, "nbf", "Not Before claim (nbf) must be an integer.");
    if (nbf > now + leeway) {
      throw new PyJwtError("The token is not yet valid (nbf)");
    }
  }

  if (Object.hasOwn(payload, "exp")) {
    const exp = intClaim(payload, "exp", "Expiration Time claim (exp) must be an integer.");
    if (exp <= now - leeway) {
      throw new PyJwtError("Signature has expired");
    }
  }

  // _validate_iss
  if (!Object.hasOwn(payload, "iss")) {
    throw new PyJwtError('Token is missing the "iss" claim');
  }
  const iss = payload.iss;
  if (typeof iss !== "string") {
    throw new PyJwtError("Payload Issuer (iss) must be a string");
  }
  if (iss !== options.issuer) {
    throw new PyJwtError("Invalid issuer");
  }

  // _validate_aud（strict=False）
  if (!Object.hasOwn(payload, "aud") || !pyTruthy(payload.aud)) {
    throw new PyJwtError('Token is missing the "aud" claim');
  }
  const rawAudience = payload.aud;
  const audienceClaims = typeof rawAudience === "string" ? [rawAudience] : rawAudience;
  if (!Array.isArray(audienceClaims)) {
    throw new PyJwtError("Invalid claim format in token");
  }
  if (audienceClaims.some((claim) => typeof claim !== "string")) {
    throw new PyJwtError("Invalid claim format in token");
  }
  if (!audienceClaims.includes(options.audience)) {
    throw new PyJwtError("Audience doesn't match");
  }

  // _validate_sub
  if (Object.hasOwn(payload, "sub") && typeof payload.sub !== "string") {
    throw new PyJwtError("Subject must be a string");
  }

  // _validate_jti
  if (Object.hasOwn(payload, "jti") && typeof payload.jti !== "string") {
    throw new PyJwtError("JWT ID must be a string");
  }
}
