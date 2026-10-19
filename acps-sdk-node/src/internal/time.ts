/**
 * 时间格式工具。
 *
 * Python 版 SDK 统一使用 `datetime.now(timezone.utc).isoformat()` 生成时间戳，
 * 形如 `2026-10-19T08:01:12.160665+00:00`；微秒为 0 时省略小数部分。
 * AIP 的增量 get、AMP 的 Fluent Bit 解析器都依赖这个格式，Node 版保持一致。
 */

function pad(value: number, width: number): string {
  return String(value).padStart(width, "0");
}

/** 把 Date 格式化为与 Python `datetime.isoformat()`（UTC）一致的字符串。 */
export function toPythonIsoformat(date: Date): string {
  const base =
    `${pad(date.getUTCFullYear(), 4)}-${pad(date.getUTCMonth() + 1, 2)}-${pad(date.getUTCDate(), 2)}` +
    `T${pad(date.getUTCHours(), 2)}:${pad(date.getUTCMinutes(), 2)}:${pad(date.getUTCSeconds(), 2)}`;
  const millis = date.getUTCMilliseconds();
  const fraction = millis === 0 ? "" : `.${pad(millis * 1000, 6)}`;
  return `${base}${fraction}+00:00`;
}

/** 当前 UTC 时间，Python isoformat 格式。 */
export function utcNowIso(): string {
  return toPythonIsoformat(new Date());
}
