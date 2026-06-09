import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import path from "node:path";

// The built bundle is emitted into the Python package so it ships in the wheel
// and is served by Datasette at /-/static-plugins/datasette_litestream/.
export default defineConfig({
  plugins: [svelte()],
  base: "/-/static-plugins/datasette_litestream/",
  build: {
    target: "esnext",
    outDir: path.resolve(__dirname, "../datasette_litestream"),
    assetsDir: "static/gen",
    emptyOutDir: false,
    manifest: "manifest.json",
    rollupOptions: {
      input: { main: path.resolve(__dirname, "src/main.ts") },
    },
  },
  server: {
    port: 5180,
    cors: true,
  },
});
