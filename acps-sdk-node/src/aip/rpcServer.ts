/**
 * AIP Direct RPC 服务端（对应 Python `acps_sdk.aip.aip_rpc_server`）
 *
 * - `CommandHandlers` / `DefaultHandlers`：可插拔的命令处理框架；
 * - `TaskManager`：内存任务存储与状态机（示例用途，生产环境应替换为持久化存储）；
 * - `handleRpcRequest`：与框架无关的核心处理函数；
 * - `createAipRpcRequestListener`：返回 `(req, res)` 监听函数，可直接挂到 `https.createServer`
 *   或 Express 路由上，对应 Python 的 `add_aip_rpc_router(app, endpoint, handlers)`。
 */

import type { IncomingMessage, ServerResponse } from "node:http";
import { randomUUID } from "node:crypto";

import { getLogger } from "../internal/logger.js";
import { formatZodError } from "../internal/schema.js";
import { utcNowIso } from "../internal/time.js";
import {
  type DataItem,
  type Product,
  type TaskCommand,
  type TaskResult,
  type TaskStatus,
  TaskCommandType,
  TaskState,
  TERMINAL_TASK_STATES,
  TaskResultSchema,
} from "./baseModel.js";
import {
  AipIdentityError,
  assertSenderMatchesExpected,
  assertSenderMatchesPeer,
  identityErrorToJsonRpc,
  normalizeAndValidateExpectedAic,
} from "./identity.js";
import { getRequestPeerAic } from "./peerCert.js";
import { type RpcRequest, type RpcResponse, RpcRequestSchema } from "./rpcModel.js";

const logger = getLogger("acps_sdk.aip.aip_rpc_server");
const DEFAULT_TASK_RESULT_SENDER = "server";

const TASK_RESULT_FIELDS = Object.keys(TaskResultSchema.shape);

/**
 * 给任务对象赋值一个此前不存在的字段时，就地把键重排回 pydantic 字段顺序（对象引用不变）。
 *
 * pydantic 给模型属性赋值不改变 `model_dump` 的字段顺序；普通 JS 对象新增的键会排到最后，
 * 序列化出的 JSON 键顺序就与 Python 版不同。
 */
function assignTaskField<K extends keyof TaskResult>(
  task: TaskResult,
  key: K,
  value: TaskResult[K],
): void {
  const existed = Object.prototype.hasOwnProperty.call(task, key);
  task[key] = value;
  if (existed) {
    return;
  }
  const record = task as unknown as Record<string, unknown>;
  const ordered = [
    ...TASK_RESULT_FIELDS.filter((field) => Object.prototype.hasOwnProperty.call(record, field)),
    ...Object.keys(record).filter((field) => !TASK_RESULT_FIELDS.includes(field)),
  ].map((field) => [field, record[field]] as const);
  for (const field of Object.keys(record)) {
    delete record[field];
  }
  for (const [field, fieldValue] of ordered) {
    record[field] = fieldValue;
  }
}

// -------- Pluggable Command Framework --------

export type StartHandler = (command: TaskCommand, task: TaskResult | null) => Promise<TaskResult>;
export type TaskHandler = (command: TaskCommand, task: TaskResult) => Promise<TaskResult>;

/**
 * Per-command optional handlers. Any handler left undefined falls back to DefaultHandlers.
 *
 * Handler signature: `(command, task) => Promise<TaskResult>`. Return the updated TaskResult snapshot.
 */
export interface CommandHandlers {
  onStart?: StartHandler;
  onGet?: TaskHandler;
  onCancel?: TaskHandler;
  onComplete?: TaskHandler;
  onContinue?: TaskHandler;
  /** Catch-all for unknown/unsupported/missing command messages */
  onMessage?: StartHandler;
}

/**
 * A simple in-memory store and state machine for managing tasks.
 * In a real application, this would be backed by a persistent database.
 */
export class TaskManager {
  private static tasks = new Map<string, TaskResult>();
  private static maxProductsBytes = new Map<string, number>();

