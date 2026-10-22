// Cloned from upstream's examples/extensions/hello-page/web/vite.config.ts:
// the SDK is a private workspace package resolved by path alias, and the
// build is one self-contained IIFE plus one stylesheet, named the way the
// manifest and upstream's asset resolver expect (dist/extension.js and
// dist/extension.css inside the Python package).
import { resolve } from "node:path";

export default {
  resolve: {
    alias: {
      "@omnigent/extension-sdk": resolve(
        import.meta.dirname,
        "../../../upstream/sdks/web-extension/src/index.ts",
      ),
    },
  },
  build: {
    outDir: resolve(import.meta.dirname, "../src/ae_omni_extension/dist"),
    emptyOutDir: true,
    lib: {
      entry: resolve(import.meta.dirname, "src/main.ts"),
      name: "AnalyticEndeavorsParkingLot",
      formats: ["iife"],
      fileName: () => "extension.js",
    },
    rollupOptions: {
      output: {
        assetFileNames: (asset) =>
          asset.names.some((name) => name.endsWith(".css")) ? "extension.css" : "[name][extname]",
      },
    },
  },
};
