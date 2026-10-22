# haiqbot 部署手册

三种形态，按场景选一种即可。

| 形态 | 适用 | QQ 客户端 | 命令入口 |
|---|---|---|---|
| A. 本机直跑 | 开发调试 | 需要（OneBot 路线） | `uv run haiq run` |
| B. Windows 服务 | 长期挂本机 | 需要（OneBot 路线） | NSSM 托管 `haiq.exe` |
| C. Docker | 服务器 / 想隔离 | 容器里跑 NapCat | `docker-compose up -d` |

---

## 0. 部署前必做

```bash
uv sync --extra dev --extra official   # 官方平台需要 official extra
uv run haiqbot init                       # 生成 haiq.toml + secrets/
uv run haiq secrets set painter.api_key  # 生图密钥（交互式，不回显）
uv run haiq doctor                     # 自检
```

`haiq doctor` 必须看到 `凭据加密：aead（file）`；启动前一定先设好 `WEFT_VAULT_PASSPHRASE`。

---

## A. 本机直跑

```bash
uv run haiq run --demo     # 先跑零成本演示，确认插件链路
uv run haiq run            # 正式启动
```

WebUI 在 `http://127.0.0.1:8080`，左侧栏四个页面：

| 页面 | 内容 |
| :--- | :--- |
| 概览 | 连接健康度（在线/掉线、连接时长、重连次数、最后事件、最近错误）、运行时长与事件计数、凭据状态 |
| 插件 | 每个插件一张卡：状态、命令、启用/停用/重载，以及该插件自己的参数表单 |
| 配置 | 系统字段（生图/对话/配额/策略/媒体），有候选项的下拉可选可手填 |
| 审计 | 最近 200 条，可按关键字过滤，默认只看控制台操作 |

右上角可切换日间/夜间主题（记在浏览器里）。看板免认证；**启停插件、重载插件、改配置、
改插件参数**要先拿写操作令牌：

```bash
uv run haiq webui token --generate   # 生成并写入 vault，终端里显示一次
```

把令牌粘到侧栏底部的「写操作令牌」框。忘记就重新生成 —— 旧令牌立即失效，**不需要重启**。
每次写操作都会落一条 `control:*` 审计。

插件参数写到 `haiq.toml` 的 `[plugins.<id>]` 并热更新内存；如果某个插件在 `on_load`
里读参数，点它的「重载」按钮即可。想彻底关掉写能力：`[webui] write_enabled = false`。

---

## B. Windows 服务（NSSM）

```powershell
winget install NSSM.NSSM
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install-service.ps1
Get-Content .\logs\haiqbot.out.log -Tail 50 -Wait
# 卸载
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install-service.ps1 -Uninstall
```

服务跑的是 `.venv\Scripts\haiq.exe run`，所以**改依赖后要重跑 `uv sync`**。

---

## C. Docker 全栈

### C.1 只跑 haiqbot（官方平台路线，不需要 QQ 客户端）

```bash
docker-compose up -d haiqbot
docker-compose logs -f haiqbot
```

### C.2 haiqbot + NapCat（OneBot 路线）

**第一步：初始化 NapCat 配置（带 token，不要用空 token）**

```bash
uv run python scripts/init_napcat.py
```

它会做三件事：
- 写 `.dev/napcat/config/webui.json`（WebUI 口令，仅本机可达）
- 写 `.dev/napcat/config/onebot11.json`（OneBot WS 监听 3001，**带 access_token**）
- 把两个 token 写进 haiq vault（`NAPCAT_WEBUI_TOKEN` / `WEFT_ONEBOT_ACCESS_TOKEN`）

**第二步：起容器**

```bash
docker-compose --profile onebot up -d
# 只有 docker 插件没装时才用 standalone 命令：
# docker-compose --profile onebot up -d
```

端口全部只绑 `127.0.0.1`：

| 端口 | 用途 |
|---|---|
| 6099 | NapCat WebUI（扫码登录 / 改配置） |
| 3001 | OneBot v11 正向 WebSocket（haiqbot 连它） |
| 8080 | haiqbot WebUI + `/i/*` 媒体 |

**第三步：登录 QQ**

浏览器打开 `http://127.0.0.1:6099/webui`，填 `.dev/napcat/config/webui.json` 里的 `token`，扫码登录。

也可以用命令行看二维码：

```bash
docker logs napcat | tail -40        # 终端二维码 + 解码 URL
docker cp napcat:/app/napcat/cache/qrcode.png ./qrcode.png
```

二维码约 2 分钟过期，过期就重启容器或用 WebUI 里的刷新。

**第四步：确认 OneBot WS 已就绪**

```bash
netstat -ano | findstr :3001
docker logs napcat | grep -i websocket
```

**第五步：启 haiqbot 接上去**

`haiq.toml`：

