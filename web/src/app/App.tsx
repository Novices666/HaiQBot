/**
 * 面板外壳：会话门 + 侧栏 + 主题切换。
 *
 * 排版契约（改这里之前先读）：
 * - 顶栏高度只有一个来源：theme.ts 的 Layout.headerHeight（56），组件里不再写死数字。
 * - 导航项永远「图标 + 文字」；折叠态只藏文字，不藏图标。
 * - 手机（<md）走抽屉；触控目标一律 ≥44px，桌面 ≥28px。
 * - 焦点可见、可跳过导航；换页后焦点落到 main，读屏用户不会在侧栏里迷路。
 */
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Alert, Button, ConfigProvider, Drawer, Flex, Form, Grid, Input, Layout, Menu, message, Space, Spin, Switch, Tag, Typography } from "antd";
import type { MenuProps } from "antd";
import zhCN from "antd/locale/zh_CN";

import { ApiError, get, post, setCsrf, setUnauthorizedHandler } from "../api";
import { buildTheme, readTheme, saveTheme, type ThemeMode } from "./theme";
import {
  AppstoreOutlined,
  ClockCircleOutlined,
  CodeOutlined,
  DashboardOutlined,
  FileTextOutlined,
  KeyOutlined,
  MenuOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
} from "@ant-design/icons";

import OverviewPage from "./pages/OverviewPage";
import PluginsPage from "./pages/PluginsPage";
import ConfigPage from "./pages/ConfigPage";
import WorkbenchPage from "./pages/WorkbenchPage";
import ObservabilityPage from "./pages/ObservabilityPage";
import ChannelsPage from "./pages/ChannelsPage";
import SecretsPage from "./pages/SecretsPage";
import JobsPage from "./pages/JobsPage";
import PanelRenderer, { usePanels } from "./panels";

type Session = { csrf: string | null; write_enabled: boolean; token_configured: boolean };

type NavItem = { key: string; label: string; icon: ReactNode };

const NAV: NavItem[] = [
  { key: "overview", label: "总览", icon: <DashboardOutlined /> },
  { key: "plugins", label: "插件与能力", icon: <AppstoreOutlined /> },
  { key: "workbench", label: "命令工作台", icon: <CodeOutlined /> },
  { key: "config", label: "配置中心", icon: <SettingOutlined /> },
  { key: "logs", label: "日志 · 审计 · 媒体", icon: <FileTextOutlined /> },
  { key: "channels", label: "白名单与风控", icon: <SafetyCertificateOutlined /> },
  { key: "secrets", label: "凭据管理", icon: <KeyOutlined /> },
  { key: "jobs", label: "定时任务", icon: <ClockCircleOutlined /> },
];

/**
 * 全局样式：只放 antd 的 token / 组件能力表达不了的东西。
 * 三类例外：原生无障碍元素（跳转链接、main 焦点）、文本换行规则、媒体查询。
 * 其余一律交给 theme.ts 的 token 和 antd 自己的 reset（main.tsx 已引 antd/dist/reset.css）。
 */
