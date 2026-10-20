/**
 * zod 与 pydantic 语义对齐的辅助函数。
 *
 * 约定（详见 docs/porting-notes.md）：
 * - pydantic `Optional[T] = None` 字段：入参接受 `null` 或缺省，解析后统一为 `undefined`，
 *   这样 `JSON.stringify` 自然省略该字段，等价于 Python 的 `model_dump(exclude_none=True)`。
 * - 自由结构的 `Dict[str, Any]` 字段内部的 `null` 原样保留（pydantic 的 exclude_none 不会深入普通 dict）。
 */

import { z } from "zod";

const nullToUndefined = (value: unknown): unknown => (value === null ? undefined : value);

/**
 * pydantic `Optional[T] = None`：接受 null/缺省，输出 undefined。
 *
 * 运行时用 preprocess 把 null 转成 undefined（transform 会让输出类型里的键变成必填）。
 * preprocess 的输入类型是 unknown，所以这里把静态类型声明为 `ZodOptional<ZodType<输出, 输入 | null>>`：
 * 输入、输出两侧该键都是可选键，输入允许 null，输出不含 null，`XxxInput` 类型因此保持有类型。
 */
export function optional<T extends z.ZodType>(
  schema: T,
): z.ZodOptional<z.ZodType<z.output<T>, z.input<T> | null>> {
  // 外层是真正的 ZodOptional（`.unwrap()` 等方法可用），内层 preprocess 负责把 null 转成 undefined
  return z.preprocess(nullToUndefined, schema.optional()).optional() as unknown as z.ZodOptional<
    z.ZodType<z.output<T>, z.input<T> | null>
  >;
}

/** 自由结构对象 `Dict[str, Any]`。 */
export const AnyDict = z.record(z.string(), z.unknown());

/** pydantic ValidationError 的对应物：把 zod 错误展开成可读消息。 */
export function formatZodError(error: z.ZodError): string {
  return error.issues
    .map((issue) => `${issue.path.length > 0 ? issue.path.join(".") : "<root>"}: ${issue.message}`)
    .join("; ");
}
