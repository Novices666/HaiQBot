# 开发指南

> 面向**改这个框架的人**。只想装起来用 → 看 [README](../README.md) 的「快速开始」。
> 想写插件 → 看 [PLUGIN-GUIDE.md](PLUGIN-GUIDE.md)。
> 想理解为什么这样分层 → 看 [ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 1. 环境

| 项 | 要求 |
|---|---|
| Python | ≥ 3.11（`requires-python`；CI 跑 3.11 / 3.12） |
| 包管理 | [uv](https://docs.astral.sh/uv/)（`uv sync --extra dev`） |
| Node.js | **只在改前端时需要**；`src/haiqbot/webui/dist/` 随包分发，跑后端不需要 node |
| 行长度 | `ruff` 配的 110（`pyproject.toml`），`target-version = "py311"` |

```bash
uv sync --extra dev
```

`dev` 额外依赖：`pytest` / `pytest-asyncio` / `ruff` / `pre-commit`。

### 口令怎么进进程

内核**不读 `.env`**，只认环境变量 `HAIQ_VAULT_PASSPHRASE`。`.env` 是「本机把口令放在哪」的约定，
由启动脚本负责读进环境变量。三个脚本分工：

| 脚本 | 作用 |
|---|---|
| `scripts/envpass.ps1` | 取口令：进程环境变量优先，其次本机 `.env` |
| `scripts/run-with-env.ps1` | 把口令放进**自己的**进程环境，再用 `cmd /c ... >> logs\haiq.out` 起 python（命令行里只有脚本路径） |
| `scripts/haiq.ps1` | 日常包装：`haiq.ps1 doctor` / `webui token --generate` / `run` |

> 为什么不直接把口令塞命令行：Windows 的命令行对同机其它进程可见，而本机是靠 WMI
> `Win32_Process Create` 起进程的 —— 那条路径不继承调用者的环境变量，只能「脚本自己读」。

## 2. 本地起一条完整链路

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dev-up.ps1
```

| 参数 | 作用 |
|---|---|
| `-SkipDocker` | 容器已在跑，只起 bot |
| `-SkipBot` | 只起容器，不起 bot |

> **这是作者本机的开发脚本，不是框架的一部分，也不含任何协议端。** 它只会 `docker start`
> 一个你**已经存在**的容器（默认名 `napcat`），没有就跳过；然后等 OneBot WS 端口监听，
> 后台拉起 `haiq run` 并等 WebUI 就绪。协议端怎么装、怎么登录是它自己的事。

它拉起的进程是**分离**的，关窗口不会带走 bot；停止请去任务管理器结束 `python.exe`。
日志在 `logs\haiq.out`，跟日志用 `scripts/logs.ps1`。

不想用脚本：

```bash
uv run haiq run            # 前台跑（口令自己 export / $env:）
uv run haiq run --demo     # 零成本演示：Mock 适配器，跑完即退，不开面板
```

## 3. 测试与检查

```bash
uv run pytest -q                                  # 全量：774 passed, 1 skipped
uv run pytest tests/test_boundaries.py -q         # 单个文件
uv run ruff check . && uv run ruff format --check .
```

`tests/` 69 个文件，按「一个关注点一个文件」切。改到哪块先跑对应文件：

| 改动区域 | 先跑这些 |
|---|---|
| 内核/插件边界 | `test_boundaries.py`、`test_kernel_refactor.py` |
| 适配器协议 | `test_adapter_onebot11.py`、`test_onebot_*.py`、`test_adapter_official.py` |
| 频道开关 | `test_channel_control.py`、`test_channel_verify.py`、`test_channel_candidates.py` |
| 凭据/vault | `test_vault_*.py`、`test_secret_slots.py`、`test_secrets_*.py` |
| 配置持久化 | `test_plugin_config_files.py`、`test_config_schema.py` |
| 面板 API | `test_webui.py`、`test_panels_api.py`、`test_media_http.py` |
| 出站/配额/幂等 | `test_outbound.py`、`test_governance_quota_idem.py` |

CI（`.github/workflows/ci.yml`）跑两件事，和本地命令一致：

1. **lint**：`ruff check .` + `ruff format --check .`
2. **test**：`pytest -q` × {ubuntu, windows} × {3.11, 3.12}

> Windows 必须在矩阵里：旧 `vault.bin`(DPAPI) 迁移路径、PowerShell 脚本的 BOM/编码
> 只有在这条链路上才真跑。

`.pre-commit-config.yaml` 另加了一道 **gitleaks**（v8.18.4）——真值误提交在这里被拦，
配合 `.gitignore` 覆盖 `secrets/`、`.env`、`.vault`。

## 4. 代码地图（改哪里）

```
src/haiqbot/
  core/          协议无关抽象：Segment / Event / Channel / Reply / errors
  kernel/        一切机制的实现（见下）
  adapters/      base(契约) · onebot11 · official · mock
  services/      media_store · media_http · scheduler · providers/base
  webui/         server · auth · api/*（jobs / secrets / panels / channels …）
  app.py         装配 + 管线 + 生命周期
  cli.py         全部子命令
  config.py      配置模型 + 环境变量覆盖 + 派生路径
plugins/         框架最小集：echo · help
plugins-extra/   自有插件：assistant · guard · admin
web/src/v2/      面板（8 页 + panels.tsx 渲染器 + theme.ts）
```

`kernel/` 里挑几个容易找错的：

| 文件 | 注意 |
|---|---|
| `channel_control.py` | 频道开关的**唯一写入口**；体检 10 个判定码 |
| `channels.py` | 和上面**不是同一个东西**：这是运行时的群维度闸门 |
| `plugin.py` | `Plugin` / `Context` / `SecretSlot` / `resolve_secret_ref`（`haiqbot.{pid}.{slot}`） |
| `jobs.py` | 持久化定时任务（`interval` / `daily`）；`services/scheduler.py` 是执行器 |
| `config_schema.py` | 配置字段的唯一定义，面板与校验共用 |
| `control.py` | 面板写操作：令牌 / 字段白名单 / 热更 / 审计 |

完整的模块速查表在 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 5. 三条硬边界（改代码前必须知道）

`tests/test_boundaries.py` 会把回潮直接钉红：

1. **内核里不许出现插件** —— 内核包内没有插件目录、没有 `plugin.toml`，源码里也不许出现
   `builtin_plugins` 这个名字（连默认配置值里都不行）。
2. **插件只许 import `haiqbot.plugin_api`** —— 伸手进 `kernel.*` 会被拦。
3. **内核的凭据表不许登记具体插件** —— 插件槽位一律动态发现（`control.CORE_SECRET_REFS`
   里出现 `plugin:<id>` 就红）。

另外 `plugins/` 只放最小集（`echo` / `help`），`plugins-extra/` 放自有插件；
把个人集成混进 `plugins/` 也会被 `test_personal_plugins_are_not_in_framework_dir` 拦下。

## 6. 文档规范（吃过亏，写成规矩）

- **不许撒谎**：没实现的东西不许写成已实现。想写“计划中”，就明确标出来。
- **CLI 子命令与配置键必须实测**：文档里出现的 `haiq <子命令>` 必须能在
  `python -m haiqbot <子命令> --help` 里找到；配置键必须能在 `config.py` /
  `plugin.toml` / 插件 `config_schema()` 里找到。
- **计数类陈述要对代码取数**，别硬编码 —— 历史上“体检 5 类 / 六类 / 10 个判定码”、
  “面板 6 页 / 8 页”、“708 passed / 774 passed”都因此过时过。写“以 X 为准”比写数字安全。
- **示例里的插件名要用现存的**（`assistant` / 虚构的 `myimage`），不要用已删除的 `painter`。
- 文档结构固定为：根 [README](../README.md)（初次使用）与 [CONTRIBUTING](../CONTRIBUTING.md)
  （贡献流程）/ 本文件（开发）/ [PLUGIN-GUIDE](PLUGIN-GUIDE.md)（写插件）/
  [ARCHITECTURE](ARCHITECTURE.md)（为什么）/ [WEBUI-SPEC](WEBUI-SPEC.md)（面板契约）/
  [DEPLOY](DEPLOY.md)（部署运维）。

提交文档前建议自检（脚本可临时写，别入库）：

| 检查 | 做法 |
|---|---|
| 幽灵命令 | 从 md 里抓 `haiq <sub> <action>`，逐个对 `--help` |
| 幽灵配置键 | 抓反引号里的 `snake_case`，在 `src/` `plugins/` 里全文找 |
| 死链/死路径 | 抓 md 里的相对路径，`Test-Path` 逐个验 |
| 代码围栏 | 统计 ``` 出现次数，必须是偶数 |

> 踩过的坑：用行号区间改 md 极易误删；在 ``` 围栏**内部**插入段落会变成代码文本。
> 改完务必回读一遍插入点周围。

## 7. 发布与仓库约定（维护者）

> 提交规范、PR 要求、报告问题的口径统一写在根 [CONTRIBUTING.md](../CONTRIBUTING.md)。
> 这里只放**改仓库本身**要知道的事 —— 两边都写就会漂移，这正是本仓库吃过亏的地方。

- 分支 `main` 直推（个人项目），不给自己设 PR 流程；外部贡献按 CONTRIBUTING 走。
- `.gitattributes`：仓库内一律 LF；`*.ps1` / `*.bat` / `*.cmd` 保留 CRLF
  （PowerShell 5.1 对 LF 的 `.ps1` 会解析出错）；`src/haiqbot/webui/dist/**` 标 `-text`。
- **不入库**：`secrets/`、`.env`、`.vault`、`logs/`、`data/`、`.dev/`、`.pi/`。
- **没有发布到 PyPI**：`pip install haiqbot` 装不到东西。
- **动不得的数据契约**：vault 键名与 AEAD AAD（`weft.vault.v2` 仍在白名单里）。
  改了老 vault 就打不开 —— 那不是「兼容包袱」，是已加密落盘的唯一副本。
- 前端构建产物 `src/haiqbot/webui/dist/` 要一起提交（部署机不装 node）：
  `cd web && npm ci && npm run build`。
- **抬版本要改两处**，两者没有联动：`pyproject.toml` 的 `version`，以及
  `src/haiqbot/__init__.py` 的 `__version__`（`haiqbot --version` 读的是后者）。
  改完跑一次全量测试并打 tag。
