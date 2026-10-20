import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/** 读取 Python 版生成的对照样例：test/fixtures/python/<pkg>/<name>.json */
export function loadPythonFixture<T = unknown>(pkg: string, name: string): T {
  const url = new URL(`./fixtures/python/${pkg}/${name}.json`, import.meta.url);
  return JSON.parse(readFileSync(fileURLToPath(url), "utf-8")) as T;
}
