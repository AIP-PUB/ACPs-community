/**
 * 对应 Python `tests/test_notification_store.py`
 * （N1：NotificationConfigStore / NotificationRegistry / NotificationDispatcher）。
 */

import { describe, expect, it } from "vitest";

import { type TaskResult, TaskState } from "../../src/aip/baseModel.js";
import type { NotificationConfig } from "../../src/aip/notificationModel.js";
import {
  NotificationConfigNotFoundError,
  NotificationConfigStore,
  NotificationDispatcher,
  NotificationRegistry,
  type NotificationSubscription,
} from "../../src/aip/notificationServer.js";
import { utcNowIso } from "../../src/internal/time.js";
import { memoryTransport } from "./_helpers.js";

const NOW = utcNowIso();

function fixedStatusTransport(status: number) {
  return memoryTransport(() => ({ status, rawBody: "" }));
}

function cfg(taskId = "t-1", url = "http://cb/notify", cfgId?: string): NotificationConfig {
  return { id: cfgId, url, token: "tok", taskId };
}

function taskResult(taskId = "t-1", state: TaskState = TaskState.Completed): TaskResult {
  return {
    type: "task-result",
    id: "tr-1",
    sentAt: NOW,
    senderRole: "partner",
    senderId: "agent",
    taskId,
    status: { state, stateChangedAt: NOW },
  };
}

describe("test_notification_store: NotificationConfigStore", () => {
  it("test_set_creates_id", () => {
    const stored = new NotificationConfigStore().set(cfg("t1"));
    expect(stored.id).toBeDefined();
    expect(stored.id?.startsWith("notification-")).toBe(true);
    // 与 pydantic model_copy 一致：生成的 id 位于首位
    expect(Object.keys(stored)[0]).toBe("id");
  });

  it("test_set_updates_existing", () => {
    const store = new NotificationConfigStore();
    const existingId = store.set(cfg("t1")).id;
    const updated = store.set({ id: existingId, url: "http://new/cb", token: "tok", taskId: "t1" });
    expect(updated.url).toBe("http://new/cb");
    expect(updated.id).toBe(existingId);
    expect(store.get("t1")).toHaveLength(1);
  });

  it("test_delete_specific", () => {
    const store = new NotificationConfigStore();
    const stored = store.set(cfg("t1"));
    expect(store.delete("t1", { configId: stored.id })).toBe(true);
    expect(store.get("t1")).toEqual([]);
  });

  it("test_delete_all_for_task", () => {
    const store = new NotificationConfigStore();
    store.set(cfg("t1"));
    store.set(cfg("t1"));
    expect(store.delete("t1", { configId: null })).toBe(true);
    expect(store.get("t1")).toEqual([]);
  });

  it("test_delete_returns_false_when_not_found", () => {
    expect(new NotificationConfigStore().delete("t-nonexistent", { configId: "nc-xxx" })).toBe(
      false,
    );
  });

  it("test_get_specific_and_all", () => {
    const store = new NotificationConfigStore();
    const c1 = store.set(cfg("t1", "http://a/cb"));
    store.set(cfg("t1", "http://b/cb"));
    const byId = store.get("t1", { configId: c1.id });
    expect(byId).toHaveLength(1);
    expect(byId[0]?.id).toBe(c1.id);
    expect(store.get("t1")).toHaveLength(2);
    expect(store.listForTask("t1")).toHaveLength(2);
  });

  it("require_for_owner raises when missing (Python KeyError)", () => {
    expect(() =>
      new NotificationConfigStore().requireForOwner(null, "t", { configId: "x" }),
    ).toThrow(NotificationConfigNotFoundError);
  });
});

describe("test_notification_store: NotificationRegistry", () => {
  it("test_registry_matches_all_states", () => {
    const registry = new NotificationRegistry();
    const sub: NotificationSubscription = { taskId: "t1", configId: "nc-1", notifyOnStates: null };
    registry.add(sub);
    expect(registry.matches("t1", TaskState.Completed)).toEqual([sub]);
    expect(registry.matches("t1", TaskState.Working)).toEqual([sub]);
  });

  it("test_registry_matches_specific_states", () => {
    const registry = new NotificationRegistry();
    const sub: NotificationSubscription = {
      taskId: "t1",
      configId: "nc-1",
      notifyOnStates: [TaskState.Completed],
    };
    registry.add(sub);
    expect(registry.matches("t1", TaskState.Completed)).toEqual([sub]);
    expect(registry.matches("t1", TaskState.Working)).toEqual([]);
  });

  it("test_registry_remove_task", () => {
    const registry = new NotificationRegistry();
    registry.add({ taskId: "t1", configId: "nc-1", notifyOnStates: null });
    registry.removeTask("t1");
    expect(registry.matches("t1", TaskState.Completed)).toEqual([]);
  });
});

