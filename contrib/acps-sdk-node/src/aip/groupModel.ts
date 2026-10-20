/**
 * AIP v2 群组模式数据模型定义（对应 Python `acps_sdk.aip.aip_group_model`）
 *
 * - 枚举类型：GroupMgmtCommandType
 * - 群组管理命令：GroupMgmtCommand（继承自 Message）
 * - 群组管理结果：GroupMgmtResult（继承自 Message）
 * - inbox 邀请与邀请错误：InboxGroupInvitation / InboxGroupInvitationError
 * - RabbitMQ 相关配置：RabbitMQRequest / RabbitMQResponse 等
 *
 * 字段顺序与 pydantic 模型一致（子类覆盖的字段保留在父类位置），
 * 因此 `JSON.stringify(Schema.parse(x))` 与 Python `model_dump(exclude_none=True)` 的键顺序相同。
 */

import { randomUUID } from "node:crypto";

import { z } from "zod";

import { optional } from "../internal/schema.js";
import { utcNowIso } from "../internal/time.js";
import { MessageSchema, TaskCommandSchema, TaskResultSchema, messageShape } from "./baseModel.js";
import { isRecord } from "./groupRuntime.js";
import { JsonRpcIdSchema, JsonRpcResponseIdSchema } from "./rpcModel.js";

// =============================================================================
// ACS 对象
// =============================================================================

/**
 * 智能体能力说明对象
 *
 * TODO: 应该用 SDK 中的 AgentCapabilitySpec 来传递 ACS 对象。
 */
export const ACSObjectSchema = z.object({
  aic: z.string(),
});
export type ACSObject = z.output<typeof ACSObjectSchema>;

// =============================================================================
// 群组信息
// =============================================================================

/** 群组信息 */
export const GroupInfoSchema = z.object({
  groupId: z.string(),
  leader: ACSObjectSchema,
  partners: z.array(ACSObjectSchema),
});
export type GroupInfo = z.output<typeof GroupInfoSchema>;

// =============================================================================
// RabbitMQ 配置
// =============================================================================

/** RabbitMQ 服务器配置 */
export const RabbitMQServerConfigSchema = z.object({
  host: z.string(),
  port: z.number().int(),
  vhost: z.string(),
  accessToken: optional(z.string()),
});
export type RabbitMQServerConfig = z.output<typeof RabbitMQServerConfigSchema>;

/** AMQP 配置 */
export const AMQPConfigSchema = z.object({
  exchange: z.string(),
  exchangeType: z.string(),
  routingKey: z.string(),
});
export type AMQPConfig = z.output<typeof AMQPConfigSchema>;

/** RabbitMQ 请求参数 */
export const RabbitMQRequestParamsSchema = z.object({
  protocol: z.string(),
  group: GroupInfoSchema,
  server: RabbitMQServerConfigSchema,
  amqp: AMQPConfigSchema,
});
export type RabbitMQRequestParams = z.output<typeof RabbitMQRequestParamsSchema>;

/** RabbitMQ 群组加入请求（继承 JSONRPCRequest） */
export const RabbitMQRequestSchema = z.object({
  jsonrpc: z.literal("2.0").default("2.0"),
  method: z.literal("group").default("group"),
  id: optional(JsonRpcIdSchema),
  params: RabbitMQRequestParamsSchema,
});
export type RabbitMQRequest = z.output<typeof RabbitMQRequestSchema>;
export type RabbitMQRequestInput = z.input<typeof RabbitMQRequestSchema>;

/** RabbitMQ 响应结果 */
export const RabbitMQResponseResultSchema = z.object({
  connectionName: z.string(),
  vhost: z.string(),
  nodeName: z.string(),
  queueName: z.string(),
  processId: optional(z.string()),
});
export type RabbitMQResponseResult = z.output<typeof RabbitMQResponseResultSchema>;

/** RabbitMQ 响应错误数据 */
export const RabbitMQResponseErrorDataSchema = z.object({
  errorType: z.string(),
  details: optional(z.unknown()),
});
export type RabbitMQResponseErrorData = z.output<typeof RabbitMQResponseErrorDataSchema>;

