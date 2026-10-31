# haiqbot 部署手册

> **第一次装、还没跑通过？** 先看仓库根 [README.md](../README.md) 的「快速开始」—— 那条路最短。
> 本文讲的是**部署形态与运维**：装成服务、上服务器、出问题怎么查。

三种形态，按场景选一种即可。

| 形态 | 适用 | 协议端 | 命令入口 |
|---|---|---|---|
| A. 本机直跑 | 开发调试 | 自己另外起 | `uv run haiq run` |
| B. Windows 服务 | 长期挂本机 | 自己另外起 | NSSM 托管 `haiq.exe` |
| C. 容器 | 服务器 / 想隔离 | 自己另外起（**不在本项目的 compose 里**） | `docker compose up -d` |

---

## 0. 部署前必做

```bash
uv sync --extra dev --extra official        # 官方平台才需要 official extra
uv run haiq doctor                          # 自检：依赖 / 凭据 / 频道 / 插件来源
uv run haiq secrets set assistant.image_api_key   # 生图密钥（交互式，不回显）
```

> **仓库自带 `haiq.toml`**，不需要 `haiq init`（它只在空环境里用，且拒绝覆盖已有文件）。

`haiq doctor` 必须看到 `凭据加密：aead（file）`。**启动前必须能拿到 vault 主口令**，
来源只有一个：环境变量 `HAIQ_VAULT_PASSPHRASE`（键名见 `[vault] passphrase_ref`）。

| 场景 | 口令放哪儿 |
|---|---|
| Windows 本机自跑 / `scripts\dev-up.ps1` | 项目根 `.env` 里一行 `HAIQ_VAULT_PASSPHRASE=...`（`scripts\haiq.ps1`、`run-with-env.ps1` 会读进环境变量） |
| Windows 服务（NSSM） | `nssm set <服务名> AppEnvironmentExtra HAIQ_VAULT_PASSPHRASE=<口令>` —— 服务没有 `.env` 概念 |
| Docker | `env_file: .env` 或 `-e HAIQ_VAULT_PASSPHRASE=...`（见 compose.yaml） |
| Linux / systemd | `Environment=` / `EnvironmentFile=` |

### 要不要加密？先选 `[vault] enc`

| 值 | 适合谁 | 代价 |
|---|---|---|
| `aead`（默认） | 会被备份 / 云盘同步 / 多人接触的机器 | 必须管好主口令；丢了所有凭据作废；每开一个新环境都要给一次 |
| `none` | 单机自用，`secrets/` 不会被拷走 | 明文（base64 信封）：谁读到文件谁就拿到密钥 |

**加密挡的是「文件自己跑出去」**：git push、OneDrive/坚果云同步、备份 tar、打包发人、
Docker 镜像里带上 `secrets/`。**它挡不住「人已经进了这台机器」** —— 主口令就在隔壁 `.env`，
这正是它收益有限的原因，别为它加额外仪式。

`none` 之后 **不需要任何口令包装脚本**：`.venv/bin/haiq …` 直接跑，
`scripts/haiq.ps1` / `envpass.ps1` / `run-with-env.ps1` 都可以不用（`dev-up.ps1` 会自动跳过口令检查）。
切换方法：把 `enc` 改成 `none`，**在还拿得到口令的时候**跑一次 `haiq secrets set <任意键>`，
这一次写入会把整库重写成明文信封；然后确认 `haiq doctor` 显示「凭据加密：none」。

**永远不要**写进 `haiq.toml`、不要放进命令行参数。口令丢了 = 现有凭据解不开，只能重新录入；
改口令用 `haiq secrets passwd`，然后同步改上面那处。

---

## A. 本机直跑

```bash
uv run haiq run            # 正式启动（面板 http://127.0.0.1:8080）
uv run haiq run --demo     # 零成本演示：Mock 适配器，跑完即退，不起服务
```

WebUI 在 `http://127.0.0.1:8080`，左侧栏 8 个页面：