describe("test_notification_store: NotificationDispatcher", () => {
  it("test_dispatcher_post_success", async () => {
    const transport = fixedStatusTransport(200);
    const registry = new NotificationRegistry();
    const store = new NotificationConfigStore();
    const stored = store.set(cfg("t1", "http://testserver/cb"));
    registry.add({ taskId: "t1", configId: stored.id as string, notifyOnStates: null });
    const dispatcher = new NotificationDispatcher(store, registry, {
      transport,
      identityBindingEnabled: false,
    });
    await dispatcher.dispatch(taskResult("t1", TaskState.Completed));
    expect(transport.requests).toHaveLength(1);
    await dispatcher.close();
  });

  it("test_dispatcher_retry_on_5xx", async () => {
    const transport = fixedStatusTransport(500);
    const registry = new NotificationRegistry();
    const store = new NotificationConfigStore();
    const stored = store.set(cfg("t1"));
    registry.add({ taskId: "t1", configId: stored.id as string, notifyOnStates: null });
    const maxRetries = 2;
    const dispatcher = new NotificationDispatcher(store, registry, {
      transport,
      identityBindingEnabled: false,
      maxRetries,
      backoffS: 0,
    });
    await dispatcher.dispatch(taskResult("t1", TaskState.Completed));
    // 1 次首发 + maxRetries 次重试
    expect(transport.requests).toHaveLength(1 + maxRetries);
    await dispatcher.close();
  });

  it("retries on transport exceptions without throwing", async () => {
    let calls = 0;
    const store = new NotificationConfigStore();
    const registry = new NotificationRegistry();
    const stored = store.set(cfg("t1"));
    registry.add({ taskId: "t1", configId: stored.id as string });
    const dispatcher = new NotificationDispatcher(store, registry, {
      identityBindingEnabled: false,
      maxRetries: 1,
      backoffS: 0,
      transport: async () => {
        calls += 1;
        throw new Error("network down");
      },
    });
    await expect(dispatcher.dispatch(taskResult("t1"))).resolves.toBeUndefined();
    expect(calls).toBe(2);
  });

  it("test_dispatcher_no_matching_subscription", async () => {
    const transport = fixedStatusTransport(200);
    const dispatcher = new NotificationDispatcher(
      new NotificationConfigStore(),
      new NotificationRegistry(),
      {
        transport,
        identityBindingEnabled: false,
      },
    );
    await dispatcher.dispatch(taskResult("t-no-sub", TaskState.Completed));
    expect(transport.requests).toHaveLength(0);
    await dispatcher.close();
  });

  it("sends nothing when any matched subscription's config is missing (Python raises KeyError first)", async () => {
    const transport = fixedStatusTransport(200);
    const store = new NotificationConfigStore();
    const registry = new NotificationRegistry();
    const stored = store.set(cfg("t1"));
    registry.add({ taskId: "t1", configId: stored.id as string });
    registry.add({ taskId: "t1", configId: "missing" });
    const dispatcher = new NotificationDispatcher(store, registry, {
      transport,
      identityBindingEnabled: false,
    });
    await expect(dispatcher.dispatch(taskResult("t1"))).rejects.toBeInstanceOf(
      NotificationConfigNotFoundError,
    );
    expect(transport.requests).toHaveLength(0);
  });

  it("requires localAic and callback TLS/transport when identity binding is enabled", () => {
    const store = new NotificationConfigStore();
    const registry = new NotificationRegistry();
    expect(
      () => new NotificationDispatcher(store, registry, { transport: fixedStatusTransport(200) }),
    ).toThrow("local_aic is required when identity_binding_enabled=True");
    expect(() => new NotificationDispatcher(store, registry, { localAic: "x" })).toThrow(
      "callback_ssl_context is required when identity_binding_enabled=True and transport is not injected",
    );
  });
});
