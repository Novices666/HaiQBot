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
      fontFamily:
        '"Inter","MiSans VF","MiSans",-apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif',
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