  static getTask(taskId: string): TaskResult | null {
    return TaskManager.tasks.get(taskId) ?? null;
  }

  /** 清空内存任务（测试用）。 */
  static clear(): void {
    TaskManager.tasks.clear();
    TaskManager.maxProductsBytes.clear();
  }

  static createTask(
    command: TaskCommand,
    initialState?: TaskState | null,
    dataItems?: DataItem[] | null,
  ): TaskResult {
    if (!command.taskId) {
      throw new Error("A command to start a task must have a taskId.");
    }
    const taskStatus: TaskStatus = {
      state: initialState ?? TaskState.Accepted,
      stateChangedAt: utcNowIso(),
      dataItems: dataItems ?? [],
    };
    const task: TaskResult = {
      type: "task-result",
      id: `result-${randomUUID()}`,
      sentAt: utcNowIso(),
      senderRole: "partner",
      // Will be overridden by actual partner
      senderId: DEFAULT_TASK_RESULT_SENDER,
      // 键顺序与 pydantic 字段顺序一致（sessionId 属于 Message 基类字段）
      sessionId: command.sessionId,
      taskId: command.taskId,
      status: taskStatus,
      commandHistory: [command],
      statusHistory: [taskStatus],
    };
    TaskManager.tasks.set(command.taskId, task);
    return task;
  }

  static updateTaskStatus(
    taskId: string,
    newState: TaskState,
    dataItems?: DataItem[] | null,
  ): TaskResult {
    const task = TaskManager.getTask(taskId);
    if (!task) {
      throw new Error(`Task with id ${taskId} not found.`);
    }
    const newStatus: TaskStatus = {
      state: newState,
      stateChangedAt: utcNowIso(),
      dataItems: dataItems ?? [],
    };
    task.status = newStatus;
    if (task.statusHistory && task.statusHistory.length > 0) {
      task.statusHistory.push(newStatus);
    } else {
      assignTaskField(task, "statusHistory", [newStatus]);
    }
    return task;
  }

  static addCommandToHistory(taskId: string, command: TaskCommand): void {
    const task = TaskManager.getTask(taskId);
    if (task) {
      if (task.commandHistory && task.commandHistory.length > 0) {
        task.commandHistory.push(command);
      } else {
        assignTaskField(task, "commandHistory", [command]);
      }
    }
  }

  /** 为任务设置产出物大小上限（对应 Python 版任务对象上的 `_aip_max_products_bytes`）。 */
  static setMaxProductsBytes(taskId: string, maxBytes: number | null): void {
    if (maxBytes === null) {
      TaskManager.maxProductsBytes.delete(taskId);
    } else {
      TaskManager.maxProductsBytes.set(taskId, maxBytes);
    }
  }

  static setProducts(taskId: string, products: Product[]): void {
    const task = TaskManager.getTask(taskId);
    if (!task) {
      return;
    }
    const maxBytes = TaskManager.maxProductsBytes.get(taskId);
    if (maxBytes !== undefined) {
      try {
        let totalBytes = 0;
        for (const product of products) {
          for (const item of product.dataItems) {
            if (item.type === "text") {
              totalBytes += Buffer.byteLength(item.text, "utf-8");
            } else if (item.type === "file" && item.bytes) {
              totalBytes += item.bytes.length;
            }
          }
        }
        if (totalBytes > maxBytes) {
          TaskManager.updateTaskStatus(taskId, TaskState.Failed, [
            {
              type: "text",
              text: `Products size ${totalBytes} bytes exceeds maxProductsBytes=${maxBytes}.`,
            },
          ]);
          return;
        }
      } catch {
        TaskManager.updateTaskStatus(taskId, TaskState.Failed, [
          { type: "text", text: "Error calculating products size." },
        ]);
        return;
      }
    }
    assignTaskField(task, "products", products);
  }
}

/**
 * Built-in default behaviors for AIP commands. Agents can reuse these from their
 * overrides if they want to apply standard semantics.
 */