const GLOBAL_CSS = `
  /* 页面底色：antd 的 reset 不设 body 背景，暗色下 body 透明会让浏览器默认色透出来
     （某些环境下是白的，切主题会闪一下）。这是原生元素，token 管不到，只能写死。 */
  :root[data-theme="light"] body { background: #ffffff; }
  :root[data-theme="dark"] body { background: #0d0d0d; }
  /* 跳过导航：键盘用户第一下 Tab 就能直达内容（原生 <a>，antd 没有对应组件） */
  .haiq-skip {
    position: fixed; top: 8px; left: 8px; z-index: 2000;
    transform: translateY(-200%); transition: transform .12s ease;
    padding: 10px 16px; border-radius: 8px;
    background: var(--ant-color-link); color: #fff; font-size: 14px; text-decoration: none;
  }
  .haiq-skip:focus-visible { transform: translateY(0); }
  /* main 只是为了程序化聚焦，不需要画焦点环 */
  #main:focus, #main:focus-visible { outline: none; }

  /* 数字等宽：表格与统计值不跳动（antd 没有这个 token） */
  .ant-table, .ant-statistic-content { font-variant-numeric: tabular-nums; }

  /* 长路径 / 长 ID / 长键名必须能断，否则会把表格或卡片顶出屏幕 */
  .ant-typography code, .ant-typography pre, code { overflow-wrap: anywhere; }
  .ant-form-item-additional, .ant-form-item-extra { max-width: 100%; overflow-wrap: anywhere; }

  /* 亮色下只把预设标签的文字压深（antd 预设色文字只有 3.4:1）。
     底色和边框继续用 antd 的原值，改的越少越不像"被覆盖过"。 */
  :root[data-theme="light"] .ant-tag.ant-tag-green:is(.ant-tag-filled, .ant-tag-outlined) { color: #1c6b0b; }
  :root[data-theme="light"] .ant-tag.ant-tag-blue:is(.ant-tag-filled, .ant-tag-outlined) { color: #0958d9; }
  :root[data-theme="light"] .ant-tag.ant-tag-red:is(.ant-tag-filled, .ant-tag-outlined) { color: #a80a1d; }
  :root[data-theme="light"] .ant-tag.ant-tag-orange:is(.ant-tag-filled, .ant-tag-outlined) { color: #874500; }
  :root[data-theme="light"] .ant-tag.ant-tag-gold:is(.ant-tag-filled, .ant-tag-outlined) { color: #7a4d00; }
  :root[data-theme="light"] .ant-tag.ant-tag-cyan:is(.ant-tag-filled, .ant-tag-outlined) { color: #00545c; }
  :root[data-theme="light"] .ant-tag.ant-tag-purple:is(.ant-tag-filled, .ant-tag-outlined) { color: #4f1ba3; }
  :root[data-theme="light"] .ant-tag.ant-tag-magenta:is(.ant-tag-filled, .ant-tag-outlined) { color: #93135f; }

  /* 危险「文字按钮」（type=link / type=text + danger）取的是 colorError，
     压白底只有 3.27:1。但 colorError 同时是 Alert 图标与边框的颜色，
     全局压深会让所有提示发闷（踩过），所以只对文字型按钮补这一档。
     实心危险按钮不动：白字压红底是 antd 全站现状，视觉上并不异常。 */
  :root[data-theme="light"] .ant-btn-dangerous.ant-btn-link,
  :root[data-theme="light"] .ant-btn-dangerous.ant-btn-text,
  :root[data-theme="light"] .ant-btn-dangerous.ant-btn-variant-link,
  :root[data-theme="light"] .ant-btn-dangerous.ant-btn-variant-text { color: #cf1322; }
  :root[data-theme="dark"] .ant-btn-dangerous.ant-btn-link,
  :root[data-theme="dark"] .ant-btn-dangerous.ant-btn-text,
  :root[data-theme="dark"] .ant-btn-dangerous.ant-btn-variant-link,
  :root[data-theme="dark"] .ant-btn-dangerous.ant-btn-variant-text { color: #ff7875; }

  /* 手机：触控目标 ≥44px（antd 的 token 是全局的，移动端只能靠媒体查询收口） */
  @media (max-width: 767px) {
    .ant-btn, .ant-input, .ant-input-number, .ant-select .ant-select-selector,
    .ant-input-affix-wrapper, .ant-picker { min-height: 44px; }
    .ant-btn-sm { min-height: 36px; }
    .ant-card-body { padding: 14px; }
    .ant-drawer .ant-menu-item { height: 44px; line-height: 44px; }
  }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: 0.01ms !important;
      transition-duration: 0.01ms !important;
      scroll-behavior: auto !important;
    }
  }
`;

