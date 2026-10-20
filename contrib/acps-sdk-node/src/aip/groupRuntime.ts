/**
 * AIP 群组模式运行时工具（对应 Python `acps_sdk.aip.aip_group_runtime`）
 *
 * - 群组 Exchange / 队列 / inbox 命名规则
 * - AMQP 连接 URL 的构造与解析（字符串与 Python 版逐字节一致）
 * - 邀请令牌与过期时间
 *
 * 文件后半部分是 asyncio 原语在 Node 下的最小替代（`sleep` / `Deferred` / `AsyncEvent` /
 * `AsyncLock` / `CancellableTask`），供 groupLeader / groupPartner 使用。
 */

import { createHash, randomBytes } from "node:crypto";

import { validateAicFormat } from "../aic/index.js";
import { pyStr, pyStrip, pyStripChars, pyTypeName } from "../internal/pyCompat.js";
import { AsyncLock } from "../internal/asyncLock.js";
import { toPythonIsoformat, utcNowIso } from "../internal/time.js";

export const INBOX_EXCHANGE_NAME = "inbox.topic";
export const INBOX_QUEUE_EXPIRES_MS = 60 * 24 * 60 * 60 * 1000;
export const INBOX_MESSAGE_TTL_MS = 7 * 24 * 60 * 60 * 1000;
export const DEFAULT_INVITATION_TIMEOUT_SECONDS = 300;
export const INVITATION_CLOCK_SKEW_SECONDS = 30;

const GROUP_ID_PATTERN = /^[A-Za-z0-9-]{1,128}$/;
const GROUP_ID_INVALID_CHAR_PATTERN = /[^A-Za-z0-9-]+/g;

/** 解析后的 AMQP 端点（对应 Python `AMQPEndpoint` dataclass）。 */
export interface AMQPEndpoint {
  readonly url: string;
  readonly host: string;
  readonly port: number;
  readonly vhost: string;
  readonly inbox: string;
}

// =============================================================================
// Python 字符串语义辅助（内部使用）
// =============================================================================

function pyRstrip(value: string, chars: string): string {
  let end = value.length;
  while (end > 0 && chars.includes(value.charAt(end - 1))) end -= 1;
  return value.slice(0, end);
}

const QUOTE_ALWAYS_SAFE = /[A-Za-z0-9_.\-~]/;

/**
 * Python `urllib.parse.quote(value, safe="")`：除 `A-Za-z0-9_.-~` 外全部按 UTF-8 百分号编码。
 * 与 `encodeURIComponent` 的差异：后者不编码 `!'()*`。 @internal
 */
export function pyQuote(value: string, safe = ""): string {
  let out = "";
  for (const byte of Buffer.from(value, "utf-8")) {
    const ch = String.fromCharCode(byte);
    if (byte < 0x80 && (QUOTE_ALWAYS_SAFE.test(ch) || safe.includes(ch))) {
      out += ch;
    } else {
      out += `%${byte.toString(16).toUpperCase().padStart(2, "0")}`;
    }
  }
  return out;
}

/** Python `urllib.parse.unquote`：非法序列原样保留，UTF-8 解码失败时用 U+FFFD 替换。 @internal */
export function pyUnquote(value: string): string {
  if (!value.includes("%")) return value;
  return value.replace(/(?:%[0-9A-Fa-f]{2})+/g, (run) => {
    const bytes = Buffer.from(run.replace(/%/g, ""), "hex");
    return new TextDecoder("utf-8").decode(bytes);
  });
}

/** Python `urllib.parse.parse_qs(query)`（keep_blank_values=False）。 @internal */
export function pyParseQs(query: string): Map<string, string[]> {
  const result = new Map<string, string[]>();
  if (!query) return result;
  for (const pair of query.split("&")) {
    if (!pair) continue;
    const eq = pair.indexOf("=");
    if (eq < 0) continue;
    const rawName = pair.slice(0, eq);
    const rawValue = pair.slice(eq + 1);
    if (!rawValue) continue;
    const name = pyUnquote(rawName.replace(/\+/g, " "));
    const decoded = pyUnquote(rawValue.replace(/\+/g, " "));
    const list = result.get(name);
    if (list) list.push(decoded);
    else result.set(name, [decoded]);
  }
  return result;
}