| 页面 | 内容 |
| :--- | :--- |
| 总览 | 连接健康度（在线/掉线、连接时长、重连次数、最后事件、最近错误）、运行时长与事件计数、凭据状态 |
| 插件与能力 | 每个插件一张卡：状态、命令、启用/停用/重载，以及该插件自己的参数表单 |
| 命令工作台 | 手工构造一条消息走完整个管线，看它被判给了谁、为什么 |
| 配置中心 | 系统字段（生图/对话/配额/策略/媒体），有候选项的下拉可选可手填 |
| 日志 · 审计 · 媒体 | 结构化日志（SSE 实时）、审计流水、媒体库 |
| 白名单与风控 | 频道开关（**默认全关**）+ 体检横幅 + 配额/限流 |
| 凭据管理 | 槽位列表（不返回值本体）、单条查看（写 `secret.reveal` 审计）、口令轮换 |
| 定时任务 | 持久化任务列表，`interval` / `daily`，可停用/删除 |

插件还能自注册页面（声明式 schema，不注入 JS），出现在「插件页面」分组里。

右上角可切换日间/夜间主题（记在浏览器里）。**整个面板都要登录**（读接口也不公开）；
首次进站先看到登录页，粘上令牌即可，会话 12 小时内有效 —— **重启进程不用重新登录**。

**启停插件、重载插件、改配置、改插件参数**都要求先有写操作令牌：

```bash
uv run haiq webui token --generate   # 生成并写入 vault，终端里显示一次
```

令牌忘了就重新生成：旧令牌与所有旧会话**立即失效**，同样不需要重启进程。
每次写操作都会落一条 `control:*` 审计。

每条被处理的事件也会落一条审计（`handled` / `unhandled` / `stopped:<原因>`），`detail` 里带
**一句回复摘要**：发了几条、消息类型、正文总长度，以及正文**前 80 字**。留摘要不留全文 ——
出问题能查「它到底怎么答的」，又不至于把群聊内容整段落库。

插件参数写到 `data/plugins/<插件id>.toml` 并热更新内存；如果某个插件在 `on_load`
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

## C. 容器（只跑 haiqbot）

`compose.yaml` 里**只有 haiqbot 一个服务**，没有协议端，以后也不会有：
OneBot v11 是一个**协议**，协议端（NapCat / Lagrange / LLOneBot / go-cqhttp …）
是别人的项目 —— 它们怎么装、跑在哪、用哪个账号，与框架无关。
框架只需要一个地址和一份 token。

### C.1 起容器

```bash
docker compose up -d
docker compose logs -f haiqbot
```

挂载了什么：

| 宿主 | 容器 | 作用 |
|---|---|---|
| `./data` | `/data` | SQLite、媒体库、插件参数 |
| `./secrets` | `/secrets` | `vault.json`（口令不在这里） |
| `./plugins` | `/app/plugins` | 框架自带插件（echo / help） |
| `./plugins-extra` | `/app/plugins-extra` | 你自己的插件（admin / assistant / guard） |
| `./haiq.docker.toml` | `/app/haiq.toml` | 容器版配置（**不能加 `:ro`**，面板保存要写它） |
| `.env` | 进程环境（`env_file`） | `HAIQ_VAULT_PASSPHRASE` |

`data_dir` 是**状态根**：`haiq.db`、`media/`、`plugins/`（插件参数）都从它派生。
容器里它由 `HAIQ_DATA_DIR=/data` 覆盖，于是三者分别是 `/data/haiq.db`、
`/data/media/`、`/data/plugins/<插件id>.toml` —— 都在 `./data` 卷里，重建容器不会丢。

能用的环境变量（都可选，不写就用 toml 里 / 默认值）：

| 变量 | 覆盖什么 | 说明 |
|---|---|---|
| `HAIQ_VAULT_PASSPHRASE` | `[vault] passphrase_ref` 指向的口令 | **必需**（`enc = "aead"` 时）。只从环境变量读，不读 `.env`（读 `.env` 的是启动器） |
| `HAIQ_DATA_DIR` | `data_dir` | 容器镜像里定成 `/data`。别指到卷外，否则重启即丢 |
| `HAIQ_SECRETS_DIR` | `secrets_dir` | 容器里是 `/secrets` |
| `HAIQ_LOG_LEVEL` | `log_level` | 排障时改 `DEBUG` |
| `HAIQ_LOG_JSON` | `log_json` | `1` / `true` / `yes` / `on` 打开 JSON 行日志 |

`plugin_dirs = ["plugins", "plugins-extra"]` 按容器内的 `WORKDIR /app` 解析 ——
**两个目录缺一个，对应的插件就整组消失，而且没有任何报错**。镜像里已经 `COPY`
了这两份，挂载又各覆盖一次（为了改插件不用重建镜像）。

