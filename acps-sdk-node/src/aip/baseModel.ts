/**
 * AIP v2 基础数据模型定义（对应 Python `acps_sdk.aip.aip_base_model`）
 *
 * - 枚举类型：TaskState, TaskCommandType
 * - 数据项：DataItem (TextDataItem, FileDataItem, StructuredDataItem)
 * - 消息基类：Message
 * - 任务命令：TaskCommand (继承自 Message)
 * - 任务结果：TaskResult (继承自 Message)
 *
 * 每个模型同时导出 zod schema（`XxxSchema`，用于校验和补默认值）和同名 TS 类型（解析后的形状）。
 * 线上 JSON 字段名与 Python 版完全一致（camelCase）。
 */

import { z } from "zod";

import { AnyDict, optional } from "../internal/schema.js";

// =============================================================================
// 枚举类型
// =============================================================================

/** 任务状态枚举 */
export const TaskState = {
  Accepted: "accepted",
  Working: "working",
  AwaitingInput: "awaiting-input",
  AwaitingCompletion: "awaiting-completion",
  Completed: "completed",
  Canceled: "canceled",
  Failed: "failed",
  Rejected: "rejected",
} as const;
export type TaskState = (typeof TaskState)[keyof typeof TaskState];
export const TaskStateSchema = z.enum(Object.values(TaskState) as [TaskState, ...TaskState[]]);

/** 终态集合：进入后不能再转移。 */
export const TERMINAL_TASK_STATES: ReadonlySet<TaskState> = new Set<TaskState>([
  TaskState.Completed,
  TaskState.Failed,
  TaskState.Rejected,
  TaskState.Canceled,
]);

/**
 * AIP v2: 任务命令类型枚举
 *
 * 注意：v1 中此枚举名为 TaskCommand，v2 重命名为 TaskCommandType，
 * 因为 TaskCommand 现在是一个继承自 Message 的对象。
 */
export const TaskCommandType = {
  Get: "get",
  Start: "start",
  Continue: "continue",
  Cancel: "cancel",
  Complete: "complete",
  ReStream: "re-stream",
} as const;
export type TaskCommandType = (typeof TaskCommandType)[keyof typeof TaskCommandType];
export const TaskCommandTypeSchema = z.enum(
  Object.values(TaskCommandType) as [TaskCommandType, ...TaskCommandType[]],
);

// =============================================================================
// 数据项类型
// =============================================================================

const dataItemBaseShape = {
  metadata: optional(AnyDict),
};

/** 文本数据项 */
export const TextDataItemSchema = z.object({
  ...dataItemBaseShape,
  type: z.literal("text").default("text"),
  text: z.string(),
});
export type TextDataItem = z.output<typeof TextDataItemSchema>;

/** 文件数据项 */
export const FileDataItemSchema = z.object({
  ...dataItemBaseShape,
  type: z.literal("file").default("file"),
  name: optional(z.string()),
  mimeType: optional(z.string()),
  uri: optional(z.string()),
  bytes: optional(z.string()),
});
export type FileDataItem = z.output<typeof FileDataItemSchema>;

/** 结构化数据项 */
export const StructuredDataItemSchema = z.object({
  ...dataItemBaseShape,
  type: z.literal("data").default("data"),
  data: AnyDict,
});
export type StructuredDataItem = z.output<typeof StructuredDataItemSchema>;

const DataItemUnionSchema = z.union([
  TextDataItemSchema,
  FileDataItemSchema,
  StructuredDataItemSchema,
]);

/** pydantic 中 `DataItem = Union[TextDataItem, FileDataItem, StructuredDataItem]` 的声明顺序。 */
const DATA_ITEM_MEMBERS = [
  ["text", TextDataItemSchema],
  ["file", FileDataItemSchema],
  ["data", StructuredDataItemSchema],
] as const;

/**
 * 复刻 pydantic smart-mode union 对缺少 `type` 的数据项的选择：
 * 在能通过校验的成员中，选“输入里属于该成员字段的键”（值为 null 也算）最多的一个；
 * 数量相同时按声明顺序 Text → File → Structured。
 * 例如 `{text: "x"}` → 文本项，`{}` → 文件项，`{name, uri, text}` → 文件项，`{name, data}` → 文件项。
 */
function resolveUntypedDataItem(value: unknown): unknown {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return value;
  }
  const record = value as Record<string, unknown>;
  if (record["type"] !== undefined) {
    return value;
  }
  const keys = Object.keys(record).filter((key) => record[key] !== undefined);
  let bestType: string | null = null;
  let bestCount = -1;
  for (const [type, schema] of DATA_ITEM_MEMBERS) {
    if (!schema.safeParse(record).success) continue;
    const count = keys.filter((key) => key in schema.shape).length;
    if (count > bestCount) {
      bestType = type;
      bestCount = count;
    }
  }
  return bestType === null ? value : { ...record, type: bestType };
}

