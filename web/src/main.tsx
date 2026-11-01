// 自托管字体（OFL）：随构建产物一起分发，不依赖 CDN / 本机已装字体。
// 只取 latin 子集——中文由系统字体栈兜底（见 theme.ts 的 fontFamily）。
import "@fontsource/inter/latin-400.css";
import "@fontsource/inter/latin-500.css";
import "@fontsource/inter/latin-600.css";
import "@fontsource/jetbrains-mono/latin-400.css";
// antd 官方 reset：浏览器默认样式（margin / box-sizing / 字体）统一由组件库接管
import "antd/dist/reset.css";

import React from "react";
import ReactDOM from "react-dom/client";
import App from "./app/App";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
