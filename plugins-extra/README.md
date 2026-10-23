# plugins-extra —— 你自己的插件，不属于框架

框架只带两个最小插件（在 `plugins/`）：`echo`（回显）和 `help`（列命令）。
**判断标准**：陌生人装好框架、不接任何外部服务，是否仍然想要它？答案是「否」的，都放这里。

这里的四个插件是我自己的需求攒出来的：

| 插件 | 干什么 | 依赖什么 |
|---|---|---|
| `painter` | `/画图 <描述>` 调生图 API 并把图发回群 | 图像 API Key（vault 槽位 `painter.api_key`） |
| `chat` | `/问 <问题>` 单轮问答、`@机器人` 多轮对话 | LLM API Key（vault 槽位 `chat.api_key`） |
| `guard` | 刷屏限流 / 广告链接 / 违禁词 / 入群欢迎 | 无外部依赖，`gating="always"` |
| `admin` | `/plugins`、`/status`（需 ADMIN） | 无外部依赖 |

## 为什么是两个目录

```
plugins/         ← 框架目录：随框架走，升级时整目录替换
plugins-extra/   ← 你自己的：框架升级不会碰
```

`haiq.toml` 里就一行：

```toml
plugin_dirs = ["plugins", "plugins-extra"]
```

不存在的目录会被**静默跳过**，所以全新环境只建 `plugins/` 也能跑，不用改这行。

## 想搬走 / 想删掉

- **搬走**：把 `plugins-extra/` 移到别的仓库，再从 `plugin_dirs` 删掉第二个路径。
- **只留一两个**：删掉不想要的目录，再从 `[plugins] enabled` 去掉对应 id。
- **新机器**：`haiq init` 生成的 `plugin_dirs` 默认就带这两个路径，目录不存在也不报错。

## 注意

这些插件用的凭据键名（`WEFT_IMAGE_API_KEY` / `WEFT_CHAT_API_KEY`）是 vault 里的
历史键名，和框架包改名无关 —— 键名是数据契约，改了老 vault 就打不开。
