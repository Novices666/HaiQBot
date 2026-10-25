# 快速开始：从零到群里收到回复

> 目标：30 分钟内让机器人在你的 QQ 群里回第一句话。
> 本文只走**最短路径**；多机部署、NSSM 服务、Docker 全栈、公网回源见 [DEPLOY.md](DEPLOY.md)。

**本文的 §0–§7 在「全新目录 + 全新 venv」上逐条跑过**（Windows 11 / Python 3.12 / uv 0.10，2026-10-25）：
`uv sync` → `.env` 写口令 → `doctor`（4 错 / 退出码 2）→ `webui token --generate` → `run`
（`GET /` 200、登录 200、`/api/overview` 有数据）→ `secrets set` → `channels enable/verify`
→ 面板接口改连接参数（`reconnected: ["onebot"]`）。**§8 必须在真群试**，本仓库替不了。

## 你会用到什么

| 组件 | 作用 | 必须吗 |
|---|---|---|
| **HaiQBot**（本仓库） | 框架本体 + 2 个最小插件（`echo`/`help`）+ 控制台 | 必须 |
| `plugins-extra/`（本仓库内） | 生图 / 对话 / 群管 / 风控四个插件，可选 | 想要这些功能才需要 |
| **一个 OneBot v11 协议端** | 把 QQ 账号变成 OneBot v11 接口（如 NapCat / Lagrange.OneBot / LLOneBot） | 接真 QQ 才需要 |

> **没有任何官方 QQ 账号申请流程。** OneBot 路线依赖第三方协议端，账号风险自负。

## 0. 准备

```bash
python --version      # 需要 3.11 或更高
uv --version          # 没有就装：https://docs.astral.sh/uv/
```

协议端按它自己的文档装（Docker、桌面版、QQ 插件……都行）；装法不影响本文任何一步。

## 1. 装框架

```bash
git clone https://github.com/Novices666/HaiQBot
cd HaiQBot
uv sync --extra dev
```

冷缓存首次约 5~6 分钟（要下 ~40 个包），之后是秒级。

> **没有发布到 PyPI。** `pip install haiqbot` 装不到东西，请用源码方式。
> 不想用 uv：`python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"`（Linux/macOS 用 `.venv/bin/pip`）。

**下文示例统一写成 `haiq <子命令>`**，实际跑的时候按平台换一种写法（三者等价）：

| 平台 / 习惯 | 怎么写 |
|---|---|
| Windows（口令在 `.env`） | `powershell -ExecutionPolicy Bypass -File scripts\haiq.ps1 <子命令>` |
| 任意平台、不想 activate | `uv run haiq <子命令>`（但 `.env` 不会被读，口令得自己 `export`/`$env:`） |
| 已 `activate` 了 `.venv` | 直接 `haiq <子命令>`（Linux/macOS 用 `.venv/bin/haiq`） |

> **仓库里已经有 `haiq.toml`**（`plugin_dirs`、`[plugins] enabled` 都填好了），
> 所以**不需要** `haiq init`。它只在没有配置的环境里用，而且不会覆盖已有文件 ——
> 对已有配置跑一次会得到 `× haiq.toml 已存在（加 --force 覆盖）`、退出码 1。

## 2. 给 vault 主口令（全新机器必做）

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
- 服务形态（NSSM / Docker / 服务器）口令怎么给：见 [DEPLOY.md](DEPLOY.md) 的 §0。

## 3. 不碰 QQ，先把服务跑起来

**这一步别跳过。** 它能在你动 QQ 之前证明框架本身是好的。

**Windows**（口令在 `.env` 里，用包装脚本；逐条对应本文实测结果）：

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

> **新装 `doctor` 一定报 4 个错误、退出码 2**（chat / image / OneBot token / 面板令牌全缺）。
> 这不是装坏了，是还没录凭据 —— 看「配置 / 依赖 / 插件来源」三项干净就行。
> 生成面板令牌后再跑一次，会变成 3 个错误（令牌那项变 `OK 来源 file`）。
> 漏了口令时会明确说「凭据库打不开」并退出码 2，不会抛 traceback。

另有一条零成本演示（Mock 适配器，不连 QQ、不烧额度）：

```bash
haiq run --demo
```

它**跑一段脚本对话就退出**（`/help` → `/画图` → `/问` → `/echo`），不起服务、不开面板 ——
用来确认「插件加载 + 命令分发 + 出图链路」是通的。想看面板请用上面的 `haiq run`。

浏览器打开 <http://127.0.0.1:8080>，粘上刚生成的令牌登录。
能看到「总览」有数据、侧栏 8 个页面，就说明框架是通的。`Ctrl+C` 停掉。

