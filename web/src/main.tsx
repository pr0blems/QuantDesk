import { createRoot } from "react-dom/client";

import { apiRequest, apiStream } from "./api/client";
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

for (const source of [
  "/assets/monitor.js?v=20260810-forecast-2",
  "/assets/ai-monitor.js?v=20260824-85",
  "/assets/paper.js?v=20260809-paper-combo-1",
  "/assets/live.js?v=20260809-font1_6x-1",
  "/assets/backtest.js?v=20260809-font1_6x-1",
]) {
  const script = document.createElement("script");
  script.src = source;
  script.async = false;
  document.head.append(script);
}

const root = document.getElementById("root");

if (!root) {
  throw new Error("QuantDesk frontend root element was not found");
}

createRoot(root).render(
  <App />,
);