export const DefaultHandlers = {
  /** Start from non-existent creates a new task; if already exists, ignore and return current snapshot. */
  async start(command: TaskCommand, task: TaskResult | null): Promise<TaskResult> {
    if (task) {
      TaskManager.addCommandToHistory(task.taskId, command);
      return task;
    }
    return TaskManager.createTask(command);
  },

  /** Incremental filtering based on commandParams (optional). */
  async get(command: TaskCommand, task: TaskResult): Promise<TaskResult> {
    const params = command.commandParams ?? {};
    const lastCommandSentAt = params["lastCommandSentAt"];
    const lastStateChangedAt = params["lastStateChangedAt"];

    let filtered = task;
    try {
      if (
        typeof lastCommandSentAt === "string" &&
        lastCommandSentAt &&
        task.commandHistory?.length
      ) {
        filtered = structuredClone(filtered);
        filtered.commandHistory = (task.commandHistory ?? []).filter(
          (message) => message.sentAt > lastCommandSentAt,
        );
      }
      if (
        typeof lastStateChangedAt === "string" &&
        lastStateChangedAt &&
        task.statusHistory?.length
      ) {
        if (filtered === task) {
          filtered = structuredClone(filtered);
        }
        filtered.statusHistory = (task.statusHistory ?? []).filter(
          (status) => status.stateChangedAt > lastStateChangedAt,
        );
      }
    } catch {
      filtered = task;
    }
    return filtered;
  },

  /** Do not overwrite terminal states; make cancel idempotent. */
  async cancel(command: TaskCommand, task: TaskResult): Promise<TaskResult> {
    if (TERMINAL_TASK_STATES.has(task.status.state)) {
      return task;
    }
    TaskManager.addCommandToHistory(task.taskId, command);
    return TaskManager.updateTaskStatus(task.taskId, TaskState.Canceled);
  },

  /** Only effective when current state == AwaitingCompletion; otherwise ignore (no state change). */
  async complete(command: TaskCommand, task: TaskResult): Promise<TaskResult> {
    if (task.status.state === TaskState.AwaitingCompletion) {
      TaskManager.addCommandToHistory(task.taskId, command);
      return TaskManager.updateTaskStatus(task.taskId, TaskState.Completed);
    }
    TaskManager.addCommandToHistory(task.taskId, command);
    return task;
  },

  /**
   * Only effective when current state is AwaitingInput or AwaitingCompletion and the command
   * carries non-empty text. By default the state is not changed; agents implement business logic.
   */
  async continue(command: TaskCommand, task: TaskResult): Promise<TaskResult> {
    // 只记录命令，不改变状态；Python 版在三种情况下（非等待态、无文本、有文本）都原样返回任务
    TaskManager.addCommandToHistory(task.taskId, command);
    return task;
  },
} as const;

/** Backfill the built-in TaskManager placeholder sender with the real local AIC. */
function bindDefaultTaskResultIdentity(
  result: TaskResult,
  localAic: string | null | undefined,
): TaskResult {
  if (localAic && result.senderId === DEFAULT_TASK_RESULT_SENDER) {
    result.senderId = localAic;
  }
  return result;
}

export interface HandleRpcRequestOptions {
  /** 本端 AIC。启用身份绑定时必填。 */
  localAic?: string | null;
  /** 是否启用 AIP 身份绑定，默认 true */
  identityBindingEnabled?: boolean;
  /** 自定义分发：提供时绕过内置 TaskManager 与 handlers */
  dispatchRequest?: (request: RpcRequest) => Promise<RpcResponse>;
}

export interface RpcRequestContext {
  /** 已解析的 JSON 请求体 */
  body: unknown;
  /** 返回经 TLS 验证的对端 AIC；身份错误应直接抛出 AipIdentityError */
  getPeerAic: () => string | null;
}

/**
 * Generic handler for AIP RPC requests.
 * It parses the request, validates it, and passes it to the agent-specific logic.
 */
