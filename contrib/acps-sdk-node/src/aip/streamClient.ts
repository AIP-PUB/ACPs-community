/**
 * AIP v2 流式传输客户端（Leader 侧，对应 Python `acps_sdk.aip.aip_stream_client`）
 *
 * - AipStreamClient：
 *   - startStream / reStream      — 向 Partner /stream 端点建立 SSE 连接，逐条产出 StreamResponse
 *   - streamWithReconnect         — 带断线重连的高级接口
 *   - complete / cancel           — 通过 RPC 端点发送命令
 *   - lastEventSeq                — 最后收到的事件序号（用于断线续传）
 * - StreamProtocolError：Partner 推送 error 帧时抛出（不触发重连）
 */

import { randomUUID } from "node:crypto";

import {
  type HttpStreamTransport,
  type HttpTransport,
  type TlsClientOptions,
  HttpStatusError,
  createNodeStreamTransport,
} from "../internal/http.js";
import { getLogger } from "../internal/logger.js";
import { utcNowIso } from "../internal/time.js";
import { type DataItem, type TaskCommand, type TaskResult, TaskCommandType } from "./baseModel.js";
import {
  assertAicMatchesExpected,
  assertSenderMatchesExpected,
  assertSenderMatchesPeer,
  extractPeerIdentity,
} from "./identity.js";
import { AipRpcClient } from "./rpcClient.js";
import { type StreamRequest, type StreamResponse, StreamResponseSchema } from "./streamModel.js";

const logger = getLogger("acps_sdk.aip.aip_stream_client");

/**
 * Partner 推送了带 error 字段的 StreamResponse 帧。
 *
 * 代表服务端明确告知不可续传（如 buffer 已过期），不应触发 re-stream 重连。
 */
export class StreamProtocolError extends Error {
  override name = "StreamProtocolError";
  constructor(
    readonly code: number,
    readonly protocolMessage: string,
    readonly data?: unknown,
  ) {
    super(`StreamProtocolError ${code}: ${protocolMessage}`);
  }
}

const RECONNECTABLE_CODES = new Set([
  "ECONNRESET",
  "ECONNREFUSED",
  "EPIPE",
  "ENOTFOUND",
  "EAI_AGAIN",
  "EHOSTUNREACH",
  "ENETUNREACH",
  "ERR_STREAM_PREMATURE_CLOSE",
]);

/** 是否为可重连的网络错误（对应 httpx 的 RemoteProtocolError / ConnectError / ReadError）。 */
export function isReconnectableStreamError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  const code = (error as NodeJS.ErrnoException).code;
  if (code && RECONNECTABLE_CODES.has(code)) return true;
  return error.message === "aborted" || error.message === "socket hang up";
}

/** 把字节块切成行，兼容 `\n`、`\r\n`、`\r`（与 httpx `aiter_lines` 一致）。 */
async function* iterLines(
  chunks: AsyncIterable<Buffer>,
  maxLineChars: number,
): AsyncGenerator<string> {
  const decoder = new TextDecoder("utf-8");
  let pending = "";
  for await (const chunk of chunks) {
    // 上一块剩下的部分不含换行（末尾可能有一个待定的 \r），从那里开始找，避免超长行被反复扫描
    const scanFrom = Math.max(0, pending.length - 1);
    pending += decoder.decode(chunk, { stream: true });
    let match: RegExpExecArray | null;
    const lineBreak = /\r\n|\n|\r/g;
    lineBreak.lastIndex = scanFrom;
    let consumed = 0;
    while ((match = lineBreak.exec(pending)) !== null) {
      // 末尾单独的 \r 可能是 \r\n 的前半，等下一块
      if (match[0] === "\r" && match.index === pending.length - 1) break;
      yield pending.slice(consumed, match.index);
      consumed = match.index + match[0].length;
    }
    pending = pending.slice(consumed);
    if (pending.length > maxLineChars) {
      throw new Error(`SSE line exceeds ${maxLineChars} characters`);
    }
  }
  pending += decoder.decode();
  if (pending.length > 0) {
    yield pending.replace(/\r$/, "");
  }
}

export interface AipStreamClientOptions {
  tls?: TlsClientOptions | null;
  /** 注入 RPC 传输层（测试用） */
  transport?: HttpTransport | null;
  /** 注入 SSE 流式传输层（测试用） */
  streamTransport?: HttpStreamTransport | null;
  expectedPartnerAic?: string | null;
  identityBindingEnabled?: boolean;
  /** 单个 SSE 事件（及单行）的最大字符数，默认 16 Mi；超出时中止流，防止异常对端耗尽内存 */
  maxEventChars?: number;
}

