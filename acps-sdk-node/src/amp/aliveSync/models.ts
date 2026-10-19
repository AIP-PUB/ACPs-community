/**
 * AMP alive-sync 查询投影模型（对应 Python `acps_sdk.amp.alive_sync.models`）。
 *
 * AliveView 是面向查询侧的只读视图，与存储行（AliveRecord）解耦。
 */

/** 单个 AIC 的 alive 查询投影（从 agent_alive_status 行读取后构造）。 */
export interface AliveView {
  readonly aic: string;
  readonly alive: boolean;
  readonly lastSeenAt: string | null;
}

/** ADP 响应中 aliveMap 的单条值结构。 */
export interface AliveOutputDict {
  alive: boolean;
  aliveLastSeenAt: string | null;
}

/** AliveView 伴生对象（Python dataclass 上的方法）。 */
export const AliveView = {
  /**
   * 转换为 ADP 响应中 aliveMap 的单条值结构（对应 Python `AliveView.to_output_dict()`）。
   *
   * 返回示例：
   *     {"alive": true, "aliveLastSeenAt": "2026-06-13T01:20:00Z"}
   *     {"alive": false, "aliveLastSeenAt": null}
   * 键 aliveLastSeenAt 始终存在（即使值为 null），以区别于「键缺失=未知 AIC」。
   */
  toOutputDict(view: AliveView): AliveOutputDict {
    return { alive: view.alive, aliveLastSeenAt: view.lastSeenAt };
  },
} as const;