镜像以 uid 10001（非 root）运行：`data/`、`secrets/`、**以及 `haiq.docker.toml` 本身**
在宿主上都要对它可写。最后一个最容易漏 —— bind mount 保留宿主属主，文件是
`你:你 644` 的话容器用户写不进去，面板「停用插件 / 保存配置」就会报
`Permission denied: 'haiq.toml'`（容器里那个路径就是它）。修法见 C.4。

面板 `http://127.0.0.1:8080`，只绑本机。

**容器专用配置**：`haiq.docker.toml` 与仓库根的 `haiq.toml` 是两份，互不影响 ——
本机开发用后者，容器用前者。它只比本机版多三处**被容器环境逼出来的**差异：
`[webui] host = "0.0.0.0"`（绑 127.0.0.1 的话宿主发布端口连不进来）、
`[media]` 不写 `dir`（写了会落到 `/app/data/media`，不在卷里）、
`[onebot] url` 用协议端的容器名。

**先建网络**（让两个容器能互相看见，两条命令）：

```bash
docker network create haiqbot_net
docker network connect haiqbot_net <你的协议端容器名>
```

`compose.yaml` 末尾把 `haiqbot_net` 声明为 `external: true`，所以这张网必须先存在。
只用官方平台、不要协议端的话，把 `networks:` 那一段删掉即可。

### C.1.1 大陆网络：用 `compose.cn.yaml`

构建镜像时要访问三处外部站点，在大陆都可能卡住：

| 卡点 | 默认行为 | `compose.cn.yaml` 里改成 |
|---|---|---|
| 基础镜像 | Docker Hub `python:3.12-slim` | `dockerproxy.net/library/python:3.12-slim` |
| 装 curl | `deb.debian.org` | `mirrors.aliyun.com`（只换主机名，路径不变） |
| 装 uv + 项目依赖 | `astral.sh` 脚本 + PyPI | pip 从 `mirrors.aliyun.com/pypi/simple` 装 uv，`uv sync` 也走它 |

用法与 `compose.yaml` 完全一样，只是多一个 `-f`：

```bash
docker compose -f compose.cn.yaml up -d --build
docker compose -f compose.cn.yaml logs -f haiqbot
docker compose -f compose.cn.yaml exec haiqbot haiq doctor
```

三个源都能用环境变量覆盖，不必改文件：

```bash
HAIQ_BASE_IMAGE=python:3.12-slim \
HAIQ_APT_MIRROR=mirrors.tuna.tsinghua.edu.cn \
HAIQ_PYPI_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
docker compose -f compose.cn.yaml up -d --build
```

镜像站会停、会改成要鉴权（实测：`docker.m.daocloud.io` 现在 401，`docker.1panel.live`
与 `docker.nju.edu.cn` 403，`dockerproxy.net` 可用）。更彻底的办法是在
`/etc/docker/daemon.json` 里配 `registry-mirrors` 让**所有**镜像拉取都走加速，
然后 `HAIQ_BASE_IMAGE=python:3.12-slim` 即可 —— 这样换站只改一处。

两个提醒：

- 镜像源只影响**构建**。容器运行时不访问这些站点，只连你的协议端和模型网关，
  所以容器内不需要任何代理配置。
- `docker compose config` 会把 `.env` 的值**明文打印**出来（包括
  `HAIQ_VAULT_PASSPHRASE`）。调试 compose 语法时别在共享屏幕/群里跑，或者先把
  `env_file` 那行注释掉。


### C.2 连你自己的协议端

框架不认识任何具体实现，只认这四条：

| mode | API 怎么走 | 事件怎么来 | 需要入站端口 | 适用 |
|---|---|---|---|---|
| `client` | 正向 WS | 同一条 WS | 否 | **默认，最省事** |
| `server` | 反向 WS | 同一条 WS | 是（默认 3002） | 协议端能连过来 |
| `http` | HTTP POST | 协议端上报 | 是（默认 3002） | 协议端只开了 HTTP |
| `sse` | HTTP POST | SSE 订阅 | **否** | 两边都在 NAT 后 |

**容器化的第一个坑：`127.0.0.1` 在容器里指容器自己，不是宿主机。**

| 协议端在哪 | `[onebot] url` 写什么 |
|---|---|
| 宿主机（原生进程 / Windows 服务） | `ws://host.docker.internal:3001` |
| 同 Docker 网络里的另一个容器 | `ws://<容器名>:3001`（推荐，见 C.1 的两条 network 命令） |
| 另一台机器 | `ws://<内网地址>:3001` |

