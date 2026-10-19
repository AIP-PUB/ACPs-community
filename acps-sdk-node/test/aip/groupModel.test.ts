import { describe, expect, it } from "vitest";

import { MessageSchema } from "../../src/aip/baseModel.js";
import { assertDirectGroupRequestIdentity } from "../../src/aip/groupIdentity.js";
import {
  GroupMgmtCommand,
  GroupMgmtCommandSchema,
  GroupMgmtCommandType,
  GroupMgmtResultSchema,
  InboxGroupInvitationErrorSchema,
  InboxGroupInvitationSchema,
  RabbitMQRequestSchema,
  RabbitMQResponseSchema,
  dumpGroupMessage,
} from "../../src/aip/groupModel.js";
import { loadPythonFixture } from "../fixtures.js";
import { LEADER_AIC, OTHER_AIC, PARTNER_AIC, plain } from "./groupFakes.js";

interface ModelsFixture {
  group_mgmt_command: Record<string, unknown>;
  group_mgmt_result: Record<string, unknown>;
  inbox_invitation: Record<string, unknown>;
  legacy_invitation: { input: Record<string, unknown>; output: Record<string, unknown> };
  defaults_invitation: { input: Record<string, unknown>; output: Record<string, unknown> };
  legacy_invitation_error: { input: Record<string, unknown>; output: Record<string, unknown> };
  invitation_error: Record<string, unknown>;
  rabbitmq_request: Record<string, unknown>;
  rabbitmq_request_with_token: Record<string, unknown>;
  rabbitmq_response_ok: Record<string, unknown>;
  rabbitmq_response_error: Record<string, unknown>;
  invalid: Record<string, { model: string; input: Record<string, unknown>; error: string }>;
  mentions: Array<{ mentions: unknown; is_mentioned: boolean; must_respond: boolean }>;
  direct_identity: { ok: string; mismatch: string; missing_leader: string };
}

const fixture = loadPythonFixture<ModelsFixture>("aip_group", "models");

function group(): Record<string, unknown> {
  return {
    groupId: "group-1",
    leader: { aic: LEADER_AIC },
    partners: [{ aic: PARTNER_AIC }],
  };
}

function amqp(): Record<string, unknown> {
  return { exchange: "group-1", exchangeType: "fanout", routingKey: "" };
}

