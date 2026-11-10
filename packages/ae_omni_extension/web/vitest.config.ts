// The page's unit tests (2026-09-24): the lot's Resume flow and the card
// toggle against a stub ExtensionContext, in jsdom. Kept apart from
// vite.config.ts so the library build config stays the one upstream's
// hello-page example uses.
import { resolve } from "node:path";

export default {
  resolve: {
    alias: {
      // serverBridge.test.ts imports upstream's real request parser, which imports this module.
      "@/lib/identity": resolve(import.meta.dirname, "test/stubs/identity.ts"),
      "@omnigent/extension-sdk": resolve(
        import.meta.dirname,
        "../../../upstream/sdks/web-extension/src/index.ts",
      ),
    },
  },
  test: {
    environment: "jsdom",
    include: ["test/**/*.test.ts"],
    // Vitest blanks CSS by default; layout.test.ts reads the stylesheet's
    // rules as text (2026-09-25).
    css: { include: [/style\.css/] },
  },
};