Linux 上 `host.docker.internal` 默认不存在，要在 compose 里加：

```yaml
services:
  haiqbot:
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

```toml
# haiq.toml —— 协议端在宿主机
[onebot]
enabled = true
mode = "client"
url = "ws://host.docker.internal:3001"
access_token_ref = "HAIQ_ONEBOT_ACCESS_TOKEN"
```

```toml
# http 模式：协议端只开了 HTTP API
[onebot]
mode = "http"
http_api_url = "http://host.docker.internal:3000"
host = "0.0.0.0"                 # 容器里收上报必须是 0.0.0.0
port = 3002
webhook_path = "/onebot"
webhook_secret_ref = ""
access_token_ref = "HAIQ_ONEBOT_ACCESS_TOKEN"
```

```toml
# sse 模式：不需要任何入站端口
[onebot]
mode = "sse"
http_api_url = "http://host.docker.internal:3000"
sse_path = "/_events"
access_token_ref = "HAIQ_ONEBOT_ACCESS_TOKEN"
```

上报鉴权顺序：配了 `webhook_secret_ref` → 只认 `X-Signature: sha1=...`（HMAC-SHA1，签名对象是原始 body）；
否则认 `Authorization: Bearer <token>`；两者都没配则**任何人推的事件都会被当真**，`doctor` 会警告。

`http` / `server` 两种模式要收到入站连接，得在 compose 里发布对应端口，并在 `[onebot] host`
里写 `0.0.0.0`；`client` / `sse` 完全不需要入站端口，这也是默认推荐它们的理由。

### C.3 重建 / 换配置

```bash
docker compose up -d --build haiqbot     # 改了代码、Dockerfile 或插件
docker compose restart haiqbot           # 只改了 .env
docker compose down                      # data/ 与 secrets/ 在宿主上，不会丢
```

### C.4 更新到新版本（git pull）

代码分三层，更新代价不一样 —— 知道这个就不用每次都重建：

| 改了什么 | 怎么生效 |
|---|---|
| `src/`（内核 / WebUI 后端 / 前端构建产物 `dist/`） | **必须重建镜像**（`dist/` 是打在镜像里的） |
| `plugins/`、`plugins-extra/` | 这两个目录是 bind mount，重启容器即可 |
| `.env`、`haiq.docker.toml` | 重启容器（配置只在进程启动时读一次） |
| `data/`、`secrets/` | 不用动 |

标准流程：

```bash
cd /path/to/HaiQBot

# 0) 先看这版动了什么。特别注意 haiq.docker.toml：它是**被跟踪**的文件，
#    你本地改过它、而这版又恰好改了它，pull 会直接拒绝。
#    （.env 在 .gitignore 里，永远不会冲突，不用管。）
git fetch origin
git diff --stat HEAD origin/main

# 1) 只有上面列表里出现 haiq.docker.toml、且你本地也改过它时才需要这一步
git stash push -m "local config" haiq.docker.toml

# 2) 拉代码（--ff-only：宁可报错，也不要一个意外的 merge commit）
git pull --ff-only

# 3) 上一步做过 stash 就放回来，并把模板里新增的键手动合进去
#    （没 stash 过会提示 "No stash entries found"，忽略即可）
git stash pop

# 4) 重建 + 重启 + 自检
docker compose up -d --build haiqbot
docker compose logs -f haiqbot                                        # 看到 haiqbot_ready
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/      # 期望 200
docker compose exec haiqbot haiq doctor                               # 全绿即可
```

面板是单页应用，更新后**浏览器要 Ctrl+F5 强刷**，否则还在跑旧 bundle。

**容器里写不进配置**（`Permission denied: 'haiq.toml'`）：容器以 uid 10001 运行，
而 `haiq.docker.toml` 是 bind mount（保留宿主属主）。你 `own:own 644` 它就写不进去：

```bash
ls -l haiq.docker.toml                    # 看属主与权限
sudo chown 10001:10001 haiq.docker.toml   # 换成容器用户（之后编辑要用 sudo 或走面板）
# 或者不换属主，只放开写权限（更省事，但本机任何用户都能改）：
chmod 666 haiq.docker.toml
docker compose exec haiqbot haiq doctor   # 应打印「配置可写：OK」而不是警告
```

面板写不进去时**不会假装成功**：启停插件会回「仅本进程生效，重启后恢复原状」，
配置中心保存则整条拒绝 —— 看到这两种提示，回来修权限。

---

## D. 公网回源（只有官方平台需要）

**OneBot 路线不需要这一步** —— 图片以 `base64://` 内嵌在消息里发出，协议端直接上传，
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