/** Python `urllib.parse.urlparse` 的结果子集。 @internal */
export interface PyUrlParts {
  scheme: string;
  netloc: string;
  path: string;
  query: string;
  fragment: string;
  username: string | null;
  password: string | null;
  hostname: string | null;
  port: number | null;
}

const WHATWG_C0_CONTROL_OR_SPACE = Array.from({ length: 33 }, (_, i) =>
  String.fromCharCode(i),
).join("");
const SCHEME_CHARS = /^[A-Za-z][A-Za-z0-9+\-.]*$/;

/**
 * Python 3.12 `urllib.parse.urlparse` 的移植（只覆盖本模块用到的部分：
 * scheme / netloc / path / query / fragment / username / password / hostname / port）。
 * 与 WHATWG URL 的关键差异：`amqp(s)` 在 WHATWG 中是非特殊 scheme，hostname 不会转小写、
 * IPv6 方括号不会去掉，因此不能直接用 `new URL()`。 @internal
 */
export function pyUrlparse(input: string): PyUrlParts {
  // 同 Python 3.12：只去掉开头的 C0 控制字符与空格，并删除所有 \t \r \n
  let start = 0;
  while (start < input.length && WHATWG_C0_CONTROL_OR_SPACE.includes(input.charAt(start)))
    start += 1;
  let url = input.slice(start).replace(/[\t\r\n]/g, "");
  let scheme = "";
  const colon = url.indexOf(":");
  if (colon > 0 && SCHEME_CHARS.test(url.slice(0, colon))) {
    scheme = url.slice(0, colon).toLowerCase();
    url = url.slice(colon + 1);
  }
  let netloc = "";
  if (url.startsWith("//")) {
    let end = url.length;
    for (const delim of "/?#") {
      const index = url.indexOf(delim, 2);
      if (index >= 0) end = Math.min(end, index);
    }
    netloc = url.slice(2, end);
    url = url.slice(end);
    if (
      (netloc.includes("[") && !netloc.includes("]")) ||
      (netloc.includes("]") && !netloc.includes("["))
    ) {
      throw new Error("Invalid IPv6 URL");
    }
  }
  let fragment = "";
  const hash = url.indexOf("#");
  if (hash >= 0) {
    fragment = url.slice(hash + 1);
    url = url.slice(0, hash);
  }
  let query = "";
  const question = url.indexOf("?");
  if (question >= 0) {
    query = url.slice(question + 1);
    url = url.slice(0, question);
  }
  const path = url;

  // userinfo / hostinfo
  const at = netloc.lastIndexOf("@");
  let username: string | null = null;
  let password: string | null = null;
  if (at >= 0) {
    const userinfo = netloc.slice(0, at);
    const sep = userinfo.indexOf(":");
    if (sep >= 0) {
      username = userinfo.slice(0, sep);
      password = userinfo.slice(sep + 1);
    } else {
      username = userinfo;
    }
  }
  const hostinfo = at >= 0 ? netloc.slice(at + 1) : netloc;
  let rawHost: string;
  let rawPort: string;
  const openBracket = hostinfo.indexOf("[");
  if (openBracket >= 0) {
    const bracketed = hostinfo.slice(openBracket + 1);
    const close = bracketed.indexOf("]");
    rawHost = close >= 0 ? bracketed.slice(0, close) : bracketed;
    const rest = close >= 0 ? bracketed.slice(close + 1) : "";
    const portSep = rest.indexOf(":");
    rawPort = portSep >= 0 ? rest.slice(portSep + 1) : "";
  } else {
    const portSep = hostinfo.indexOf(":");
    rawHost = portSep >= 0 ? hostinfo.slice(0, portSep) : hostinfo;
    rawPort = portSep >= 0 ? hostinfo.slice(portSep + 1) : "";
  }
  let hostname: string | null = null;
  if (rawHost) {
    const percent = rawHost.indexOf("%");
    hostname =
      percent >= 0
        ? rawHost.slice(0, percent).toLowerCase() + rawHost.slice(percent)
        : rawHost.toLowerCase();
  }
  let port: number | null = null;
  if (rawPort) {
    if (!/^[0-9]+$/.test(rawPort)) {
      throw new Error(`Port could not be cast to integer value as '${rawPort}'`);
    }
    port = Number.parseInt(rawPort, 10);
    if (!(port >= 0 && port <= 65535)) {
      throw new Error("Port out of range 0-65535");
    }
  }
  return { scheme, netloc, path, query, fragment, username, password, hostname, port };
}

