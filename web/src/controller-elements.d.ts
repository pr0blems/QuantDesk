import type { ApiRequestOptions, ApiStreamOptions } from "./api/client";

export type PageControllerName = "ai-monitor-dashboard" | "backtest-workbench" | "contract-monitor" | "live-dashboard" | "paper-dashboard" | "strategy-center";

export type PageController = {
  openResearch?: (symbol: string, timeframe: string, context?: unknown) => void;
  pause?: () => void;
  start?: () => void;
};

declare global {
  interface Window {
    quantdeskApi: (path: string, options?: ApiRequestOptions) => Promise<unknown>;
    quantdeskApiStream: (path: string, options?: ApiStreamOptions) => Promise<Response>;
    quantdeskOpenAiMonitorSocket: () => Promise<WebSocket>;
    quantdeskOpenMonitorMarketSocket: (symbol: string) => Promise<WebSocket>;
    quantdeskOpenPaperMarketSocket: (symbols: string[]) => Promise<WebSocket>;
    quantdeskHasPageController: (name: PageControllerName) => boolean;
    quantdeskMountPageController: (name: PageControllerName, host: HTMLElement) => PageController;
    quantdeskGetMountedPageController: (host: HTMLElement | null) => PageController | null;
    quantdeskUnmountPageController: (host: HTMLElement) => void;
  }
}

export {};
