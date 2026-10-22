/// <reference types="vite/client" />

// 动态 import 的样式文件需要模块声明（v2 只吃 antd reset，旧面板吃 styles.css）。
declare module "*.css";