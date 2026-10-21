/**
 * AIP v2 流式传输服务端实现（对应 Python `acps_sdk.aip.aip_stream_server`）
 *
 * S1 层（纯逻辑，无 HTTP 依赖）：
 *   - BufferedStreamEvent   —— 带序列号的缓冲事件
 *   - TaskStreamChannel     —— 单任务事件通道（发布/回放/订阅/关闭）
 *   - StreamHub             —— 多任务通道管理器
 *
 * S2 层（HTTP）：
 *   - formatSse                  —— StreamResponse → SSE 帧
 *   - buildStreamResponse        —— 构建正常事件帧
 *   - buildStreamErrorResponse   —— 构建错误帧
 *   - StreamHandlers             —— 应用层回调
 *   - handleStreamRequest        —— 与框架无关的核心处理
 *   - createAipStreamRequestListener —— `(req, res)` 监听函数（对应 add_aip_stream_router）
 */

import type { IncomingMessage, ServerResponse } from "node:http";

import { getLogger } from "../internal/logger.js";
import { formatZodError } from "../internal/schema.js";
import {
  type TaskCommand,
  type TaskResult,
  TaskCommandType,
  TERMINAL_TASK_STATES,
} from "./baseModel.js";
import {
  AUTHENTICATION_REQUIRED_CODE,
  AipIdentityError,
  assertSenderMatchesExpected,
  assertSenderMatchesPeer,
  identityErrorToJsonRpc,
  normalizeAndValidateExpectedAic,
} from "./identity.js";
import { getRequestPeerAic } from "./peerCert.js";
import type { JsonRpcId } from "./rpcModel.js";
import { readJsonBody, writeJson } from "./rpcServer.js";
import {
  type ProductChunkEvent,
  type StreamEventPayload,
  type StreamResponse,
  type TaskStatusUpdateEvent,
  SSE_HEADERS,
  SSE_MEDIA_TYPE,
  StreamRequestSchema,
} from "./streamModel.js";

const logger = getLogger("acps_sdk.aip.aip_stream_server");

/** 通道里可发布的事件负载。 */
export type StreamPayload = TaskResult | TaskCommand | TaskStatusUpdateEvent | ProductChunkEvent;

// ===========================================================================
// S1：纯逻辑层
// ===========================================================================

/** 带序列号的缓冲事件。 */
export interface BufferedStreamEvent {
  readonly eventSeq: number;
  readonly payload: StreamPayload;
  readonly isTerminal: boolean;
}

/** 无界异步队列，`null` 作为结束哨兵。 */
class AsyncQueue<T> {
  private items: Array<T | null> = [];
  private waiters: Array<(value: T | null) => void> = [];

  put(item: T | null): void {
    const waiter = this.waiters.shift();
    if (waiter) {
      waiter(item);
    } else {
      this.items.push(item);
    }
  }

  /** 取下一项；signal 中止时以 null 结束等待（用于客户端断开后释放订阅）。 */
  get(signal?: AbortSignal): Promise<T | null> {
    if (this.items.length > 0) {
      return Promise.resolve(this.items.shift() as T | null);
    }
    if (signal?.aborted) {
      return Promise.resolve(null);
    }
    return new Promise((resolve) => {
      const onAbort = (): void => {
        this.waiters = this.waiters.filter((candidate) => candidate !== waiter);
        resolve(null);
      };
      const waiter = (value: T | null): void => {
        signal?.removeEventListener("abort", onAbort);
        resolve(value);
      };
      this.waiters.push(waiter);
      signal?.addEventListener("abort", onAbort, { once: true });
    });
  }
}

/**
 * 单任务 SSE 事件通道。
 *
 * - 维护单调递增序列号
 * - 以环形缓冲存储历史事件供重连时回放
 * - 支持多个并发订阅者
 * - 终态事件发布后自动关闭通道
 */
export class TaskStreamChannel {
  private readonly maxBuffer: number;
  private buffer: BufferedStreamEvent[] = [];
  private nextSeq = 1;
  private closed = false;
  private queues: Array<AsyncQueue<BufferedStreamEvent>> = [];

