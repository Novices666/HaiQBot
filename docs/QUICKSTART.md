# 快速开始：从零到群里收到回复

> 目标：30 分钟内让机器人在你的 QQ 群里回第一句话。
> 本文只走**最短路径**；多机部署、NSSM 服务、Docker 全栈、公网回源见 [DEPLOY.md](DEPLOY.md)。

## 你会用到什么

| 组件 | 作用 | 必须吗 |
|---|---|---|
| **HaiQBot**（本仓库） | 框架本体 + 2 个最小插件（`echo`/`help`）+ 控制台 | 必须 |
| `plugins-extra/`（本仓库内） | 生图 / 对话 / 群管 / 风控四个插件，可选 | 想要这些功能才需要 |
| **NapCat** | 协议端，把 QQ 账号变成 OneBot v11 接口 | 接真 QQ 才需要 |
| **Docker Desktop** | 跑 NapCat 最省事的方式 | 可选（也可装桌面版 NapCat） |

> **没有任何官方 QQ 账号申请流程。** OneBot 路线依赖第三方协议端，账号风险自负。

## 0. 准备

```bash
python --version      # 需要 3.11 或更高
uv --version          # 没有就装：https://docs.astral.sh/uv/
docker --version      # 只在接 NapCat 时需要
```

## 1. 装框架

```bash
git clone https://github.com/Novices666/HaiQBot
cd HaiQBot
uv sync --extra dev
```

> **没有发布到 PyPI。** `pip install haiqbot` 装不到东西，请用源码方式。
> 不想用 uv：`python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"`（Linux/macOS 用 `.venv/bin/pip`）。

## 2. 生成配置

```bash
uv run haiq init
```

会生成：

```
haiq.toml          ← 主配置（不含任何密钥）
secrets/           ← 凭据库目录（已 gitignore）
```

`haiq.toml` 里两个字段值得先看一眼：

```toml
plugin_dirs = ["plugins"]     # 插件从哪找；必须写在任何 [section] 之前

[plugins]
enabled = [...]               # 启用哪些插件；init 会按实际发现的插件填
```

## 3. 先不碰 QQ，把框架跑起来

**这一步别跳过。** 它能在你动 QQ 之前证明框架本身是好的。

```bash
uv run haiq doctor        # 自检：依赖、凭据、频道、插件来源
uv run haiq run --demo    # 不连 QQ、不烧额度的本地演示
```

### Windows：一条命令起整条链路

本机直跑时，Docker（NapCat）+ bot 可以用一个脚本拉起：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dev-up.ps1
```

它按顺序做四件事：起 Docker Desktop（已在跑就跳过）→ 起 `napcat` 容器（同名容器已存在
就直接复用，不重建）→ 等 OneBot WS `3001` 监听 → 后台拉起 `haiq run` 并等 WebUI 就绪。

| 参数 | 作用 |
| :--- | :--- |
| `-SkipDocker` | 容器已在跑，只起 bot |
| `-SkipBot` | 只起容器，不起 bot |

口令不会出现在命令行：`dev-up.ps1` 调 `scripts\run-with-vault.ps1`，后者从
`secrets\.vault-pass.dpapi`（Windows DPAPI，仅本机本账户可解）解出口令、设进自己的
进程环境，再用 `cmd /c ... >> logs\haiq.out` 起 python。命令行走的是脚本路径。
换机器或换账户时 DPAPI 解不开：先 `$env:HAIQ_VAULT_PASSPHRASE=...`，再跑一次
`scripts\rotate-vault-pass.ps1` 重建 DPAPI 文件。

浏览器打开 <http://127.0.0.1:8080>，用面板令牌登录：

```bash
uv run haiq webui token --generate     # 生成并写入 vault，命令会打印一次
```

能看到「总览」有数据、侧栏 8 个页面，就说明框架是通的。`Ctrl+C` 停掉。

## 4. 录入凭据

真值只进 vault，配置文件里只写 `*_ref` 键名。

```bash
# 对话 / 生图密钥（按需，没有就跳过）
uv run haiq secrets set HAIQ_CHAT_API_KEY
uv run haiq secrets set HAIQ_IMAGE_API_KEY
```

`secrets set` 是**交互式输入、不回显、不进 shell history**。

> vault 默认用 AES-256-GCM 加密，主口令从环境变量 `HAIQ_VAULT_PASSPHRASE` 读。
> 没设就先用明文跑：`haiq.toml` 里 `[vault] enc = "none"`。
> 想加密：`haiq secrets reencrypt`（会把现有条目重新加密并提示怎么设口令）。

## 5. 起 NapCat

```bash
# 先建挂载目录
mkdir -p .dev/napcat/qq .dev/napcat/config     # PowerShell: New-Item -ItemType Directory -Force .dev/napcat/qq, .dev/napcat/config

