import { defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";

export default defineConfig({
  plugins: [svelte({ hot: false })],
  // Under vitest, resolve Svelte's *browser* (client) build so mount() works in
  // jsdom — without this, Svelte resolves its SSR entry and mount() throws
  // "lifecycle_function_unavailable".
  resolve: {
    conditions: ["browser"],
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
});