// =============================================================================
// 命名规则
// =============================================================================

export function ensureValidAic(aic: string): string {
  const [valid, error] = validateAicFormat(aic);
  if (!valid) {
    throw new Error(error || `Invalid AIC: ${aic}`);
  }
  return aic;
}

export function ensureValidGroupId(groupId: string): string {
  if (!GROUP_ID_PATTERN.test(groupId)) {
    throw new Error("group_id must match [a-zA-Z0-9-] and be at most 128 characters");
  }
  return groupId;
}

export function normalizeGroupId(groupId: string): string {
  let normalized = pyStripChars(pyStrip(groupId).replace(GROUP_ID_INVALID_CHAR_PATTERN, "-"), "-");
  if (!normalized) {
    throw new Error("group_id cannot be empty after normalization");
  }
  if (normalized.length > 128) {
    normalized = pyRstrip(normalized.slice(0, 128), "-");
  }
  return ensureValidGroupId(normalized);
}

export function buildInboxQueueName(aic: string): string {
  return `inbox_${ensureValidAic(aic)}`;
}

export function buildGroupExchangeName(leaderAic: string, groupId: string): string {
  return `group_${ensureValidAic(leaderAic)}_${ensureValidGroupId(groupId)}`;
}

export function buildGroupQueueName(leaderAic: string, groupId: string, memberAic: string): string {
  return `${buildGroupExchangeName(leaderAic, groupId)}_${ensureValidAic(memberAic)}`;
}

// =============================================================================
// 连接 URL
// =============================================================================

function encodeVhost(vhost: string): string {
  let normalized = pyStrip(vhost) || "/";
  if (normalized === "/") {
    return "%2F";
  }
  if (normalized.startsWith("/")) {
    normalized = normalized.slice(1);
  }
  return pyQuote(normalized);
}

export function buildExternalConnectionUrl({
  host,
  port,
  vhost,
  connectionName,
}: {
  host: string;
  port: number;
  vhost: string;
  connectionName: string;
}): string {
  const encodedVhost = encodeVhost(vhost);
  const encodedName = pyQuote(connectionName);
  return `amqps://${host}:${port}/${encodedVhost}?auth=external&name=${encodedName}`;
}

export function buildPlainConnectionUrl({
  host,
  port,
  vhost,
  username,
  password,
  connectionName,
}: {
  host: string;
  port: number;
  vhost: string;
  username: string;
  password: string;
  connectionName?: string | null;
}): string {
  const encodedUser = pyQuote(username);
  const encodedPassword = pyQuote(password);
  const encodedVhost = encodeVhost(vhost);
  let url = `amqp://${encodedUser}:${encodedPassword}@${host}:${port}/${encodedVhost}`;
  if (connectionName) {
    url = `${url}?name=${pyQuote(connectionName)}`;
  }
  return url;
}

export function replaceAicPlaceholder(url: string, aic: string | null | undefined): string {
  if (!url.includes("{AIC}")) {
    return url;
  }
  if (!aic) {
    throw new Error("AMQP endpoint contains {AIC} placeholder but AIC is missing");
  }
  return url.replaceAll("{AIC}", ensureValidAic(aic));
}

