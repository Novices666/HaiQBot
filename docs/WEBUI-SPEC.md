# 面板（WebUI）规格

前端：`web/`（Vite 5 + React 18 + TypeScript + Ant Design 6 + Recharts）
后端：`src/haiqbot/webui/`（Starlette）
产物：`src/haiqbot/webui/dist/`（**入库**，随 wheel 分发，部署机不需要 node）

---

## 1. 设计取舍（先说清楚为什么长这样）

| 决定 | 理由 |
|---|---|
| 单令牌 + 会话 Cookie | 单用户工具，做多用户/角色是过度设计；但"读也公开"不行，读接口会泄露配置与审计 |
| 无状态会话（HMAC 签名） | 没有服务端会话表，**重启进程不影响已有会话**（12h TTL 内照用）；换令牌 = 踢掉所有会话 |
| 写操作额外要 `X-Haiq-CSRF` | SameSite=Lax 已挡大部分跨站；反代场景下不保险，双保险成本极低 |
| **日志走 SSE，其余轮询** | 日志是唯一「刷得够快才有用」的页面，用 `EventSource`；其它页 1~3 秒轮询，少一层长连接故障面 |
| **列表接口永不返回值** | 列表只给「已设置 / 未设置」；要看单条明文必须单独请求，且每次都写 `secret.reveal` 审计 |
| **工作台预演 ≠ 真发** | 预演不调用 `rule.invoker`（否则面板就变成"远程执行入口"）；真发是另一个端点，只发文本、不触发插件 |
| dist 入库 | 部署与 CI 不需要 node；`npm run build` 只在前端改动时跑 |

---

## 2. 认证

```
POST /api/auth/login   {token}         → 200 {csrf, expires_at, write_enabled} + Set-Cookie(haiq_session)
POST /api/auth/logout                  → 清 Cookie
GET  /api/auth/session                 → 当前会话信息（前端启动时调它判断是否已登录）
```

- Cookie：`HttpOnly` + `SameSite=Lax` + `Path=/`，TTL 12h；HTTPS（或反代 `X-Forwarded-Proto: https`）才加 `Secure`
- 令牌：vault 的 `webui.token_ref`（默认 `HAIQ_WEBUI_TOKEN`），**每次请求现取** → `haiq webui token --generate` 立即生效，旧会话自动失效
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
| 工作台 | `POST /api/workbench/parse`（dry-run 预演）、`POST /api/workbench/send`（真发文本，必须 `confirm=true`） |
| 凭据 | `GET /api/secrets`（列表，不含值）、`GET /api/secrets/{key}`（单条明文，写审计）、`POST /api/secrets`（写入）、`DELETE /api/secrets/{key}`、`POST /api/secrets/passwd`（轮换主口令） |
| 定时任务 | `GET /api/jobs`、`POST /api/jobs`、`POST /api/jobs/{id}/{enable,disable,run}`、`DELETE /api/jobs/{id}` |
| 插件页面 | `GET /api/panels`、`GET /api/panels/{pid}`、`POST /api/panels/{pid}/action` |
| 实时日志 | `GET /api/logs/stream?since=&level=`（SSE；并发上限 6，15 秒心跳） |

**日志环形缓冲**：`kernel/log_ring.py`，400 条，纯内存。处理器排在脱敏之后 → 缓冲里天然没有真值。面板按 `seq` 增量拉取。

**配置历史**：每次写前落一份 `data/webui/config-history/*.toml`（保留 30 份），回滚前先校验 TOML 可解析。

---

## 4. 前端

```
web/src/
  api.ts            统一信封 + CSRF + 401 跳登录
  main.tsx          入口（只渲染 V2App）
  v2/App.tsx        登录门 + 左侧导航（含插件自注册页）+ 顶栏（写权限 / 亮暗切换）
  v2/theme.ts       Ant Design 主题 + 亮暗模式（localStorage: haiq-theme）
  v2/lib.ts         usePoll（轮询 hook）/ 时间与时长格式化
  v2/panels.tsx     PanelPage 渲染器（只认 4 类 section，未知降级为只读 JSON）
  v2/pages/         Overview、Plugins、Workbench、Config、Observability、Channels、Secrets、Jobs
```

开发：`cd web && npm run dev`（5173，`/api` 与 `/i` 代理到 8080）
构建：`cd web && npm run build`（产物写入 `src/haiqbot/webui/dist/`）
产物缺失时后端返回一页"未构建"说明，而不是 500。

---

## 5. 安全清单

- [x] 读接口也要会话（不止写）
- [x] 写接口要 CSRF 头
- [x] 只读模式一键封锁全部写操作
- [x] 凭据列表只显示「是否已设置」与键名；单条明文按需请求并写 `secret.reveal` 审计
- [x] 写入只进 vault，绝不回写 `haiq.toml`
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

## 工作台：预演与真发是两件事

| | 预演 `POST /api/workbench/parse` | 真发 `POST /api/workbench/send` |
| :--- | :--- | :--- |
| 调用插件 handler | **不会** | **不会** |
| 真的发出消息 | 不会 | **会**（只发文本，不构造图片/文件） |
| 经过出站闸门 | 不涉及 | **会**（同频道串行 + 最小间隔 + 全局限速 + 频控退避） |
| 需要的开关 | 无 | `confirm: true`，且面板不能是只读模式 |
| 写审计 | 无 | `webui:workbench.send` |

为什么分成两个端点而不是给 `parse` 加参数：预演是**只读**的，真发有副作用。
混在一个端点里，任何一次"手滑点错"的代价就从"看到一堆不匹配原因"变成了"群里多一条消息"。

真发请求体：

```json
{"text": "链路测试", "platform": "onebot", "channel_kind": "group",
 "channel_id": "674425800", "confirm": true}
```

失败一律回 502 `send_failed` 并带原始错误（例如 `MediaSendError: …`），
不会把适配器的异常吞成 200。

## 已有端点在前端的落点

后端端点齐了不等于面板上能看见。以下四个此前"后端有、UI 没暴露"，现已接上：

| 端点 | 落在哪 | 用途 |
| :--- | :--- | :--- |
| `GET /api/invocations/stats?hours=` | 日志 · 审计 · 媒体 → 调用流水 → 能力统计 | 按「能力 + 状态」聚合次数/平均/最大耗时 |
| `POST /api/invocations/delete` | 调用流水 → 清理 | 只按「N 天前」删，后端拒绝空过滤 |
| `GET /api/services` | 插件与能力 → 运行时能力与命令 → 能力提供者 | **运行时真实注册**的 owner（插件表里的 `provides` 只是声明，两者对不上说明注册失败） |
| `GET /api/commands` | 插件与能力 → 运行时能力与命令 → 命令速查 | 全局命令清单，不用逐个展开插件 |

`GET /api/config/raw` 没有单独接：配置页的「原始 TOML」标签读的是 `/api/config` 的 `raw` 字段，
信息完全相同，多接一个入口只会多一条要维护的路径。