// 对应 tests/test_aip_group_models_identity.py
describe("test_aip_group_models_identity", () => {
  it("test_inbox_group_invitation_is_message_like_and_backfills_legacy_fields", () => {
    const invitation = InboxGroupInvitationSchema.parse({
      type: "group-invitation",
      protocol: "rabbitmq:4.2",
      expiresAt: "2026-06-24T00:00:00Z",
      invitationToken: "token-1",
      group: group(),
      amqp: amqp(),
    });
    expect(MessageSchema.safeParse(invitation).success).toBe(true);
    expect(invitation.senderRole).toBe("leader");
    expect(invitation.senderId).toBe(LEADER_AIC);
    expect(invitation.groupId).toBe("group-1");
    expect(invitation.id.startsWith("invite-")).toBe(true);
    expect(invitation.sentAt).toBeTruthy();
  });

  it("test_inbox_group_invitation_error_backfills_sender_from_partner_aic", () => {
    const message = InboxGroupInvitationErrorSchema.parse({
      type: "group-invitation-error",
      groupId: "group-1",
      partnerAic: PARTNER_AIC,
      invitationToken: "token-1",
      error: { code: -32020, message: "rejected" },
    });
    expect(MessageSchema.safeParse(message).success).toBe(true);
    expect(message.senderRole).toBe("partner");
    expect(message.senderId).toBe(PARTNER_AIC);
    expect(message.partnerAic).toBe(PARTNER_AIC);
    expect(message.id.startsWith("invite-err-")).toBe(true);
    expect(message.sentAt).toBeTruthy();
  });

  it("test_inbox_group_invitation_error_new_payload_does_not_require_partner_aic", () => {
    const message = InboxGroupInvitationErrorSchema.parse({
      id: "invite-err-1",
      sentAt: "2026-06-24T00:00:00Z",
      senderRole: "partner",
      senderId: PARTNER_AIC,
      groupId: "group-1",
      invitationToken: "token-1",
      error: { code: -32020, message: "rejected" },
    });
    expect(message.partnerAic).toBeUndefined();
  });

  it("test_inbox_group_invitation_rejects_sender_id_mismatch_with_group_leader", () => {
    expect(() =>
      InboxGroupInvitationSchema.parse({
        type: "group-invitation",
        id: "invite-1",
        sentAt: "2026-06-24T00:00:00Z",
        senderRole: "leader",
        senderId: PARTNER_AIC,
        groupId: "group-1",
        protocol: "rabbitmq:4.2",
        expiresAt: "2026-06-24T00:00:00Z",
        invitationToken: "token-1",
        group: group(),
        amqp: amqp(),
      }),
    ).toThrow("senderId must equal group.leader.aic");
  });

  it("test_inbox_group_invitation_rejects_group_id_mismatch_with_group_payload", () => {
    expect(() =>
      InboxGroupInvitationSchema.parse({
        type: "group-invitation",
        id: "invite-1",
        sentAt: "2026-06-24T00:00:00Z",
        senderRole: "leader",
        senderId: LEADER_AIC,
        groupId: "group-x",
        protocol: "rabbitmq:4.2",
        expiresAt: "2026-06-24T00:00:00Z",
        invitationToken: "token-1",
        group: group(),
        amqp: amqp(),
      }),
    ).toThrow("groupId must equal group.groupId");
  });

  it("test_inbox_group_invitation_error_rejects_partner_aic_sender_mismatch", () => {
    expect(() =>
      InboxGroupInvitationErrorSchema.parse({
        type: "group-invitation-error",
        id: "invite-err-1",
        sentAt: "2026-06-24T00:00:00Z",
        senderRole: "partner",
        senderId: PARTNER_AIC,
        partnerAic: LEADER_AIC,
        groupId: "group-1",
        invitationToken: "token-1",
        error: { code: -32020, message: "rejected" },
      }),
    ).toThrow("partnerAic must equal senderId when present");
  });

  it("does not mutate the caller's input during legacy backfill", () => {
    const input = {
      protocol: "rabbitmq:4.2",
      expiresAt: "x",
      invitationToken: "t",
      group: group(),
      amqp: amqp(),
    };
    InboxGroupInvitationSchema.parse(input);
    expect(Object.keys(input)).toEqual([
      "protocol",
      "expiresAt",
      "invitationToken",
      "group",
      "amqp",
    ]);
  });
});

