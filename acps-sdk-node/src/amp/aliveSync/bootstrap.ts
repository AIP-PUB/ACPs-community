/**
 * AMP alive-sync 自举起始 offset 时间锚点计算（§7.5 第 8 条）。
 *
 * 对应 Python `acps_sdk.amp.alive_sync.bootstrap`。SDK 只做纯时间计算；
 * `offsetsForTimes` 的实际调用由宿主侧的 Kafka 客户端完成。
 */

import { reprStr } from "../../internal/pyCompat.js";

interface ParsedIsoDatetime {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
  microsecond: number;
  /** UTC 偏移（微秒）；null 表示无时区（naive，按本地时间解释）。 */
  offsetMicros: number | null;
}

const DIGITS = /^[0-9]+$/;

class InvalidIsoFormat extends Error {}

function parseDigits(text: string): number {
  if (!DIGITS.test(text)) {
    throw new InvalidIsoFormat(text);
  }
  return Number(text);
}

/** `HH[:?MM[:?SS[{.,}f+]]]`，返回 [时, 分, 秒, 微秒]；小数超过 6 位截断（同 Python）。 */
function parseHhMmSsFf(text: string): [number, number, number, number] {
  const comps: [number, number, number, number] = [0, 0, 0, 0];
  let pos = 0;
  let hasSep = false;
  for (let comp = 0; comp < 3; comp += 1) {
    if (text.length - pos < 2) {
      throw new InvalidIsoFormat("Incomplete time component");
    }
    comps[comp] = parseDigits(text.slice(pos, pos + 2));
    pos += 2;
    const next = text.slice(pos, pos + 1);
    if (comp === 0) {
      hasSep = next === ":";
    }
    if (!next || comp >= 2) {
      break;
    }
    if (hasSep && next !== ":") {
      throw new InvalidIsoFormat("Invalid time separator");
    }
    pos += hasSep ? 1 : 0;
  }
  if (pos < text.length) {
    const sep = text.charAt(pos);
    if (sep !== "." && sep !== ",") {
      throw new InvalidIsoFormat("Invalid microsecond component");
    }
    const fraction = text.slice(pos + 1);
    if (!DIGITS.test(fraction)) {
      throw new InvalidIsoFormat("Non-digit values in fraction");
    }
    comps[3] = Number(fraction.slice(0, 6).padEnd(6, "0"));
  }
  return comps;
}

function isLeap(year: number): boolean {
  return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
}

function daysInMonth(year: number, month: number): number {
  return [31, isLeap(year) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1] ?? 0;
}

/**
 * Python 3.11+ `datetime.fromisoformat()` 常用子集：
 * `YYYY-MM-DD` / `YYYYMMDD`，可选「任意单字符分隔 + 时间」，时间为 `HH[:MM[:SS[.ffffff]]]`
 * （或无冒号的基本格式），时区为 `Z` / `±HH[:MM[:SS[.ffffff]]]`。不支持 ISO 周日期（`YYYY-Www-D`）。
 */
function parseIsoDatetime(text: string): ParsedIsoDatetime {
  if (text.length < 8) {
    throw new InvalidIsoFormat(text);
  }
  const dateLen = text.charAt(4) === "-" ? 10 : 8;
  const dateText = text.slice(0, dateLen);
  const timeText = text.slice(dateLen + 1);

  const hasDash = dateText.charAt(4) === "-";
  if (dateText.length !== dateLen || (hasDash && dateText.charAt(7) !== "-")) {
    throw new InvalidIsoFormat(text);
  }
  const year = parseDigits(dateText.slice(0, 4));
  const month = parseDigits(hasDash ? dateText.slice(5, 7) : dateText.slice(4, 6));
  const day = parseDigits(hasDash ? dateText.slice(8, 10) : dateText.slice(6, 8));

  let hour = 0;
  let minute = 0;
  let second = 0;
  let microsecond = 0;
  let offsetMicros: number | null = null;

  // 日期之后只要还有字符（哪怕只有分隔符），就必须跟一个合法的时间部分（同 CPython：'2026-06-13T' 非法）
  if (text.length > dateLen) {
    const tzMatch = /[-+Z]/.exec(timeText);
    const tzPos = tzMatch ? tzMatch.index : -1;
    [hour, minute, second, microsecond] = parseHhMmSsFf(
      tzPos >= 0 ? timeText.slice(0, tzPos) : timeText,
    );
    if (tzPos >= 0) {
      const tzText = timeText.slice(tzPos + 1);
      if (timeText.charAt(tzPos) === "Z") {
        if (tzText !== "") {
          throw new InvalidIsoFormat(text);
        }
        offsetMicros = 0;
      } else {
        if (tzText.length === 0 || tzText.length === 1 || tzText.length === 3) {
          throw new InvalidIsoFormat("Malformed time zone string");
        }
        const [tzHour, tzMinute, tzSecond, tzMicro] = parseHhMmSsFf(tzText);
        const sign = timeText.charAt(tzPos) === "-" ? -1 : 1;
        offsetMicros = sign * (((tzHour * 60 + tzMinute) * 60 + tzSecond) * 1_000_000 + tzMicro);
        if (Math.abs(offsetMicros) >= 86_400_000_000) {
          throw new InvalidIsoFormat("offset must be strictly between -24h and 24h");
        }
      }
    }
  }

  if (year < 1 || month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) {
    throw new InvalidIsoFormat("date out of range");
  }
  if (hour > 23 || minute > 59 || second > 59) {
    throw new InvalidIsoFormat("time out of range");
  }
  return { year, month, day, hour, minute, second, microsecond, offsetMicros };
}

