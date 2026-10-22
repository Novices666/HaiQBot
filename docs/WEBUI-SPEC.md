# 面板（WebUI）规格

前端：`web/`（Vite 5 + React 18 + TypeScript，零运行时依赖除 react/react-router）
后端：`src/haiqbot/webui/`（Starlette）
产物：`src/haiqbot/webui/dist/`（**入库**，随 wheel 分发，部署机不需要 node）

---

## 1. 设计取舍（先说清楚为什么长这样）

| 决定 | 理由 |
|---|---|
| 单令牌 + 会话 Cookie | 单用户工具，做多用户/角色是过度设计；但"读也公开"不行，读接口会泄露配置与审计 |
| 无状态会话（HMAC 签名） | 没有服务端会话表，重启即失效；换令牌 = 踢掉所有会话 |
| 写操作额外要 `X-Haiq-CSRF` | SameSite=Lax 已挡大部分跨站；反代场景下不保险，双保险成本极低 |
| 轮询而非 SSE/WS | 面板要的是"刚刚发生了什么"，1~3 秒粒度够；少一层长连接故障面 |
| **密钥真值永不经过面板** | 只展示"是否已设置"+ 复制命令，真值只在 vault |
| 工作台只预演 | 不调用 `rule.invoker`，否则面板就变成"远程执行入口" |
| dist 入库 | 部署与 CI 不需要 node；`npm run build` 只在前端改动时跑 |

---

## 2. 认证

```
POST /api/auth/login   {token}         → 200 {csrf, expires_at, write_enabled} + Set-Cookie(haiq_session)
POST /api/auth/logout                  → 清 Cookie
GET  /api/auth/session                 → 当前会话信息（前端启动时调它判断是否已登录）
```

- Cookie：`HttpOnly` + `SameSite=Lax` + `Path=/`，TTL 12h；HTTPS（或反代 `X-Forwarded-Proto: https`）才加 `Secure`
- 令牌：vault 的 `webui.token_ref`（默认 `WEFT_WEBUI_TOKEN`），**每次请求现取** → `haiq webui token --generate` 立即生效，旧会话自动失效
- 登录限速：同 IP 60 秒内 5 次失败 → 429
- 未配令牌时登录返回 503 `token_missing`（**不是**免登录）

中间件顺序（`webui/server.py`）：会话校验 → 只读开关 → CSRF。非 GET 在 `write_enabled=false` 时一律 403 `readonly`。

---

## 3. 接口

统一信封：`{"ok": true, "data": …}` / `{"ok": false, "error": {"code", "message", "hint?"}}`

| 分组 | 接口 |
|---|---|
| 可观测 | `GET /api/overview`、`GET /api/logs?since=&level=&limit=`、`GET /api/audit?limit=` |
| 插件 | `GET /api/plugins`、`GET /api/plugins/{id}`、`POST /api/plugins/{id}/{enable,disable,reload,config}`、`GET /api/commands`、`GET /api/services` |
| 配置 | `GET /api/config`（fields/secrets/raw）、`POST /api/config`、`GET /api/config/history`、`POST /api/config/rollback`、`GET /api/config/raw` |
| 渠道 | `GET /api/channels`、`POST /api/channels/{channel}`、`DELETE /api/channels/{channel}` |
| 媒体 | `GET /api/media`、`DELETE /api/media/{id}`、**`GET /i/{id}`（唯一保留的旧路由：已发消息里的图片 URL 指向它）** |
| 调用 | `GET /api/invocations`、`GET /api/invocations/stats`、`POST /api/invocations/delete` |
| 工作台 | `POST /api/workbench/parse`（dry-run） |

**日志环形缓冲**：`kernel/log_ring.py`，400 条，纯内存。处理器排在脱敏之后 → 缓冲里天然没有真值。面板按 `seq` 增量拉取。

**配置历史**：每次写前落一份 `data/webui/config-history/*.toml`（保留 30 份），回滚前先校验 TOML 可解析。

---

## 4. 前端

```
web/src/
  api.ts            统一信封 + CSRF + 401 跳登录
  lib/hooks.ts      usePoll / useToast / 时间格式化
  components/ui.tsx Card / Stat / Pill / Dot / Empty / Banner / Toast / Field
  pages/            Login、Overview、Plugins、ConfigCenter、Workbench、Observability、Channels
  App.tsx           路由 + 登录门 + 左侧导航 + 顶栏状态
  styles.css        设计令牌（暗色、13px 基准、6px 圆角、等宽数字）
```

开发：`cd web && npm run dev`（5173，`/api` 与 `/i` 代理到 8080）
构建：`cd web && npm run build`（产物写入 `src/haiqbot/webui/dist/`）
产物缺失时后端返回一页"未构建"说明，而不是 500。

---

## 5. 安全清单

- [x] 读接口也要会话（不止写）
- [x] 写接口要 CSRF 头
- [x] 只读模式一键封锁全部写操作
- [x] 凭据只显示"是否已设置"与键名
- [x] 日志入缓冲前已脱敏
- [x] 配置回滚前校验 TOML
- [x] `/i/*` 只按 media_id 查，不接受路径
- [x] 登录限速 + 令牌轮换即时生效

## 单面板（v1 已下线）

`/` 就是唯一面板（React 18 + Ant Design），旧的 v1 前端已整体删除：不再有 `pages/`、
`components/`、`styles.css`，也不再有按路径分流。`/v2` 只是历史路径，前端会 `replaceState` 收敛到 `/`。

## 插件自定义页（PanelPage）

| 端点 | 作用 |
| :--- | :--- |
| `GET /api/panels` | 导航清单（只读插件元信息，不执行插件代码） |
| `GET /api/panels/{pid}` | 页面 schema + `actions` 白名单 |
| `POST /api/panels/{pid}/action` | 执行动作（会话 → 只读 → CSRF，写审计 `panel.action`） |

前端只渲染 `text / field_group / readonly_table / action_button` 四类 section，
**未知类型降级为只读 JSON**；动作必须先在页面里声明，声明之外一律 403。
插件侧契约见 `docs/PLUGIN-GUIDE.md`。

## 频道体检

`GET /api/channels/verify` → `{problems, summary}`，页面顶部按 `error/warn/info` 显示红/黄/绿横幅，
每条问题后面带「去修」直接打开该频道的编辑抽屉。
