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

## 快速开始

30 分钟内让机器人在你的 QQ 群里回第一句话。下面是**最短路径**（Windows / Linux 两套命令、
每个配置项填什么、14 条故障怎么排查，见 **[docs/INSTALL.md](docs/INSTALL.md)**）。

```bash
git clone https://github.com/Novices666/HaiQBot && cd HaiQBot
uv sync --extra dev                        # 需要 Python ≥ 3.11 与 uv
```

### 1. 给 vault 主口令

凭据库默认加密，**主口令只有一个来源：环境变量 `HAIQ_VAULT_PASSPHRASE`**。
干净克隆里没有 `.env`，复制模板再加一行（`.env` 已 gitignore，后面的 API Key 也写这里）：

```bash
cp .env.example .env                                  # PowerShell: Copy-Item .env.example .env
```

再往 `.env` 里加一行（用编辑器加，别用 `>>` 追加重定向）：

```bash
HAIQ_VAULT_PASSPHRASE=<自定口令>
```

> **PowerShell 5.1 的 `echo ... >> .env` 会把文件写成 UTF-16LE（带 BOM）**，之后 `findstr`
> 和各种 `.env` 解析都认不出来。要用命令行加就写
> `Add-Content .env "HAIQ_VAULT_PASSPHRASE=<自定口令>"`（实测输出无 BOM）。

> **口令丢了 = 现有凭据解不开**，没法找回，存进密码管理器。
> 只在本机自用、`secrets/` 不会被拷走？可把 `[vault] enc` 改成 `"none"` 跳过整段口令。

### 2. 先不碰 QQ，把服务跑起来

这一步别跳过 —— 它能在动 QQ 之前证明框架本身是好的。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\haiq.ps1 doctor                  # 自检
powershell -ExecutionPolicy Bypass -File scripts\haiq.ps1 webui token --generate  # 面板令牌（只打印一次）
powershell -ExecutionPolicy Bypass -File scripts\haiq.ps1 run                     # 启动
```

打开 <http://127.0.0.1:8080>，用刚生成的令牌登录。看到「总览」有数据、侧栏 8 个页面，框架就是通的。

> 新装 `doctor` **一定**报「凭据缺失」类错误（还没录凭据）。退出码规则：**有 error = 2，
> 只有 warning = 1，干净 = 0**；warning 常见来源是系统代理 —— 它会让「文字正常、发图失败」。
> 零成本试跑：`haiq run --demo`（Mock 适配器，不连 QQ、跑完即退）。

### 3. 接协议端，并在面板里填连接

内核只认 **OneBot v11 这个协议** —— NapCat / Lagrange / LLOneBot 都一样。在协议端那边开正向
WebSocket（最常见），记下地址与 `access_token`，然后面板两步：

1. **凭据管理 → 新建**：键名 `HAIQ_ONEBOT_ACCESS_TOKEN`，值填 access_token（列表只显示「已设置」）。
2. **配置中心 → 接入**：启用 OneBot、角色 `client`、填 WS 地址、Access Token 键名填上面那个键名。
   机器人 QQ 号**留空即可**（事件自带 `self_id`）。保存后**就地重连**，不用重启进程。

日志出现 `onebot_connected` 就是通了。逐字段说明、`server` / `http` / `sse` 三种模式见
[docs/INSTALL.md](docs/INSTALL.md) §6。

### 4. 开频道（默认全关）

不开频道，bot 收到消息什么都不做。

```bash
haiq channels enable onebot:group:<你的群号> --plugins echo,help,assistant
haiq channels verify          # 体检：有没有「配了却不生效」
```

### 5. 在群里验证

| 发什么 | 期望 |
|---|---|
| `/echo 你好` | 回 `你好`（消息通路 OK） |
| `@机器人 /help` | 列出命令（插件加载 OK） |
| `@机器人 我叫小明` → `@机器人 我叫什么` | 第二句能记住（多轮对话 OK） |

卡住了看 [docs/INSTALL.md](docs/INSTALL.md) 的「自检清单」与「常见故障」。

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

一个最小插件就是 `__init__.py` + `plugin.toml` 两个文件，写法和内置能力见
**[docs/PLUGIN-GUIDE.md](docs/PLUGIN-GUIDE.md)**。

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

管线的六个中间件、事件面归一化、去重与超时细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 安全模型

四道闸：**真值只进 vault**（`secrets/` 已 gitignore + gitleaks pre-commit）、
**日志自动脱敏**（vault 取值即登记，出现即打码）、**不进 URL**（对外只发 `/i/{随机ID}`）、
**入口不裸奔**（面板 / 媒体 / 反向 WS 只监听 `127.0.0.1`）。

面板侧另有一条：列表接口永远不返回值本体，看单条明文要单独请求，且每次都写 `secret.reveal` 审计。

机制与取舍见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 的「防泄露四道闸」。

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
| **[docs/INSTALL.md](docs/INSTALL.md)** | 安装与首次配置（详细版）：两套平台命令、逐字段说明、`[policy]`、自检清单、常见故障全表 |
| **[docs/DEPLOY.md](docs/DEPLOY.md)** | 部署运维：本机 / NSSM 服务 / Docker Compose / 大陆网络 / 深度排障 |
| **[docs/PLUGIN-GUIDE.md](docs/PLUGIN-GUIDE.md)** | 写插件：触发器、上下文、凭据槽位、服务契约、自定义面板页 |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | 分层、能力协商、密钥隔离、事件管线、模块速查、归属决策台账 |
| **[docs/WEBUI-SPEC.md](docs/WEBUI-SPEC.md)** | 控制台契约：路由、鉴权、PanelPage |
| **[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)** | 开发：环境、测试、代码地图、文档规范、发布约定 |
| **[CONTRIBUTING.md](CONTRIBUTING.md)** | 参与贡献：能提什么、提交前必跑什么、提交信息与 PR 要求 |

## 维护与许可

维护者 [@Novices666](https://github.com/Novices666)。问题请走 issue，漏洞与凭据相关问题不要开公开
issue（见 [CONTRIBUTING.md](CONTRIBUTING.md)）。

[MIT](LICENSE)