export function parseAmqpEndpointUrl(
  url: string,
  { aic }: { aic?: string | null } = {},
): AMQPEndpoint {
  const resolvedUrl = replaceAicPlaceholder(url, aic);
  const parsed = pyUrlparse(resolvedUrl);
  if (parsed.scheme !== "amqp" && parsed.scheme !== "amqps") {
    throw new Error(`Unsupported AMQP endpoint scheme: ${parsed.scheme}`);
  }
  if (!parsed.hostname) {
    throw new Error(`AMQP endpoint missing hostname: ${resolvedUrl}`);
  }

  const inbox = pyParseQs(parsed.query).get("inbox")?.[0];
  if (!inbox) {
    throw new Error(`AMQP endpoint missing inbox query parameter: ${resolvedUrl}`);
  }

  const host = parsed.hostname;
  const port = parsed.port || (parsed.scheme === "amqps" ? 5671 : 5672);
  const path = parsed.path || "/";
  let vhost = path.startsWith("/") ? pyUnquote(path.slice(1)) : pyUnquote(path);
  if (!vhost) {
    vhost = "/";
  }

  return Object.freeze({ url: resolvedUrl, host, port, vhost, inbox });
}

/**
 * 非数组、非 null 的对象（对应 Python 对 dict / 任意对象按键取值的场景；不要求是纯对象原型，
 * 因此与 `isPlainObject` 不同，类实例也算）。 @internal
 */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function getEndpointUrl(
  acsData: Record<string, unknown> | null | undefined,
  transport: string,
  { aic }: { aic?: string | null } = {},
): string | null {
  if (!isRecord(acsData)) {
    return null;
  }
  const endpoints = acsData.endPoints;
  if (!Array.isArray(endpoints)) {
    return null;
  }
  const targetTransport = transport.toUpperCase();
  for (const endpoint of endpoints) {
    if (!isRecord(endpoint)) continue;
    const endpointTransport = Object.hasOwn(endpoint, "transport") ? endpoint.transport : "";
    if (pyStr(endpointTransport).toUpperCase() !== targetTransport) continue;
    const rawUrl = endpoint.url;
    if (typeof rawUrl === "string" && pyStrip(rawUrl)) {
      return replaceAicPlaceholder(pyStrip(rawUrl), aic);
    }
  }
  return null;
}

// =============================================================================
// 邀请令牌与时间
// =============================================================================

/** 对应 Python `secrets.token_urlsafe(32)`。 */
export function createInvitationToken(): string {
  return randomBytes(32).toString("base64url");
}

export function utcNow(): Date {
  return new Date();
}

/** 对应 Python `utc_now_iso()`；与 `src/internal/time.ts` 的实现相同，直接复用。 */
export { utcNowIso };

export function calculateInvitationExpiry(timeoutSeconds: number): string {
  return toPythonIsoformat(new Date(utcNow().getTime() + timeoutSeconds * 1000));
}

const DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

function isLeapYear(year: number): boolean {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
}

function invalidIsoformat(value: string): Error {
  return new Error(`Invalid isoformat string: '${value}'`);
}

/** ISO 周日期转公历日期（UTC 毫秒）。 */
function isoWeekToEpochDay(year: number, week: number, weekday: number): number {
  const jan4 = Date.UTC(year, 0, 4);
  const jan4Weekday = (new Date(jan4).getUTCDay() + 6) % 7; // 周一 = 0
  const week1Monday = jan4 - jan4Weekday * 86_400_000;
  return week1Monday + ((week - 1) * 7 + (weekday - 1)) * 86_400_000;
}

