# 参与贡献

> 先确认目标：这是**框架**，不是成品机器人 —— 装完默认什么都不响应。
> 改代码前请读一遍「不能破坏的三条边界」。

技术细节（口令怎么进进程、一键起链路、代码地图、测试矩阵）在
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)；为什么这样分层在
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 三种贡献

| 类型 | 怎么做 |
|---|---|
| **插件**（最欢迎） | 放**你自己的仓库**，别往本仓 `plugins/` 里塞。想给别人用 → 打成 pip 包并注册 `haiqbot.plugins` entry point。想收进 `plugins-extra/` 请先开 issue 说明依赖哪家 API |
| **修 bug / 加内核能力** | 请附复现步骤；能加测试就加（见 `tests/` 现有切法） |
| **文档** | 和代码同等重要。本仓库对文档准确性的要求**异常严格**，理由见下 |

## 起步

```bash
uv sync --extra dev
uv run pytest -q          # 应当 774 passed, 1 skipped
```

## 提交前必须过

```bash
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
```

CI（`.github/workflows/ci.yml`）跑的就是这两条，矩阵是 `{ubuntu, windows} × {3.11, 3.12}`。
**Windows 那条不是摆设**：旧 `vault.bin`(DPAPI) 迁移、PowerShell 脚本的 BOM/编码只有在这条链路上才真跑。

`.pre-commit-config.yaml` 还挂了 **gitleaks** —— 真值误提交会在这一层被拦。
另外请确认没把 `secrets/`、`.env` 加进暂存区。

## 不能破坏的三条边界

`tests/test_boundaries.py` 会把回潮直接钉红：

1. **内核里不许出现插件** —— 内核包内没有插件目录、没有 `plugin.toml`，源码里连
   `builtin_plugins` 这个名字都不许出现。
2. **插件只许 import `haiqbot.plugin_api`** —— 伸手进 `kernel.*` 会被拦。
3. **内核的凭据表不许登记具体插件** —— 槽位一律动态发现。

`plugins/` 只放最小集（`echo` / `help`），个人集成放 `plugins-extra/`。

## 提交信息

Conventional Commits + 中文正文，正文写**为什么**而不只是改了什么：

```
fix(config): 环境变量覆盖提前到所有派生路径之前

容器里 haiq.toml 写的是相对 data_dir = "data"，真实落点是 HAIQ_DATA_DIR=/data。
旧顺序在「读插件参数」之后才覆盖，于是启动读 /app/data/plugins/（镜像里不存在）
→ 插件回默认值，而面板写 /data/plugins/ —— 读和写不是同一个文件。

现在覆盖放在最前，派生路径全部跟同一个 data_dir 走。
回归：test_reads_plugin_config_from_env_data_dir
```

一次提交一件事。把「修 bug」和「顺手格式化整个文件」混在一笔里，review 成本会翻好几倍。

## Pull Request

- 说清**动机**与**影响面**；附测试，或说明为什么测不了。
- **别夹带无关改动**（格式化、重命名、抬版本号）。
- 破坏兼容的改动必须写迁移路径 —— 这个仓库里有两类东西动不得：`vault` 键名与 AEAD AAD
  （改了老 vault 就打不开）。
- 前端改动要一起提交构建产物 `src/haiqbot/webui/dist/`（部署机不装 node）。
  构建方法：`cd web && npm ci && npm run build`。

## 报告问题

- 附 `haiq doctor` 的输出 —— 它一次说清凭据缺几条、依赖是否齐、有没有代理干扰。
- 贴日志前先脱敏。日志层会自动把真值打成 `***`，但你手抄的内容别带 token。
- **安全漏洞别开公开 issue**：凭据相关（vault 实现、令牌校验、脱敏漏点、越权）请私下联系
  维护者，不要在 issue 里贴可复现的真实令牌。

## 文档改动也算贡献

三条硬规矩（吃过亏，写成了规矩）：

1. **不许撒谎**：没实现的东西不许写成已实现；要写「计划中」就明确标出来。
2. **CLI 子命令与配置键必须实测**：`haiq <子命令>` 要能在 `python -m haiqbot <子命令> --help`
   里找到；配置键要能在 `config.py` / `plugin.toml` / 插件 `config_schema()` 里找到。
3. **计数类陈述对代码取数**，别硬编码 —— 这个仓库真出过「体检 5 类 / 六类 / 10 个判定码」、
   「面板 6 页 / 8 页」、「708 passed / 774 passed」这种自相矛盾。

自检方法与两个踩过的坑（按行号改 md 会误删、在 ``` 围栏内部插入会变代码）
见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) 第 6 节。

## 本项目不做的事

[ARCHITECTURE.md](docs/ARCHITECTURE.md) 末尾列了「明确不做的事」——插件市场、群内系统命令、
`opt_out` 第三态、插件沙箱等。往这些方向提 PR 会被直接关掉，不是不礼貌，是已经决定过。
