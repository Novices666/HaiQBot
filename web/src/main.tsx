// 自托管字体（OFL）：随构建产物一起分发，不依赖 CDN / 本机已装字体。
// 只取 latin 子集——中文由系统字体栈兜底（见 theme.ts 的 fontFamily）。
import "@fontsource/inter/latin-400.css";
import "@fontsource/inter/latin-500.css";
import "@fontsource/inter/latin-600.css";
import "@fontsource/jetbrains-mono/latin-400.css";

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