function parseIsoDate(text: string, original: string): { epoch: number; length: number } {
  let match = /^(\d{4})-W(\d{2})(?:-(\d))?/.exec(text) ?? /^(\d{4})W(\d{2})(\d)?/.exec(text);
  if (match) {
    const year = Number(match[1]);
    const week = Number(match[2]);
    const weekday = match[3] === undefined ? 1 : Number(match[3]);
    if (!(week > 0 && week < 53)) {
      // 第 53 周只在“1 月 1 日是周四，或闰年且 1 月 1 日是周三”的年份存在
      const firstWeekday = (new Date(Date.UTC(year, 0, 1)).getUTCDay() + 6) % 7; // 周一 = 0
      const has53 = firstWeekday === 3 || (firstWeekday === 2 && isLeapYear(year));
      if (!(week === 53 && has53)) throw new Error(`Invalid week: ${week}`);
    }
    if (weekday < 1 || weekday > 7)
      throw new Error(`Invalid weekday: ${weekday} (range is [1, 7])`);
    return { epoch: isoWeekToEpochDay(year, week, weekday), length: match[0].length };
  }
  match = /^(\d{4})-(\d{2})-(\d{2})/.exec(text) ?? /^(\d{4})(\d{2})(\d{2})/.exec(text);
  if (!match) throw invalidIsoformat(original);
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (year < 1) throw new Error(`year ${year} is out of range`);
  if (month < 1 || month > 12) throw new Error("month must be in 1..12");
  const maxDay = month === 2 && isLeapYear(year) ? 29 : (DAYS_IN_MONTH[month - 1] ?? 31);
  if (day < 1 || day > maxDay) throw new Error("day is out of range for month");
  const epoch = new Date(0);
  epoch.setUTCFullYear(year, month - 1, day);
  epoch.setUTCHours(0, 0, 0, 0);
  return { epoch: epoch.getTime(), length: match[0].length };
}

/** 解析 `HH[:MM[:SS[.f]]]` 或 `HH[MM[SS[.f]]]`，返回毫秒数（小数秒截断到微秒再取毫秒）。 */
function parseIsoTimeOfDay(text: string, original: string, isOffset: boolean): number {
  const match =
    /^(\d{2})(?::(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?$/.exec(text) ??
    /^(\d{2})(?:(\d{2})(?:(\d{2})(?:[.,](\d+))?)?)?$/.exec(text);
  if (!match) throw invalidIsoformat(original);
  const hour = Number(match[1]);
  const minute = match[2] === undefined ? 0 : Number(match[2]);
  const second = match[3] === undefined ? 0 : Number(match[3]);
  const micros = match[4] === undefined ? 0 : Number(match[4].slice(0, 6).padEnd(6, "0"));
  if (hour > 23)
    throw new Error(isOffset ? invalidIsoformat(original).message : "hour must be in 0..23");
  if (minute > 59)
    throw new Error(isOffset ? invalidIsoformat(original).message : "minute must be in 0..59");
  if (second > 59)
    throw new Error(isOffset ? invalidIsoformat(original).message : "second must be in 0..59");
  return ((hour * 60 + minute) * 60 + second) * 1000 + Math.floor(micros / 1000);
}

/**
 * 对应 Python `parse_utc_datetime`：`Z` 后缀视为 `+00:00`，无时区按 UTC，最后统一换算到 UTC。
 *
 * 解析规则与 Python 3.12 `datetime.fromisoformat` 对齐（扩展/基本格式日期、ISO 周日期、
 * 任意单字符日期时间分隔符、小数秒、`±HH[:MM[:SS[.f]]]` 偏移）；精度为毫秒。
 */
export function parseUtcDatetime(value: string): Date {
  let normalized = pyStrip(value);
  if (normalized.endsWith("Z")) {
    normalized = `${normalized.slice(0, -1)}+00:00`;
  }
  const date = parseIsoDate(normalized, normalized);
  let rest = normalized.slice(date.length);
  if (rest === "") {
    return new Date(date.epoch);
  }
  // 日期与时间之间允许任意单个字符
  rest = rest.slice(1);
  let offsetMs = 0;
  const tzIndex = rest.search(/[+-]/);
  let timePart = rest;
  if (tzIndex >= 0) {
    timePart = rest.slice(0, tzIndex);
    const sign = rest.charAt(tzIndex) === "-" ? -1 : 1;
    const offsetText = rest.slice(tzIndex + 1);
    if (!offsetText) throw invalidIsoformat(normalized);
    offsetMs = sign * parseIsoTimeOfDay(offsetText, normalized, true);
  }
  if (!timePart) throw invalidIsoformat(normalized);
  const timeMs = parseIsoTimeOfDay(timePart, normalized, false);
  return new Date(date.epoch + timeMs - offsetMs);
}

export function invitationIsExpired(
  expiresAt: string,
  { skewSeconds = INVITATION_CLOCK_SKEW_SECONDS }: { skewSeconds?: number } = {},
): boolean {
  const expiry = parseUtcDatetime(expiresAt);
  return utcNow().getTime() >= expiry.getTime() + skewSeconds * 1000;
}

// =============================================================================
// asyncio 原语的最小替代（内部使用）
// =============================================================================

/** 对应 `asyncio.CancelledError`：任务被取消时抛出。 */
export class CancelledError extends Error {
  override name = "CancelledError";
  constructor(message = "cancelled") {
    super(message);
  }
}

/** 对应 `asyncio.sleep`；`signal` 中止时以 `CancelledError` 拒绝。定时器 unref，不阻止进程退出。 */
export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new CancelledError());
      return;
    }
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(new CancelledError());
    };
    const timer = setTimeout(
      () => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      },
      Math.max(0, ms),
    );
    timer.unref?.();
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/** 对应 `asyncio.Future`：可在外部设置结果或异常。未被等待的拒绝不会触发 unhandledRejection。 */
export class Deferred<T> {
  readonly promise: Promise<T>;
  private resolveFn!: (value: T) => void;
  private rejectFn!: (reason: unknown) => void;
  private state: "pending" | "fulfilled" | "rejected" = "pending";
  private error: unknown = undefined;

