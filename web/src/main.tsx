import { createRoot } from "react-dom/client";

import "./controllers/controller-runtime.js";
import "./controllers/strategies.js";
import "./controllers/monitor.js";
import "./controllers/ai-monitor.js";
import "./controllers/paper.js";
import "./controllers/live.js";
import "./controllers/backtest.js";

import {
  apiRequest,
  apiStream,
  openAiMonitorWebSocket,
  openMonitorMarketWebSocket,
  openPaperMarketWebSocket,
} from "./api/client";
import { App } from "./App";
import "./styles.css";

try {
  document.documentElement.dataset.theme = window.localStorage.getItem("quantdesk.theme") === "light" ? "light" : "dark";
} catch {
  document.documentElement.dataset.theme = "dark";
}

window.quantdeskApi = (path, options = {}) => {
  const normalized = path.replace(/^\/api\/v2/, "") || "/";
  return apiRequest(normalized as `/${string}`, options);
};

window.quantdeskApiStream = (path, options = {}) => {
  const normalized = path.replace(/^\/api\/v2/, "") || "/";
  return apiStream(normalized as `/${string}`, options);
};

window.quantdeskOpenAiMonitorSocket = openAiMonitorWebSocket;
window.quantdeskOpenMonitorMarketSocket = openMonitorMarketWebSocket;
window.quantdeskOpenPaperMarketSocket = openPaperMarketWebSocket;

const root = document.getElementById("root");

if (!root) {
  throw new Error("QuantDesk frontend root element was not found");
}

createRoot(root).render(
  <App />,
);