  constructor(maxBuffer = 200) {
    this.maxBuffer = maxBuffer;
  }

  /** 已发布的最大序列号（尚未发布任何事件时为 0）。 */
  get latestSeq(): number {
    return this.nextSeq - 1;
  }

  /** 缓冲区中最旧事件的序列号（缓冲为空时为 1）。 */
  get oldestBufferedSeq(): number {
    return this.buffer[0]?.eventSeq ?? 1;
  }

  get isClosed(): boolean {
    return this.closed;
  }

  /** 当前活跃订阅者数量（客户端断开后应回落）。 */
  get subscriberCount(): number {
    return this.queues.length;
  }

  /** 发布一条事件；终态事件发布后自动关闭通道。 */
  async publish(
    payload: StreamPayload,
    { isTerminal = false }: { isTerminal?: boolean } = {},
  ): Promise<BufferedStreamEvent> {
    const event: BufferedStreamEvent = Object.freeze({
      eventSeq: this.nextSeq,
      payload,
      isTerminal,
    });
    this.nextSeq += 1;
    this.buffer.push(event);
    if (this.buffer.length > this.maxBuffer) {
      this.buffer.splice(0, this.buffer.length - this.maxBuffer);
    }
    for (const queue of this.queues) {
      queue.put(event);
    }
    if (isTerminal) {
      await this.close();
    }
    return event;
  }

  /** 返回 seq > lastEventSeq 的缓冲事件列表；null/undefined 表示返回全部。 */
  replay(lastEventSeq?: number | null): BufferedStreamEvent[] {
    if (lastEventSeq === null || lastEventSeq === undefined) {
      return [...this.buffer];
    }
    return this.buffer.filter((event) => event.eventSeq > lastEventSeq);
  }

  /**
   * 判断 lastEventSeq 对应的重连是否可续传。
   *
   * - null：始终可续传（从头重放）
   * - lastEventSeq >= oldestBufferedSeq - 1：可续传
   * - 否则：缓冲已溢出，不可续传
   */
  canResume(lastEventSeq?: number | null): boolean {
    if (lastEventSeq === null || lastEventSeq === undefined) {
      return true;
    }
    if (this.buffer.length === 0) {
      return true;
    }
    return lastEventSeq >= this.oldestBufferedSeq - 1;
  }

  /**
   * 订阅事件流：先回放缓冲中的历史事件，再推送实时事件；通道关闭时迭代正常结束。
   * 若通道在订阅前已关闭，回放后直接结束。
   */
  async *subscribe(
    lastEventSeq?: number | null,
    { signal }: { signal?: AbortSignal } = {},
  ): AsyncGenerator<BufferedStreamEvent> {
    const queue = new AsyncQueue<BufferedStreamEvent>();
    const replayed = this.replay(lastEventSeq);
    const alreadyClosed = this.closed;
    if (!alreadyClosed) {
      this.queues.push(queue);
    }

    try {
      for (const event of replayed) {
        if (signal?.aborted) return;
        yield event;
      }
      if (alreadyClosed) {
        return;
      }
      while (true) {
        const item = await queue.get(signal);
        if (item === null) {
          break;
        }
        yield item;
      }
    } finally {
      this.queues = this.queues.filter((candidate) => candidate !== queue);
    }
  }

  /** 关闭通道，通知所有订阅者结束迭代。 */
  async close(): Promise<void> {
    if (this.closed) {
      return;
    }
    this.closed = true;
    for (const queue of this.queues) {
      queue.put(null);
    }
  }
}

/**
 * 管理所有活跃任务的 TaskStreamChannel。
 *
 * Partner 通过 StreamHub 发布事件；HTTP 层通过 StreamHub 获取通道并订阅。
 */
export class StreamHub {
  private channels = new Map<string, TaskStreamChannel>();

  constructor(private readonly maxBuffer = 200) {}

  /** 获取已有通道或创建新通道。 */
  getOrCreateChannel(taskId: string): TaskStreamChannel {
    let channel = this.channels.get(taskId);
    if (!channel) {
      channel = new TaskStreamChannel(this.maxBuffer);
      this.channels.set(taskId, channel);
    }
    return channel;
  }