**Q: 容器起来了，但连不上协议端？**
按顺序查：① 协议端已登录且 OneBot 服务在监听；② `[onebot] url` 指对了
（容器里的 `127.0.0.1` 不是宿主机，见 C.2）；③ 两侧 `access_token` 一致；
④ `haiq doctor` 看有没有代理告警。协议端自身怎么登录，看它自己的文档。

**Q: 面板里保存的生图 / 对话参数，一重启容器就变回默认值？**
插件参数写的是 `<data_dir>/plugins/<插件id>.toml`，容器里 = `/data/plugins/<id>.toml`。
旧版在这里有个真 bug：**启动时读插件参数用的是环境变量覆盖之前的 `data_dir`**
（toml 里写的是相对路径 `data`，于是去 `/app/data/plugins/` 找 —— 镜像里没有这个目录，
`.dockerignore` 把 `data/` 排除了），而**写**却写进 `/data`。读和写不是同一个文件，
于是「面板提示保存成功、`/data/plugins/*.toml` 里也真有值，重启后却全是默认值」。已修复：
现在读、写、`haiq.db`、`media/` 全部跟同一个 `data_dir` 走。
再遇到这类症状，按三步查：

1. `docker compose exec haiqbot haiq doctor | grep 数据目录` —— 是不是 `/data`；
2. 宿主上 `data/plugins/<插件id>.toml` 里有没有你保存的那个值；
3. 日志里搜 `plugin_config_unreadable`（文件在但读不动：权限 / 编码），
   或 `plugin_config_inline_deprecated`（值还写在主 toml 的 `[plugins.<id>]` 里）。

**Q: 部署到新机器后，`@机器人` / `/问` 不回话了？**
先跑 `haiq doctor`，看新加的那行 `插件接口：...`。最常见的原因是
**`data/plugins/<插件id>.toml` 根本不存在**（`data/` 不入库），于是参数全取出厂默认值：
`base_url = https://api.openai.com/v1` 配着某家第三方网关的密钥 → 请求被对方 401，
群里只会看到「对话失败：ProviderError: 对话接口返回 401」。
按顺序核对三件事：

1. `插件接口：assistant.chat_base_url=...` 是不是你的网关地址（不是 `api.openai.com`）；
2. `assistant.chat_model` 是不是该网关有的模型名（面板「插件与能力 → assistant → 配置」
   里的下拉是**现拉接口**的，能选到就说明地址与密钥都对）；
3. `assistant.chat_provider` / `image_provider` 不是 `mock`（mock 不出网，只回固定内容）。

另外两个「不回话」的原因不带报错：**频道开关默认全关**（`haiq channels verify` 看一眼），
以及 `@机器人` 之外的消息本来就不会触发对话。

**Q: 日志里同一个 trace_id 既有 `llm_retry` 又有 `handler_timeout`，群里完全没反应？**
`chat_timeout_seconds`（单次请求）× `chat_retries+1` 大于 handler 的 180 秒时，
内核会在半路取消 handler，而 `CancelledError` 不是 `Exception` ——
连「对话失败」这句话都发不出去。现在默认加了 `chat_budget_seconds = 120`
（横跨所有重试的总时限，必须小于 180），到点会明确回一句「对话超时」。
从旧版升级的话，要么升上来，要么自己把 `chat_timeout_seconds` 调到 25 左右。

**Q: 日志一直刷 `llm_retry`，对话和生图都不工作？**
先在容器里看一眼代理变量：

```bash
docker exec haiqbot env | grep -i proxy
```

如果看到 `HTTP_PROXY=http://127.0.0.1:端口` 这类**指向本机**的值，就是它：
容器里的 `127.0.0.1` 是**容器自己**，不是宿主机上的 Clash —— 每个请求都连到死端口，
于是 `llm_retry ... ConnectError` 刷满日志，对话/生图全挂（实测 0/8 成功）。
来源通常是 `~/.docker/config.json` 的 `proxies`，或宿主机
`/etc/systemd/system/docker.service.d/http-proxy.conf`；只给**构建**用代理的话，
用完就删掉，别留在 daemon/客户端配置里。确需容器走代理就写
`http://host.docker.internal:端口` 并给 compose 加 `extra_hosts`。
`haiq doctor` 现在会把这个组合直接报成**错误**并给出修法，不会再只给一句警告。