```toml
[onebot]
enabled = true
mode = "client"
url = "ws://127.0.0.1:3001"
access_token_ref = "WEFT_ONEBOT_ACCESS_TOKEN"
```

```bash
uv run haiq run
# 日志应出现 onebot_connected
```

### C.2.1 四种连接方式怎么选

OneBot v11 的接法全支持，`haiq.toml` 的 `[onebot] mode` 决定用哪种：

| mode | API 怎么走 | 事件怎么来 | 需要入站端口 | 适用 |
|---|---|---|---|---|
| `client` | 正向 WS | 同一条 WS | 否 | **默认，最省事** |
| `server` | 反向 WS | 同一条 WS | 是（默认 3002） | haiqbot 在 NAT 后、协议端能连过来 |
| `http` | HTTP POST | 协议端 HTTP 上报到 haiqbot | 是（默认 3002） | 协议端只开了 HTTP |
| `sse` | HTTP POST | SSE 订阅 `/events` | **否** | 两边都在 NAT 后、只要出网 |

```toml
# http 模式示例
[onebot]
mode = "http"
http_api_url = "http://127.0.0.1:3000"   # 协议端 HTTP API
host = "127.0.0.1"                       # haiqbot 收上报的监听地址
port = 3002
webhook_path = "/onebot"
webhook_secret_ref = ""                  # 填了就用 HMAC-SHA1 校验 X-Signature
access_token_ref = "WEFT_ONEBOT_ACCESS_TOKEN"

# sse 模式示例（不需要入站端口）
[onebot]
mode = "sse"
http_api_url = "http://127.0.0.1:3000"
sse_path = "/_events"
# NapCat 的 HTTP API 与 SSE 是两个端口（3000 / 3003）时必须显式写完整地址：
sse_url = "http://127.0.0.1:3003/_events"
access_token_ref = "WEFT_ONEBOT_ACCESS_TOKEN"
```

上报鉴权顺序：配了 `webhook_secret_ref` → 只认 `X-Signature: sha1=...`（HMAC-SHA1，签名对象是原始 body）；
否则认 `Authorization: Bearer <token>`；两者都没配则**任何人推的事件都会被当真**，`doctor` 会警告。

**真机实测（NapCat 4.18 / Docker Desktop）踩到的三个坑：**

1. NapCat 的 HTTP 上报**只带** `X-Signature: sha1=<HMAC-SHA1(body, token)>` 与 `X-Self-ID`，
   **没有** `Authorization` 头 —— 所以 HMAC 密钥必须能回落到 `access_token`，否则合法上报一律 401。
2. 协议端在容器里时 `[onebot] host` 必须是 `0.0.0.0`（容器经 `host.docker.internal` 回连），
   并且容器的 `NO_PROXY` 要包含 `host.docker.internal`，否则上报会被容器内的代理吞掉。
3. 容器重建后若退回二维码登录，说明启动参数缺 `-e ACCOUNT=<QQ号>`（镜像 entrypoint 靠它做快速登录）。

### C.3 重建 / 换账号

```bash
docker rm -f napcat                       # 只删容器，登录态在 ./.dev/napcat/qq 里
rm -rf .dev/napcat/qq/*                        # 真要换账号才删
docker-compose --profile onebot up -d
```

---

## D. 公网回源（只有官方平台需要）

**OneBot 路线不需要这一步** —— 图片以 `base64://` 内嵌在消息里发出，NapCat 直接上传，
完全不依赖你的服务器能被公网访问（`image_transport = "auto"` 默认行为）。

官方平台则必须：腾讯要能拉到你生成的图，`127.0.0.1` 它够不着。

1. 反代**只放行** `/i/*`，其余一律拒绝：

```nginx
location /i/ {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
}
location / { return 403; }
```

2. `haiq.toml`：

```toml
[media]
public_base_url = "https://bot.example.com"
```

3. 官方后台「开发设置 → 消息URL配置」登记该域名（消息正文含 URL 时必须）。

---

## E. 常见问题

**Q: 本地 `/i/xxx` 返回 502？**
本机有系统代理（Clash 等），`httpx` 把 `127.0.0.1` 也送进代理了。
验证时用 `trust_env=False`：`httpx.AsyncClient(trust_env=False)`。
`haiq doctor` 会打印检测到的代理。

**Q: `docker compose` 提示 unknown command？**
这台机器只装了 standalone 版，用 `docker-compose`（带横杠）。

**Q: 容器起来但 3001 没监听？**
NapCat 的 OneBot 网络配置在登录成功后才生效。先扫码登录。

**Q: 二维码扫了没反应？**
二维码 2 分钟过期。`docker restart napcat` 重新生成，或直接用 WebUI 里的。

