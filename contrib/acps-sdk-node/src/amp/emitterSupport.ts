/**
 * AMP 各发射器共用的内部工具（不从包入口导出类型以外的内容）。
 *
 * - NDJSON 追加写：每一行（含换行符）都用一次 `fs.appendFileSync` 以追加模式（O_APPEND）整块写入，
 *   与 Python 版单次 `f.write(line + "\n")` 相同，保证 Fluent Bit 尾追的文件里不会出现被其它写入者
 *   （同进程的 `emitSync()`、其它进程）插入而撕裂的行。不用 `fs.promises.appendFile`：它会把大缓冲区
 *   拆成多次 write()，其它写入可能夹在分块之间。
 *   异步版另外对同一文件路径串行排队，保证同一进程内多次 `emit()` 按调用顺序落盘。
 * - 追踪选项（camelCase）→ LogRecord 顶层字段（snake_case）的映射。
 * - 周期发射循环：与 Python 的 `run_periodic` 一样一直运行（定时器保持进程存活），
 *   直到 `stop()` 或 AbortSignal 结束循环。
 */

import { appendFileSync, mkdirSync } from "node:fs";
import { mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";

import { utcNowIso } from "../internal/time.js";

// ─── 选项 ────────────────────────────────────────────────────────────────────

/** 发射时可附带的追踪 / 级别字段（对应 Python emit_sync 的同名关键字参数）。 */
export interface RecordEmitOptions {
  traceId?: string | null;
  spanId?: string | null;
  parentSpanId?: string | null;
  correlationId?: string | null;
  severityText?: string | null;
  severityNumber?: number | null;
}

/** RecordEmitOptions → LogRecord 顶层字段（null/undefined 由 schema 统一处理为缺省）。 */
export function recordTailFields(options: RecordEmitOptions): Record<string, unknown> {
  return {
    trace_id: options.traceId,
    span_id: options.spanId,
    parent_span_id: options.parentSpanId,
    correlation_id: options.correlationId,
    severity_text: options.severityText,
    severity_number: options.severityNumber,
  };
}

/** `datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")`（Access / Message / System 日志使用）。 */
export function utcNowIsoZ(): string {
  return utcNowIso().replace("+00:00", "Z");
}

/** Python `bool(dict)`：非空对象为真。 */
export function isNonEmptyRecord(value: Record<string, unknown> | null | undefined): boolean {
  return value !== null && value !== undefined && Object.keys(value).length > 0;
}

// ─── NDJSON 追加写 ───────────────────────────────────────────────────────────

/** 同步追加一行（自动创建父目录）。失败时抛出，由调用方记录 WARNING。 */
export function appendLineSync(file: string, line: string): void {
  mkdirSync(dirname(file), { recursive: true });
  appendFileSync(file, `${line}\n`, "utf-8");
}

const pendingWrites = new Map<string, Promise<void>>();

/**
 * 异步追加一行（自动创建父目录），同一绝对路径上的写入按调用顺序串行执行。
 * 行本身仍是一次同步整块写入（见文件头说明）。
 * 失败时返回的 Promise 拒绝，由调用方记录 WARNING；不影响排在后面的写入。
 */
export function appendLine(file: string, line: string): Promise<void> {
  const key = resolve(file);
  const previous = pendingWrites.get(key) ?? Promise.resolve();
  const write = previous.then(async () => {
    await mkdir(dirname(file), { recursive: true });
    appendFileSync(file, `${line}\n`, "utf-8");
  });
  const settled = write.then(
    () => undefined,
    () => undefined,
  );
  pendingWrites.set(key, settled);
  void settled.then(() => {
    if (pendingWrites.get(key) === settled) {
      pendingWrites.delete(key);
    }
  });
  return write;
}

// ─── 周期发射 ────────────────────────────────────────────────────────────────

export interface RunPeriodicOptions {
  /** 中止信号；中止后当前这次发射完成即退出循环。 */
  signal?: AbortSignal;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolveSleep) => {
    if (signal.aborted) {
      resolveSleep();
      return;
    }
    const done = (): void => {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolveSleep();
    };
    // 不 unref()：与 Python 一样，周期循环在被停止前保持进程存活
    const timer = setTimeout(done, Math.max(0, ms));
    signal.addEventListener("abort", done, { once: true });
  });
}

/** 周期循环的登记表：`stop()` 结束由同一发射器启动的全部循环。 */
export class PeriodicLoops {
  private readonly controllers = new Set<AbortController>();

  /**
   * 立即执行一次 task，随后每 intervalSeconds 秒执行一次，直到 stop() 或 signal 中止。
   * task 抛出的异常会结束循环并向调用方传播（与 Python 版 run_periodic 一致；写入失败已在 task 内吞掉）。
   */
  async run(
    task: () => Promise<unknown>,
    intervalSeconds: number,
    { signal }: RunPeriodicOptions = {},
  ): Promise<void> {
    const controller = new AbortController();
    this.controllers.add(controller);
    const stopSignal = signal ? AbortSignal.any([signal, controller.signal]) : controller.signal;
    try {
      while (!stopSignal.aborted) {
        await task();
        if (stopSignal.aborted) {
          break;
        }
        await sleep(intervalSeconds * 1000, stopSignal);
      }
    } finally {
      this.controllers.delete(controller);
    }
  }

  /** 结束全部正在运行的循环（各循环在当前发射完成后返回）。 */
  stop(): void {
    for (const controller of this.controllers) {
      controller.abort();
    }
  }
}