**Q: 数据在容器里重建后没了？**
镜像里把 `HAIQ_DATA_DIR` 定成了 `/data`。裸 `docker run` 忘了挂卷时，
数据库/媒体/插件参数都落在容器可写层 —— 容器一删就没，而界面上看不出异常。
`haiq doctor` 现在会打印实际生效的 `数据目录：`，跑一次确认它指向卷内路径
（compose 里是 `/data`，对应宿主 `./data`）。

**Q: `/画图` 之后很久没动静，或者审计里看到 `assistant` 的相关失败？**
OneBot 路线两个已知坑，都已在代码里修掉，若从旧版本升级请确认没跑着旧进程：
1. **事件死锁** —— 收包循环 inline await 事件处理，处理里又要 `send_group_msg` 等回包，
   回包只能由同一循环读 → 死锁到 `api_timeout`。现已改为 `create_task` 异步派发。
2. **媒体发送超时** —— 2 MB 图转 base64 后 2.8 MB，协议端上传可能 >15s。现已拆成
   `api_timeout_seconds`(15s) 与 `send_timeout_seconds`(120s) 两档。

**Q: 群里出现「生成失败，已记录 trace」，但图片其实到了？**
同上的超时误判。可用 `get_group_msg_history` 核对真实投递结果再判断。

**Q: 数据在哪？**

| 内容 | 路径 |
|---|---|
| haiqbot 数据库 / 媒体 | `data/` |
| haiqbot 凭据 | `secrets/vault.json`（scrypt + AES-256-GCM，密码走环境变量） |

`secrets/`、`data/` 都已在 `.gitignore` 里。
协议端自己的配置与登录态在它自己的目录里（本仓库的 `.dev/` 只是我本机的例子），
**不属于本项目**，备份与迁移请按它的文档来。

## 改名说明（weft → HaiQBot）

| 项 | 现在的值 |
| :--- | :--- |
| 命令 | `haiq` |
| 包 / 导入名 | `haiqbot` |
| 配置文件 | `haiq.toml` |
| 数据库 | `data/haiq.db` |
| 会话 Cookie | `haiq_session` |
| CSRF 头 | `X-Haiq-CSRF` |

**刻意没有改的东西**：vault 里的键名与口令环境变量仍是 `HAIQ_*`（`HAIQ_VAULT_PASSPHRASE`、
`HAIQ_WEBUI_TOKEN` …）。它们是**已经加密落盘的数据契约**，改名要重写 vault，收益为零。
老库也能直接解：AEAD 的旧 AAD `weft.vault.v2` 仍在白名单里，下次写入自动升级成新 AAD。

**开关体检**：`haiq channels verify` 会把「配了却不生效」的情况点出来（插件名拼错、
未声明的 feature、`enabled=1` 但列表为空、always 插件写多余了）。有 error 时退出码 1。

## 附录：协议端参考（不是框架的一部分）

> 这一节讲的是**协议端**，不是 haiqbot。放进来只是因为排障时绕不开它。
> 换任何别的 OneBot v11 实现，本节可以整段忽略。

两种形态跑的是**同一份 NapCat**，区别只在 QQ 客户端跑在哪：

| 形态 | QQ 在哪 | 媒体上传受谁影响 |
| :--- | :--- | :--- |
| Docker（`mlikiowa/napcat-docker`） | 容器内的 Linux QQ | 宿主机代理 / TUN 会劫持容器出站 → 发图失败 |
| Windows 原生（`NapCat.Shell`） | 本机一份独立 QQ | 直接走宿主网络，没有容器这一层 |

同一账号、同一 `image_transport = "auto"` 下的实测对比：

| | Docker | Windows 原生 |
| :--- | :--- | :--- |
| 成功率 | 11 次里 1 次（9%） | 单次验证成功，长期未测 |
| 耗时 | 失败时 21 秒以上超时 | `outbound_media_sent elapsed_ms=4125`（2.3 MB PNG） |
| 报错 | `Timeout` / `rich media transfer failed` | 无重试、无异常 |

**结论：发图失败优先怀疑容器网络，而不是腾讯侧限制。**
上方「本机代理」那条只解释了 Docker 形态下的现象；如果关掉代理仍然发不出去，
换 Windows 原生版本是成本最低的验证手段——`haiq.toml` 一行都不用改，
协议端仍是 `ws://127.0.0.1:3001`。