  /** 获取已有通道，不存在则返回 null。 */
  getChannel(taskId: string): TaskStreamChannel | null {
    return this.channels.get(taskId) ?? null;
  }

  /** 从 Hub 中移除通道记录（不关闭通道本身）。 */
  removeChannel(taskId: string): void {
    this.channels.delete(taskId);
  }

  /** 发布 TaskResult 事件；isTerminal 未指定时按状态自动判断。 */
  async publishTaskResult(
    taskId: string,
    taskResult: TaskResult,
    { isTerminal }: { isTerminal?: boolean | null } = {},
  ): Promise<void> {
    const channel = this.getOrCreateChannel(taskId);
    const terminal =
      isTerminal === undefined || isTerminal === null
        ? TERMINAL_TASK_STATES.has(taskResult.status.state)
        : isTerminal;
    await channel.publish(taskResult, { isTerminal: terminal });
  }

  async publishStatusUpdate(taskId: string, event: TaskStatusUpdateEvent): Promise<void> {
    await this.getOrCreateChannel(taskId).publish(event, { isTerminal: false });
  }

  async publishProductChunk(taskId: string, event: ProductChunkEvent): Promise<void> {
    await this.getOrCreateChannel(taskId).publish(event, { isTerminal: false });
  }

  /** 关闭并移除通道。 */
  async closeStream(taskId: string): Promise<void> {
    const channel = this.channels.get(taskId);
    this.channels.delete(taskId);
    if (channel) {
      await channel.close();
    }
  }
}

// ===========================================================================
// S2：HTTP 层
// ===========================================================================

/** 将 StreamResponse 序列化为 SSE 数据帧：`data: <json>\n\n`（省略空字段）。 */
export function formatSse(response: StreamResponse): string {
  return `data: ${JSON.stringify(response)}\n\n`;
}

/** 构建正常事件帧 StreamResponse（含 result，不含 error）。 */
export function buildStreamResponse(
  rpcId: JsonRpcId | null | undefined,
  event: BufferedStreamEvent,
): StreamResponse {
  return {
    jsonrpc: "2.0",
    id: rpcId ?? undefined,
    result: {
      eventSeq: event.eventSeq,
      eventData: event.payload as StreamEventPayload,
    },
  };
}

/** 构建纯错误帧 StreamResponse（result 为空，含 error）。 */
export function buildStreamErrorResponse(
  rpcId: JsonRpcId | null | undefined,
  code: number,
  message: string,
  data?: unknown,
): StreamResponse {
  return {
    jsonrpc: "2.0",
    id: rpcId ?? undefined,
    error: { code, message, ...(data === undefined || data === null ? {} : { data }) },
  };
}

/**
 * 可插拔的流处理回调（与 RPC 的 CommandHandlers 对应）。
 *
 * onStreamStart: 收到 stream(start) 时调用——业务层据此启动任务，后台通过 hub.publish* 注入事件。
 * onReStream:    收到 re-stream 且通道不存在时调用（可选）——业务层可尝试恢复通道；返回 null 则走 404。
 */
export interface StreamHandlers {
  onStreamStart: (command: TaskCommand) => Promise<void>;
  onReStream?: (command: TaskCommand) => Promise<TaskStreamChannel | null>;
}

export interface HandleStreamRequestOptions {
  localAic?: string | null;
  identityBindingEnabled?: boolean;
  /**
   * 客户端断开时中止：订阅随之释放（对应 Python 中 Starlette 在断开时取消 SSE 生成器）。
   * 自行编写 HTTP 层时务必在连接关闭时触发它，否则阻塞中的订阅不会被回收。
   */
  signal?: AbortSignal;
}

/** handleStreamRequest 的结果：HTTP 错误（对应 Python HTTPException）或 SSE 帧序列。 */
export type StreamRequestOutcome =
  | { kind: "error"; status: number; detail: Record<string, unknown> }
  | { kind: "stream"; frames: AsyncIterable<string> };