/**
 * Leader 侧流式传输客户端。
 *
 * 与 Partner 的 /stream 端点建立 SSE 连接，解析事件并逐条产出；complete / cancel 走 RPC 端点。
 */
export class AipStreamClient {
  readonly partnerStreamUrl: string;
  readonly partnerRpcUrl: string;
  readonly leaderId: string;
  private readonly expectedPartnerAic: string | null;
  private readonly identityBindingEnabled: boolean;
  private readonly streamTransport: HttpStreamTransport;
  private readonly rpcClient: AipRpcClient;
  private _lastEventSeq: number | null = null;
  private readonly maxEventChars: number;

  constructor(
    partnerStreamUrl: string,
    partnerRpcUrl: string,
    leaderId: string,
    options: AipStreamClientOptions = {},
  ) {
    this.partnerStreamUrl = partnerStreamUrl;
    this.partnerRpcUrl = partnerRpcUrl;
    this.leaderId = leaderId;
    this.expectedPartnerAic = options.expectedPartnerAic ?? null;
    this.identityBindingEnabled = options.identityBindingEnabled ?? true;
    this.maxEventChars = options.maxEventChars ?? 16 * 1024 * 1024;

    if (this.identityBindingEnabled && !this.expectedPartnerAic) {
      throw new Error("expected_partner_aic is required when identity_binding_enabled=True");
    }
    if (!this.identityBindingEnabled) {
      logger.warn(
        `AIP identity binding disabled for Stream client partner_stream_url=${this.partnerStreamUrl}`,
      );
    }
    this.streamTransport =
      options.streamTransport ?? createNodeStreamTransport(options.tls ?? undefined);
    this.rpcClient = new AipRpcClient(partnerRpcUrl, leaderId, {
      tls: options.tls,
      transport: options.transport,
      expectedPartnerAic: options.expectedPartnerAic,
      identityBindingEnabled: options.identityBindingEnabled,
    });
  }

  /** 最后成功接收的 SSE 事件序号。可传入 reStream 用于断线续传。 */
  get lastEventSeq(): number | null {
    return this._lastEventSeq;
  }

  /** 构建 TaskCommand。taskId 未指定时自动生成。 */
  makeCommand(
    commandType: TaskCommandType,
    sessionId: string,
    {
      taskId,
      textContent,
      commandParams,
    }: {
      taskId?: string | null;
      textContent?: string | null;
      commandParams?: Record<string, unknown> | null;
    } = {},
  ): TaskCommand {
    const resolvedTaskId = taskId ?? `task-${randomUUID()}`;
    const dataItems: DataItem[] = textContent ? [{ type: "text", text: textContent }] : [];
    // 键顺序与 pydantic TaskCommand 字段顺序一致
    return {
      type: "task-command",
      id: `cmd-${randomUUID()}`,
      sentAt: utcNowIso(),
      senderRole: "leader",
      senderId: this.leaderId,
      dataItems: dataItems.length > 0 ? dataItems : undefined,
      sessionId,
      command: commandType,
      commandParams: commandParams ?? undefined,
      taskId: resolvedTaskId,
    };
  }

  /** 将积累的多个 data: 值以 LF 拼接后解析；失败返回 null（容错，不抛出）。 */
  private emitAccumulated(dataParts: string[]): StreamResponse | null {
    if (dataParts.length === 0) return null;
    try {
      const parsed = StreamResponseSchema.safeParse(JSON.parse(dataParts.join("\n")));
      return parsed.success ? parsed.data : null;
    } catch {
      return null;
    }
  }

  /** 通用流入口：发送完整构造好的 TaskCommand 并返回 SSE 事件迭代器。 */
  openStream(command: TaskCommand): AsyncGenerator<StreamResponse> {
    return this.stream(command);
  }

  /** 向 Partner 发起 stream/start。 */
  startStream(
    sessionId: string,
    { taskId, textContent }: { taskId?: string | null; textContent?: string | null } = {},
  ): AsyncGenerator<StreamResponse> {
    // 新任务从头计数，避免沿用上一个任务的序号（Python 版不重置，复用客户端时会跳过新任务的前几个事件）
    this._lastEventSeq = null;
    return this.stream(this.makeCommand(TaskCommandType.Start, sessionId, { taskId, textContent }));
  }

  /** 向 Partner 发起 re-stream 重连，从 lastEventSeq 之后续传。 */
  reStream(
    taskId: string,
    sessionId: string,
    lastEventSeq?: number | null,
  ): AsyncGenerator<StreamResponse> {
    return this.stream(
      this.makeCommand(TaskCommandType.ReStream, sessionId, {
        taskId,
        commandParams:
          lastEventSeq === null || lastEventSeq === undefined ? null : { lastEventSeq },
      }),
    );
  }