function Login({ onDone }: { onDone: (s: Session) => void }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const submit = async (values: { token: string }) => {
    setLoading(true);
    setError(null);
    try {
      const result = await post<{ csrf: string; write_enabled: boolean }>("/api/auth/login", {
        token: values.token.trim(),
      });
      setCsrf(result.csrf);
      onDone({ csrf: result.csrf, write_enabled: result.write_enabled, token_configured: true });
    } catch (exc) {
      setError(exc as ApiError);
    } finally {
      setLoading(false);
    }
  };

  return (
    <Flex align="center" justify="center" style={{ minHeight: "100vh", padding: 24 }}>
      <div style={{ width: "100%", maxWidth: 384 }}>
        <Flex align="center" gap={10} style={{ marginBottom: 6 }}>
          <span style={{ color: "var(--ant-color-link)", display: "inline-flex" }}>
            <RobotOutlined style={{ fontSize: 24 }} />
          </span>
          <Typography.Title level={3} style={{ margin: 0 }}>
            HaiQBot 控制台
          </Typography.Title>
        </Flex>
        <Typography.Paragraph type="secondary" style={{ marginBottom: 20 }}>
          粘贴面板令牌。在运行 haiqbot 的机器上执行 haiq webui token --show 取令牌；确实要换新的才用
          --generate（旧令牌与所有会话立即失效，不用重启进程）。
        </Typography.Paragraph>
        {error ? (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message={error.message}
            description={error.hint ?? (error.code === "token_missing" ? "先在机器上生成令牌" : undefined)}
          />
        ) : null}
        <Form layout="vertical" onFinish={submit} requiredMark={false}>
          <Form.Item
            name="token"
            label="令牌"
            rules={[{ required: true, message: "填令牌" }]}
            extra="令牌只在本机浏览器与面板进程之间传递，不会外发。"
          >
            <Input.Password autoFocus placeholder="••••••••" autoComplete="current-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={loading} size="large">
            进入
          </Button>
        </Form>
      </div>
    </Flex>
  );
}

