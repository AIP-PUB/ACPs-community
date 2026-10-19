/**
 * 对应 Python `tests/test_stream_channel.py`（S1：TaskStreamChannel / StreamHub 纯逻辑）。
 */

import { describe, expect, it } from "vitest";

import { type TaskResult, TaskState } from "../../src/aip/baseModel.js";
import {
  type BufferedStreamEvent,
  StreamHub,
  TaskStreamChannel,
} from "../../src/aip/streamServer.js";
import { utcNowIso } from "../../src/internal/time.js";
import { flushAsync } from "./_helpers.js";

const NOW = utcNowIso();

function taskResult(state: TaskState = TaskState.Working, taskId = "t-1"): TaskResult {
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

function withTimeout<T>(promise: Promise<T>, ms = 2000): Promise<T> {
  return Promise.race([
    promise,
    new Promise<T>((_, reject) => setTimeout(() => reject(new Error("timeout")), ms)),
  ]);
}

describe("test_stream_channel: TaskStreamChannel", () => {
  it("test_next_seq_monotonic", async () => {
    const ch = new TaskStreamChannel(10);
    expect(ch.latestSeq).toBe(0);
    for (let i = 0; i < 3; i += 1) await ch.publish(taskResult());
    expect(ch.latestSeq).toBe(3);
  });

  it("test_oldest_buffered_seq", async () => {
    const ch = new TaskStreamChannel(3);
    expect(ch.oldestBufferedSeq).toBe(1);
    for (let i = 0; i < 4; i += 1) await ch.publish(taskResult());
    // 缓冲区只保留 seq 2,3,4；oldest = 2
    expect(ch.oldestBufferedSeq).toBe(2);
  });

  it("test_publish_and_replay", async () => {
    const ch = new TaskStreamChannel(10);
    for (let i = 0; i < 5; i += 1) await ch.publish(taskResult());
    expect(ch.replay(2).map((e) => e.eventSeq)).toEqual([3, 4, 5]);
  });

  it("test_replay_none_returns_all", async () => {
    const ch = new TaskStreamChannel(10);
    for (let i = 0; i < 3; i += 1) await ch.publish(taskResult());
    expect(ch.replay(null)).toHaveLength(3);
    expect(ch.replay()).toHaveLength(3);
  });

  it("test_can_resume_valid", async () => {
    const ch = new TaskStreamChannel(5);
    for (let i = 0; i < 3; i += 1) await ch.publish(taskResult());
    expect(ch.canResume(1)).toBe(true);
  });

  it("test_can_resume_expired", async () => {
    const ch = new TaskStreamChannel(3);
    for (let i = 0; i < 5; i += 1) await ch.publish(taskResult());
    // oldest=3, lastEventSeq=1 → 过期；lastEventSeq=2 (== oldest-1) 仍可续传
    expect(ch.canResume(1)).toBe(false);
    expect(ch.canResume(2)).toBe(true);
  });

  it("test_can_resume_none", async () => {
    const ch = new TaskStreamChannel(5);
    for (let i = 0; i < 3; i += 1) await ch.publish(taskResult());
    expect(ch.canResume(null)).toBe(true);
    expect(new TaskStreamChannel().canResume(99)).toBe(true); // 空缓冲始终可续传
  });

  it("test_subscribe_replays_then_live", async () => {
    const ch = new TaskStreamChannel(10);
    for (let i = 0; i < 3; i += 1) await ch.publish(taskResult());
    const received: BufferedStreamEvent[] = [];
    const collector = (async () => {
      for await (const ev of ch.subscribe(1)) {
        received.push(ev);
        if (received.length === 3) break;
      }
    })();
    await flushAsync(); // 让重放先执行（seq 2,3）
    await ch.publish(taskResult()); // 实时第 4 条
    await withTimeout(collector);
    expect(received.map((e) => e.eventSeq)).toEqual([2, 3, 4]);
  });

  it("test_close_stops_subscribe", async () => {
    const ch = new TaskStreamChannel(10);
    const task = (async () => {
      const events: BufferedStreamEvent[] = [];
      for await (const ev of ch.subscribe()) events.push(ev);
      return events;
    })();
    await flushAsync();
    await ch.publish(taskResult());
    await ch.close();
    const result = await withTimeout(task);
    expect(result.length).toBeGreaterThanOrEqual(1);
    expect(ch.isClosed).toBe(true);
  });

  it("subscribing to an already closed channel replays and ends", async () => {
    const ch = new TaskStreamChannel(10);
    await ch.publish(taskResult());
    await ch.publish(taskResult(TaskState.Completed), { isTerminal: true });
    const events: BufferedStreamEvent[] = [];
    for await (const ev of ch.subscribe()) events.push(ev);
    expect(events.map((e) => e.eventSeq)).toEqual([1, 2]);
  });

  it("test_terminal_event_closes_channel", async () => {
    const ch = new TaskStreamChannel(10);
    await ch.publish(taskResult(), { isTerminal: true });
    expect(ch.isClosed).toBe(true);
  });
});

describe("test_stream_channel: StreamHub", () => {
  it("test_hub_publish_task_result_terminal_auto", async () => {
    const hub = new StreamHub();
    hub.getOrCreateChannel("t-hub");
    await hub.publishTaskResult("t-hub", taskResult(TaskState.Completed, "t-hub"));
    const ch = hub.getChannel("t-hub");
    expect(ch).not.toBeNull();
    expect(ch?.isClosed).toBe(true);
  });

  it("explicit isTerminal overrides the state-based default", async () => {
    const hub = new StreamHub();
    await hub.publishTaskResult("t", taskResult(TaskState.Completed, "t"), { isTerminal: false });
    expect(hub.getChannel("t")?.isClosed).toBe(false);
    await hub.publishTaskResult("t", taskResult(TaskState.Working, "t"), { isTerminal: true });
    expect(hub.getChannel("t")?.isClosed).toBe(true);
  });

  it("test_hub_close_stream_removes_channel", async () => {
    const hub = new StreamHub();
    const ch = hub.getOrCreateChannel("t-remove");
    await hub.closeStream("t-remove");
    expect(hub.getChannel("t-remove")).toBeNull();
    expect(ch.isClosed).toBe(true);
  });

  it("removeChannel drops the record without closing the channel", () => {
    const hub = new StreamHub();
    const ch = hub.getOrCreateChannel("t");
    hub.removeChannel("t");
    expect(hub.getChannel("t")).toBeNull();
    expect(ch.isClosed).toBe(false);
  });
});
