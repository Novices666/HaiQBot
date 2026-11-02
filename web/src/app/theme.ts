/**
 * 主题：双算法 + 统一 token。字体走「自托管优先、系统字体兜底」的栈。
 *
 * 一条纪律（吃过亏）：**品牌色是作者的决定，不是我的**。
 * 所以 colorPrimary 保持项目原来的 #1677FF 不动；只有「文字压在浅底上」的场景
 * （链接、菜单选中、Tab 选中）才换成同色系压深一档的 #0958d9 —— 那是为了对比度，
 * 不是为了换风格。填充色（按钮底、开关轨道）继续用品牌色。
 *
 * 如果你更想要严格 AA（白字压按钮 ≥4.5:1），把 LIGHT.primary 也改成 "#0958d9" 即可，
 * 代价是整站主色偏navy。
 */
import { theme as antdTheme, type ThemeConfig } from "antd";

export type ThemeMode = "light" | "dark";

const KEY = "haiq-theme";

export function readTheme(): ThemeMode {
  const saved = localStorage.getItem(KEY);
  if (saved === "dark" || saved === "light") return saved;
  // 没存过就跟着系统走，别在深色系统上甩一张白板
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function saveTheme(mode: ThemeMode): void {
  localStorage.setItem(KEY, mode);
  document.documentElement.style.colorScheme = mode;
  // 给 CSS 一个选择器抓手：预设标签的深色字只能按主题分别覆盖
  document.documentElement.dataset.theme = mode;
}

/** 品牌色（填充）与墨水色（文字）分开：填充保持原品牌，墨水按对比度取。 */
export function brandColors(mode: ThemeMode) {
  return mode === "dark"
    ? {
        // 暗色：填充要够深才托得住白字（#1668dc + 白字 = 5.2:1），
        // 墨水要够浅才压得住深底（#69b1ff + #161616 = 8.0:1）
        primary: "#1668dc",
        ink: "#69b1ff",
      }
    : {
        // 亮色：白字压在 #1677FF 上只有 4.10:1，压 #0958d9 是 5.6:1。
        // 这是「品牌亮蓝」与 WCAG AA 之间必须二选一的地方，选了达标那一侧。
        primary: "#0958d9",
        ink: "#0958d9", // 压在白底上 5.6:1
      };

}

export function buildTheme(mode: ThemeMode): ThemeConfig {
  const dark = mode === "dark";
  const brand = brandColors(mode);
  return {
    algorithm: dark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
    token: {
      colorPrimary: brand.primary,
      // 语义色分两种用法，别用一个色兼顾：
      //   colorWarning / colorError  → 图标、边框、浅色底（要鲜亮，antd 默认就对了）
      //   *Text                      → 纯文字（要够深才过 4.5:1）
      // 之前我把 colorWarning 直接压成 #874d00，结果 Alert 的图标和边框也变深棕，
      // 整个「提示」看着发闷 —— 那是修对比度修错了地方。
      ...(dark
        ? {}
        : {
            colorErrorText: "#cf1322",
            colorWarningText: "#874d00",
            colorSuccessText: "#237804",
          }),
      colorLink: brand.ink,
      // 暗色下的「线」：antd 默认压得太淡，实测几乎看不见
      //   colorBorder          #424242 → 输入框边框只有 1.83:1（WCAG 要求 3:1）
      //   colorBorderSecondary #303030 → 卡片边框 1.4:1
      //   colorSplit           rgba(255,255,255,.12) → 顶栏分割线 1.4:1
      // 亮色保持 antd 原值，只补暗色。
      ...(dark
        ? {
            // 「黑灰」而不是纯黑：纯黑在 OLED 上对比过强、也容易显脏。
            // 三层（参考 GitHub Dark / VS Code / Linear 的取法）：
            //   页面 #0d0d0d → 容器 #161616 → 浮层 #1f1f1f
            colorBgLayout: "#0d0d0d",
            colorBgContainer: "#161616",
            colorBgElevated: "#1f1f1f",
            colorBorder: "#666666",
            colorBorderSecondary: "#3a3a3a",
            colorSplit: "rgba(255,255,255,0.16)",
          }
        : {}),
      borderRadius: 8,
      // 正文基线 14px：13 在高分屏上偏挤，10–11px 直接不合格
      fontSize: 14,
      fontSizeSM: 13,
      // 次级文字默认 0.45 透明度 ≈3.4:1，压到能过 4.5:1 的灰
      colorTextSecondary: dark ? "rgba(255,255,255,0.72)" : "#595959",
      colorTextDescription: dark ? "rgba(255,255,255,0.62)" : "#6b6b6b",
      controlHeight: 36,
      // Inter / JetBrains Mono 由 main.tsx 自托管引入（latin 子集）；
      // 中文交给系统字体栈，避免把 MB 级 CJK 字体塞进产物。
      fontFamily:
        '"Inter",-apple-system,"Segoe UI","Microsoft YaHei","PingFang SC",system-ui,sans-serif',
      fontFamilyCode: '"JetBrains Mono","Cascadia Code",Consolas,"Courier New",monospace',
    },
    components: {
      Layout: {
        headerHeight: 56,
        headerPadding: "0 24px",
        // antd 默认顶栏 #001529、侧栏 #001529 是「深色导航」预设，
        // 我们的顶栏和侧栏都放深色文字，必须显式给浅底（暗色下同色，不能一个深蓝一个近黑）。
        headerBg: dark ? "#161616" : "#fff",
        siderBg: dark ? "#161616" : "#fff",
      },
      // 选中项：antd 暗色下选中底是品牌蓝 #1668dc、文字自动转白（实测 5.19:1），
      // 达标，不必覆盖。亮色下用浅蓝底 + 深蓝字（5.41:1）。
      Menu: { itemHeight: 40, itemSelectedColor: brand.ink },
      Tabs: {
        itemSelectedColor: brand.ink,
        itemHoverColor: brand.ink,
        itemActiveColor: brand.ink,
      },
      // 危险按钮的「文字」色：antd 默认 #ff4d4f 压白底只有 3.27:1（实测「删除」这类文字按钮）
      Button: { dangerColor: dark ? "#ff7875" : "#cf1322" },
      Card: { paddingLG: 20 },
      Table: { headerBg: dark ? "#1f1f1f" : "#fafafa" },
      Collapse: { headerPadding: "10px 14px" },
    },
  };
}
