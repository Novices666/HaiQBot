/// <reference types="vite/client" />

// 样式文件走 Vite 处理，TS 需要一个模块声明（否则 import "x.css" 报错）。
declare module "*.css";
