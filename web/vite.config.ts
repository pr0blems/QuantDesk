import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { defineConfig, loadEnv } from "vite";
import type { Plugin } from "vite";
import react from "@vitejs/plugin-react";

const adminSourceDir = resolve(__dirname, "admin-source");

function adminUiPlugin(): Plugin {
  const adminHtml = readFileSync(resolve(adminSourceDir, "admin.html"), "utf8")
    .replace(/\/assets\/admin\.css\?v=[^"]+/, "/next/admin/admin.css")
    .replace(/\/assets\/admin\.js\?v=[^"]+/, "/next/admin/admin.js")
    .replaceAll('href="/admin"', 'href="/next/admin/"');
  const adminScript = readFileSync(resolve(adminSourceDir, "admin.js"), "utf8")
    .replaceAll('"/admin/login"', '"/next/admin/"')
    .replaceAll('`/admin#${activeView}`', '`/next/admin/#${activeView}`')
    .replaceAll('`/admin#${view}`', '`/next/admin/#${view}`');
  const adminStyles = readFileSync(resolve(adminSourceDir, "admin.css"), "utf8");

  return {
    name: "quantdesk-admin-ui",
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const path = request.url?.split("?", 1)[0];
        const assets: Record<string, { contentType: string; source: string }> = {
          "/next/admin": { contentType: "text/html; charset=utf-8", source: adminHtml },
          "/next/admin/": { contentType: "text/html; charset=utf-8", source: adminHtml },
          "/next/admin/admin.css": { contentType: "text/css; charset=utf-8", source: adminStyles },
          "/next/admin/admin.js": { contentType: "text/javascript; charset=utf-8", source: adminScript },
        };
        const asset = path ? assets[path] : undefined;
        if (!asset) return next();
        response.statusCode = 200;
        response.setHeader("Content-Type", asset.contentType);
        response.end(asset.source);
      });
    },
    generateBundle() {
      this.emitFile({ type: "asset", fileName: "admin/index.html", source: adminHtml });
      this.emitFile({ type: "asset", fileName: "admin/admin.css", source: adminStyles });
      this.emitFile({ type: "asset", fileName: "admin/admin.js", source: adminScript });
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.VITE_DEV_API_TARGET || "http://127.0.0.1:8200";

  return {
    base: "/next/",
    plugins: [react(), adminUiPlugin()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true,
        },
      },
    },
    preview: {
      host: "127.0.0.1",
      port: 4173,
      strictPort: true,
    },
    build: {
      outDir: "dist",
      sourcemap: false,
    },
  };
});
