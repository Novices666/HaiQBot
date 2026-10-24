/** v2 主题：双算法 + 统一 token。字体走「自托管优先、系统字体兜底」的栈。 */
import { theme as antdTheme, type ThemeConfig } from "antd";

export type ThemeMode = "light" | "dark";

const KEY = "haiq-theme";

export function readTheme(): ThemeMode {
  return localStorage.getItem(KEY) === "dark" ? "dark" : "light";
}

export function saveTheme(mode: ThemeMode): void {
  localStorage.setItem(KEY, mode);
  document.documentElement.style.colorScheme = mode;
}

export function buildTheme(mode: ThemeMode): ThemeConfig {
  const dark = mode === "dark";
  return {
    algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    token: {
      colorPrimary: "#1677FF",
      borderRadius: 6,
      fontSize: 13,
      controlHeight: 30,
      // Inter / JetBrains Mono 由 main.tsx 自托管引入（latin 子集）；
      // 中文交给系统字体栈，避免把 MB 级 CJK 字体塞进产物。
      fontFamily:
        '"Inter",-apple-system,"Segoe UI","Microsoft YaHei","PingFang SC",system-ui,sans-serif',
      fontFamilyCode: '"JetBrains Mono","Cascadia Code",Consolas,"Courier New",monospace',
    },
    components: {
      Layout: { headerHeight: 52, headerPadding: "0 20px" },
      Card: { paddingLG: 18 },
      Table: { headerBg: dark ? "#1d1d1d" : "#fafafa" },
      Menu: { itemHeight: 36 },
    },
  };
}
