# 框架改造：从「内核带服务」到「一切能力都可插拔」



> 状态：**已实施完毕**。P1/P2/P3/P4/P8/P11/P12 已落地，P5/P6/P7 因定位收敛取消（见 §4 与文末）。
> §3 是当时的方案稿：**3.1 / 3.5 已落地，3.2 / 3.3 / 3.4 随 P5/P6/P7 一起取消** —— 别照着 §3 去找不存在的代码。

> 起因：生图 / 对话的配置被写进了内核（`[image]` / `[chat]` 顶层段 + `App.setup` 里构造 provider），

> 与「通用框架 + 插件生态」的定位冲突。



---



## 1. 目标形态



```

┌─ 内核（不可拔） ────────────────────────────────────────────┐

│  core/        Segment · Event · Channel · Reply · errors     │

│  kernel/      bus（分发骨架）· registry · plugin · LiveConfig │

│               request_context · config_schema · control      │

│               governance: 限流 + 审计                         │

│               log: 脱敏（5 条判据）                            │

│               store · vault · runtime                        │

│  adapters/base.py   Adapter 抽象 + 平台能力协商（契约，非实现）│

│  webui/       只读「能力注册表」，不摸 broker/llm 等具体对象    │

└──────────────────────────────────────────────────────────────┘

        ▲ provides                        ▲ provides

┌─ 插件（全部可热拔） ─────────────────────────────────────────┐

│  adapter-onebot      适配器实现                               │

│  adapter-official    适配器实现                               │

│  matcher             规则编译（默认装，否则插件永不被触发）     │

│  guard-quota         配额（按动作计数）                        │

│  guard-idempotent    msg_id 去重                              │

│  image-service       生图 provider（提供 image:generate）      │

│  painter             消费 image:generate + /画图               │

│  chat-service        对话 provider（提供 llm:chat）            │

│  chat                消费 llm:chat + 对话                      │

│  echo                随包分发，可卸                            │

│  help                外置                                     │

│  admin               外置                                     │

└──────────────────────────────────────────────────────────────┘

```

> **实际落地与上图有出入**（上图是最初的目标形态）：`adapter-*` / `matcher` / `guard-*` **留在内核**
> （P5/P6/P7 取消）；`painter` 与 `chat` 已合并为 `assistant`（一个插件自带生图 + 对话两套 provider）；
> `plugins/` 里是 `echo` + `help`，`admin` / `guard` / `assistant` 在 `plugins-extra/`。



**关键概念**：插件既能**消费能力**（`capabilities = ["image:generate"]` → 拿到 `ctx.image`），

也能**提供能力**（注册 provider 实现）。内核只认能力名字和句柄契约。



拔掉 `image-service` → `ctx.image` 立刻变 `None` → 消费方走已有降级分支。



---



## 2. 决策台账



| # | 决策 | 选择 |

| :--- | :--- | :--- |

| A1 | 事件与协议抽象 | 内核 |

| A2 | 插件运行时（发现/加载/卸载/能力解析/热重载） | 内核 |

| A3 | 配置 + 凭据 + 存储 | 内核 |

| A4 | 适配器 | **契约留内核，实现做插件** |

| A5 | 总线 | 内核（分发骨架） |

| A6 | 匹配器 | **插件**（默认装一个，否则插件永不触发） |

| A7 | 限流 | 内核（0 插件时仍保护框架自身） |

| A8 | 审计 | 内核（0 插件时仍要能诊断「收到但无人处理」） |

| A9 | 配额 | **插件**（计量对象是插件产生的昂贵动作） |

| A10 | 幂等去重 | **插件**（按尺子属"与插件相关"；拔掉会有静默代价，见 §5） |

| A11 | 脱敏日志 | 内核，并**增加第 5 条判据**（扫 `[plugins.*]` 形似凭据的值自动登记） |

| A12 | 控制面板 | 内核，但**解耦**成读能力注册表 |

| A13 | 生图/对话服务 | **与插件合一**（插件自带 provider；`painter` + `chat` 后来合并成 `assistant`） |

| A14 | 热插拔深度 | **运行时全量热拔**，无需重启进程 |

| A15 | echo | 随包分发，可卸 |

| A16 | help / admin | 完全外置 |



