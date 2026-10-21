/** AMP 测试共用工具：临时目录（对应 pytest tmp_path）与 WARNING 捕获（对应 caplog）。 */

import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach } from "vitest";

import { type Logger, setLoggerFactory } from "../../src/internal/logger.js";

/** 每个用例一个新的临时目录，用例结束后删除。 */
export function useTmpPath(): () => string {
  let current = "";
  beforeEach(() => {
    current = mkdtempSync(join(tmpdir(), "acps-amp-"));
  });
  afterEach(() => {
    rmSync(current, { recursive: true, force: true });
  });
  return () => current;
}

export interface CapturedLog {
  level: keyof Logger;
  name: string;
  message: string;
  args: unknown[];
}

/** 捕获 getLogger() 输出（默认静默），用例结束后恢复。 */
export function useLogCapture(): CapturedLog[] {
  const records: CapturedLog[] = [];
  beforeEach(() => {
    records.length = 0;
    setLoggerFactory((name) => {
      const log =
        (level: keyof Logger) =>
        (message: string, ...args: unknown[]): void => {
          records.push({ level, name, message, args });
        };
      return { debug: log("debug"), info: log("info"), warn: log("warn"), error: log("error") };
    });
  });
  afterEach(() => {
    setLoggerFactory(undefined);
  });
  return records;
}

/** 读取 NDJSON 文件的全部行（去掉末尾换行）。 */
export function readLines(file: string): string[] {
  return readFileSync(file, "utf-8").trim().split("\n");
}

/** 读取单行 NDJSON 文件并解析。 */
export function readSingleRecord(file: string): Record<string, unknown> {
  return JSON.parse(readFileSync(file, "utf-8").trim()) as Record<string, unknown>;
}

/** 返回一个父路径是普通文件的日志路径：mkdir 必然失败，用于模拟写入失败（对应 patch Path.open）。 */
export function unwritablePath(dir: string, name: string): string {
  const blocker = join(dir, "not-a-directory");
  writeFileSync(blocker, "");
  return join(blocker, name);
}

export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** 等待指定毫秒（测试用）。 */
export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