export async function handleRpcRequest(
  context: RpcRequestContext,
  agentHandlers: CommandHandlers,
  { localAic = null, identityBindingEnabled = true, dispatchRequest }: HandleRpcRequestOptions = {},
): Promise<RpcResponse> {
  const parsed = RpcRequestSchema.safeParse(context.body);
  if (!parsed.success) {
    return {
      jsonrpc: "2.0",
      id: null,
      error: { code: -32700, message: "Parse error", data: formatZodError(parsed.error) },
    };
  }
  const rpcRequest = parsed.data;
  const command = rpcRequest.params.command;
  const taskId = command.taskId;
  // JSON-RPC 要求响应带 id；请求未提供时为 null
  const id = rpcRequest.id ?? null;

  if (identityBindingEnabled && !localAic) {
    throw new Error("local_aic is required when identity_binding_enabled=True");
  }

  const checkResult = (result: TaskResult): void => {
    if (identityBindingEnabled && localAic) {
      assertSenderMatchesExpected(result, localAic);
    }
  };

  if (identityBindingEnabled) {
    try {
      assertSenderMatchesPeer(command, context.getPeerAic());
    } catch (error) {
      if (error instanceof AipIdentityError) {
        return { jsonrpc: "2.0", id, error: identityErrorToJsonRpc(error) };
      }
      throw error;
    }
  }

  if (!taskId && command.command !== TaskCommandType.Start) {
    return {
      jsonrpc: "2.0",
      id,
      error: {
        code: -32602,
        message: "Invalid params",
        data: "taskId is required for non-Start commands.",
      },
    };
  }

  if (dispatchRequest) {
    try {
      const response = await dispatchRequest(rpcRequest);
      if (response.result) {
        checkResult(response.result);
      }
      return response;
    } catch (error) {
      if (error instanceof AipIdentityError) {
        return { jsonrpc: "2.0", id, error: identityErrorToJsonRpc(error) };
      }
      throw error;
    }
  }

  // --- Command Dispatch via pluggable handlers ---
  const task = taskId ? TaskManager.getTask(taskId) : null;
  const notFound = (): RpcResponse => ({
    jsonrpc: "2.0",
    id,
    error: { code: -32001, message: "Task not found", data: { taskId } },
  });

  try {
    let result: TaskResult;
    switch (command.command) {
      case TaskCommandType.Start:
        result = agentHandlers.onStart
          ? await agentHandlers.onStart(command, task)
          : bindDefaultTaskResultIdentity(await DefaultHandlers.start(command, task), localAic);
        break;
      case TaskCommandType.Get:
        if (!task) return notFound();
        result = agentHandlers.onGet
          ? await agentHandlers.onGet(command, task)
          : bindDefaultTaskResultIdentity(await DefaultHandlers.get(command, task), localAic);
        break;
      case TaskCommandType.Cancel:
        if (!task) return notFound();
        result = agentHandlers.onCancel
          ? await agentHandlers.onCancel(command, task)
          : bindDefaultTaskResultIdentity(await DefaultHandlers.cancel(command, task), localAic);
        break;
      case TaskCommandType.Complete:
        if (!task) return notFound();
        result = agentHandlers.onComplete
          ? await agentHandlers.onComplete(command, task)
          : bindDefaultTaskResultIdentity(await DefaultHandlers.complete(command, task), localAic);
        break;
      case TaskCommandType.Continue:
        if (!task) return notFound();
        result = agentHandlers.onContinue
          ? await agentHandlers.onContinue(command, task)
          : bindDefaultTaskResultIdentity(await DefaultHandlers.continue(command, task), localAic);
        break;
      default:
        // Unknown or missing command -> try catch-all handler if provided
        if (!task) return notFound();
        if (agentHandlers.onMessage) {
          result = await agentHandlers.onMessage(command, task);
          break;
        }
        return {
          jsonrpc: "2.0",
          id,
          error: {
            code: -32602,
            message: "Invalid params",
            data: `Unknown or missing command: ${command.command}`,
          },
        };
    }
    checkResult(result);
    return { jsonrpc: "2.0", id, result };
  } catch (error) {
    if (error instanceof AipIdentityError) {
      return { jsonrpc: "2.0", id, error: identityErrorToJsonRpc(error) };
    }
    // If the agent logic fails, update the task state to 'failed'
    const message = error instanceof Error ? error.message : String(error);
    const failedTask = TaskManager.updateTaskStatus(taskId ?? "", TaskState.Failed, [
      { type: "text", text: `Agent execution failed: ${message}` },
    ]);
    return { jsonrpc: "2.0", id, result: failedTask };
  }
}