/** 公历日期 → 自 1970-01-01 起的天数（proleptic Gregorian，任意年份）。 */
function daysFromCivil(year: number, month: number, day: number): number {
  const y = month <= 2 ? year - 1 : year;
  const era = Math.floor(y / 400);
  const yearOfEra = y - era * 400;
  const dayOfYear = Math.floor((153 * ((month + 9) % 12) + 2) / 5) + day - 1;
  const dayOfEra =
    yearOfEra * 365 + Math.floor(yearOfEra / 4) - Math.floor(yearOfEra / 100) + dayOfYear;
  return era * 146097 + dayOfEra - 719468;
}

/** naive 值按本地时区解释后的 epoch 毫秒（Python 的 astimezone() 对 naive 值同样使用本地时区）。 */
function localEpochMs(dt: ParsedIsoDatetime): number {
  const local = new Date(2000, 0, 1, dt.hour, dt.minute, dt.second, 0);
  local.setFullYear(dt.year, dt.month - 1, dt.day);
  return local.getTime();
}

/** 与 Python `dt.astimezone(timezone.utc).timestamp()` 相同的浮点 POSIX 时间戳（秒）。 */
function pythonTimestamp(dt: ParsedIsoDatetime): number {
  let totalMicros: bigint;
  if (dt.offsetMicros === null) {
    totalMicros = BigInt(localEpochMs(dt)) * 1000n + BigInt(dt.microsecond);
  } else {
    const seconds =
      daysFromCivil(dt.year, dt.month, dt.day) * 86_400 +
      (dt.hour * 60 + dt.minute) * 60 +
      dt.second;
    totalMicros = BigInt(seconds) * 1_000_000n + BigInt(dt.microsecond) - BigInt(dt.offsetMicros);
  }
  // Python: timedelta.total_seconds() = 总微秒 / 10**6（整数真除法，正确舍入）
  return Number(totalMicros) / 1e6;
}

/**
 * 计算 Kafka `offsetsForTimes` 的入参时间戳（epoch ms）。
 *
 * 将 snapshot generatedAt 解析为 epoch ms，减去 lookbackSeconds * 1000，
 * 得到「snapshot 生成时刻前若干秒」的毫秒时间戳。结果不低于 0（避免传负值）。
 *
 * @param generatedAt ISO 8601 UTC 时间戳字符串（如 "2026-06-13T01:20:00Z"）
 * @param lookbackSeconds 回看裕量，单位秒（§7.5 第 8 条，典型值 300）
 * @throws Error generatedAt 格式无法解析为 ISO 8601 时间戳
 */
export function seekTimestampMs(generatedAt: string, lookbackSeconds: number): number {
  let text = generatedAt;
  if (text.endsWith("Z")) {
    text = `${text.slice(0, -1)}+00:00`;
  }
  let dt: ParsedIsoDatetime;
  try {
    dt = parseIsoDatetime(text);
  } catch (error) {
    throw new Error(`generated_at 格式无效: ${reprStr(text)}`, { cause: error });
  }
  const epochMs = Math.trunc(pythonTimestamp(dt) * 1000);
  const result = epochMs - lookbackSeconds * 1000;
  return Math.max(result, 0);
}

export interface NextLookbackOptions {
  /** 倍增系数（默认 2）。 */
  factor?: number;
  /** 上限（单位秒，§7.5 第 8 条）。 */
  maxSeconds: number;
}

/**
 * 自举免误报：缺口疑似 offset 过晚时倍增回看窗口。
 *
 * 每次调用将 current 乘以 factor，直至达到或超过 maxSeconds 则钳制到 maxSeconds。
 * 当返回值已等于 maxSeconds 时，调用方应退而使用 Kafka earliest offset。
 */
export function nextLookbackSeconds(
  current: number,
  { factor = 2, maxSeconds }: NextLookbackOptions,
): number {
  return Math.min(current * factor, maxSeconds);
}
