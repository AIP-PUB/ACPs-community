/**
 * AIP v2 通知方式服务端实现（对应 Python `acps_sdk.aip.aip_notification_server`）
 *
 * N1 层（纯逻辑，无 HTTP 依赖）：
 *   - NotificationConfigStore   —— 通知配置存储
 *   - NotificationSubscription  —— 订阅记录
 *   - NotificationRegistry      —— 订阅注册表
 *   - NotificationDispatcher    —— 回调分发器（含重试）
 *
 * N2 层（HTTP）：
 *   - NotificationHandlers      —— 应用层回调
 *   - NotificationService       —— 聚合 store/registry/dispatcher
 *   - createAipNotificationRequestListener —— 对应 add_aip_notification_router
 */

import { randomUUID } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";

import { validateAicFormat } from "../aic/index.js";
import {
  type HttpTransport,
  type TlsClientOptions,
  createNodeTransport,
} from "../internal/http.js";
import { getLogger } from "../internal/logger.js";
import { formatZodError } from "../internal/schema.js";
import { type TaskCommand, type TaskResult, type TaskState, TaskStateSchema } from "./baseModel.js";
import {
  AipIdentityError,
  AUTHENTICATION_REQUIRED_CODE,
  InvalidPeerCertificateError,
  PeerAicMissingError,
  assertSenderMatchesExpected,
  assertSenderMatchesPeer,
  identityErrorToJsonRpc,
  normalizeAic,
  normalizeAndValidateExpectedAic,
} from "./identity.js";
import {
  type NotificationConfig,
  type NotificationDeleteRequest,
  type NotificationDeleteResponse,
  type NotificationGetRequest,
  type NotificationGetResponse,
  type NotificationRequest,
  type NotificationResponse,
  type NotificationStartRequest,
  NOTIFICATION_TOKEN_HEADER,
  NotificationDeleteRequestSchema,
  NotificationGetRequestSchema,
  NotificationRequestSchema,
  NotificationStartRequestSchema,
} from "./notificationModel.js";
import { getRequestPeerAic } from "./peerCert.js";
import type { JsonRpcId } from "./rpcModel.js";
import { readJsonBody, writeJson } from "./rpcServer.js";

const logger = getLogger("acps_sdk.aip.aip_notification_server");
const UNSCOPED_OWNER = "__notification_unscoped__";

/** 通知配置不存在（对应 Python 的 KeyError）。 */
export class NotificationConfigNotFoundError extends Error {
  override name = "NotificationConfigNotFoundError";
}

/** notification/start 参数非法（对应 Python 的 ValueError）。 */
export class NotificationParamsError extends Error {
  override name = "NotificationParamsError";
}

// ===========================================================================
// N1：纯逻辑层
// ===========================================================================

/** 通知配置存储（内存实现）。 */
export class NotificationConfigStore {
  // owner -> taskId -> configId -> NotificationConfig
  private store = new Map<string, Map<string, Map<string, NotificationConfig>>>();

  private ownerKey(ownerAic: string | null | undefined): string {
    return ownerAic || UNSCOPED_OWNER;
  }

  /** 新建或更新配置。无 id 时自动生成。 */
  set(
    config: NotificationConfig,
    { ownerAic }: { ownerAic?: string | null } = {},
  ): NotificationConfig {
    const ownerKey = this.ownerKey(ownerAic);
    let ownerStore = this.store.get(ownerKey);
    if (!ownerStore) {
      ownerStore = new Map();
      this.store.set(ownerKey, ownerStore);
    }
    let taskConfigs = ownerStore.get(config.taskId);
    if (!taskConfigs) {
      taskConfigs = new Map();
      ownerStore.set(config.taskId, taskConfigs);
    }
    let stored = config;
    if (!config.id) {
      // 与 pydantic `model_copy(update={"id": ...})` 一致：id 保持在字段首位
      const { id: _missing, ...rest } = config;
      stored = { id: `notification-${randomUUID()}`, ...rest };
    }
    taskConfigs.set(stored.id as string, stored);
    return stored;
  }