  constructor() {
    this.promise = new Promise<T>((resolve, reject) => {
      this.resolveFn = resolve;
      this.rejectFn = reject;
    });
    this.promise.catch(() => undefined);
  }

  done(): boolean {
    return this.state !== "pending";
  }

  setResult(value: T): void {
    if (this.state !== "pending") throw new Error("invalid state: future already done");
    this.state = "fulfilled";
    this.resolveFn(value);
  }

  setException(error: unknown): void {
    if (this.state !== "pending") throw new Error("invalid state: future already done");
    this.state = "rejected";
    this.error = error;
    this.rejectFn(error);
  }

  /** 已完成且以异常结束时返回该异常；成功返回 null；未完成时抛错（同 Python）。 */
  exception(): unknown {
    if (this.state === "pending") throw new Error("invalid state: exception is not set");
    return this.state === "rejected" ? this.error : null;
  }
}

/** 对应 `asyncio.Event`。 */
export class AsyncEvent {
  private flag = false;
  private waiters: Array<() => void> = [];

  isSet(): boolean {
    return this.flag;
  }

  set(): void {
    if (this.flag) return;
    this.flag = true;
    const waiters = this.waiters.splice(0);
    for (const wake of waiters) wake();
  }

  clear(): void {
    this.flag = false;
  }

  wait(): Promise<true> {
    if (this.flag) return Promise.resolve(true);
    return new Promise<true>((resolve) => this.waiters.push(() => resolve(true)));
  }
}

/** 对应 `asyncio.Lock`（FIFO）：复用 `src/internal/asyncLock.ts`，保留本模块的导出名。 */
export { AsyncLock };

/** 对应 `asyncio.Task`：可取消的后台任务，取消时向任务函数传入的 signal 发出中止。 */
export interface CancellableTask {
  /** 任务结束的 Promise；被取消时以 `CancelledError` 拒绝（未被等待也不会触发 unhandledRejection） */
  readonly promise: Promise<void>;
  cancel(): boolean;
  done(): boolean;
  cancelled(): boolean;
}

