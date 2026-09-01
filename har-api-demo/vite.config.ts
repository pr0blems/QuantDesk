import { readFile } from "node:fs/promises";
import { defineConfig, loadEnv } from "vite";
import type { Connect, Plugin } from "vite";
import react from "@vitejs/plugin-react";

type HarHeader = { name: string; value: string };
type HarEntry = { request: { method: string; url: string; headers?: HarHeader[] } };
type HarFile = { log?: { entries?: HarEntry[] } };

const excludedHeaders = new Set(["accept-encoding", "connection", "content-length", "cookie", "host", "origin", "referer"]);

function jsonResponse(res: Connect.ServerResponse, statusCode: number, payload: unknown) {
  res.statusCode = statusCode;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify(payload));
}

function marketProxyPlugin(harPath: string): Plugin {
  let templatePromise: Promise<{ url: string; headers: Record<string, string> }> | null = null;

  const loadTemplate = () => {
    templatePromise ??= readFile(harPath, "utf8").then((text) => {
      const har = JSON.parse(text.replace(/^\uFEFF/, "")) as HarFile;
      const entry = har.log?.entries?.find((item) => {
        if (item.request.method !== "GET") return false;
        try { return new URL(item.request.url).pathname === "/market/v2/indices"; }
        catch { return false; }
      });
      if (!entry) throw new Error("HAR 中没有 /market/v2/indices 请求模板");
      const headers = Object.fromEntries((entry.request.headers ?? [])
        .filter((header) => !excludedHeaders.has(header.name.toLowerCase()))
        .map((header) => [header.name, header.value]));
      return { url: entry.request.url, headers };
    });
    return templatePromise;
  };

  const attachMiddleware = (middlewares: Connect.Server) => {
    middlewares.use("/api/market/indices", async (req, res, next) => {
      if (req.method !== "GET") return next();
      if (!harPath) return jsonResponse(res, 503, { error: "MARKET_HAR_PATH 未配置" });
      try {
        const template = await loadTemplate();
        const upstream = await fetch(template.url, {
          method: "GET",
          headers: template.headers,
          cache: "no-store",
          signal: AbortSignal.timeout(8000),
        });
        if (!upstream.ok) {
          return jsonResponse(res, upstream.status, { error: `上游行情接口返回 HTTP ${upstream.status}` });
        }
        const payload = await upstream.json() as {
          ret?: number;
          serverTime?: number;
          indices?: Array<{
            market?: string;
            marketStatus?: string;
            tradingStatus?: number;
            timeZone?: string;
            indices?: Array<{ symbol?: string; name?: string; latestPrice?: number; preClose?: number; timestamp?: number }>;
          }>;
        };
        const usMarket = payload.indices?.find((item) => item.market === "US");
        return jsonResponse(res, 200, {
          ret: payload.ret,
          serverTime: payload.serverTime,
          market: "US",
          marketStatus: usMarket?.marketStatus,
          tradingStatus: usMarket?.tradingStatus,
          timeZone: usMarket?.timeZone,
          indices: usMarket?.indices ?? [],
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : "本地行情代理请求失败";
        return jsonResponse(res, 502, { error: message });
      }
    });
  };

  return {
    name: "local-har-market-proxy",
    configureServer(server) { attachMiddleware(server.middlewares); },
    configurePreviewServer(server) { attachMiddleware(server.middlewares); },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  return {
    plugins: [react(), marketProxyPlugin(env.MARKET_HAR_PATH ?? "")],
    server: { port: 4178 },
    preview: { port: 4178 },
  };
});