  /** 删除配置。configId 未指定时删除该 task 全部配置。返回是否有实际删除。 */
  deleteForOwner(
    ownerAic: string | null | undefined,
    taskId: string,
    { configId }: { configId?: string | null } = {},
  ): boolean {
    const ownerKey = this.ownerKey(ownerAic);
    const ownerStore = this.store.get(ownerKey);
    const taskConfigs = ownerStore?.get(taskId);
    if (!ownerStore || !taskConfigs || taskConfigs.size === 0) {
      return false;
    }
    if (configId !== undefined && configId !== null) {
      if (!taskConfigs.has(configId)) {
        return false;
      }
      taskConfigs.delete(configId);
      if (taskConfigs.size === 0) ownerStore.delete(taskId);
      if (ownerStore.size === 0) this.store.delete(ownerKey);
      return true;
    }
    ownerStore.delete(taskId);
    if (ownerStore.size === 0) this.store.delete(ownerKey);
    return true;
  }

  /** 查询配置。configId 给定则返回该条；否则返回 task 全部配置。 */
  getForOwner(
    ownerAic: string | null | undefined,
    taskId: string,
    { configId }: { configId?: string | null } = {},
  ): NotificationConfig[] {
    const taskConfigs = this.store.get(this.ownerKey(ownerAic))?.get(taskId);
    if (!taskConfigs) {
      return [];
    }
    if (configId !== undefined && configId !== null) {
      const config = taskConfigs.get(configId);
      return config ? [config] : [];
    }
    return [...taskConfigs.values()];
  }

  requireForOwner(
    ownerAic: string | null | undefined,
    taskId: string,
    { configId }: { configId: string },
  ): NotificationConfig {
    const configs = this.getForOwner(ownerAic, taskId, { configId });
    const config = configs[0];
    if (!config) {
      throw new NotificationConfigNotFoundError(`NotificationConfig ${configId} not found`);
    }
    return config;
  }

  delete(taskId: string, { configId }: { configId?: string | null } = {}): boolean {
    return this.deleteForOwner(null, taskId, { configId });
  }

  get(taskId: string, { configId }: { configId?: string | null } = {}): NotificationConfig[] {
    return this.getForOwner(null, taskId, { configId });
  }

  listForTask(taskId: string): NotificationConfig[] {
    return this.get(taskId);
  }
}

/** 一条订阅记录。 */
export interface NotificationSubscription {
  taskId: string;
  configId: string;
  ownerAic?: string | null;
  notifyOnStates?: TaskState[] | null;
}

/** 通知订阅注册表：记录哪些任务订阅了通知以及希望收到哪些状态的通知。 */
export class NotificationRegistry {
  private subs = new Map<string, NotificationSubscription[]>();

  /** 注册一条订阅。 */
  add(subscription: NotificationSubscription): void {
    const list = this.subs.get(subscription.taskId) ?? [];
    list.push(subscription);
    this.subs.set(subscription.taskId, list);
  }

  /** 移除一个任务的所有订阅。 */
  removeTask(taskId: string): void {
    this.subs.delete(taskId);
  }

  /** 返回命中指定 taskId 与 state 的所有订阅；notifyOnStates 为空时命中所有状态。 */
  matches(taskId: string, state: TaskState): NotificationSubscription[] {
    return (this.subs.get(taskId) ?? []).filter(
      (sub) =>
        !sub.notifyOnStates ||
        sub.notifyOnStates.length === 0 ||
        sub.notifyOnStates.includes(state),
    );
  }
}

export interface NotificationDispatcherOptions {
  localAic?: string | null;
  /** 回调 HTTPS 客户端 TLS 选项（对应 Python 的 callback_ssl_context） */
  callbackTls?: TlsClientOptions | null;
  /** 注入传输层以便测试替换真实 HTTP */
  transport?: HttpTransport | null;
  identityBindingEnabled?: boolean;
  maxRetries?: number;
  /** 退避基数（秒），对应 Python 的 backoff_s */
  backoffS?: number;
}

/** 可中止、不阻止进程退出的等待。 */
function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(done, ms);
    timer.unref();
    signal.addEventListener("abort", done, { once: true });
    function done(): void {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
  });
}

/**
 * 通知回调分发器。
 *
 * 对命中订阅，并发向各回调 URL 发送 POST 请求（请求体为 TaskResult JSON）。
 * 非 2xx 或网络异常时按指数退避重试；全部失败后记日志，不抛出。
 */
export class NotificationDispatcher {
  private readonly localAic: string | null;
  private readonly identityBindingEnabled: boolean;
  private readonly maxRetries: number;
  private readonly backoffS: number;
  private readonly transport: HttpTransport;
  private readonly closing = new AbortController();

