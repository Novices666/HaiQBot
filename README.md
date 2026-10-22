# HaiQBot

**QQ Bot 框架：OneBot v11 + 官方 OpenAPI 双通道，插件与内核彻底分离。**

[![CI](https://github.com/Novices666/HaiQBot/actions/workflows/ci.yml/badge.svg)](https://github.com/Novices666/HaiQBot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-3776ab)
![License](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-534%20passed-brightgreen)

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
| **频道开关** | 默认全关，按群按功能粒度开启；内置体检能识别 5 类「配了却不生效」 |
| **限流与风控** | 出站闸门（每群间隔 + 全局每分钟）、每用户/每群配额、幂等去重、刷屏拦截 |
| **控制台** | 8 个页面 + 插件可自注册页面（声明式 schema，不注入 JS） |
| **可观测** | 结构化脱敏日志（SSE 实时推送）、审计流水、调用统计、媒体库 |
| **定时任务** | 持久化（落库）、`interval` / `daily` 两种调度、失败不中断 |

## 快速开始

```bash
git clone https://github.com/Novices666/HaiQBot && cd HaiQBot
uv sync --extra dev          # 也可以用 pip install -e ".[dev]"
uv run haiq init             # 生成 haiq.toml + secrets/
uv run haiq doctor           # 自检：依赖 / 凭据 / 频道 / 插件来源
uv run haiq run --demo       # 不连 QQ、不烧额度的本地演示
```

> **WebUI 已随包分发**（`src/haiqbot/webui/dist/` 是构建产物），跑起来不需要 Node.js。
> 想改前端才需要 `cd web && npm ci && npm run dev`。

跑起来后打开 <http://127.0.0.1:8080>，令牌在 vault 里：

```bash
uv run haiq webui token --generate   # 生成并写入 vault
```

> **没有发布到 PyPI。** `pip install haiqbot` 现在装不到东西，请用上面的源码方式。

接真 QQ（OneBot v11）三步：

1. 装协议端（推荐 [NapCat](https://github.com/NapNeko/NapCatQQ)），扫码登录
2. `haiq.toml` 里填 `[onebot] url` 与 `access_token_ref`，然后 `haiq secrets set WEFT_ONEBOT_ACCESS_TOKEN`
3. `haiq run` → 去面板「白名单与风控」把群加进来

完整流程见 **[docs/QUICKSTART.md](docs/QUICKSTART.md)**，含 NapCat 容器、扫码、排障。

## 插件放哪

```
HaiQBot/
├── plugins/                 ← 所有插件都在这（随仓库分发的 6 个也在这里）
│   ├── echo/  __init__.py  plugin.toml
│   ├── help/  admin/  painter/  chat/  guard/
│   └── <你自己的插件>/       ← 新增插件就放这
├── src/haiqbot/             ← 内核，没有插件代码
└── haiq.toml                ← plugin_dirs + [plugins] enabled
```

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

## 文档

| 文档 | 内容 |
|---|---|
| **[docs/QUICKSTART.md](docs/QUICKSTART.md)** | 从零到群里收到回复（含 NapCat 扫码与常见故障） |
| **[docs/PLUGIN-GUIDE.md](docs/PLUGIN-GUIDE.md)** | 写插件：触发器、上下文、凭据槽位、服务契约、自定义面板页 |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | 分层、能力协商、密钥隔离、事件管线、模块速查 |
| **[docs/DEPLOY.md](docs/DEPLOY.md)** | 部署：本机 / NSSM 服务 / Docker Compose，以及运维排障 |
| **[docs/WEBUI-SPEC.md](docs/WEBUI-SPEC.md)** | 控制台契约：路由、鉴权、PanelPage |
| **[docs/REFRACTOR-PLAN.md](docs/REFRACTOR-PLAN.md)** | 设计取舍的来龙去脉（为什么这样拆） |

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

1. **内核不认识插件** —— 没有 `if plugin_id == "painter"`，只有规则与服务契约。
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

细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#防泄露四道闸)。

## 命令行

```bash
haiq init                        # 生成配置与 secrets/
haiq doctor                      # 自检（有问题会直接说，退出码 1/2）
haiq run [--demo]                # 启动
haiq channels list|verify|enable|disable [targets...]   # 频道开关（应急入口）
haiq jobs list|add|enable|disable|rm                    # 定时任务（应急入口）
haiq secrets list|set|rm|export|passwd                  # 凭据管理
haiq plugin list|reload|enable|disable <id>             # 插件管理
haiq webui token [--generate]                           # 面板令牌
```

`channels` / `jobs` 只依赖数据库，**服务没跑也能管理** —— 这是面板挂掉时的兜底。

## 当前状态（诚实版）

| 项 | 状态 |
|---|---|
| OneBot v11 四种模式 | ✅ 真机验证过（NapCat 4.18 + Docker） |
| 官方 OpenAPI 通道 | ⚠️ 代码完整、单测覆盖，**从未真机验证**（缺 AppID/AppSecret） |
| 6 个随仓库插件 | ✅ echo / help / admin / painter / chat / guard |
| 控制台 8 页 + 插件页 | ✅ 真实 Chromium 点过一遍，console 零错误 |
| 测试 | 534 passed；`ruff check` / `format` 全绿 |
| 图片发送 | ⚠️ 偶发 `rich media transfer failed`；宿主机开着系统代理时必失败 |
| 面板内重启/停机 | ❌ 未做（需要进程守护，否则退出后没人拉起） |
| 子进程隔离 | ❌ 未做（插件一律 inline 加载，`isolation` 目前只是声明） |

## 参与开发

```bash
uv sync --extra dev
uv run pytest -q                 # 534 passed
uv run ruff check . && uv run ruff format --check .
uv run haiq run --demo           # 不连 QQ 的本地演示
```

提交前请确保 `pytest` 与 `ruff` 都干净。改动涉及插件边界时，`tests/test_boundaries.py`
会拦下「内核里出现插件」这类回潮。

## 许可证

[MIT](LICENSE)