docker run -d --name napcat --restart unless-stopped \
  --shm-size=1g \
  -e TZ=Asia/Shanghai \
  -e ACCOUNT=<你的QQ号> \
  -p 127.0.0.1:3000:3000 -p 127.0.0.1:3001:3001 -p 127.0.0.1:3003:3003 -p 127.0.0.1:6099:6099 \
  -v "$(pwd)/.dev/napcat/qq:/app/.config/QQ" \
  -v "$(pwd)/.dev/napcat/config:/app/napcat/config" \
  mlikiowa/napcat-docker:latest
```

PowerShell 把 `$(pwd)` 换成 `${PWD}`。

> `.dev/` 是**外部项目目录**的约定位置：NapCat 的登录态和配置放这里，整个目录不入库。
> 详见 [`.dev/README.md`](../.dev/README.md)。

## 6. 扫码登录

两种任选：

**方式 A：容器日志里的链接**

```bash
docker logs napcat 2>&1 | grep -i qrcode     # 容器里会打印一个可解码的二维码 URL
```

**方式 B：NapCat WebUI（推荐）**

浏览器打开 <http://127.0.0.1:6099/webui>，用 `.dev/napcat/config/webui.json` 里的 token 登录，直接出二维码。

二维码 2 分钟过期，过期就 `docker restart napcat` 重新出。

## 7. 让 haiqbot 连上 NapCat

NapCat 登录成功后，OneBot 配置才会真正生效。它写的 token 在：

```
.dev/napcat/config/onebot11_<你的QQ号>.json
```

把里面的 token 录进 vault（**别贴到任何聊天窗口里**）：

```bash
uv run haiq secrets set HAIQ_ONEBOT_ACCESS_TOKEN
```

确认 `haiq.toml` 的 OneBot 段：

```toml
[onebot]
enabled = true
mode = "client"                                  # 我们主动连 NapCat 的正向 WS
url = "ws://127.0.0.1:3001"
access_token_ref = "HAIQ_ONEBOT_ACCESS_TOKEN"
image_transport = "auto"                         # 有本地文件就内嵌 base64，无需公网
```

## 8. 起服务并开频道

```bash
uv run haiq run
```

日志看到这两行就算连上了：

```
onebot_connected     url=ws://127.0.0.1:3001
plugins_ready        loaded=['echo', 'help', 'admin', 'assistant', 'guard']
```

**关键一步：频道默认全关。** 不开频道，bot 收到消息什么都不做。

面板「白名单与风控」→ 新建频道 → `onebot:group:<你的群号>` → 勾选插件 → 保存。

或者用 CLI：

```bash
uv run haiq channels enable onebot:group:<你的群号> --plugins echo,help,assistant
uv run haiq channels verify          # 体检：有没有「配了却不生效」
```

## 9. 在群里验证

按顺序试，出问题时容易定位：

| 发什么 | 期望 | 说明 |
|---|---|---|
| `/echo 你好` | 回 `你好` | 验证消息通路 |
| `@机器人 /help` | 列出命令 | 验证插件加载 |
| `/问 1+1 等于几` | 回答案 | 单轮对话（不带上下文） |
| `@机器人 我叫小明` → `@机器人 我叫什么` | 第二句能记住 | 多轮对话，保留 `history_turns` 轮 |

## 自检清单

一条不对就照着排查：

- [ ] `haiq doctor` 没有 error（exit code 0）
- [ ] 日志有 `plugins_ready`，`failed=[]`
- [ ] 日志有 `onebot_connected`
- [ ] 面板「白名单与风控」里那个群是**启用**状态
- [ ] 该群的 `plugins` 列表包含你想用的插件
- [ ] `haiq channels verify` 没报告静默失效

## 常见故障

| 现象 | 原因 / 怎么修 |
|---|---|
| 群里 @ 了没反应 | 频道没开（默认全关）；或该群 `plugins` 列表里没有对应插件。跑 `haiq channels verify` |
| 日志里 `插件来源 0 个` | `plugin_dirs` 写错位置（必须放在任何 `[section]` 前）或路径不存在（相对路径按**启动时工作目录**解析） |
| `vault 已加密但未提供密码` | 没设 `HAIQ_VAULT_PASSPHRASE`；或换过口令没重启（派生密钥是进程内缓存的） |
| 文字正常、发图必失败 | **宿主机系统代理开着** → 容器内 NapCat 上传被劫走。先关代理。另外检查 NapCat 的 `baseTimeout` 是否 ≥ 60000 |
| 二维码刷不出来 | `docker restart napcat`；或看 `docker logs napcat` 里的链接 |
| 面板登录不了 | 令牌没生成：`haiq webui token --generate`；或 vault 口令变了导致读不到令牌 |
| 改了插件配置没生效 | `ctx.config` 是活视图，但插件读配置的方式要对（`Mapping` 而非 `dict`）；纯代码改动必须重启 |

更细的排障（媒体发送、SSE/HTTP 模式、代理实验矩阵、频道体检）见 [DEPLOY.md](DEPLOY.md)。

## 下一步

- 写自己的插件 → [PLUGIN-GUIDE.md](PLUGIN-GUIDE.md)
- 理解为什么这样设计 → [ARCHITECTURE.md](ARCHITECTURE.md)
- 部署成服务 / 上服务器 → [DEPLOY.md](DEPLOY.md)