  constructor(
    private readonly configStore: NotificationConfigStore,
    private readonly registry: NotificationRegistry,
    {
      localAic = null,
      callbackTls = null,
      transport = null,
      identityBindingEnabled = true,
      maxRetries = 3,
      backoffS = 1.0,
    }: NotificationDispatcherOptions = {},
  ) {
    this.localAic = localAic;
    this.identityBindingEnabled = identityBindingEnabled;
    this.maxRetries = maxRetries;
    this.backoffS = backoffS;
    if (identityBindingEnabled && localAic === null) {
      throw new Error("local_aic is required when identity_binding_enabled=True");
    }
    if (identityBindingEnabled && transport === null && callbackTls === null) {
      throw new Error(
        "callback_ssl_context is required when identity_binding_enabled=True and transport is not injected",
      );
    }
    if (identityBindingEnabled) {
      // 创建时就校验格式，而不是等到第一次分发才失败
      normalizeAndValidateExpectedAic(localAic, { fieldName: "local_aic" });
    }
    this.transport = transport ?? createNodeTransport(callbackTls ?? undefined);
  }

  /** 分发通知：找到匹配订阅，并发调用各回调端点。 */
  async dispatch(taskResult: TaskResult): Promise<void> {
    if (this.identityBindingEnabled) {
      assertSenderMatchesExpected(taskResult, this.localAic);
    }
    const subs = this.registry.matches(taskResult.taskId, taskResult.status.state);
    if (subs.length === 0) {
      return;
    }
    // 先解析全部配置再发送：任一配置缺失时与 Python 一样直接抛错，不发出任何回调
    const configs = subs.map((sub) =>
      this.configStore.requireForOwner(sub.ownerAic, taskResult.taskId, { configId: sub.configId }),
    );
    await Promise.allSettled(configs.map((config) => this.postOnce(config, taskResult)));
  }

  /** 向单个回调 URL 发送通知，带指数退避重试。 */
  private async postOnce(config: NotificationConfig, taskResult: TaskResult): Promise<void> {
    const headers = {
      [NOTIFICATION_TOKEN_HEADER]: config.token,
      "Content-Type": "application/json",
    };
    const body = JSON.stringify(taskResult);

    for (let attempt = 0; attempt < 1 + this.maxRetries; attempt += 1) {
      if (this.closing.signal.aborted) {
        return;
      }
      try {
        const response = await this.transport({
          url: config.url,
          method: "POST",
          headers,
          body,
          signal: this.closing.signal,
        });
        if (response.status >= 200 && response.status < 300) {
          return;
        }
        logger.warn(
          `Notification callback non-2xx: url=${config.url} status=${response.status} attempt=${attempt}`,
        );
      } catch (error) {
        logger.warn(
          `Notification callback exception: url=${config.url} attempt=${attempt} error=${String(error)}`,
        );
      }
      if (attempt < this.maxRetries) {
        await sleep(this.backoffS * 2 ** attempt * 1000, this.closing.signal);
      }
    }
    logger.warn(
      `Notification callback gave up after all retries: url=${config.url} task_id=${taskResult.taskId}`,
    );
  }

  /** 停止分发：中止进行中的回调请求与重试等待。 */
  async close(): Promise<void> {
    this.closing.abort();
  }
}

// ===========================================================================
// N2：HTTP 层
// ===========================================================================

/**
 * 通知服务的应用层回调接口。
 *
 * 「双阶段」设计（Leader 先 RPC start，再 notification/start 订阅）中 onNotificationStart 通常不设置；
 * 只有「单阶段」设计才注册此回调。
 */
export interface NotificationHandlers {
  onNotificationStart?: ((command: TaskCommand) => Promise<void>) | null;
}

export interface NotificationServiceOptions extends NotificationDispatcherOptions {
  handlers?: NotificationHandlers | null;
}

/** 聚合 store / registry / dispatcher 的服务对象。 */
export class NotificationService {
  readonly store = new NotificationConfigStore();
  readonly registry = new NotificationRegistry();
  readonly localAic: string | null;
  readonly identityBindingEnabled: boolean;
  readonly dispatcher: NotificationDispatcher;
  readonly handlers: NotificationHandlers | null;

  constructor({ handlers = null, ...options }: NotificationServiceOptions = {}) {
    this.localAic = options.localAic ?? null;
    this.identityBindingEnabled = options.identityBindingEnabled ?? true;
    this.dispatcher = new NotificationDispatcher(this.store, this.registry, options);
    this.handlers = handlers;
  }

  /** 触发与 taskResult 匹配的所有订阅回调。 */
  async dispatch(taskResult: TaskResult): Promise<void> {
    await this.dispatcher.dispatch(taskResult);
  }