  /** 建立 SSE 连接并解析事件流，自动更新 lastEventSeq。 */
  private async *stream(command: TaskCommand): AsyncGenerator<StreamResponse> {
    const streamRequest: StreamRequest = {
      jsonrpc: "2.0",
      method: "stream",
      id: randomUUID(),
      params: { message: command },
    };
    if (this.identityBindingEnabled) {
      assertSenderMatchesExpected(command, this.leaderId);
    }
    const response = await this.streamTransport({
      url: this.partnerStreamUrl,
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(streamRequest),
    });
    try {
      let remoteAic: string | null = null;
      if (this.identityBindingEnabled) {
        remoteAic = assertAicMatchesExpected(
          extractPeerIdentity(response.peerCertificate)?.aic ?? null,
          this.expectedPartnerAic,
          { actualLabel: "TLS server AIC", expectedLabel: "expected_partner_aic" },
        );
      }
      if (response.status < 200 || response.status >= 300) {
        // 错误响应体只保留前 1 MiB，避免异常对端无限输出
        const chunks: Buffer[] = [];
        let size = 0;
        for await (const chunk of response.body) {
          size += chunk.length;
          if (size > 1024 * 1024) break;
          chunks.push(chunk);
        }
        throw new HttpStatusError(response.status, Buffer.concat(chunks));
      }

      const deliver = (parsed: StreamResponse): StreamResponse => {
        if (this.identityBindingEnabled && parsed.result) {
          assertSenderMatchesPeer(parsed.result.eventData, remoteAic);
        }
        if (parsed.result) {
          this._lastEventSeq = parsed.result.eventSeq;
        }
        return parsed;
      };

      let dataParts: string[] = [];
      let eventChars = 0;
      for await (const line of iterLines(response.body, this.maxEventChars)) {
        if (line.startsWith("data:")) {
          let value = line.slice(5);
          if (value.startsWith(" ")) value = value.slice(1);
          eventChars += value.length;
          if (eventChars > this.maxEventChars) {
            throw new Error(`SSE event exceeds ${this.maxEventChars} characters`);
          }
          dataParts.push(value);
        } else if (line === "") {
          if (dataParts.length > 0) {
            const parsed = this.emitAccumulated(dataParts);
            dataParts = [];
            eventChars = 0;
            if (parsed) yield deliver(parsed);
          }
        }
        // 其他行（comment、event:、id:、retry:）忽略
      }
      if (dataParts.length > 0) {
        const parsed = this.emitAccumulated(dataParts);
        if (parsed) yield deliver(parsed);
      }
    } finally {
      response.close();
    }
  }

  /**
   * 带断线重连的高级流式接口。
   *
   * 首次调用 startStream；连接中断时自动用 reStream(lastEventSeq) 续传。
   * Partner 返回 error 帧（StreamProtocolError）时直接抛出，不重连。
   */
  async *streamWithReconnect(
    sessionId: string,
    userInput: string,
    {
      taskId,
      maxReconnects = 3,
      backoffS = 1.0,
    }: { taskId?: string | null; maxReconnects?: number; backoffS?: number } = {},
  ): AsyncGenerator<StreamResponse> {
    const id = taskId ?? `task-${randomUUID()}`;
    let reconnectCount = 0;
    let generator = this.startStream(sessionId, { taskId: id, textContent: userInput });

    while (true) {
      try {
        for await (const event of generator) {
          if (event.error) {
            throw new StreamProtocolError(event.error.code, event.error.message, event.error.data);
          }
          yield event;
        }
        break;
      } catch (error) {
        if (error instanceof StreamProtocolError || !isReconnectableStreamError(error)) {
          throw error;
        }
        if (reconnectCount >= maxReconnects) {
          throw error;
        }
        reconnectCount += 1;
        await new Promise((resolve) => setTimeout(resolve, backoffS * reconnectCount * 1000));
        generator = this.reStream(id, sessionId, this._lastEventSeq);
      }
    }
  }

  /** 发送 complete 命令到 RPC 端点。 */
  async complete(taskId: string, sessionId: string): Promise<TaskResult> {
    return this.rpcClient.completeTask(taskId, sessionId);
  }

  /** 发送 cancel 命令到 RPC 端点。 */
  async cancel(taskId: string, sessionId: string): Promise<TaskResult> {
    return this.rpcClient.cancelTask(taskId, sessionId);
  }

  /** 释放资源（保留以对齐 Python API）。 */
  async close(): Promise<void> {
    await this.rpcClient.close();
  }
}
