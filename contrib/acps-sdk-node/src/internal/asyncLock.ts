/**
 * 对应 Python `asyncio.Lock` 的异步互斥锁（FIFO）。
 *
 * 语义（与 `src/aip/groupRuntime.ts` 里原有的 `AsyncLock` 一致）：
 * - `runExclusive(fn)` / `run(fn)`（同一实现的两个名字）：排在所有先到的调用之后执行 `fn`，
 *   按调用顺序先进先出；`fn` 抛错或拒绝时同样释放锁，错误原样传给调用方；
 * - `locked()`：仅当某个 `fn` 正在执行（已拿到锁、尚未结束）时为 `true`。
 *   与 asyncio 一样，前一个持有者释放后、下一个等待者恢复执行前的瞬间为 `false`。
 *
 * 不可重入：在 `fn` 内部再次调用同一把锁会永久等待（asyncio.Lock 同样如此）。
 */
export class AsyncLock {
  private tail: Promise<void> = Promise.resolve();
  private held = false;

  /** 是否有任务正持有锁，对应 `asyncio.Lock.locked()`。 */
  locked(): boolean {
    return this.held;
  }

  /** 独占执行 `fn`，对应 `async with lock:`。 */
  async runExclusive<T>(fn: () => Promise<T>): Promise<T> {
    const previous = this.tail;
    let release!: () => void;
    this.tail = new Promise<void>((resolve) => {
      release = resolve;
    });
    await previous;
    this.held = true;
    try {
      return await fn();
    } finally {
      this.held = false;
      release();
    }
  }

  /** `runExclusive` 的别名。 */
  run<T>(fn: () => Promise<T>): Promise<T> {
    return this.runExclusive(fn);
  }
}
