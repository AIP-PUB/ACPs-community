/**
 * 对应 Python `tests/test_notification_model.py`（N0：通知方式数据模型）。
 */

import { describe, expect, it } from "vitest";

import { TaskCommandType, TaskState } from "../../src/aip/baseModel.js";
import {
  NOTIFICATION_TOKEN_HEADER,
  NotificationConfigSchema,
  NotificationDeleteRequestSchema,
  NotificationDeleteResultSchema,
  NotificationGetRequestSchema,
  NotificationRequestSchema,
  NotificationStartParamsSchema,
  NotificationStartRequestSchema,
} from "../../src/aip/notificationModel.js";
import { utcNowIso } from "../../src/internal/time.js";

const NOW = utcNowIso();

const taskCommand = {
  id: "cmd-1",
  sentAt: NOW,
  senderRole: "leader",
  senderId: "leader-1",
  sessionId: "sess-1",
  command: TaskCommandType.Start,
  taskId: "task-1",
  commandParams: { notificationConfigId: "nc-abc" },
} as const;

describe("test_notification_model", () => {
  it("test_config_id_optional", () => {
    const cfg = NotificationConfigSchema.parse({
      url: "http://leader/cb",
      token: "tok123",
      taskId: "t-1",
    });
    expect(cfg.id).toBeUndefined();
    expect(cfg.url).toBe("http://leader/cb");
  });

  it("test_config_id_set", () => {
    const cfg = NotificationConfigSchema.parse({
      id: "nc-1",
      url: "http://leader/cb",
      token: "tok",
      taskId: "t-1",
    });
    expect(cfg.id).toBe("nc-1");
  });

  it("test_notification_request_default_method", () => {
    const req = NotificationRequestSchema.parse({
      params: { url: "http://x/cb", token: "t", taskId: "t-1" },
    });
    expect(req.method).toBe("notification/set");
  });

  it("test_delete_request_default_method", () => {
    const req = NotificationDeleteRequestSchema.parse({ params: { taskId: "t-1" } });
    expect(req.method).toBe("notification/delete");
  });

  it("test_get_request_default_method", () => {
    const req = NotificationGetRequestSchema.parse({ params: { taskId: "t-1" } });
    expect(req.method).toBe("notification/get");
  });

  it("test_delete_result_success", () => {
    expect(NotificationDeleteResultSchema.parse({}).success).toBe(true);
  });

  it("test_start_request_default_method", () => {
    const req = NotificationStartRequestSchema.parse({ params: { message: taskCommand } });
    expect(req.method).toBe("notification/start");
  });

  it("test_start_request_parse", () => {
    const req = NotificationStartRequestSchema.parse({
      jsonrpc: "2.0",
      method: "notification/start",
      id: "1",
      params: { message: { type: "task-command", ...taskCommand, command: "start" } },
    });
    expect(req.params.message.command).toBe(TaskCommandType.Start);
    expect(req.params.message.commandParams).toBeDefined();
    expect(req.params.message.commandParams?.["notificationConfigId"]).toBe("nc-abc");
  });

  it("test_start_params_notify_on_states_optional", () => {
    expect(
      NotificationStartParamsSchema.parse({ notificationConfigId: "nc-1" }).notifyOnStates,
    ).toBeUndefined();
    const p2 = NotificationStartParamsSchema.parse({
      notificationConfigId: "nc-1",
      notifyOnStates: [TaskState.Completed, TaskState.Failed],
    });
    expect(p2.notifyOnStates).toContain(TaskState.Completed);
  });

  it("test_token_header_constant", () => {
    expect(NOTIFICATION_TOKEN_HEADER).toBe("X-ACPs-AIP-Notification-Token");
  });
});