async function* sseEventGenerator(
  hub: StreamHub,
  taskId: string | null,
  rpcId: JsonRpcId | undefined,
  lastEventSeq: number | null,
  localAic: string | null,
  identityBindingEnabled: boolean,
  signal: AbortSignal | undefined,
): AsyncGenerator<string> {
  const channel = taskId === null ? null : hub.getChannel(taskId);
  if (!channel) {
    // Python 在 taskId 缺失时格式化为 "None"
    yield formatSse(
      buildStreamErrorResponse(rpcId, -32001, `Task stream not found: ${taskId ?? "None"}`),
    );
    return;
  }
  for await (const event of channel.subscribe(lastEventSeq, { signal })) {
    if (identityBindingEnabled) {
      assertSenderMatchesExpected(event.payload, localAic);
    }
    yield formatSse(buildStreamResponse(rpcId, event));
  }
}

/**
 * 与框架无关的流式请求处理：解析 StreamRequest，校验命令类型，返回 SSE 帧序列或 HTTP 错误。
 *
 * handlers 接受 StreamHandlers 或单个函数（等价于 onStreamStart）。
 */
export async function handleStreamRequest(
  context: { body: unknown; getPeerAic: () => string | null },
  hub: StreamHub,
  handlers: StreamHandlers | ((command: TaskCommand) => Promise<void>),
  { localAic = null, identityBindingEnabled = true, signal }: HandleStreamRequestOptions = {},
): Promise<StreamRequestOutcome> {
  const { onStreamStart, onReStream } =
    typeof handlers === "function" ? { onStreamStart: handlers, onReStream: undefined } : handlers;

  const parsed = StreamRequestSchema.safeParse(context.body);
  if (!parsed.success) {
    return {
      kind: "error",
      status: 400,
      detail: { code: -32700, message: "Parse error", data: formatZodError(parsed.error) },
    };
  }
  const command = parsed.data.params.message;
  const taskId = command.taskId;
  const rpcId = parsed.data.id;

  if (identityBindingEnabled && !localAic) {
    throw new Error("local_aic is required when identity_binding_enabled=True");
  }

  if (identityBindingEnabled) {
    try {
      assertSenderMatchesPeer(command, context.getPeerAic());
    } catch (error) {
      if (error instanceof AipIdentityError) {
        const rpcError = identityErrorToJsonRpc(error);
        return {
          kind: "error",
          status: rpcError.code === AUTHENTICATION_REQUIRED_CODE ? 401 : 403,
          detail: { ...rpcError },
        };
      }
      throw error;
    }
  }

  if (command.command === TaskCommandType.Start) {
    if (!taskId) {
      return {
        kind: "error",
        status: 400,
        detail: { code: -32602, message: "taskId is required for stream/start" },
      };
    }
    // 确保通道存在（在 onStreamStart 异步触发任务处理之前先创建通道）
    hub.getOrCreateChannel(taskId);
    // 异步启动任务处理（fire-and-forget）
    void Promise.resolve()
      .then(() => onStreamStart(command))
      .catch((error: unknown) => logger.error("on_stream_start failed", error));
    return {
      kind: "stream",
      frames: sseEventGenerator(hub, taskId, rpcId, null, localAic, identityBindingEnabled, signal),
    };
  }

  if (command.command === TaskCommandType.ReStream) {
    const rawSeq = command.commandParams?.["lastEventSeq"];
    if (rawSeq !== undefined && rawSeq !== null && !Number.isSafeInteger(rawSeq)) {
      // Python 在比较时抛 TypeError（500）；这里明确返回参数错误
      return {
        kind: "error",
        status: 400,
        detail: { code: -32602, message: "commandParams.lastEventSeq must be an integer" },
      };
    }
    const lastEventSeq = typeof rawSeq === "number" ? rawSeq : null;
    let channel = taskId ? hub.getChannel(taskId) : null;
    if (!channel && onReStream) {
      channel = await onReStream(command);
    }
    if (!channel) {
      return {
        kind: "error",
        status: 404,
        detail: { code: -32001, message: `Stream not found: ${taskId ?? "None"}` },
      };
    }
    if (!channel.canResume(lastEventSeq)) {
      return {
        kind: "stream",
        frames: (async function* () {
          yield formatSse(
            buildStreamErrorResponse(
              rpcId,
              -32002,
              "Cannot re-stream: buffer overflow, please restart",
            ),
          );
        })(),
      };
    }
    return {
      kind: "stream",
      // 与 Python 一致：onReStream 恢复出的通道只用于可续传判断，事件仍从 hub 中按 taskId 读取
      frames: sseEventGenerator(
        hub,
        taskId ?? null,
        rpcId,
        lastEventSeq,
        localAic,
        identityBindingEnabled,
        signal,
      ),
    };
  }

  return {
    kind: "error",
    status: 400,
    detail: { code: -32602, message: `Unsupported stream command: ${command.command}` },
  };
}

