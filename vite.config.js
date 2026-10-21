import { writeFile } from "node:fs/promises";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [
    react(),
    {
      name: "inline-frontend-assets",
      enforce: "post",
      generateBundle(_options, bundle) {
        const htmlFile = bundle["index.html"];
        if (!htmlFile || htmlFile.type !== "asset") {
          throw new Error("The frontend build did not emit index.html.");
        }

        let html = String(htmlFile.source);
        for (const file of Object.values(bundle)) {
          if (file.type === "chunk" && file.isEntry) {
            html = html.replace(
              /<script\b[^>]*\bsrc="[^"]+"[^>]*><\/script>/,
              () => `<script type="module">${file.code}</script>`,
            );
          } else if (
            file.type === "asset" &&
            file.fileName.endsWith(".css")
          ) {
            html = html.replace(
              /<link\b[^>]*\bhref="[^"]+\.css"[^>]*>/,
              `<style>${String(file.source)}</style>`,
            );
          }
        }

        htmlFile.source = html;
      },
      async writeBundle(_options, bundle) {
        const htmlFile = bundle["index.html"];
        if (!htmlFile || htmlFile.type !== "asset") {
          throw new Error("The frontend build did not emit index.html.");
        }
        await writeFile(new URL("../index.html", import.meta.url), htmlFile.source);
      },
    },
  ],
});