/** RabbitMQ 响应错误（继承 JSONRPCError） */
export const RabbitMQResponseErrorSchema = z.object({
  code: z.number().int(),
  message: z.string(),
  data: optional(RabbitMQResponseErrorDataSchema),
});
export type RabbitMQResponseError = z.output<typeof RabbitMQResponseErrorSchema>;

/** RabbitMQ 群组加入响应（继承 JSONRPCResponse） */
export const RabbitMQResponseSchema = z.object({
  jsonrpc: z.literal("2.0").default("2.0"),
  /** 同 JSONRPCResponse：响应必须带 id，无法确定时为 null（保留 null） */
  id: JsonRpcResponseIdSchema,
  result: optional(RabbitMQResponseResultSchema),
  error: optional(RabbitMQResponseErrorSchema),
});
export type RabbitMQResponse = z.output<typeof RabbitMQResponseSchema>;
export type RabbitMQResponseInput = z.input<typeof RabbitMQResponseSchema>;

// =============================================================================
// AIP v2 群组管理类型
// =============================================================================

/**
 * AIP v2: 群组管理命令类型枚举
 *
 * 注意：v1 中此枚举名为 GroupMgmtCommand，v2 重命名为 GroupMgmtCommandType，
 * 因为 GroupMgmtCommand 现在是一个继承自 Message 的类。
 */
export const GroupMgmtCommandType = {
  GET_STATUS: "get-status",
  LEAVE_GROUP: "leave-group",
  DISBAND: "disband",
  MUTE: "mute",
  UNMUTE: "unmute",
} as const;
export type GroupMgmtCommandType = (typeof GroupMgmtCommandType)[keyof typeof GroupMgmtCommandType];
export const GroupMgmtCommandTypeSchema = z.enum(
  Object.values(GroupMgmtCommandType) as [GroupMgmtCommandType, ...GroupMgmtCommandType[]],
);

/** 群组成员状态 */
export const GroupMemberStatusSchema = z.object({
  connected: z.boolean(),
  muted: z.boolean(),
});
export type GroupMemberStatus = z.output<typeof GroupMemberStatusSchema>;

/**
 * AIP v2 群组管理命令
 *
 * 继承自 Message，用于发送群组管理相关的命令。群组管理命令必须携带 groupId 以明确目标群组。
 */
export const GroupMgmtCommandSchema = z.object({
  type: z.literal("group-mgmt-command").default("group-mgmt-command"),
  ...messageShape,
  /** 必填，覆盖基类的可选声明（键位置保持在基类中的位置） */
  groupId: z.string(),
  command: GroupMgmtCommandTypeSchema,
});
export type GroupMgmtCommand = z.output<typeof GroupMgmtCommandSchema>;
export type GroupMgmtCommandInput = z.input<typeof GroupMgmtCommandSchema>;

type MentionsCarrier = Pick<GroupMgmtCommand, "mentions">;
type CommandCarrier = Pick<GroupMgmtCommand, "command">;

/** GroupMgmtCommand 的方法（对应 Python `GroupMgmtCommand` 上的属性与实例方法）。 */
export const GroupMgmtCommand = {
  /** 是否是状态查询命令（Python `is_status_query`） */
  isStatusQuery(command: CommandCarrier): boolean {
    return command.command === GroupMgmtCommandType.GET_STATUS;
  },

  /** 是否是退出请求（Python `is_leave_request`） */
  isLeaveRequest(command: CommandCarrier): boolean {
    return command.command === GroupMgmtCommandType.LEAVE_GROUP;
  },

  /** 是否是静音命令（Python `is_mute_command`） */
  isMuteCommand(command: CommandCarrier): boolean {
    return command.command === GroupMgmtCommandType.MUTE;
  },

  /** 是否是取消静音命令（Python `is_unmute_command`） */
  isUnmuteCommand(command: CommandCarrier): boolean {
    return command.command === GroupMgmtCommandType.UNMUTE;
  },

  /**
   * 检查指定的 AIC 是否被提及（Python `is_mentioned`）
   *
   * @returns true 表示被提及（需要响应），false 表示未被提及
   */
  isMentioned(command: MentionsCarrier, aic: string): boolean {
    const mentions = command.mentions;
    if (
      mentions === undefined ||
      mentions === null ||
      (Array.isArray(mentions) && mentions.length === 0)
    ) {
      return false;
    }
    if (mentions === "all") {
      return true;
    }
    return Array.isArray(mentions) && mentions.includes(aic);
  },

  /**
   * 检查指定的 AIC 是否必须响应（Python `must_respond`）
   *
   * @returns true 表示必须响应，false 表示可选
   */
  mustRespond(command: MentionsCarrier, aic: string): boolean {
    const mentions = command.mentions;
    if (mentions === "all") {
      return true;
    }
    return Array.isArray(mentions) && mentions.includes(aic);
  },
} as const;