**Q: 群里只看到「🎨 排队中…」没有图，审计是 `painter unhandled`？**
OneBot 路线两个已知坑，都已在代码里修掉，若从旧版本升级请确认没跑着旧进程：
1. **事件死锁** —— 收包循环 inline await 事件处理，处理里又要 `send_group_msg` 等回包，
   回包只能由同一循环读 → 死锁到 `api_timeout`。现已改为 `create_task` 异步派发。
2. **媒体发送超时** —— 2 MB 图转 base64 后 2.8 MB，NapCat 上传可能 >15s。现已拆成
   `api_timeout_seconds`(15s) 与 `send_timeout_seconds`(120s) 两档。

**Q: 群里出现「生成失败，已记录 trace」，但图片其实到了？**
同上的超时误判。可用 `get_group_msg_history` 核对真实投递结果再判断。

**Q: 数据在哪？**

| 内容 | 路径 |
|---|---|
| haiqbot 数据库 / 媒体 | `data/` |
| haiqbot 凭据 | `secrets/vault.json`（scrypt + AES-256-GCM，密码走环境变量） |
| NapCat 配置（含 token） | `.dev/napcat/config/` |
| QQ 登录态 | `.dev/napcat/qq/` |

`secrets/`、`data/`、`.dev/` 都已在 `.gitignore` 里。

## 改名说明（weft → HaiQBot）

| 项 | 现在的值 |
| :--- | :--- |
| 命令 | `haiq` |
| 包 / 导入名 | `haiqbot` |
| 配置文件 | `haiq.toml` |
| 数据库 | `data/haiq.db` |
| 会话 Cookie | `haiq_session` |
| CSRF 头 | `X-Haiq-CSRF` |

**刻意没有改的东西**：vault 里的键名与口令环境变量仍是 `WEFT_*`（`WEFT_VAULT_PASSPHRASE`、
`WEFT_WEBUI_TOKEN` …）。它们是**已经加密落盘的数据契约**，改名要重写 vault，收益为零。
老库也能直接解：AEAD 的旧 AAD `weft.vault.v2` 仍在白名单里，下次写入自动升级成新 AAD。

**开关体检**：`haiq channels verify` 会把「配了却不生效」的情况点出来（插件名拼错、
未声明的 feature、`enabled=1` 但列表为空、always 插件写多余了）。有 error 时退出码 1。

## 图片发不出去？先看这三件事

排障顺序别搞反，前两条占了绝大多数。

**1. 本机代理（最常见）**

系统代理会接管 Docker 的出站流量，**容器里 QQ 的媒体上传随之失败**。表现非常一致：

- 文字消息秒发、图片必然失败
- NapCat 日志：`Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg`
  或 `EventChecker Failed ... "errMsg": "rich media transfer failed"`
- 关掉宿主机的代理/系统代理/TUN 后立刻恢复（实测从 22~70 秒失败 → 1.8 秒成功）

`haiq doctor` 检测到系统代理时会直接提示这一点。容器内的 `HTTP_PROXY` 环境变量反而无关
（bridge 网络里 `127.0.0.1:10808` 指的是容器自己）。

**2. NapCat 的等待时间**

`.dev/napcat/config/onebot11_<uin>.json` 里：

```json
"timeout": { "baseTimeout": 10000, "uploadSpeedKBps": 256, "maxTimeout": 1800000 }
```

`baseTimeout` 默认 **10 秒**，图片上传常常超过它 —— 结果是把真实错误掩盖成 `Timeout`。
建议改成 `60000`。改完再看错误：如果变成 `rich media transfer failed`，那就是真的上传失败
（去查第 1 条），而不是"等得不够久"。

**3. 发送方式（`[onebot] image_transport`）**

| 值 | 行为 | 适用 |
| :--- | :--- | :--- |
| `auto`（默认） | 有本地文件就 base64 内嵌 | **推荐**，不依赖任何端口/域名 |
| `url` | 把 URL 交给协议端去拉 | 需要**公网可达** URL：腾讯的中转会由服务端拉图，`127.0.0.1` / `host.docker.internal` / 局域网 IP 会被**立即拒绝** |

只有走 `url` 方案时才需要：

```toml
[media]
public_base_url = "https://你的公网域名"   # 公网可达
serve_port = 8090                          # 只读媒体服务，0 = 关闭
```

`serve_port` 起的是一个**只暴露 `/i/<id>` 和 `/healthz`** 的小服务（不含面板）；
面板本身始终只绑 `127.0.0.1`。

## 频道开关体检

```bash
haiq channels verify        # 有 error 时退出码 1
```

它会点出「配了却不生效」的四类问题：插件名拼错、feature 未声明、`enabled=1` 但功能列表为空、
`always` 插件写进名单里多余。`haiq doctor` 也会顺带跑一遍（error 计入退出码），
面板「白名单与风控」页顶部同样有红/黄/绿横幅（`GET /api/channels/verify`）。
