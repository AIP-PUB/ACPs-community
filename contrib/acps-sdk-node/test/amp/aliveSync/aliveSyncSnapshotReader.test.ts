/** 对应 Python tests/amp/alive_sync/test_alive_sync_snapshot_reader.py：parseMetaLine / parseSnapshotRow / snapshotRowToRecord。 */

import { describe, expect, it } from "vitest";

import {
  SnapshotProtocolError,
  parseMetaLine,
  parseSnapshotRow,
  snapshotRowToRecord,
} from "../../../src/amp/aliveSync/index.js";

const VALID_META = {
  recordType: "snapshot-meta",
  type: "amp-alive-delta",
  cutoverSeqByShard: { "hb-000": "42" },
  generatedAt: "2026-06-13T01:20:00Z",
};

const VALID_ROW = {
  shard: "hb-000",
  seq: "43",
  type: "amp-alive-delta",
  id: "urn:amp:alive:AIC-001",
  version: "43",
  op: "upsert",
  kind: "snapshot",
  payload: { aic: "AIC-001", lastSeenAt: "2026-06-13T01:19:50Z" },
};

function expectProtocolError(fn: () => unknown, pattern: RegExp): void {
  expect(fn).toThrow(SnapshotProtocolError);
  expect(fn).toThrow(pattern);
}

describe("TestParseMetaLine", () => {
  it("test_valid_meta_dict", () => {
    const meta = parseMetaLine(JSON.stringify(VALID_META));
    expect(meta.generatedAt).toBe("2026-06-13T01:20:00Z");
    expect(meta.cutoverSeqByShard).toEqual({ "hb-000": "42" });
  });
  it("test_valid_meta_bytes", () => {
    expect(parseMetaLine(Buffer.from(JSON.stringify(VALID_META))).generatedAt).toBe(
      "2026-06-13T01:20:00Z",
    );
  });
  it("test_invalid_json_raises", () => {
    expectProtocolError(() => parseMetaLine("not-json"), /JSON 解析失败/);
  });
  it("test_wrong_record_type_raises", () => {
    expectProtocolError(
      () => parseMetaLine(JSON.stringify({ ...VALID_META, recordType: "wrong" })),
      /recordType/,
    );
  });
  it("test_wrong_type_raises", () => {
    expectProtocolError(
      () => parseMetaLine(JSON.stringify({ ...VALID_META, type: "other" })),
      /type/,
    );
  });
  it("field validation failures are wrapped", () => {
    expectProtocolError(
      () => parseMetaLine(JSON.stringify({ ...VALID_META, generatedAt: 1 })),
      /snapshot meta 行字段校验失败/,
    );
  });
  // 与 Python 的差异：Python 分别抛 UnicodeDecodeError / AttributeError，Node 统一为 SnapshotProtocolError
  it("invalid UTF-8 bytes are wrapped as SnapshotProtocolError", () => {
    expectProtocolError(() => parseMetaLine(Uint8Array.from([0xff, 0xfe])), /UTF-8 解码失败/);
    expectProtocolError(() => parseSnapshotRow(Uint8Array.from([0xc3, 0x28])), /UTF-8 解码失败/);
  });
  it("non-object JSON lines are wrapped as SnapshotProtocolError", () => {
    for (const line of ["[1, 2]", "42", "null", '"text"']) {
      expectProtocolError(() => parseMetaLine(line), /不是 JSON 对象/);
      expectProtocolError(() => parseSnapshotRow(line), /不是 JSON 对象/);
    }
  });
});

describe("TestParseSnapshotRow", () => {
  it("test_valid_row", () => {
    const env = parseSnapshotRow(JSON.stringify(VALID_ROW));
    expect(env.shard).toBe("hb-000");
    expect(env.seq).toBe("43");
  });
  it("test_valid_row_bytes", () => {
    expect(parseSnapshotRow(Buffer.from(JSON.stringify(VALID_ROW))).shard).toBe("hb-000");
  });
  it("test_wrong_kind_raises", () => {
    expectProtocolError(
      () => parseSnapshotRow(JSON.stringify({ ...VALID_ROW, kind: "enter_alive" })),
      /kind/,
    );
  });
  it("test_wrong_op_raises", () => {
    expectProtocolError(
      () => parseSnapshotRow(JSON.stringify({ ...VALID_ROW, op: "delete" })),
      /op/,
    );
  });
  it("test_missing_payload_raises", () => {
    const { payload: _payload, ...bad } = VALID_ROW;
    expectProtocolError(() => parseSnapshotRow(JSON.stringify(bad)), /payload/);
  });
});

describe("TestSnapshotRowToRecord", () => {
  it("test_converts_correctly", () => {
    const record = snapshotRowToRecord(parseSnapshotRow(JSON.stringify(VALID_ROW)));
    expect(record).toEqual({
      aic: "AIC-001",
      alive: true,
      version: 43,
      shard: "hb-000",
      lastSeenAt: "2026-06-13T01:19:50Z",
    });
  });
});
