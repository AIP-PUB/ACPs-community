/**
 * 对应 Python `tests/test_stream_model.py`（S0：aip_stream_model 调整项）。
 */

import { describe, expect, it } from "vitest";

import { TaskCommandType } from "../../src/aip/baseModel.js";
import {
  ReStreamCommandParamsSchema,
  SSE_HEADERS,
  SSE_MEDIA_TYPE,
  StreamEventDataSchema,
  StreamEventPayloadSchema,
  StreamResponseSchema,
} from "../../src/aip/streamModel.js";
import { utcNowIso } from "../../src/internal/time.js";

const NOW = utcNowIso();

const taskResult = {
  type: "task-result",
  id: "tr-1",
  sentAt: NOW,
  senderRole: "partner",
  senderId: "agent",
  taskId: "task-1",
  status: { state: "working", stateChangedAt: NOW },
};

describe("test_stream_model", () => {
  it("test_stream_response_result_optional", () => {
    const resp = StreamResponseSchema.parse({
      id: "1",
      error: { code: -32001, message: "task not found" },
    });
    expect(resp.result).toBeUndefined();
    expect(resp.error).toBeDefined();
  });

  it("test_stream_response_with_result", () => {
    const resp = StreamResponseSchema.parse({
      id: "1",
      result: { eventSeq: 1, eventData: taskResult },
    });
    expect(resp.result?.eventSeq).toBe(1);
  });

  it("test_stream_event_data_discriminated_union_task_result", () => {
    const ed = StreamEventDataSchema.parse({ eventSeq: 1, eventData: taskResult });
    expect(ed.eventData.type).toBe("task-result");
    expect(ed.eventData.type === "task-result" && ed.eventData.taskId).toBe("task-1");
  });

  it("test_stream_event_data_discriminated_union_task_command", () => {
    const ed = StreamEventDataSchema.parse({
      eventSeq: 2,
      eventData: {
        type: "task-command",
        id: "cmd-1",
        sentAt: NOW,
        senderRole: "leader",
        senderId: "leader-1",
        command: "start",
        taskId: "task-1",
      },
    });
    expect(ed.eventData.type).toBe("task-command");
    expect(ed.eventData.type === "task-command" && ed.eventData.command).toBe(
      TaskCommandType.Start,
    );
  });

  it("test_stream_event_data_discriminated_union_status_update", () => {
    const ed = StreamEventDataSchema.parse({
      eventSeq: 3,
      eventData: { ...taskResult, type: "task-status-update", id: "ev-1" },
    });
    expect(ed.eventData.type).toBe("task-status-update");
    expect(ed.eventData.type === "task-status-update" && ed.eventData.taskId).toBe("task-1");
  });

  it("test_stream_event_data_discriminated_union_product_chunk", () => {
    const ed = StreamEventDataSchema.parse({
      eventSeq: 4,
      eventData: {
        type: "product-chunk",
        id: "ev-2",
        sentAt: NOW,
        senderRole: "partner",
        senderId: "agent",
        taskId: "task-1",
        product: { id: "prod-1", dataItems: [{ type: "text", text: "hello" }] },
        append: true,
        lastChunk: false,
      },
    });
    expect(ed.eventData.type).toBe("product-chunk");
    expect(ed.eventData.type === "product-chunk" && ed.eventData.append).toBe(true);
  });

  it("requires the type discriminator (pydantic Field(discriminator='type'))", () => {
    const { type: _type, ...untyped } = taskResult;
    expect(StreamEventDataSchema.safeParse({ eventSeq: 1, eventData: untyped }).success).toBe(
      false,
    );
  });

  it("test_restream_params_parse", () => {
    expect(ReStreamCommandParamsSchema.parse({ lastEventSeq: 5 }).lastEventSeq).toBe(5);
    expect(ReStreamCommandParamsSchema.parse({ lastEventSeq: null }).lastEventSeq).toBeUndefined();
    expect(ReStreamCommandParamsSchema.parse({}).lastEventSeq).toBeUndefined();
  });

  it("test_sse_constants_defined", () => {
    expect(SSE_MEDIA_TYPE).toBe("text/event-stream");
    expect(SSE_HEADERS).toHaveProperty("Cache-Control");
    expect(SSE_HEADERS).toHaveProperty("Connection");
  });

  it("test_stream_event_payload_type_alias", () => {
    expect(StreamEventPayloadSchema).toBeDefined();
  });
});
