/**
 * 轻量日志接口。
 *
 * Python 版在每个模块上挂 `logging.NullHandler()`，默认不输出；Node 版同样默认静默，
 * 由使用方通过 `setLoggerFactory` 接入自己的日志库（pino、winston、console 等）。
 */

export interface Logger {
  debug(message: string, ...args: unknown[]): void;
  info(message: string, ...args: unknown[]): void;
  warn(message: string, ...args: unknown[]): void;
  error(message: string, ...args: unknown[]): void;
}

export type LoggerFactory = (name: string) => Logger;

const noop = (): void => undefined;

const silentLogger: Logger = { debug: noop, info: noop, warn: noop, error: noop };

let factory: LoggerFactory = () => silentLogger;
const resolved = new Map<string, Logger>();

/** 设置全局日志工厂。传 `undefined` 恢复为静默。 */
export function setLoggerFactory(next: LoggerFactory | undefined): void {
  factory = next ?? (() => silentLogger);
  resolved.clear();
}

/** 当前工厂为该名称创建的日志器（每个名称只创建一次，切换工厂后重新创建）。 */
function resolve(name: string): Logger {
  let logger = resolved.get(name);
  if (!logger) {
    logger = factory(name);
    resolved.set(name, logger);
  }
  return logger;
}

/** 获取指定名称的日志器。实际输出走调用时的工厂，便于运行时切换。 */
export function getLogger(name: string): Logger {
  return {
    debug: (message, ...args) => resolve(name).debug(message, ...args),
    info: (message, ...args) => resolve(name).info(message, ...args),
    warn: (message, ...args) => resolve(name).warn(message, ...args),
    error: (message, ...args) => resolve(name).error(message, ...args),
  };
}