/**
 * 数据项联合类型。
 *
 * 带 `type` 时按字面量匹配；缺少 `type` 时按 pydantic smart-mode union 的规则选择成员
 * （见 `resolveUntypedDataItem`），解析结果与 Python 版一致。
 */
export const DataItemSchema = z.preprocess(
  (value: z.input<typeof DataItemUnionSchema>) => resolveUntypedDataItem(value),
  DataItemUnionSchema,
);
export type DataItem = z.output<typeof DataItemSchema>;

// =============================================================================
// 任务状态和产出物
// =============================================================================

/** 任务状态 */
export const TaskStatusSchema = z.object({
  state: TaskStateSchema,
  stateChangedAt: z.string(),
  dataItems: optional(z.array(DataItemSchema)),
});
export type TaskStatus = z.output<typeof TaskStatusSchema>;

/** 产出物 */
export const ProductSchema = z.object({
  id: z.string(),
  name: optional(z.string()),
  description: optional(z.string()),
  dataItems: z.array(DataItemSchema),
});
export type Product = z.output<typeof ProductSchema>;

// =============================================================================
// 命令参数
// =============================================================================

/** Get 命令参数 */
export const GetCommandParamsSchema = z.object({
  /** AIP v2: 从 lastMessageSentAt 重命名 */
  lastCommandSentAt: optional(z.string()),
  lastStateChangedAt: optional(z.string()),
});
export type GetCommandParams = z.output<typeof GetCommandParamsSchema>;

/** Start 命令参数 */
export const StartCommandParamsSchema = z.object({
  timeout: optional(z.number().int()),
  maxProductsBytes: optional(z.number().int()),
});
export type StartCommandParams = z.output<typeof StartCommandParamsSchema>;

// =============================================================================
// AIP v2 消息类型
// =============================================================================

export const SenderRoleSchema = z.enum(["leader", "partner"]);
export type SenderRole = z.output<typeof SenderRoleSchema>;

export const MentionsSchema = z.union([z.literal("all"), z.array(z.string())]);

/** Message 的公共字段（`type` 由各子类型放在首位，保持与 pydantic 字段顺序一致）。 */
export const messageShape = {
  id: z.string(),
  sentAt: z.string(),
  senderRole: SenderRoleSchema,
  senderId: z.string(),
  mentions: optional(MentionsSchema),
  dataItems: optional(z.array(DataItemSchema)),
  groupId: optional(z.string()),
  sessionId: optional(z.string()),
};

/**
 * AIP v2 消息基类
 *
 * 所有交互数据对象都继承自此类。Message 是中立的、双向的、无状态的。
 */
export const MessageSchema = z.object({
  type: z.string().default("message"),
  ...messageShape,
});
export type Message = z.output<typeof MessageSchema>;

/**
 * AIP v2 任务命令
 *
 * 继承自 Message，用于 Leader 向 Partner 发送任务相关的命令。
 */
export const TaskCommandSchema = z.object({
  type: z.literal("task-command").default("task-command"),
  ...messageShape,
  command: TaskCommandTypeSchema,
  commandParams: optional(AnyDict),
  /** 新建任务时可不填 */
  taskId: optional(z.string()),
});
export type TaskCommand = z.output<typeof TaskCommandSchema>;
export type TaskCommandInput = z.input<typeof TaskCommandSchema>;

/**
 * AIP v2 任务结果
 *
 * 继承自 Message，用于 Partner 向 Leader 返回任务的状态和结果。
 */
export const TaskResultSchema = z.object({
  type: z.literal("task-result").default("task-result"),
  ...messageShape,
  taskId: z.string(),
  status: TaskStatusSchema,
  products: optional(z.array(ProductSchema)),
  /** AIP v2: 从 messageHistory 重命名 */
  commandHistory: optional(z.array(TaskCommandSchema)),
  statusHistory: optional(z.array(TaskStatusSchema)),
});
export type TaskResult = z.output<typeof TaskResultSchema>;
export type TaskResultInput = z.input<typeof TaskResultSchema>;

/** 判断数据项是否为文本项。 */
export function isTextDataItem(item: DataItem): item is TextDataItem {
  return item.type === "text";
}

/** 判断数据项是否为文件项。 */
export function isFileDataItem(item: DataItem): item is FileDataItem {
  return item.type === "file";
}

/** 判断数据项是否为结构化数据项。 */
export function isStructuredDataItem(item: DataItem): item is StructuredDataItem {
  return item.type === "data";
}
