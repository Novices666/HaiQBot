import React from "react";
import ReactDOM from "react-dom/client";
import V2App from "./v2/App";

// 单一面板入口。/v2 是历史路径，收敛到根路径（旧书签/浏览器历史仍能用）。
if (window.location.pathname.startsWith("/v2")) {
  const rest = window.location.pathname.replace(/^\/v2/, "") || "/";
  window.history.replaceState(null, "", rest + window.location.hash);
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <V2App />
  </React.StrictMode>,
);