/** 读取请求体（上限默认 10 MiB）。 */
export async function readRequestBody(
  req: IncomingMessage,
  limitBytes = 10 * 1024 * 1024,
): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk as string);
    size += buffer.length;
    if (size > limitBytes) {
      throw new Error(`Request body exceeds ${limitBytes} bytes`);
    }
    chunks.push(buffer);
  }
  return Buffer.concat(chunks);
}

/** 把对象写成 JSON 响应。 */
export function writeJson(res: ServerResponse, status: number, payload: unknown): void {
  const body = JSON.stringify(payload);
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.setHeader("Content-Length", Buffer.byteLength(body));
  res.end(body);
}

/** Express 等框架已解析的 body（`req.body`）会被优先使用。 */
interface MaybeParsedRequest extends IncomingMessage {
  body?: unknown;
}

export async function readJsonBody(
  req: MaybeParsedRequest,
): Promise<{ ok: true; value: unknown } | { ok: false; error: string }> {
  if (req.body !== undefined && !Buffer.isBuffer(req.body) && typeof req.body !== "string") {
    return { ok: true, value: req.body };
  }
  try {
    const raw =
      typeof req.body === "string"
        ? req.body
        : Buffer.isBuffer(req.body)
          ? req.body.toString("utf-8")
          : (await readRequestBody(req)).toString("utf-8");
    return { ok: true, value: JSON.parse(raw) as unknown };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

export interface AipRpcListenerOptions {
  localAic?: string | null;
  identityBindingEnabled?: boolean;
}

/**
 * Create an HTTP request listener for the AIP RPC endpoint
 * （对应 Python `add_aip_rpc_router`，路径匹配交给调用方的路由）。
 *
 * ```ts
 * const listener = createAipRpcRequestListener(handlers, { localAic: MY_AIC });
 * https.createServer(tlsOptions, (req, res) => {
 *   if (req.method === "POST" && req.url === "/rpc") return void listener(req, res);
 *   res.statusCode = 404; res.end();
 * });
 * ```
 */
export function createAipRpcRequestListener(
  agentHandlers: CommandHandlers,
  { localAic = null, identityBindingEnabled = true }: AipRpcListenerOptions = {},
): (req: IncomingMessage, res: ServerResponse) => Promise<void> {
  if (identityBindingEnabled) {
    // 创建时就校验配置，避免每个请求都以 500 失败
    normalizeAndValidateExpectedAic(localAic, { fieldName: "local_aic" });
  } else {
    logger.warn("AIP identity binding disabled for RPC server endpoint");
  }
  return async (req, res) => {
    const body = await readJsonBody(req as MaybeParsedRequest);
    if (!body.ok) {
      writeJson(res, 200, {
        jsonrpc: "2.0",
        id: null,
        error: { code: -32700, message: "Parse error", data: body.error },
      } satisfies RpcResponse);
      return;
    }
    try {
      const response = await handleRpcRequest(
        { body: body.value, getPeerAic: () => getRequestPeerAic(req) },
        agentHandlers,
        { localAic, identityBindingEnabled },
      );
      writeJson(res, 200, response);
    } catch (error) {
      logger.error("AIP RPC request failed", error);
      writeJson(res, 500, { detail: "Internal Server Error" });
    }
  };
}