export function createTask(fn: (signal: AbortSignal) => Promise<void>): CancellableTask {
  const controller = new AbortController();
  let finished = false;
  let wasCancelled = false;
  const promise = (async () => {
    // 与 asyncio.create_task 一样，任务体在下一轮事件循环才开始执行
    await Promise.resolve();
    if (controller.signal.aborted) {
      throw new CancelledError();
    }
    await fn(controller.signal);
  })().then(
    () => {
      finished = true;
    },
    (error: unknown) => {
      finished = true;
      if (error instanceof CancelledError) wasCancelled = true;
      throw error;
    },
  );
  promise.catch(() => undefined);
  return {
    promise,
    cancel(): boolean {
      if (finished) return false;
      controller.abort();
      return true;
    },
    done: () => finished,
    cancelled: () => wasCancelled,
  };
}

// =============================================================================
// Python 语义辅助：日志 / 异常文本、JSON 消息体（内部使用）
// =============================================================================

/** 对应 `json.JSONDecodeError`。 @internal */
export class JSONDecodeError extends SyntaxError {
  override name = "JSONDecodeError";
}

const STRICT_UTF8 = new TextDecoder("utf-8", { fatal: true });

/**
 * 对应 `json.loads(message.body.decode())`：UTF-8 严格解码（失败抛 TypeError，
 * 对应 UnicodeDecodeError），JSON 语法错误抛 `JSONDecodeError`。 @internal
 */
export function decodeJsonBody(body: Uint8Array): unknown {
  const text = STRICT_UTF8.decode(body);
  try {
    return JSON.parse(text) as unknown;
  } catch (error) {
    throw new JSONDecodeError(error instanceof Error ? error.message : String(error));
  }
}

/** 对应 Python 对 JSON 消息体调用 `.get()`：不是 dict 时抛错（Python 为 AttributeError）。 @internal */
export function requireJsonDict(value: unknown): Record<string, unknown> {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  throw new TypeError(`'${pyTypeName(value)}' object has no attribute 'get'`);
}

/** 对应 Python `str(exc)`。 @internal */
export function pyErrorStr(error: unknown): string {
  return error instanceof Error ? error.message : pyStr(error);
}

/** 对应 Python 字符串切片 `s[:n]`（按码点）。 @internal */
export function pySlice(value: string, end: number): string {
  const chars = Array.from(value);
  return chars.length <= end ? value : chars.slice(0, end).join("");
}

/** 对应 Python 对消息体 dict 的 `body.get(key, default)`：只看自有属性。 @internal */
export function dictGet(
  body: Record<string, unknown>,
  key: string,
  fallback: unknown = undefined,
): unknown {
  return Object.hasOwn(body, key) ? body[key] : fallback;
}

/** `null` / `undefined` → `undefined`，其余按字符串透传（用于可选的日志 / 埋点字段）。 @internal */
export function asOptionalString(value: unknown): string | undefined {
  return value === null || value === undefined ? undefined : (value as string);
}

/** 读取 AMQP 头（amqplib 把 longstr 解码为字符串，字节串时按 UTF-8 解码）。 @internal */
export function headerValue(
  headers: Record<string, unknown> | null | undefined,
  key: string,
): string | null {
  if (!headers) return null;
  const value = headers[key];
  if (value === null || value === undefined) return null;
  if (value instanceof Uint8Array) return Buffer.from(value).toString("utf-8");
  return String(value);
}

/**
 * 日志中代替邀请令牌等凭据：只输出长度与 SHA-256 前 8 位，便于比对而不泄露原文。 @internal
 */
export function redactToken(token: string | null | undefined): string {
  if (token === null || token === undefined) return "None";
  const digest = createHash("sha256").update(token, "utf-8").digest("hex").slice(0, 8);
  return `<redacted len=${token.length} sha256=${digest}>`;
}

/**
 * 日志中隐藏 URL 里的 userinfo（`scheme://user:pass@host` → `scheme://***@host`），
 * 避免把 RPC / ACL / AMQP 地址中的凭据写进日志。也可用于包含 URL 的整段文本（如 httpx 风格的错误信息）。 @internal
 */
export function redactUrl(text: string | null | undefined): string {
  if (text === null || text === undefined) return "None";
  return text.replace(/([A-Za-z][A-Za-z0-9+.-]*:\/\/)[^/?#@\s'"]*@/g, "$1***@");
}