  /** dispatch 的别名，与设计文档命名对齐。 */
  async notify(taskResult: TaskResult): Promise<void> {
    await this.dispatcher.dispatch(taskResult);
  }

  /** 处理 notification/set 请求的业务逻辑（不含 HTTP 解析）。 */
  async handleSet(
    req: NotificationRequest,
    { ownerAic }: { ownerAic?: string | null } = {},
  ): Promise<NotificationResponse> {
    const config = this.store.set(req.params, { ownerAic });
    return { jsonrpc: "2.0", id: req.id, result: config };
  }

  /** 处理 notification/delete，不存在时 result 为空。 */
  async handleDelete(
    req: NotificationDeleteRequest,
    { ownerAic }: { ownerAic?: string | null } = {},
  ): Promise<NotificationDeleteResponse> {
    const success = this.store.deleteForOwner(ownerAic, req.params.taskId, {
      configId: req.params.notificationConfigId,
    });
    return { jsonrpc: "2.0", id: req.id, result: success ? { success: true } : undefined };
  }

  /** 处理 notification/get 请求的业务逻辑（不含 HTTP 解析）。 */
  async handleGet(
    req: NotificationGetRequest,
    { ownerAic }: { ownerAic?: string | null } = {},
  ): Promise<NotificationGetResponse> {
    const configs = this.store.getForOwner(ownerAic, req.params.taskId, {
      configId: req.params.notificationConfigId,
    });
    return { jsonrpc: "2.0", id: req.id, result: configs };
  }

  /**
   * 处理 notification/start 的业务逻辑，返回 JSON-RPC 响应对象。
   *
   * 缺少必要字段时抛 NotificationParamsError；配置不存在抛 NotificationConfigNotFoundError。
   */
  async handleStart(
    req: NotificationStartRequest,
    { ownerAic = null }: { ownerAic?: string | null } = {},
  ): Promise<{ jsonrpc: "2.0"; id: JsonRpcId | null; result: true }> {
    const command = req.params.message;
    if (this.identityBindingEnabled) {
      assertSenderMatchesPeer(command, ownerAic);
    }
    const taskId = command.taskId;
    if (!taskId) {
      throw new NotificationParamsError("taskId is required");
    }
    const commandParams = command.commandParams ?? {};
    const configId = commandParams["notificationConfigId"];
    if (!configId || typeof configId !== "string") {
      throw new NotificationParamsError("commandParams.notificationConfigId is required");
    }
    const config = this.store.requireForOwner(ownerAic, taskId, { configId });

    let notifyOnStates: TaskState[] | null = null;
    const rawStates = commandParams["notifyOnStates"];
    if (Array.isArray(rawStates) && rawStates.length > 0) {
      const parsed = TaskStateSchema.array().safeParse(rawStates);
      notifyOnStates = parsed.success ? parsed.data : null;
    }

    this.registry.add({ taskId, configId: config.id as string, ownerAic, notifyOnStates });

    const onStart = this.handlers?.onNotificationStart;
    if (onStart) {
      void Promise.resolve()
        .then(() => onStart(command))
        .catch((error: unknown) => logger.error("on_notification_start failed", error));
    }
    // Python 返回的是普通 dict（不经 exclude_none），请求无 id 时输出 "id": null
    return { jsonrpc: "2.0", id: req.id ?? null, result: true };
  }

  /** 释放内部资源。 */
  async close(): Promise<void> {
    await this.dispatcher.close();
  }
}

/** 根据对端 AIC 解析通知配置的属主；未启用身份绑定时返回 null。 */
export function resolveNotificationOwner(
  peerAic: string | null | undefined,
  identityBindingEnabled: boolean,
): string | null {
  if (!identityBindingEnabled) {
    return null;
  }
  const normalized = normalizeAic(peerAic);
  if (normalized === null) {
    throw new PeerAicMissingError("peer AIC is required for notification identity binding");
  }
  const [valid, error] = validateAicFormat(normalized);
  if (!valid) {
    throw new InvalidPeerCertificateError(`peer AIC is invalid: ${error}`);
  }
  return normalized;
}

/** HTTP 层错误（对应 FastAPI HTTPException）。 */
class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(`HTTP ${status}`);
  }
}

function identityHttpError(error: AipIdentityError): HttpError {
  const rpcError = identityErrorToJsonRpc(error);
  return new HttpError(rpcError.code === AUTHENTICATION_REQUIRED_CODE ? 401 : 403, rpcError);
}