---



## 3. 需要新增的内核机制



### 3.1 能力提供者（capability provider）



现在能力只能由内核发放（`build_capabilities` 里 `CAP_IMAGE → ImageCap(broker)`）。

改成：



```python
class Plugin:
    provides: dict[str, Callable[[Any], Any]] = {}  # 能力名 → 工厂
```



加载插件时，若 `provides` 非空，内核把它登记进 `runtime.providers`。

消费方加载时，`build_capabilities` 从 `runtime.providers` 里取实现来构造句柄。



**失效传播**：provider 被卸载 → 所有使用该能力的插件句柄置空，并在下次调用时返回

「能力不可用」，而不是抛异常炸掉。



### 3.2 中间件插槽（治理插件化）

> **已取消**（P5）：治理是安全边界，中间件不开放给插件。



`Bus.add_middleware(name, fn, order)` 已存在，但插件拿不到。

新增 `ctx.pipeline.on_event(fn, order=...)`：插件可注册中间件。

内核自身保留限流 + 审计两个中间件（order 10 / 90），其余中间件由插件注册。



### 3.3 规则编译契约（匹配器插件化）

> **已取消**（P6）：匹配器留在内核。下文提到的 `no_matcher_registered` 从未实现。



装饰器只往函数上挂 spec（现状已是如此）。新增契约：



```python
class RuleCompiler:  # 匹配器插件实现它
    def compile(self, plugin_id: str, func: Any, spec: dict) -> MatchRule | None: ...
```



内核在插件加载后，把每个 spec 交给已注册的 compiler；没有 compiler 就记一条

`no_matcher_registered` 警告（插件加载成功但永不触发）。



### 3.4 适配器插件化

> **已取消**（P7）：适配器属于内核，`[onebot]` / `[official]` 保留顶层配置。



`adapters/base.py` 的 `Adapter` 抽象留内核。新增：



```python
class Plugin:
    adapter_factory: Callable[[cfg], Adapter] | None = None
```



内核按 `[plugins.<id>]` 的配置实例化并 `start()`。`[onebot]` / `[official]` 顶层段

迁到对应插件的配置段。



### 3.5 webui 解耦



面板不再读 `runtime.broker` / `runtime.llm`，改为读：

- `runtime.providers`（哪些能力有人提供）

- `runtime.registry`（插件与命令）

- `runtime.config`（字段元数据）



---



## 4. 分期与进度



| 期 | 内容 | 风险 | 状态 |

| :--- | :--- | :--- | :--- |

| P1 | 能力提供者 + 失效传播；`[image]`/`[chat]` 迁进插件配置 | 中 | ✅ 已完成（含老配置自动迁移 + 备份） |

| P2 | 出站闸门：同频道串行 / 最小间隔 / 全局限速 / 频控退避重试 | 中 | ✅ 已完成（`kernel/outbound.py`） |

| P3 | CI：GitHub Actions（ruff + pytest × {ubuntu,windows} × {3.11,3.12}） | 低 | ✅ 已完成（`.github/workflows/ci.yml`） |

| P4 | invocation 调用流水接线 + 群维度闸门（`channel_config`） | 中 | ✅ 已完成（`kernel/invocation.py`、`kernel/channels.py`） |

| P5 | 中间件插槽；配额/幂等抽成治理插件 | — | ❌ 已取消（治理是安全边界，不该可插拔） |

| P6 | 规则编译契约；匹配器插件化 | — | ❌ 已取消（拔掉匹配器 = 框架变植物人，收益小于风险） |

| P7 | 适配器契约 + onebot/official 插件化 | — | ❌ 已取消（适配器属于内核，`[onebot]`/`[official]` 保留顶层配置） |

| P8 | webui 解耦 + 面板重做（含群聊页） | 低 | ✅ 已完成（当时 6 页，现 8 页；令牌按 V1–V5 落地） |

| P9 | echo 随包可卸 / help+admin 外置 | 低 | ✅ 已定：`plugins/` = echo + help（最小集）；`plugins-extra/` = assistant / guard / admin |

| P10 | 凭据回显、自重启 | 中 | ⬜ 未开始 |

