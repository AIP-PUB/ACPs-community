/**
 * `acps-sdk/amp` 入口的运行时导出名，与 Python `acps_sdk.amp.__all__` 对照。
 *
 * 期望列表由 exports.json（gen_amp.py 从 Python 包实时读取 `__all__` 并分类）按 docs/porting-notes.md 的
 * 改名规则推导，Python 侧增删导出时这里会直接失败。
 */

import { describe, expect, it } from "vitest";

import { loadPythonFixture } from "../fixtures.js";
import * as amp from "../../src/amp/index.js";

type ExportKind =
  "model" | "dataclass" | "protocol" | "enum" | "class" | "function" | "type_alias" | "constant";

interface PythonExport {
  name: string;
  kind: ExportKind;
  methods: string[];
}

const { all } = loadPythonFixture<{ all: PythonExport[] }>("amp", "exports");

const camelCase = (name: string): string =>
  name.replace(/_([a-z0-9])/g, (_match, ch: string) => ch.toUpperCase());

/** Python 导出 → Node 入口上的运行时名字（类型别名、Protocol、无方法的 dataclass 只在编译期存在）。 */
function runtimeNames({ name, kind, methods }: PythonExport): string[] {
  switch (kind) {
    case "model":
      // zod schema；Python 模型上的方法放在同名伴生对象上（如 AuditLogRecord.newLogId()）
      return methods.length > 0 ? [`${name}Schema`, name] : [`${name}Schema`];
    case "dataclass":
      // 接口；有方法时导出同名伴生对象（如 AliveView.toOutputDict()）
      return methods.length > 0 ? [name] : [];
    case "protocol":
      return [];
    case "type_alias":
      // typing.Literal 别名 → TS 联合类型 + 同名 zod 枚举 schema
      return [`${name}Schema`];
    case "function":
      return [camelCase(name)];
    default:
      return [name];
  }
}

/** Node 入口额外导出的名字：Python 版经子模块导入（trace_context、metrics_demo）或枚举的 zod schema。 */
const NODE_EXTRAS = [
  "TRACEPARENT_HEADER",
  "formatTraceparent",
  "newSpanId",
  "newTraceId",
  "parseTraceparent",
  "DemoMetricsSampler",
  "MetricFamilySchema",
];

describe("acps-sdk/amp exports", () => {
  it("runtime exports equal the renamed Python __all__ plus documented Node extras", () => {
    const expected = [...all.flatMap(runtimeNames), ...NODE_EXTRAS].sort();
    expect(Object.keys(amp).sort()).toEqual(expected);
  });

  it("companion objects carry the Python methods (camelCase)", () => {
    const exported = amp as unknown as Record<string, Record<string, unknown>>;
    for (const entry of all.filter((e) => e.methods.length > 0)) {
      for (const method of entry.methods) {
        expect(typeof exported[entry.name]?.[camelCase(method)], `${entry.name}.${method}`).toBe(
          "function",
        );
      }
    }
  });

  it("aliases point at the same schemas", () => {
    expect(amp.MessageAckSchema).toBe(amp.MessageSettlementSchema);
    expect(amp.AuditLogBodySchema).toBe(amp.AuditBodySchema);
  });

  it("record companions expose newLogId()", () => {
    for (const companion of [
      amp.HeartbeatLogRecord,
      amp.MetricsLogRecord,
      amp.AccessLogRecord,
      amp.MessageLogRecord,
      amp.AuditLogRecord,
    ]) {
      expect(companion.newLogId()).toMatch(/^[0-9a-f-]{36}$/);
    }
  });
});
