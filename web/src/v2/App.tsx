/** 面板 v2 外壳：会话门 + 侧栏 + 主题切换。/v2 独立于旧面板，互不影响。 */

import { useEffect, useMemo, useState } from "react";

import {

  Alert,

  Button,

  ConfigProvider,

  Flex,

  Form,

  Input,

  Layout,

  Menu,

  Space,

  Spin,

  Switch,

  Tag,

  Typography,

  message,

} from "antd";

import { ApiError, get, post, setCsrf, setUnauthorizedHandler } from "../api";

import type { MenuProps } from "antd";
import { buildTheme, readTheme, saveTheme, type ThemeMode } from "./theme";

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



const NAV = [

  { key: "overview", label: "总览" },

  { key: "plugins", label: "插件与能力" },

  { key: "workbench", label: "命令工作台" },

  { key: "config", label: "配置中心" },

  { key: "logs", label: "日志 · 审计 · 媒体" },

  { key: "channels", label: "白名单与风控" },

  { key: "secrets", label: "凭据管理" },

  { key: "jobs", label: "定时任务" },

];



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

      <div style={{ width: 380 }}>

        <Typography.Title level={3} style={{ marginBottom: 4 }}>

          HaiQBot 控制台

        </Typography.Title>

        <Typography.Paragraph type="secondary" style={{ marginBottom: 20 }}>

          粘贴面板令牌（haiq secrets export HAIQ_WEBUI_TOKEN 可取）。

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

        <Form layout="vertical" onFinish={submit}>

          <Form.Item name="token" label="令牌" rules={[{ required: true, message: "填令牌" }]}>

            <Input.Password autoFocus placeholder="••••••••" />

          </Form.Item>

          <Button type="primary" htmlType="submit" block loading={loading}>

            进入

          </Button>

        </Form>

      </div>

    </Flex>

  );

}



export default function V2App() {

  const [mode, setMode] = useState<ThemeMode>(readTheme);

  const [session, setSession] = useState<Session | null>(null);

  const [booting, setBooting] = useState(true);

  const [page, setPage] = useState<string>(() => window.location.hash.replace("#", "") || "overview");

  const [collapsed, setCollapsed] = useState(false);



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



  const goto = (key: string) => {

    setPage(key);

    window.location.hash = key;

  };



  const theme = useMemo(() => buildTheme(mode), [mode]);
  const panels = usePanels();

  // 静态页 + 插件自注册页。插件页永远排在后面，且分组显示。
  const menuItems: MenuProps["items"] = useMemo(
    () => [
      ...NAV,
      ...(panels.length > 0
        ? [
            {
              type: "group" as const,
              label: "插件页面",
              children: panels.map((panel) => ({
                key: `panel:${panel.pid}`,
                label: panel.title,
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



  return (

    <ConfigProvider theme={theme}>

      <style>

        {"html,body{margin:0;padding:0}*{box-sizing:border-box}"}

      </style>

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

        <Layout style={{ minHeight: "100vh" }}>

          <Layout.Sider

            collapsible

            collapsed={collapsed}

            onCollapse={setCollapsed}

            width={208}

            theme={mode === "dark" ? "dark" : "light"}

            style={{ borderInlineEnd: "1px solid rgba(5,5,5,0.06)" }}

          >

            <div

              style={{

                height: 52,

                lineHeight: "52px",

                paddingInline: collapsed ? 0 : 16,

                textAlign: collapsed ? "center" : "left",

                fontWeight: 600,

                fontSize: 15,

                letterSpacing: 0.2,

                whiteSpace: "nowrap",

                overflow: "hidden",

              }}

            >

              {collapsed ? "w" : "HaiQBot 控制台"}

            </div>

            <Menu

              mode="inline"

              theme={mode === "dark" ? "dark" : "light"}

              selectedKeys={[page]}

              items={menuItems}

              onClick={(info) => goto(info.key)}

            />

          </Layout.Sider>



          <Layout>

            <Layout.Header

              style={{

                display: "flex",

                alignItems: "center",

                gap: 12,

                background: mode === "dark" ? "#141414" : "#fff",

                borderBottom: "1px solid rgba(5,5,5,0.06)",

              }}

            >

              <Typography.Text strong>{titleOf(page)}</Typography.Text>

              <span style={{ flex: 1 }} />

              <Space size={8}>

                {session.write_enabled ? <Tag color="green">可写</Tag> : <Tag>只读</Tag>}

                <Switch

                  size="small"

                  checked={mode === "dark"}

                  onChange={(v) => setMode(v ? "dark" : "light")}

                  checkedChildren="暗"

                  unCheckedChildren="亮"

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



            <Layout.Content style={{ padding: 20 }}>

              <div style={{ maxWidth: 1280, margin: "0 auto" }}>{body}</div>

            </Layout.Content>

          </Layout>

        </Layout>

      )}

    </ConfigProvider>

  );

}