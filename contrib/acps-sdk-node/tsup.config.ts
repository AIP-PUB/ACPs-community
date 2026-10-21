import { defineConfig } from "tsup";

export default defineConfig({
  entry: {
    index: "src/index.ts",
    "aic/index": "src/aic/index.ts",
    "acs/index": "src/acs/index.ts",
    "adp/index": "src/adp/index.ts",
    "aip/index": "src/aip/index.ts",
    "amp/index": "src/amp/index.ts",
    "oidc/index": "src/oidc/index.ts",
  },
  format: ["esm", "cjs"],
  dts: true,
  sourcemap: true,
  clean: true,
  target: "node20",
  splitting: true,
  treeshake: true,
});