/**
 * AIP v2 群组管理结果
 *
 * 继承自 Message，用于返回群组成员的状态信息。群组管理结果必须携带 groupId 以明确所属群组。
 * Partner 在回复时应回显收到的 GroupMgmtCommand 中的 sessionId（而非自行生成）。
 */
export const GroupMgmtResultSchema = z.object({
  type: z.literal("group-mgmt-result").default("group-mgmt-result"),
  ...messageShape,
  /** 必填，覆盖基类的可选声明 */
  groupId: z.string(),
  status: GroupMemberStatusSchema,
});
export type GroupMgmtResult = z.output<typeof GroupMgmtResultSchema>;
export type GroupMgmtResultInput = z.input<typeof GroupMgmtResultSchema>;

/** Inbox 邀请错误数据。 */
export const GroupInvitationErrorDataSchema = RabbitMQResponseErrorDataSchema;
export type GroupInvitationErrorData = z.output<typeof GroupInvitationErrorDataSchema>;

/** Inbox 邀请错误对象。 */
export const GroupInvitationErrorSchema = z.object({
  code: z.number().int(),
  message: z.string(),
  data: optional(GroupInvitationErrorDataSchema),
});
export type GroupInvitationError = z.output<typeof GroupInvitationErrorSchema>;
export type GroupInvitationErrorInput = z.input<typeof GroupInvitationErrorSchema>;

function setDefault(data: Record<string, unknown>, key: string, value: () => unknown): void {
  if (!Object.hasOwn(data, key)) {
    data[key] = value();
  }
}

/** 对应 `InboxGroupInvitation._fill_legacy_identity_fields`（mode="before"）。 */
function fillLegacyInvitationFields(input: unknown): unknown {
  if (!isRecord(input)) {
    return input;
  }
  const data: Record<string, unknown> = { ...input };
  const group = data.group;
  let groupId = data.groupId;
  if ((groupId === null || groupId === undefined) && isRecord(group)) {
    groupId = group.groupId;
  }
  if (groupId !== null && groupId !== undefined) {
    data.groupId = groupId;
  }
  if ((data.senderId === null || data.senderId === undefined) && isRecord(group)) {
    const leader = group.leader;
    if (isRecord(leader)) {
      const leaderAic = leader.aic;
      if (leaderAic) {
        data.senderId = leaderAic;
      }
    }
  }
  setDefault(data, "id", () => `invite-${randomUUID()}`);
  setDefault(data, "sentAt", utcNowIso);
  setDefault(data, "senderRole", () => "leader");
  return data;
}

/** 通过 inbox.topic 发送的群组邀请。 */
export const InboxGroupInvitationSchema = z
  .preprocess(
    fillLegacyInvitationFields,
    z.object({
      type: z.literal("group-invitation").default("group-invitation"),
      ...messageShape,
      senderRole: z.literal("leader").default("leader"),
      groupId: z.string(),
      protocol: z.string(),
      expiresAt: z.string(),
      invitationToken: z.string(),
      group: GroupInfoSchema,
      amqp: AMQPConfigSchema,
    }),
  )
  .superRefine(
    (value, ctx) => {
      // 对应 `_validate_identity_consistency`（mode="after"）
      if (value.groupId !== value.group.groupId) {
        ctx.addIssue({ code: "custom", message: "groupId must equal group.groupId" });
        return;
      }
      if (value.senderId !== value.group.leader.aic) {
        ctx.addIssue({ code: "custom", message: "senderId must equal group.leader.aic" });
      }
    },
    { when: (payload) => payload.issues.length === 0 },
  );
