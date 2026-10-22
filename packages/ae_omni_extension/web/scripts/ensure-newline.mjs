// Vite writes the bundle without a trailing newline; the committed dist is
// diffed by `just ext-build`, and a file that ends without one trips
// end-of-file checks. Same script as upstream's hello-page example.
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

for (const name of ["extension.js", "extension.css"]) {
  const path = resolve(import.meta.dirname, "../../src/ae_omni_extension/dist", name);
  const content = await readFile(path, "utf8");
  if (!content.endsWith("\n")) await writeFile(path, `${content}\n`);
}
