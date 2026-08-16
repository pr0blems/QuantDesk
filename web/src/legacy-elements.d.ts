import type { ApiRequestOptions, ApiStreamOptions } from "./api/client";

declare global {
  interface Window {
    quantdeskApi: (path: string, options?: ApiRequestOptions) => Promise<unknown>;
    quantdeskApiStream: (path: string, options?: ApiStreamOptions) => Promise<Response>;
  }

  namespace JSX {
    interface IntrinsicElements {
      "backtest-workbench": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>;
      "contract-monitor": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>;
      "live-dashboard": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>;
      "paper-dashboard": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>;
      "strategy-center": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement>;
    }
  }
}

export {};