describe("Python fixtures: aip_group/models", () => {
  it("serializes group management command / result identically (field order included)", () => {
    expect(JSON.stringify(GroupMgmtCommandSchema.parse(fixture.group_mgmt_command))).toBe(
      JSON.stringify(fixture.group_mgmt_command),
    );
    expect(JSON.stringify(GroupMgmtResultSchema.parse(fixture.group_mgmt_result))).toBe(
      JSON.stringify(fixture.group_mgmt_result),
    );
    expect(JSON.stringify(dumpGroupMessage(fixture.group_mgmt_command))).toBe(
      JSON.stringify(fixture.group_mgmt_command),
    );
  });

  it("serializes invitations and invitation errors identically", () => {
    expect(JSON.stringify(InboxGroupInvitationSchema.parse(fixture.inbox_invitation))).toBe(
      JSON.stringify(fixture.inbox_invitation),
    );
    expect(JSON.stringify(InboxGroupInvitationErrorSchema.parse(fixture.invitation_error))).toBe(
      JSON.stringify(fixture.invitation_error),
    );
  });

  it("applies the same legacy backfill rules", () => {
    expect(JSON.stringify(InboxGroupInvitationSchema.parse(fixture.legacy_invitation.input))).toBe(
      JSON.stringify(fixture.legacy_invitation.output),
    );
    expect(
      JSON.stringify(InboxGroupInvitationErrorSchema.parse(fixture.legacy_invitation_error.input)),
    ).toBe(JSON.stringify(fixture.legacy_invitation_error.output));
    // id / sentAt 由运行时生成：比较其余字段与键顺序
    const parsed = InboxGroupInvitationSchema.parse(fixture.defaults_invitation.input) as Record<
      string,
      unknown
    >;
    const expected = fixture.defaults_invitation.output;
    expect(Object.keys(parsed)).toEqual(Object.keys(expected));
    expect({ ...parsed, id: undefined, sentAt: undefined }).toEqual({
      ...expected,
      id: undefined,
      sentAt: undefined,
    });
    expect(String(parsed.id)).toMatch(/^invite-[0-9a-f-]{36}$/);
  });

  it("serializes RabbitMQ request / response identically", () => {
    for (const key of ["rabbitmq_request", "rabbitmq_request_with_token"] as const) {
      expect(JSON.stringify(RabbitMQRequestSchema.parse(fixture[key]))).toBe(
        JSON.stringify(fixture[key]),
      );
    }
    for (const key of ["rabbitmq_response_ok", "rabbitmq_response_error"] as const) {
      expect(JSON.stringify(RabbitMQResponseSchema.parse(fixture[key]))).toBe(
        JSON.stringify(fixture[key]),
      );
    }
  });

  it("rejects the same invalid payloads with the same messages", () => {
    for (const [name, testCase] of Object.entries(fixture.invalid)) {
      const schema =
        testCase.model === "InboxGroupInvitationError"
          ? InboxGroupInvitationErrorSchema
          : InboxGroupInvitationSchema;
      const result = schema.safeParse(testCase.input);
      expect(result.success, name).toBe(false);
      const pythonMessage = testCase.error;
      const issues = result.error?.issues ?? [];
      expect(issues.length, name).toBeGreaterThan(0);
      for (const issue of issues) {
        if (issue.path.length === 0) {
          // 模型级校验（model_validator）：错误文本与 Python 一致
          expect(pythonMessage, name).toContain(issue.message);
        } else {
          // 字段级校验：同一个字段出错（pydantic 在错误里列出字段名）
          expect(pythonMessage, name).toContain(`\n${String(issue.path[0])}\n`);
        }
      }
    }
  });

  it("evaluates mentions like GroupMgmtCommand.is_mentioned / must_respond", () => {
    for (const testCase of fixture.mentions) {
      const command = GroupMgmtCommandSchema.parse({
        id: "m",
        sentAt: "t",
        senderRole: "leader",
        senderId: LEADER_AIC,
        command: GroupMgmtCommandType.GET_STATUS,
        groupId: "g",
        mentions: testCase.mentions,
      });
      expect(
        GroupMgmtCommand.isMentioned(command, PARTNER_AIC),
        JSON.stringify(testCase.mentions),
      ).toBe(testCase.is_mentioned);
      expect(
        GroupMgmtCommand.mustRespond(command, PARTNER_AIC),
        JSON.stringify(testCase.mentions),
      ).toBe(testCase.must_respond);
      expect(GroupMgmtCommand.isStatusQuery(command)).toBe(true);
      expect(GroupMgmtCommand.isLeaveRequest(command)).toBe(false);
      expect(GroupMgmtCommand.isMuteCommand({ command: "mute" })).toBe(true);
      expect(GroupMgmtCommand.isUnmuteCommand({ command: "unmute" })).toBe(true);
    }
  });

  it("checks direct group request identity like Python", () => {
    const request = plain(RabbitMQRequestSchema.parse(fixture.rabbitmq_request));
    expect(assertDirectGroupRequestIdentity(request, { peerAic: LEADER_AIC.toLowerCase() })).toBe(
      fixture.direct_identity.ok,
    );
    expect(() => assertDirectGroupRequestIdentity(request, { peerAic: OTHER_AIC })).toThrow(
      fixture.direct_identity.mismatch,
    );
    expect(() =>
      assertDirectGroupRequestIdentity({ params: null }, { peerAic: OTHER_AIC }),
    ).toThrow(fixture.direct_identity.missing_leader);
  });
});