export default function App() {
  const [mode, setMode] = useState<ThemeMode>(readTheme);
  const [session, setSession] = useState<Session | null>(null);
  const [booting, setBooting] = useState(true);
  const [page, setPage] = useState<string>(() => window.location.hash.replace("#", "") || "overview");
  const [collapsed, setCollapsed] = useState(false);
  const [navOpen, setNavOpen] = useState(false);

  /** 手机断点：md（768px）以下走抽屉导航，不再固定侧栏。 */
  const screens = Grid.useBreakpoint();
  const isMobile = screens.md === false;

  useEffect(() => {
    saveTheme(mode);
  }, [mode]);

  // 浏览器前进/后退或手改 hash 时同步页面（同文档跳转不会重新挂载组件）
  useEffect(() => {
    const onHash = () => setPage(window.location.hash.replace("#", "") || "overview");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => setSession(null));
    const boot = async () => {
      try {
        const info = await get<Session>("/api/auth/session");
        if (info.csrf) setCsrf(info.csrf);
        setSession(info);
      } catch {
        setSession(null);
      } finally {
        setBooting(false);
      }
    };
    void boot();
    return () => setUnauthorizedHandler(null);
  }, []);

  // 换页后把焦点交给内容区：读屏用户不必再从侧栏 Tab 一遍
  useEffect(() => {
    if (booting || session === null) return;
    document.getElementById("main")?.focus({ preventScroll: true });
  }, [page, booting, session]);

  const goto = (key: string) => {
    setPage(key);
    window.location.hash = key;
  };

  const theme = useMemo(() => buildTheme(mode), [mode]);
  const panels = usePanels();

  /** 静态页 + 插件自注册页。插件页永远排在后面，且分组显示。 */
  const menuItems: MenuProps["items"] = useMemo(
    () => [
      ...NAV.map((item) => ({ key: item.key, label: item.label, icon: item.icon })),
      ...(panels.length > 0
        ? [
            {
              type: "group" as const,
              label: "插件页面",
              children: panels.map((panel) => ({
                key: `panel:${panel.pid}`,
                label: panel.title,
                icon: <AppstoreOutlined />,
              })),
            },
          ]
        : []),
    ],
    [panels],
  );

  const titleOf = (key: string) =>
    NAV.find((item) => item.key === key)?.label ??
    panels.find((panel) => `panel:${panel.pid}` === key)?.title ??
    "面板";

  const body = useMemo(() => {
    // 插件自注册页：内核不认识它，只按声明渲染
    if (page.startsWith("panel:")) {
      return <PanelRenderer pid={page.slice("panel:".length)} />;
    }
    switch (page) {
      case "plugins":
        return <PluginsPage />;
      case "workbench":
        return <WorkbenchPage />;
      case "config":
        return <ConfigPage />;
      case "logs":
        return <ObservabilityPage />;
      case "channels":
        return <ChannelsPage />;
      case "secrets":
        return <SecretsPage />;
      case "jobs":
        return <JobsPage />;
      default:
        return <OverviewPage />;
    }
  }, [page]);

  const nav = (
    <Menu
      mode="inline"
      selectedKeys={[page]}
      items={menuItems}
      style={{ borderInlineEnd: "none" }}
      onClick={(info) => {
        goto(info.key);
        setNavOpen(false);
      }}
    />
  );

  const brand = (showText: boolean) => (
    <Flex
      align="center"
      justify={showText ? "flex-start" : "center"}
      gap={10}
      style={{
        height: 56,
        paddingInline: showText ? 16 : 0,
        color: "var(--ant-color-link)",
        fontWeight: 600,
        fontSize: 15,
        letterSpacing: 0.2,
        whiteSpace: "nowrap",
        overflow: "hidden",
        borderBottom: "1px solid var(--ant-color-split)",
      }}
    >
      <RobotOutlined style={{ fontSize: 20 }} />
      {showText ? <span style={{ color: "inherit" }}>HaiQBot 控制台</span> : null}
    </Flex>
  );

  return (
    <ConfigProvider theme={theme} locale={zhCN}>
      <style>{GLOBAL_CSS}</style>

      {booting ? (
        <Flex align="center" justify="center" style={{ minHeight: "100vh" }}>
          <Spin />
        </Flex>
      ) : session === null ? (
        <Login
          onDone={(next) => {
            setSession(next);
            message.success("已登录");
          }}
        />
      ) : (
        <Layout style={{ height: "100vh", overflow: "hidden" }}>
          <a className="haiq-skip" href="#main">
            跳到主内容
          </a>

          {!isMobile && (
            <Layout.Sider
              collapsible
              collapsed={collapsed}
              onCollapse={setCollapsed}
              width={208}
              // Sider 的 theme 默认值就是 "dark"（深色导航预设：容器 #001529、折叠按钮 #002140），
              // 全局暗色模式由 ConfigProvider 的算法管，这里必须显式写 light，否则侧栏会出现第二种颜色。
              theme="light"
              style={{ borderInlineEnd: "1px solid var(--ant-color-split)" }}
            >
              {brand(!collapsed)}
              {nav}
            </Layout.Sider>
          )}

          <Layout>
            <Layout.Header
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                borderBottom: "1px solid var(--ant-color-split)",
              }}
            >
              {isMobile ? (
                <Button
                  type="text"
                  aria-label="打开导航"
                  onClick={() => setNavOpen(true)}
                  style={{ width: 44, height: 44, display: "inline-flex", alignItems: "center", justifyContent: "center", marginInlineStart: -8 }}
                >
                  <MenuOutlined />
                </Button>
              ) : null}

              <Typography.Text strong style={{ minWidth: 0 }} ellipsis>
                {titleOf(page)}
              </Typography.Text>

              <span style={{ flex: 1 }} />

              <Space size={8}>
                {session.write_enabled ? <Tag color="green">可写</Tag> : <Tag>只读</Tag>}
                <Switch
                  checked={mode === "dark"}
                  onChange={(v) => setMode(v ? "dark" : "light")}
                  aria-label="暗色模式"
                />
                <Button
                  size="small"
                  onClick={async () => {
                    try {
                      await post("/api/auth/logout");
                    } catch {
                      /* 本地会话照样清掉 */
                    }
                    setCsrf("");
                    setSession(null);
                  }}
                >
                  退出
                </Button>
              </Space>
            </Layout.Header>

            <Layout.Content style={{ padding: isMobile ? 12 : 20, overflow: "auto" }}>
              <main
                id="main"
                tabIndex={-1}
                style={{ maxWidth: 1280, margin: "0 auto", outline: "none" }}
              >
                {body}
              </main>
            </Layout.Content>
          </Layout>

          {isMobile ? (
            <Drawer
              placement="left"
              open={navOpen}
              onClose={() => setNavOpen(false)}
              width={264}
              title={
                <Flex align="center" gap={8} style={{ color: "var(--ant-color-link)" }}>
                  <RobotOutlined style={{ fontSize: 18 }} />
                  <span style={{ color: "inherit", fontSize: 15 }}>HaiQBot</span>
                </Flex>
              }
              styles={{ body: { padding: 0 } }}
            >
              {nav}
            </Drawer>
          ) : null}
        </Layout>
      )}
    </ConfigProvider>
  );
}