/** 把 handleStreamRequest 的结果写到 HTTP 响应。 */
export async function writeStreamOutcome(
  res: ServerResponse,
  outcome: StreamRequestOutcome,
): Promise<void> {
  if (outcome.kind === "error") {
    writeJson(res, outcome.status, { detail: outcome.detail });
    return;
  }
  res.statusCode = 200;
  res.setHeader("Content-Type", `${SSE_MEDIA_TYPE}; charset=utf-8`);
  for (const [name, value] of Object.entries(SSE_HEADERS)) {
    res.setHeader(name, value);
  }
  res.flushHeaders();
  // 连接可能在进入这里之前就已关闭（例如前面的中间件耗时较长）
  let clientGone = res.destroyed;
  res.on("close", () => {
    clientGone = true;
  });
  const iterator = outcome.frames[Symbol.asyncIterator]();
  try {
    while (!clientGone) {
      const next = await iterator.next();
      if (next.done) break;
      if (!res.write(next.value)) {
        // 对已关闭的响应写入会返回 false 且不再触发 drain / close，直接结束
        if (res.destroyed) break;
        // 背压：等客户端读走缓冲或连接关闭
        await new Promise<void>((resolve) => {
          const done = (): void => {
            res.off("drain", done);
            res.off("close", done);
            resolve();
          };
          res.once("drain", done);
          res.once("close", done);
        });
      }
    }
    res.end();
  } catch (error) {
    logger.error("AIP stream aborted", error);
    res.destroy(error instanceof Error ? error : new Error(String(error)));
  } finally {
    await iterator.return?.(undefined);
  }
}

/**
 * Create an HTTP request listener for the AIP stream endpoint（对应 Python `add_aip_stream_router`）。
 */
/** 流式监听函数选项（断开检测由监听函数自己处理，不接受外部 signal）。 */
export interface AipStreamListenerOptions {
  localAic?: string | null;
  identityBindingEnabled?: boolean;
}

export function createAipStreamRequestListener(
  hub: StreamHub,
  handlers: StreamHandlers | ((command: TaskCommand) => Promise<void>),
  { localAic = null, identityBindingEnabled = true }: AipStreamListenerOptions = {},
): (req: IncomingMessage, res: ServerResponse) => Promise<void> {
  if (identityBindingEnabled) {
    // 创建时就校验配置，避免每个请求都以 500 失败
    normalizeAndValidateExpectedAic(localAic, { fieldName: "local_aic" });
  } else {
    logger.warn("AIP identity binding disabled for Stream server endpoint");
  }
  return async (req, res) => {
    // 先挂断开监听再读请求体；若连接在此之前已关闭，立即中止
    const disconnected = new AbortController();
    res.on("close", () => disconnected.abort());
    if (res.destroyed) disconnected.abort();
    const body = await readJsonBody(req);
    if (!body.ok) {
      writeJson(res, 400, { detail: { code: -32700, message: "Parse error", data: body.error } });
      return;
    }
    try {
      const outcome = await handleStreamRequest(
        { body: body.value, getPeerAic: () => getRequestPeerAic(req) },
        hub,
        handlers,
        { localAic, identityBindingEnabled, signal: disconnected.signal },
      );
      await writeStreamOutcome(res, outcome);
    } catch (error) {
      logger.error("AIP stream request failed", error);
      if (!res.headersSent) {
        writeJson(res, 500, { detail: "Internal Server Error" });
      } else {
        res.destroy();
      }
    }
  };
}
