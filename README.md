# HaiQBot

**QQ Bot 框架：OneBot v11 + 官方 OpenAPI 双通道，插件与内核彻底分离。**

[![CI](https://github.com/Novices666/HaiQBot/actions/workflows/ci.yml/badge.svg)](https://github.com/Novices666/HaiQBot/actions/workflows/ci.yml)

![Python](https://img.shields.io/badge/python-3.11%2B-3776ab)

![License](https://img.shields.io/badge/license-MIT-green)

> 插件永远拿不到密钥，也永远不需要知道自己跑在哪个平台。

---

## 它是什么

一个把「协议通道」和「插件能力」彻底拆开的 QQ 机器人框架：

- **内核只做四件事**：收事件、判定谁能处理、管出站闸门、提供受限句柄。
- **插件是独立目录**，放在仓库根的 `plugins/` 里，和第三方插件走同一条加载路径。内核代码里没有任何一个插件的名字。
- **密钥只活在 vault**：配置里永远只有 `*_ref` 键名，插件通过槽位句柄取用，真值不进日志、不进 URL、不回显。

## 它不是什么

- 不是开箱即用的成品机器人 —— 装完默认**什么都不响应**，要你去面板开频道。
- 不是插件市场 —— 没有在线安装，插件就是目录或 pip 包。
- 不替你申请 QQ 账号 —— OneBot 路线需要一个第三方协议端（NapCat / Lagrange），账号风险自负。

## 特性

| | |
|---|---|
| **双通道** | OneBot v11（client / server / http / sse 四种模式）+ 官方 QQ OpenAPI，同一套插件代码 |
| **能力协商** | 插件不写 `if platform == "qq"`，按目标平台能力自动降级（发图 → 发链接 → 文本提示） |
| **凭据隔离** | AES-256-GCM（scrypt 派生）+ 可轮换主口令；插件只能拿 `SecretHandle`，拿不到明文 |
| **频道开关** | 默认全关，按群按功能粒度开启；内置体检按 10 个判定码点出「配了却不生效」 |
| **限流与风控** | 出站闸门（每群间隔 + 全局每分钟）、每用户/每群配额、幂等去重、刷屏拦截 |
| **控制台** | 8 个页面 + 插件可自注册页面（声明式 schema，不注入 JS） |
| **可观测** | 结构化脱敏日志（SSE 实时推送）、审计流水、调用统计、媒体库 |
| **定时任务** | 持久化（落库）、`interval` / `daily` 两种调度、失败不中断 |

## 快速开始：从零到群里收到回复

> 目标：30 分钟内让机器人在你的 QQ 群里回第一句话。本节只走**最短路径**；
> 多机部署、NSSM 服务、Docker 全栈、公网回源见 [docs/DEPLOY.md](docs/DEPLOY.md)。

**本节 §0–§7 在「全新目录 + 全新 venv」上逐条跑过**（Windows 11 / Python 3.12 / uv 0.10）：
`uv sync` → `.env` 写口令 → `doctor`（「凭据缺失」若干 / 退出码 2）→ `webui token --generate` → `run`
（`GET /` 200、登录 200、`/api/overview` 有数据）→ `secrets set` → `channels enable/verify`
→ 面板接口改连接参数（`reconnected: ["onebot"]`）。**§8 必须在真群试**，本仓库替不了。

### 0. 你会用到什么

| 组件 | 作用 | 必须吗 |
|---|---|---|
| **HaiQBot**（本仓库） | 框架本体 + 2 个最小插件（`echo`/`help`）+ 控制台 | 必须 |
| `plugins-extra/`（本仓库内） | 生图 / 对话 / 群管 / 风控四个插件，可选 | 想要这些功能才需要 |
| **一个 OneBot v11 协议端** | 把 QQ 账号变成 OneBot v11 接口（如 NapCat / Lagrange.OneBot / LLOneBot） | 接真 QQ 才需要 |

> **没有任何官方 QQ 账号申请流程。** OneBot 路线依赖第三方协议端，账号风险自负。

### 1. 准备与安装

```bash
python --version      # 需要 3.11 或更高
uv --version          # 没有就装：https://docs.astral.sh/uv/
```

```bash
git clone https://github.com/Novices666/HaiQBot
cd HaiQBot
uv sync --extra dev
```

冷缓存首次约 5~6 分钟（要下 ~40 个包），之后是秒级。

> **没有发布到 PyPI。** `pip install haiqbot` 装不到东西，请用源码方式。
> 不想用 uv：`python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"`（Linux/macOS 用 `.venv/bin/pip`）。

协议端按它自己的文档装（Docker、桌面版、QQ 插件……都行）；装法不影响本节任何一步。

**下文示例统一写成 `haiq <子命令>`**，实际跑的时候按平台换一种写法（三者等价）：

| 平台 / 习惯 | 怎么写 |
|---|---|
| Windows（口令在 `.env`） | `powershell -ExecutionPolicy Bypass -File scripts\haiq.ps1 <子命令>` |
| 任意平台、不想 activate | `uv run haiq <子命令>`（但 `.env` 不会被读，口令得自己 `export`/`$env:`） |
| 已 `activate` 了 `.venv` | 直接 `haiq <子命令>`（Linux/macOS 用 `.venv/bin/haiq`） |

> **仓库里已经有 `haiq.toml`**（`plugin_dirs`、`[plugins] enabled` 都填好了），
> 所以**不需要** `haiq init`。它只在没有配置的环境里用，而且不会覆盖已有文件 ——
> 对已有配置跑一次会得到 `× haiq.toml 已存在（加 --force 覆盖）`、退出码 1。

### 2. 给 vault 主口令（全新机器必做）

> **只在本机自用、`secrets/` 不会被拷走？** 可以把 `haiq.toml` 的 `[vault] enc` 改成 `"none"`，
> 直接跳过本节整段口令环节（代价：明文，谁读到文件谁拿到密钥）。判断标准见
> [docs/DEPLOY.md](docs/DEPLOY.md) 的「要不要加密」。

凭据库默认 AES-256-GCM 加密，**主口令只有一个来源：环境变量 `HAIQ_VAULT_PASSPHRASE`**
（键名可改，见 `[vault] passphrase_ref`）。干净克隆里既没有 `secrets/vault.json`、也没有口令，所以：

- `haiq run` 能起来，但面板登录不了（日志里 `webui_token_missing`）；
- `scripts\dev-up.ps1` 会停在这一步，并告诉你去哪儿补。

本机开发：**干净克隆里没有 `.env`**，先从模板复制一份，再把口令写进去
（`.env` 已在 `.gitignore`，不入库；后面加 API Key 也写这里）：

```bash
cp .env.example .env            # PowerShell: Copy-Item .env.example .env
```

然后加一行：

```bash
HAIQ_VAULT_PASSPHRASE=<自定口令>
```

口令长度技术上不强制，但别用弱口令（`haiq secrets passwd` 轮换时要求 ≥ 12 位）。
只想对当前终端有效也行（Linux/macOS 用 `export`），不写 `.env`：

```powershell
$env:HAIQ_VAULT_PASSPHRASE = "<自定口令>"
```

要点：

- **口令丢了 = 现有凭据解不开**（没法找回，只能重新录入）。存进密码管理器。
- 改口令：`haiq secrets passwd` 交互式重写 vault.json，然后把 `.env` / 服务环境变量里的值同步改掉。
- **别在 `haiq.toml` 里写口令** —— 那里只放 `*_ref` 键名。
- 服务形态（NSSM / Docker / 服务器）口令怎么给：见 [docs/DEPLOY.md](docs/DEPLOY.md) 的 §0。

### 3. 不碰 QQ，先把服务跑起来

**这一步别跳过。** 它能在你动 QQ 之前证明框架本身是好的。

**Windows**（口令在 `.env` 里，用包装脚本）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\haiq.ps1 doctor                 # 自检
powershell -ExecutionPolicy Bypass -File scripts\haiq.ps1 webui token --generate # 面板令牌（只打印一次）
powershell -ExecutionPolicy Bypass -File scripts\haiq.ps1 run                    # 前台启动
```

**Linux / macOS**（先把口令放环境变量，再直接用 venv 里的 `haiq`）：

```bash
export HAIQ_VAULT_PASSPHRASE='<自定口令>'
uv run haiq doctor
uv run haiq webui token --generate
uv run haiq run
```

> 干净克隆里 `haiq` **不在 PATH 上**：要么 `uv run haiq ...`，要么用 Windows 那个包装脚本，
> 要么先 `activate` venv。直接敲 `haiq` 会得到「命令不存在」。

> **新装 `doctor` 一定报「凭据缺失」类错误、退出码 2** —— 几条取决于你带着多少插件：
> 仓库自带 `plugins-extra/` 时，`assistant` 的对话 / 生图两个密钥是**必需**项，
> 加面板令牌一共 3 条；只留 `plugins/`（echo / help）时只有面板令牌这 1 条。
> 这不是装坏了，是还没录凭据 —— 看「配置 / 依赖 / 插件来源」三项干净就行。
> 生成面板令牌后再跑一次，会少一条（令牌那项变 `OK 来源 file`）。
> 漏了口令时会明确说「凭据库打不开」并退出码 2，不会抛 traceback。

另有一条零成本演示（Mock 适配器，不连 QQ、不烧额度）：

```bash
haiq run --demo
```

它**跑一段脚本对话就退出**（`/help` → `/画图` → `/问` → `/echo`），不起服务、不开面板 ——
用来确认「插件加载 + 命令分发 + 出图链路」是通的。想看面板请用上面的 `haiq run`。

浏览器打开 <http://127.0.0.1:8080>，粘上刚生成的令牌登录。
能看到「总览」有数据、侧栏 8 个页面，就说明框架是通的。`Ctrl+C` 停掉。

> **整个面板都要登录**（读接口也不公开）：令牌用 `haiq webui token --generate` 生成，只显示一次；
> 会话 12 小时有效，重启进程不用重新登录。

> **WebUI 已随包分发**（`src/haiqbot/webui/dist/` 是构建产物），跑起来不需要 Node.js。
> 想改前端才需要 `cd web && npm ci && npm run dev`，见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

### 4. 录凭据

真值只进 vault，配置文件里只写 `*_ref` 键名。

```bash
haiq secrets set HAIQ_CHAT_API_KEY     # 对话（按需，没有就跳过）
haiq secrets set HAIQ_IMAGE_API_KEY    # 生图（按需，没有就跳过）
haiq secrets list                      # 看哪些槽位还是「未设置」
```

`secrets set` 是**交互式输入、不回显、不进 shell history**。
也可以写槽位名（`haiq secrets set assistant.image_api_key`），效果一样。

> 口令来源只有一处：环境变量 `HAIQ_VAULT_PASSPHRASE`（本机放在 `.env`，由启动器读进去）。
> 想彻底关掉加密：`haiq.toml` 里 `[vault] enc = "none"` —— 只在你确信这台机器没人能读时用。

### 5. 接一个 OneBot v11 协议端

内核只认 **OneBot v11 这个协议**，不认具体实现 —— 你装哪个、装在哪、用什么方式装，
都不影响后面的步骤。常见的几个：

| 实现 | 形态 | 说明 |
|---|---|---|
| NapCat | Docker / 桌面版 | 只是举例；它的容器与扫码见 [docs/DEPLOY.md](docs/DEPLOY.md) 的附录 |
| Lagrange.OneBot | 桌面版 | C# 实现 |
| LLOneBot | QQNT 插件 | 复用你已登录的 QQ 客户端 |
| 别的 | 只要实现 OneBot v11 | 能给出下面两样东西就行 |

它需要提供两样东西：

1. **连接方式**（看你在协议端那边开的是哪种）
   - **正向 WebSocket**（最常见）：协议端监听某个端口，例如 `ws://127.0.0.1:3001`，haiqbot 连过去
   - **HTTP / SSE**：协议端提供 HTTP API（例如 `http://127.0.0.1:3000`），事件走 SSE 或 POST 回我们
2. **access_token**：协议端里配的令牌。**别贴到任何聊天窗口里**，下一步只往面板里填。

> 本仓库不含任何协议端，也不替你选。第三方协议端有账号风险，自己判断。

### 6. 起来，然后在面板里把它接上

```bash
haiq run
```

这时日志会一直刷 `onebot_reconnect ... ConnectionRefusedError`（1s/2s/4s… 退避）——
正常，因为还没配连接；面板和 CLI 都照常可用。

打开 <http://127.0.0.1:8080>（令牌见 §3），两步配完：

**① 凭据管理 → 新建**：键名 `HAIQ_ONEBOT_ACCESS_TOKEN`，值填协议端给的 access_token。
列表里只显示「已设置」，不回显真值（看单条明文会落一条 `secret.reveal` 审计）。

**② 配置中心 →「接入」分组**，填完保存：

| 字段 | 填什么 |
|---|---|
| 启用 OneBot | 开 |
| 连接角色 | `client`（正向 WS，最常见）／`server`（我们监听，等协议端连入） |
| 协议端 WS 地址 | 协议端给的正向 WS 地址。`3001` 只是常见默认值（协议端侧可改），以它显示给你的为准 |
| 本机监听地址 / 端口 | **只在需要入站时用**：`server`（我们起 WS 等协议端连入）和 `http`（我们起 HTTP 收事件上报），默认 `127.0.0.1:3002`。`client` / `sse` 模式忽略这两项 —— 它们连的地址在上面「协议端 WS 地址」和 `http_api_url` 里 |
| Access Token 键名 | `HAIQ_ONEBOT_ACCESS_TOKEN`（**键名**，不是令牌本身） |
| 机器人 QQ 号 | **留空即可**：事件和心跳都带 `self_id`，适配器会自己记下来；只有协议端从不发 `meta_event` 时才需要填 |

保存后会立刻按新参数重连接入端：响应里 `reconnected: ["onebot"]`，日志里 `adapters_restarted`，
不用重启进程。连上了就能看到：

```
onebot_connected     url=ws://127.0.0.1:3001
```

> HTTP / SSE 模式：四种连接方式怎么选、地址该怎么填，见 [docs/DEPLOY.md](docs/DEPLOY.md) 的 C.2。
> 不想用面板：改 `haiq.toml` 的 `[onebot]` 段 + `haiq secrets set HAIQ_ONEBOT_ACCESS_TOKEN`，
> 然后重启进程（这两项改了要重连/重启才生效）。

### 7. 开频道（默认全关）

不开频道，bot 收到消息什么都不做。

面板「白名单与风控」→ 新建频道 → `onebot:group:<你的群号>` → 勾选插件 → 保存。
或者用 CLI：

```bash
haiq channels enable onebot:group:<你的群号> --plugins echo,help,assistant
haiq channels verify          # 体检：有没有「配了却不生效」
```

#### 顺带：谁能用管理命令、哪些频道不处理（`[policy]`）

频道开关决定「这个群里跑哪些插件」，`[policy]` 决定「谁是管理员」和「哪些频道一律不碰」。
两者都在 `haiq.toml`，面板「配置中心」也能改：

```toml
[policy]
owners = ["10001"]                 # 主人：最高权限
admins = ["10002"]                 # 管理员：能用 admin 插件的命令
channel_whitelist = []             # 留空 = 不限制；填了就只处理这些频道
channel_blacklist = []             # 优先级高于白名单，写了就一律丢
handle_timeout_seconds = 8.0       # 单个处理器超时
max_handlers_per_event = 1         # 同一事件最多跑几个处理器
```

频道写法与 `haiq channels` 一致（`onebot:group:<群号>`）。`owners` / `admins` 填的是
**发送者 QQ 号**；配置只能给权限「加码」——平台本身给群主的身份不会被配置降级。

### 8. 在群里验证

按顺序试，出问题时容易定位：

| 发什么 | 期望 | 说明 |
|---|---|---|
| `/echo 你好` | 回 `你好` | 验证消息通路 |
| `@机器人 /help` | 列出命令 | 验证插件加载 |
| `/问 1+1 等于几` | 回答案 | 单轮对话（不带上下文） |
| `@机器人 我叫小明` → `@机器人 我叫什么` | 第二句能记住 | 多轮对话，保留 `chat_history_turns` 轮 |

### 自检清单

一条不对就照着排查：

- [ ] `haiq doctor`：除「凭据缺失」外没有 error。退出码规则：**有 error = 2，只有 warning = 1，干净 = 0**
      （warning 常见来源是系统代理，它会让「文字正常、发图失败」，所以要当回事）
- [ ] 日志有 `plugins_ready`，`failed=[]`
- [ ] 日志有 `onebot_connected`
- [ ] 面板「白名单与风控」里那个群是**启用**状态
- [ ] 该群的 `plugins` 列表包含你想用的插件
- [ ] `haiq channels verify` 没报告静默失效

### 常见故障

| 现象 | 原因 / 怎么修 |
|---|---|
| `haiq`：命令不存在 | 干净克隆里它不在 PATH：用 `uv run haiq ...`、`scripts\haiq.ps1`，或先 activate venv（见 §1） |
| `凭据库打不开：vault 已加密但未提供密码` | 当前 shell 没有 `HAIQ_VAULT_PASSPHRASE`：写进 `.env` 并用 `scripts\haiq.ps1`，或先 `$env:`/`export`；见 §2 |
| `警告：没拿到 vault 主口令` | 同上；`doctor` 也会这么提示 |
| `× haiq.toml 已存在（加 --force 覆盖）` | 仓库本来就带配置，不需要 `haiq init`。真要重生成加 `--force` |
| 跑完 `run --demo` 就打不开面板 | demo 是脚本演示，跑完即退、不起服务；要看面板用 `haiq run` |
| 面板里改了连接参数，没连上 | 保存后会就地重连（提示「已按新参数重连接入端」）；仍连不上就看协议端那边端口/token 对不对 |
| 日志一直刷 `onebot_reconnect` | 协议端没起、地址写错、token 不匹配——三者之一。地址和 token 都在面板「配置中心」核对 |
| 群里 @ 了没反应 | 频道没开（默认全关）；或该群 `plugins` 列表里没有对应插件。跑 `haiq channels verify` |
| 日志里 `插件来源 0 个` | `plugin_dirs` 写错位置（必须放在任何 `[section]` 前）或路径不存在（相对路径按**启动时工作目录**解析） |
| 换过口令后一直解不开 | 派生密钥在进程内缓存，改口令必须重启服务 |
| 文字正常、发图必失败 | **宿主机系统代理开着** → 协议端（尤其容器里的）上传被劫走。先关代理；NapCat 这类协议端还要看它自己的上传超时（`baseTimeout` ≥ 60000） |
| 扫码/登录出不来二维码 | 那是协议端的事，看它自己的文档；以 NapCat 为例见 [docs/DEPLOY.md](docs/DEPLOY.md) 的附录 |
| 面板登录不了 | 令牌没生成：`haiq webui token --generate`；或口令变了导致读不到令牌（重启服务） |
| 改了插件配置没生效 | `ctx.config` 是活视图，但插件读配置的方式要对（`Mapping` 而非 `dict`）；纯代码改动必须重启 |

更细的排障（媒体发送、SSE/HTTP 模式、代理实验矩阵、频道体检）见 [docs/DEPLOY.md](docs/DEPLOY.md)。

## 插件放哪

```
HaiQBot/
├── plugins/                 ← 框架最小集：echo（回显）、help（列命令）
│   └── echo/  __init__.py  plugin.toml
├── plugins-extra/           ← 你自己的插件（框架升级不会碰）
│   └── <你的插件>/           ← haiq plugin new <id> --dir plugins-extra
├── src/haiqbot/             ← 内核，没有插件代码
└── haiq.toml                ← plugin_dirs + [plugins] enabled
```

两个目录都在 `plugin_dirs` 里，不存在的会被静默跳过 —— 全新环境只建 `plugins/` 也能跑。
框架自带的只有 `echo` / `help`；生图、对话、群管、风控这些绑定具体 API Key 的插件在
`plugins-extra/`，不属于框架（见 [plugins-extra/README.md](plugins-extra/README.md)）。

最小插件就两个文件：

```python
# plugins/hello/__init__.py
from haiqbot import Context, Plugin, on_command


class Hello(Plugin):
    id = "hello"
    name = "打招呼"

    @on_command("hi", usage="hi <名字>")
    async def hi(self, ctx: Context, name: str = "") -> None:
        await ctx.reply.text(f"你好，{name or '陌生人'}")
```

```toml
# plugins/hello/plugin.toml
id = "hello"
name = "打招呼"
version = "0.1.0"
api_version = 1
isolation = "inline"
```

再把它加进 `haiq.toml` 的 `[plugins] enabled`，重启即可。详见 **[docs/PLUGIN-GUIDE.md](docs/PLUGIN-GUIDE.md)**。

## 装别人的插件

内核只认两种分发形态，其余一概不认（这样插件生态不会被某个目录约定绑死）：

| 形态 | 适合 | 怎么装 |
|---|---|---|
| **本地目录**（含 `plugin.toml`） | 自己写、快速迭代 | 目录放进 `plugin_dirs` 指向的地方（默认 `plugins/`） |
| **pip 包**（entry point） | 发布给别人用 | `pip install haiqbot-plugin-xxx` → `haiq plugin enable xxx` |

```bash
haiq plugin list            # 看装了什么、从哪来的（entrypoint / local）
haiq plugin enable  <id>    # 写进 plugins.enabled，重启生效
haiq plugin disable <id>
```

> ⚠️ `plugin_dirs` 是**顶层字段**，必须写在 `haiq.toml` 里第一个 `[section]` 之前。
> 写进别的段会变成段内键被静默忽略 —— 框架会告警 `config_field_misplaced`，
> `haiq doctor` 也会提示「插件来源 0 个」。相对路径按**启动时的工作目录**解析。

## 架构一眼

```
协议端 ──event──▶ Adapter ──▶ Bus（去重 / 频道闸门 / 配额 / 超时）
                                 │
                                 ▼
                          Matcher 选规则（权限、频道、优先级）
                                 │
                                 ▼
                           Plugin handler ──▶ ctx.reply / ctx.service
                                 │
                                 ▼
                       OutboundLimiter（每群间隔 / 全局速率 / 重试）
                                 │
                                 ▼
                             Adapter ──▶ 平台
```

三道硬边界：

1. **内核不认识插件** —— 没有 `if plugin_id == "assistant"`，只有规则与服务契约。
2. **插件不认识平台** —— 能力协商 + 自动降级，插件里没有平台分支。
3. **插件不认识密钥** —— 只有槽位句柄，真值在 vault。

## 安全模型

四道闸，任何一道破了都算严重问题：

| 闸 | 落地方式 |
|---|---|
| **不进仓库** | 真值只进 `secrets/`（已 gitignore）+ pre-commit 防误提交 |
| **不进日志** | structlog 脱敏 processor；vault 取值即登记，日志里出现就打成 `***` |
| **不进 URL** | 生图结果落本地中转，对外只发 `/i/{随机ID}` —— 无参数、不可枚举 |
| **不裸奔** | 媒体服务与 OneBot 反向 WS 只监听 `127.0.0.1`，公网最多反代 `/i/*` |

面板侧另有一条：**列表接口永远不返回值本体**，看单条明文要单独请求，
且每次都写 `secret.reveal` 审计（谁在什么时候看过密钥是可见的）。

细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 命令行

```bash
haiq init                        # 生成配置与 secrets/
haiq doctor                      # 自检（退出码：有 error=2 / 只有 warning=1 / 干净=0）
haiq run [--demo]                # 启动
haiq channels list|verify|enable|disable [targets...]   # 频道开关（应急入口）
haiq jobs list|add|enable|disable|rm                    # 定时任务（应急入口）
haiq secrets list|set|rm|export|reencrypt|passwd        # 凭据管理
haiq plugin list|new|reload|enable|disable <id>         # 插件管理（new = 生成骨架）
haiq webui token [--generate]                           # 面板令牌
```

`haiq vault` 是 `haiq secrets` 的旧名，留着兼容；`set`/`list`/`rm` 之外的新东西
（`passwd` / `reencrypt` / 槽位名）只在 `secrets` 下。

`channels` / `jobs` 只依赖数据库，**服务没跑也能管理** —— 这是面板挂掉时的兜底。

## 当前状态（诚实版）

| 项 | 状态 |
|---|---|
| OneBot v11 四种模式 | ✅ 真机验证过（NapCat 4.18 + Docker） |
| 官方 OpenAPI 通道 | ⚠️ 代码层面已适配（失败语义 / 健康度 / actions 声明都有单测），**从未真机验证**（缺 AppID/AppSecret） |
| 随仓库插件 | ✅ 框架最小集 echo / help；自有 assistant / guard / admin |
| 控制台 8 页 + 插件页 | ✅ 真实 Chromium 点过一遍，console 零错误 |
| 测试 | 774 passed；`ruff check` / `format` 全绿 |
| 图片发送 | ⚠️ 偶发 `rich media transfer failed`；宿主机开着系统代理时必失败 |
| 面板内重启/停机 | ❌ 未做（需要进程守护，否则退出后没人拉起） |
| 子进程隔离 | ❌ 未做（插件一律 inline 加载，`isolation` 目前只是声明） |

## 文档

| 文档 | 内容 |
|---|---|
| **本 README** | 初次使用：从零到群里收到回复（含协议端接入、自检清单、常见故障） |
| **[docs/DEPLOY.md](docs/DEPLOY.md)** | 部署运维：本机 / NSSM 服务 / Docker Compose / 大陆网络 / 深度排障 |
| **[docs/PLUGIN-GUIDE.md](docs/PLUGIN-GUIDE.md)** | 写插件：触发器、上下文、凭据槽位、服务契约、自定义面板页 |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | 分层、能力协商、密钥隔离、事件管线、模块速查、归属决策台账 |
| **[docs/WEBUI-SPEC.md](docs/WEBUI-SPEC.md)** | 控制台契约：路由、鉴权、PanelPage |
| **[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)** | 开发：环境、测试、代码地图、文档规范、发布约定 |
| **[CONTRIBUTING.md](CONTRIBUTING.md)** | 参与贡献：能提什么、提交前必跑什么、提交信息与 PR 要求 |

## 许可证

[MIT](LICENSE)