### Windows + NapCat：一条命令起整条链路（可选）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dev-up.ps1
```

> 这条脚本是**给 NapCat 容器准备的便利**，不是框架的一部分 —— 用别的协议端就自己起它，
> 然后按 §6 在面板里填连接。脚本做的事：起 Docker Desktop（已在跑就跳过）→ 起 `napcat` 容器
> （同名容器已存在就直接复用，不重建）→ 等 OneBot WS `3001` 监听 → 后台拉起 `haiq run`
> 并等 WebUI 就绪。NapCat 侧的容器细节、扫码登录见 [DEPLOY.md](DEPLOY.md) 的 C.2。

| 参数 | 作用 |
| :--- | :--- |
| `-SkipDocker` | 容器已在跑，只起 bot |
| `-SkipBot` | 只起容器，不起 bot |

口令不会出现在命令行上：`dev-up.ps1` 调 `scripts\run-with-env.ps1`，后者自己从环境变量 /
`.env` 取口令、设进自己的进程环境，再用 `cmd /c ... >> logs\haiq.out` 起 python
（命令行里只有脚本路径）。它拉起的进程是**分离**的，关窗口不会带走 bot；
停止请去任务管理器结束 `python.exe`。日志在 `logs\haiq.out`。

## 4. 录凭据

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

## 5. 接一个 OneBot v11 协议端

内核只认 **OneBot v11 这个协议**，不认具体实现 —— 你装哪个、装在哪、用什么方式装，
都不影响后面的步骤。常见的几个：

| 实现 | 形态 | 说明 |
|---|---|---|
| NapCat | Docker / 桌面版 | 容器细节见 [DEPLOY.md](DEPLOY.md) 的 C.2 |
| Lagrange.OneBot | 桌面版 | C# 实现 |
| LLOneBot | QQNT 插件 | 复用你已登录的 QQ 客户端 |
| 别的 | 只要实现 OneBot v11 | 能给出下面两样东西就行 |

它需要提供两样东西：

1. **连接方式**（看你在协议端那边开的是哪种）
   - **正向 WebSocket**（最常见）：协议端监听某个端口，例如 `ws://127.0.0.1:3001`，haiqbot 连过去
   - **HTTP / SSE**：协议端提供 HTTP API（例如 `http://127.0.0.1:3000`），事件走 SSE 或 POST 回我们
2. **access_token**：协议端里配的令牌。**别贴到任何聊天窗口里**，下一步只往面板里填。

> 本仓库不含任何协议端，也不替你选。第三方协议端有账号风险，自己判断。

## 6. 起来，然后在面板里把它接上

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
| 协议端 WS 地址 | 协议端给的正向 WS 地址。`3001` 只是常见默认值（NapCat 侧可改），以它显示给你的为准 |
| 本机监听地址 / 端口 | **只在需要入站时用**：`server`（我们起 WS 等协议端连入）和 `http`（我们起 HTTP 收事件上报），默认 `127.0.0.1:3002`。`client` / `sse` 模式忽略这两项 —— 它们连的地址在上面「协议端 WS 地址」和 `http_api_url` 里 |
| Access Token 键名 | `HAIQ_ONEBOT_ACCESS_TOKEN`（**键名**，不是令牌本身） |
| 机器人 QQ 号 | **留空即可**：事件和心跳都带 `self_id`，适配器会自己记下来；只有协议端从不发 `meta_event` 时才需要填 |

保存后会立刻按新参数重连接入端：响应里 `reconnected: ["onebot"]`，日志里 `adapters_restarted`，
不用重启进程。连上了就能看到：

```
onebot_connected     url=ws://127.0.0.1:3001
```

> HTTP / SSE 模式：四种连接方式怎么选、地址该怎么填，见 [DEPLOY.md](DEPLOY.md) 的 C.2.1。
> 不想用面板：改 `haiq.toml` 的 `[onebot]` 段 + `haiq secrets set HAIQ_ONEBOT_ACCESS_TOKEN`，
> 然后重启进程（这两项改了要重连/重启才生效）。

## 7. 开频道（默认全关）

不开频道，bot 收到消息什么都不做。

面板「白名单与风控」→ 新建频道 → `onebot:group:<你的群号>` → 勾选插件 → 保存。
或者用 CLI：

```bash
haiq channels enable onebot:group:<你的群号> --plugins echo,help,assistant
haiq channels verify          # 体检：有没有「配了却不生效」
```

## 8. 在群里验证

按顺序试，出问题时容易定位：

| 发什么 | 期望 | 说明 |
|---|---|---|
| `/echo 你好` | 回 `你好` | 验证消息通路 |
| `@机器人 /help` | 列出命令 | 验证插件加载 |
| `/问 1+1 等于几` | 回答案 | 单轮对话（不带上下文） |
| `@机器人 我叫小明` → `@机器人 我叫什么` | 第二句能记住 | 多轮对话，保留 `history_turns` 轮 |

## 自检清单

一条不对就照着排查：

- [ ] `haiq doctor`：除「凭据缺失」外没有 error（凭据录齐后退出码才是 0）
- [ ] 日志有 `plugins_ready`，`failed=[]`
- [ ] 日志有 `onebot_connected`
- [ ] 面板「白名单与风控」里那个群是**启用**状态
- [ ] 该群的 `plugins` 列表包含你想用的插件
- [ ] `haiq channels verify` 没报告静默失效

## 常见故障

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
| 文字正常、发图必失败 | **宿主机系统代理开着** → 协议端（尤其容器里的）上传被劫走。先关代理；NapCat 还要看它的 `baseTimeout` ≥ 60000 |
| 扫码/登录出不来二维码 | 那是协议端的事，看它的文档；NapCat 见 [DEPLOY.md](DEPLOY.md) 的 C.2 |
| 面板登录不了 | 令牌没生成：`haiq webui token --generate`；或口令变了导致读不到令牌（重启服务） |
| 改了插件配置没生效 | `ctx.config` 是活视图，但插件读配置的方式要对（`Mapping` 而非 `dict`）；纯代码改动必须重启 |

更细的排障（媒体发送、SSE/HTTP 模式、代理实验矩阵、频道体检）见 [DEPLOY.md](DEPLOY.md)。

## 下一步

- 写自己的插件 → [PLUGIN-GUIDE.md](PLUGIN-GUIDE.md)
- 理解为什么这样设计 → [ARCHITECTURE.md](ARCHITECTURE.md)
- 部署成服务 / 上服务器 → [DEPLOY.md](DEPLOY.md)