### NapCat 实测三坑（4.18 / Docker Desktop）

1. HTTP 上报**只带** `X-Signature: sha1=<HMAC-SHA1(body, token)>` 与 `X-Self-ID`，
   **没有** `Authorization` 头 —— 所以 HMAC 密钥必须能回落到 `access_token`，否则合法上报一律 401。
2. 它在容器里时，haiqbot 侧 `[onebot] host` 必须是 `0.0.0.0`（它经 `host.docker.internal` 回连），
   并且它容器的 `NO_PROXY` 要包含 `host.docker.internal`，否则上报会被容器内的代理吞掉。
3. 容器重建后退回二维码登录，说明启动参数缺 `-e ACCOUNT=<QQ号>`（镜像 entrypoint 靠它快速登录）。

### Windows 原生的两个坑（实测）

1. **不要直接用官方 `launcher.bat`。** 它从注册表
   （`HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ`）
   读 QQ 路径，多数机器上指向**用户自己正在用的 QQ** —— 直接跑会把个人号注入成 bot。
   要自己写启动脚本，把 QQ 路径写死到 NapCat 目录下那份独立 QQ。
2. **不需要管理员权限。** 官方脚本用 `net session` 强制要求管理员，
   实测非提权即可启动并完成注入，可以省掉 UAC。

另外：`NapCat.Shell.Windows.OneKey.zip` 只有 0.9 MB，但会再下 300 MB 的 QQ；
它走镜像下载极慢（实测 50 秒才 3%），直接从 GitHub release 取
`NapCat.Shell.zip` 反而快（34 MB / 20 秒）。

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

以 NapCat 为例，它的 `onebot11_<uin>.json` 里有（别的协议端各自的配置项）：

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
serve_port = 8090                          # 只读媒体服务，默认 0（关闭）
```

`serve_port` 起的是一个**只暴露 `/i/<id>` 和 `/healthz`** 的小服务（不含面板）；
面板本身始终只绑 `127.0.0.1`。

**默认是关的**：不写 `[media]` 段时 `serve_port = 0`，`/i/<id>` 不监听任何端口。
（`serve_host` 默认 `0.0.0.0`，一旦开了端口就是全网可达 —— 所以默认必须是关，
确实要发公网 URL 时才手动开。）

**路径只认一个根**：`db_path` 和 `media.dir` 不写就跟着 `data_dir` 走
（`<data_dir>/haiq.db`、`<data_dir>/media`）。搬数据只改 `data_dir` 一处即可；
显式写了 `db_path` 或 `[media] dir` 的，以写的为准。

## 频道开关体检

```bash
haiq channels verify        # 有 error 时退出码 1
```

它会点出「配了却不生效」。判定码共 **10 个**：其中 7 个 error/warn 意味着**真的不响应**，
3 个 info 只是提示（不影响功能）。

| 代码 | 级别 | 含义 |
|---|---|---|
| `unknown_plugin` | error | 频道里写的插件 id 连目录/包都找不到 |
| **`plugin_not_enabled`** | error | **插件在磁盘上，但没写进 `[plugins] enabled` —— 永远不会被加载** |
| `unknown_feature` | error | 插件没有声明这个 feature |
| `bad_target` | error | 写法不合法（例：`assistant:` 冒号后面是空的） |
| `enabled_plugin_missing` | error | 全局：`[plugins] enabled` 里的名字找不到对应插件 |
| `nothing_enabled` | warn | `enabled=1` 但功能列表为空 |
| `plugin_declares_no_feature` | warn | 写了 `插件:功能`，但该插件一个 feature 都没声明 |
| `channel_disabled` | info | `enabled=0`：整条频道关闭，列表里的功能也不响应 |
| `always_on` | info | `always` 插件写进名单里是多余的 |
| `plugin_disabled` | info | 全局：插件在磁盘上但没进 `enabled`，任何频道都不会响应 |

第二行是最常见的坑：目录建了、频道也勾了，唯独忘了 `enabled` —— 表现是**完全静默**。
启动日志也会打一条 `plugins_discovered_but_disabled` 把它点出来。

`haiq doctor` 也会顺带跑一遍（error 计入退出码），面板「白名单与风控」页顶部同样有
红/黄/绿横幅（`GET /api/channels/verify`）。