| P11 | 能力彻底插件化：内核不认识 `image`/`llm`，能力合并为服务（`ServiceSpec` + blocked 三态） | 中 | ✅ 已完成（`test_capability_provider.py`、`test_service_contract.py`） |

| P12 | 富消息段（record/video/markdown/keyboard/合并转发）+ 官方富媒体 | 中 | ✅ 已完成（真机验过 face / 合并转发 / 视频） |



> 定位已收敛为「**QQ Bot 框架：OneBot v11 + 官方 OpenAPI 双通道**」，P5/P6/P7 因此取消。

> 下一步优先级已改为：OneBot 动作面补全 → OneBot 连接模式补全 → `ctx.session` →

> 可观测性（`/healthz` + metrics + 日志轮转 + DB 版本表 + 备份）→ 插件级出站配额。



> 期号已按「先解决线上真问题」重排：出站降速（防封号）→ CI（防回归）→

> 流水与群配置（面板页要有真实数据才不至于空转 —— 当时按 6 页算，现为 8 页）。原表的 P2/P3/P7 内容不变，

> 只是顺序提前；剩余各期的相对顺序保持原样。



每期结束必须：`ruff check` 干净 + `ruff format --check` 干净 + 全量测试通过 + `haiq run --demo` 通过。



---



## 5. 已知代价（只对当时未实施的部分成立）

> 1–3 是 P5/P6/P7 的代价，**这三期已取消**（见 §4 与「定位收敛」），代价不再成立，留作决策记录。

1. ~~**幂等插件化后有静默灾难窗口**：拔掉它，QQ 重复推送会导致重复生图（2MB / 35s / 双倍额度），
   且不报错。面板上要给显著提示。~~ —— P5 取消：幂等留在内核，不可拔。
2. ~~**匹配器可拔 = 可把框架拔成植物人**：默认必须装一个，且面板要能看出「当前没有匹配器」。~~
   —— P6 取消：匹配器留在内核。
3. ~~**适配器插件化会改连接参数位置**：`[onebot]` → `[plugins.onebot]`，老配置需要迁移脚本。~~
   —— P7 取消：`[onebot]` / `[official]` 仍是顶层段。
4. **外置化破坏开箱即用**：`pip install haiqbot` 之后只有 `echo` / `help`（且默认关），
   用户要自己放 `plugins/` 才能真正干活。README 必须第一步就讲清楚。
5. ~~**面板在改造期间会短暂显示空服务区**（P1 完成前 `[image]`/`[chat]` 还在内核）。~~
   —— P1 已完成：`[image]` / `[chat]` 早已迁进插件配置。



---



## 6. 不做的事



- 不引入 DI 容器 / 事件溯源等重型模式

- 不改 `Context` 的字段名（避免第三方插件全面失效）

- 不做插件沙箱（M8 子进程隔离仍延后）

- 不为「兼容旧插件」保留双轨代码

## 状态更新（已落地）

- **面板双轨已合并**：v1（手写 CSS）已删除，只剩 Ant Design 单面板；`/v2` 收敛到 `/`。
- **内核不再占用任何群内命令名**：改名前的 `/weft …` 那套群命令已全部取消（现在群里一条系统命令都没有）；开关入口 = 面板 + `haiq channels`（应急）。
- **频道开关**：默认全关 + `plugin:feature` 粒度；唯一写入口 `kernel/channel_control.py`；
  `always` 豁免已实现并被 `guard` 插件真机验证。
- **插件可扩展面板**：`PanelPage` 契约已上线（见 `WEBUI-SPEC.md`）。
- **媒体**：`media.gc` 已由内核定时任务调度；新增可选只读媒体服务（默认关闭）。
- **故障可观测性**：媒体发送失败不再静默，区分「生成失败 / 发送失败」并给出脱敏原因。
- **配置持久化（修 bug）**：`HAIQ_DATA_DIR` 覆盖 `data_dir` 的顺序曾晚于「读插件参数」，
  导致容器里「面板保存成功、重启后全是默认值」（读 `/app/data/plugins/`、写 `/data/plugins/`）。
  现在覆盖放在所有派生路径之前，读/写/`haiq.db`/`media/` 跟同一个 `data_dir` 走；
  回归测试 `tests/test_plugin_config_files.py::test_reads_plugin_config_from_env_data_dir`。