export type NotificationMethod = "set" | "delete" | "get" | "start";

/**
 * 与框架无关的通知端点处理：返回 `{status, body}`。
 * 对应 Python `add_aip_notification_router` 注册的四个路由。
 */
export async function handleNotificationRequest(
  service: NotificationService,
  method: NotificationMethod,
  context: { body: unknown; getPeerAic: () => string | null },
): Promise<{ status: number; body: unknown }> {
  const resolveOwner = (): string | null => {
    try {
      // 未启用身份绑定时不读取对端证书（与 RPC / 流式端点一致）
      return service.identityBindingEnabled
        ? resolveNotificationOwner(context.getPeerAic(), true)
        : null;
    } catch (error) {
      if (error instanceof AipIdentityError) {
        throw identityHttpError(error);
      }
      throw error;
    }
  };
  const parseError = (message: string): HttpError =>
    new HttpError(400, { code: -32700, message: "Parse error", data: message });

  try {
    switch (method) {
      case "set": {
        const parsed = NotificationRequestSchema.safeParse(context.body);
        if (!parsed.success) throw parseError(formatZodError(parsed.error));
        return {
          status: 200,
          body: await service.handleSet(parsed.data, { ownerAic: resolveOwner() }),
        };
      }
      case "delete": {
        const parsed = NotificationDeleteRequestSchema.safeParse(context.body);
        if (!parsed.success) throw parseError(formatZodError(parsed.error));
        const response = await service.handleDelete(parsed.data, { ownerAic: resolveOwner() });
        if (!response.result) {
          throw new HttpError(404, { code: -32001, message: "Notification config not found" });
        }
        return { status: 200, body: response };
      }
      case "get": {
        const parsed = NotificationGetRequestSchema.safeParse(context.body);
        if (!parsed.success) throw parseError(formatZodError(parsed.error));
        return {
          status: 200,
          body: await service.handleGet(parsed.data, { ownerAic: resolveOwner() }),
        };
      }
      case "start": {
        const parsed = NotificationStartRequestSchema.safeParse(context.body);
        if (!parsed.success) throw parseError(formatZodError(parsed.error));
        const ownerAic = resolveOwner();
        try {
          return { status: 200, body: await service.handleStart(parsed.data, { ownerAic }) };
        } catch (error) {
          if (error instanceof AipIdentityError) throw identityHttpError(error);
          if (error instanceof NotificationParamsError) {
            throw new HttpError(400, { code: -32602, message: error.message });
          }
          if (error instanceof NotificationConfigNotFoundError) {
            // Python 中 str(KeyError(msg)) 带单引号，保持一致
            throw new HttpError(404, { code: -32001, message: `'${error.message}'` });
          }
          throw error;
        }
      }
    }
  } catch (error) {
    if (error instanceof HttpError) {
      return { status: error.status, body: { detail: error.detail } };
    }
    throw error;
  }
}

/**
 * 创建通知端点的 HTTP 监听函数：按路径 `{basePath}/notification/{set|delete|get|start}` 分发，
 * 仅接受 POST；其他路径返回 false 交由调用方继续路由。
 */
export function createAipNotificationRequestListener(
  service: NotificationService,
  { basePath = "" }: { basePath?: string } = {},
): (req: IncomingMessage, res: ServerResponse) => Promise<boolean> {
  const prefix = basePath.replace(/\/+$/, "");
  const routes = new Map<string, NotificationMethod>(
    (["set", "delete", "get", "start"] as const).map((method) => [
      `${prefix}/notification/${method}`,
      method,
    ]),
  );
  return async (req, res) => {
    if (req.method !== "POST") {
      return false;
    }
    // 不用 new URL：形如 `http://[` 的请求目标会让它抛错；这里只需要路径部分
    const path = (req.url ?? "/").split("?", 1)[0] ?? "/";
    const method = routes.get(path);
    if (!method) {
      return false;
    }
    const body = await readJsonBody(req);
    if (!body.ok) {
      writeJson(res, 400, { detail: { code: -32700, message: "Parse error", data: body.error } });
      return true;
    }
    try {
      const result = await handleNotificationRequest(service, method, {
        body: body.value,
        getPeerAic: () => getRequestPeerAic(req),
      });
      writeJson(res, result.status, result.body);
    } catch (error) {
      logger.error("AIP notification request failed", error);
      writeJson(res, 500, { detail: "Internal Server Error" });
    }
    return true;
  };
}