export type InboxGroupInvitation = z.output<typeof InboxGroupInvitationSchema>;
/** 构造 / 解析时允许缺省 id / sentAt / senderRole / senderId / groupId（由 legacy 回填规则补齐）。 */
export type InboxGroupInvitationInput = Omit<
  InboxGroupInvitation,
  "type" | "id" | "sentAt" | "senderRole" | "senderId" | "groupId"
> &
  Partial<
    Pick<InboxGroupInvitation, "type" | "id" | "sentAt" | "senderRole" | "senderId" | "groupId">
  >;

/** 对应 `InboxGroupInvitationError._fill_legacy_sender_id`（mode="before"）。 */
function fillLegacyInvitationErrorFields(input: unknown): unknown {
  if (!isRecord(input)) {
    return input;
  }
  const data: Record<string, unknown> = { ...input };
  if (
    (data.senderId === null || data.senderId === undefined) &&
    data.partnerAic !== null &&
    data.partnerAic !== undefined
  ) {
    data.senderId = data.partnerAic;
  }
  setDefault(data, "id", () => `invite-err-${randomUUID()}`);
  setDefault(data, "sentAt", utcNowIso);
  setDefault(data, "senderRole", () => "partner");
  return data;
}

/** Partner 通过 inbox.topic 回传的邀请错误。 */
export const InboxGroupInvitationErrorSchema = z
  .preprocess(
    fillLegacyInvitationErrorFields,
    z.object({
      type: z.literal("group-invitation-error").default("group-invitation-error"),
      ...messageShape,
      senderRole: z.literal("partner").default("partner"),
      groupId: z.string(),
      partnerAic: optional(z.string()),
      invitationToken: z.string(),
      error: GroupInvitationErrorSchema,
    }),
  )
  .superRefine(
    (value, ctx) => {
      // 对应 `_validate_identity_consistency`（mode="after"）
      if (value.partnerAic !== undefined && value.partnerAic !== value.senderId) {
        ctx.addIssue({ code: "custom", message: "partnerAic must equal senderId when present" });
      }
    },
    { when: (payload) => payload.issues.length === 0 },
  );
export type InboxGroupInvitationError = z.output<typeof InboxGroupInvitationErrorSchema>;
/** 构造 / 解析时允许缺省 id / sentAt / senderRole / senderId（由 legacy 回填规则补齐）。 */
export type InboxGroupInvitationErrorInput = Omit<
  InboxGroupInvitationError,
  "type" | "id" | "sentAt" | "senderRole" | "senderId" | "error"
> &
  Partial<Pick<InboxGroupInvitationError, "type" | "id" | "sentAt" | "senderRole" | "senderId">> & {
    error: z.input<typeof GroupInvitationErrorSchema>;
  };

// =============================================================================
// 序列化
// =============================================================================

const MESSAGE_SCHEMAS: Record<string, z.ZodType> = {
  "task-command": TaskCommandSchema,
  "task-result": TaskResultSchema,
  "group-mgmt-command": GroupMgmtCommandSchema,
  "group-mgmt-result": GroupMgmtResultSchema,
  "group-invitation": InboxGroupInvitationSchema,
  "group-invitation-error": InboxGroupInvitationErrorSchema,
};

/**
 * 对应 pydantic `message.model_dump(exclude_none=True)`：按 `type` 选择模型重新校验，
 * 得到与 Python 相同的字段集合与键顺序（`JSON.stringify` 自然省略 undefined）。 @internal
 */
export function dumpGroupMessage(message: { type?: string } & object): Record<string, unknown> {
  const schema = MESSAGE_SCHEMAS[String(message.type)] ?? MessageSchema;
  return schema.parse(message) as Record<string, unknown>;
}
