# haiqbot 架构

## 一句话

**插件永远拿不到密钥，也永远不需要知道自己在哪个平台。**

## 分层

```
插件层（不可信）
   ↓ 只能通过 Context 的基础设施句柄（net / kv / storage / schedule / moderation）
   ↓ 以及自己在 services 里声明过的服务句柄 ctx.service("…")
内核五支柱   registry · bus · capabilities · governance · vault
   ↓ 只发放句柄，不认识任何具体能力
内核基础设施   media_store · scheduler · jobs（框架自带的少数服务）
   ↓
适配层   official(botpy) · onebot11 · mock   → 统一 Event/Segment/Channel/Reply
```


## 四个核心设计

### 1. 能力协商取代平台判断

插件里**禁止**出现 `if platform == "qq"`。平台差异全部收敛到：

```python
ctx.reply.capabilities.image  # 能否直接发图

ctx.reply.capabilities.image_by_url  # 能不能让平台自己拉 URL（官方=否）

ctx.reply.capabilities.markdown

ctx.reply.capabilities.window_ttl  # 被动回复窗口（官方 300s，OneBot 无限制）
```

插件只写 `await ctx.reply.image(ref)`，框架按目标平台自动选择：直接发图 / 发链接 / 文本提示。

### 2. 密钥只活在 provider 里

| 组件 | 能否接触凭据 |
|---|---|
| `vault` | 是（唯一出口） |
| provider（`painter` / `chat` **插件自带**、`official` adapter） | 是（唯一使用方；由 `pctx.slot("api_key")` 发放鉴权头，provider 本身不接触密钥） |
| 插件 handler | **否** —— `Context` 字段白名单里没有任何 secret |
| `bus` / `registry` / `governance` | 否 |

`tests/test_vault_redaction.py::test_context_has_no_secret_fields` 是这条的结构性断言。


### 3. 隔离级别可声明，第一版只做 inline

`plugin.toml` 的 `isolation = "inline" | "subprocess"`。当前一律 inline，声明 `subprocess`
会告警并按 inline 加载。真正的进程隔离是 v2 的 ABI，不在第一版范围内。

### 4. 服务契约：插件之间只认名字

内核**不认识**"生图"或"对话"。它只维护一张服务表：

```python
provides = {
    "image:generate": ServiceSpec(
        name="image:generate", factory="build_image_service", version=1, methods=("generate",)
    )
}

services = {"image:generate": "1"}  # 我自己也要用
```

- 提供方声明 `provides`，消费方声明 `services`；取未声明的服务直接 `CapabilityDenied`。
- 版本用 `parse_version_requirement()` 解析，声明了 `methods` 就会在 provide 时校验方法真的存在。
- 依赖不满足时插件进 **blocked** 三态：对象在、面板可见、规则不注册、原因明确；依赖恢复后自动重试加载。
- 加载顺序由依赖拓扑排序决定，成环直接 `ContractError`，不会静默死锁。


## 事件管线

```
adapter 归一化 → ① trace(注入 trace_id) → ② dedupe → ③ scope → ④ quota → ⑤ route → handler → ⑥ audit
```

- ①② 在 `App._on_event` 里，③④ 是可插拔 middleware，⑤ 是 `Bus._dispatch`，⑥ 在 publish 之后
- 任一环可 `StopPipeline` 短路
- 每个 handler 独立超时（默认 8s），插件卡死不会拖垮整条链
- 每个 handler 独立超时（默认 8s），插件卡死不会拖垮整条链
### 事件面（OneBot）

| post_type | 归一化成 | 说明 |
|---|---|---|
| `message` | `MessageEvent` | 群 / 私聊 |
| `notice` | `NoticeEvent` | 入群退群、撤回、禁言、管理员变更、戳一戳…；`operator_id` 与 `actor` 分开 |
| `request` | `RequestEvent` | 加好友 / 入群申请 / 群邀请；`flag` 是审批凭据，必须原样回传 |
| `meta_event` | `Event(kind=META)` | 只放行 `lifecycle`；`heartbeat` 每 30s 一条，只用来记 `bot_id` |
| `message_sent` | **丢弃** | 自己发的消息，进管线会导致自我回话 |

内核只做「谁是谁」的语义拆分（当事人 / 操作者 / 相关 ID），平台原值全部留在 `Event.raw`。
## 两枪回复与窗口

```
命中 → reply.ack()  （msg_seq=1）
     → 生图
     → 剩余 < 30s 就不再调用（省额度）
     → reply.image() （msg_seq=2）
```

`msg_seq` 由 `Reply` 自增管理，插件完全无感。

## 图片怎么发出去（两条通道）

| 平台 | 通道 | 依赖公网？ |
|---|---|---|
| QQ 官方 | `post /files` 上传 URL → `msg_type=7` | **是**，平台要回源拉图 |
| OneBot | `{"type":"image","data":{"file":"base64://…"}}` | **否**，字节跟着消息走 |

`PlatformCapabilities.image_by_base64` 就是这个差异的声明位。`Reply.image(ref)` 把
`url` / `media_id` / `path` 一起塞进 segment，适配器自己挑：

- OneBot：读到本地 `path` → base64 内嵌（2.1 MB 图 → 2.8 MB 消息）；读不到或超
  `image_max_bytes` → 退回 URL
- 官方：只认 URL，`path` 被忽略

`haiq.toml` 的 `[onebot] image_transport` 可强制 `base64` 或 `url`。

## 防泄露四道闸

| 闸 | 机制 |
|---|---|
| 不进仓库 | 真值只进 `secrets/`（gitignore）+ gitleaks pre-commit |
| 不进日志 | structlog 脱敏 processor；vault 取值即 `register_secret()`，出现即打码 |
| 不进 URL | provider 签名 URL 一律落地中转，对外只发 `/i/{token_urlsafe(16)}` |
| 不裸奔入口 | webui / media / OneBot 反 WS 仅监听 `127.0.0.1`，公网只反代 `/i/*` |

## 一个实际会踩的坑：系统代理

Windows「Internet 选项 → 局域网设置」里的代理会被 `httpx`（`trust_env=True`）读到，
**连 `http://127.0.0.1/...` 的请求也会被送去代理**，本地验证 `/i/*` 时表现为 `502 Bad Gateway`。

- 出网调用（生图 provider）走代理是预期行为，不要关 `trust_env`；
- 本地验证回源时用 `httpx.AsyncClient(trust_env=False)`；
- `haiq doctor` 会打印检测到的系统代理与 `no_proxy` 例外。

## 模块速查

| 路径 | 职责 |
|---|---|
| `haiqbot/core/*` | 协议无关抽象（Segment / Event / Channel / Reply） |
| `haiqbot/kernel/bus.py` | 中间件链 + 优先级分发 + 超时 |
| `haiqbot/kernel/matcher.py` | 声明式匹配（命令 / 正则 / 角色 / 平台） |
| `haiqbot/kernel/registry.py` | 发现（entry point / 本地目录）、加载、卸载、重载 |
| `haiqbot/kernel/capabilities.py` | 基础设施句柄发放（net / storage / kv / schedule / moderation / platform） |
| `haiqbot/kernel/plugin.py` | Plugin/Context/触发器 + `ServiceSpec` 服务契约 |
| `haiqbot/kernel/governance.py` | 令牌桶 / 日配额 / 幂等 / 审计 |
| `haiqbot/kernel/outbound.py` | 出站闸门：同频道串行 + 最小间隔 + 全局限速 + 频控退避 |
| `haiqbot/kernel/invocation.py` | 能力调用流水（谁在哪个群要了什么、多久、成没成） |
| `haiqbot/kernel/channels.py` | 群维度闸门（整群静默 / 只放行部分插件 / 每群配额） |
| `haiqbot/kernel/request_context.py` | 用 contextvar 把「当前事件」带给能力句柄 |
| `haiqbot/kernel/config_schema.py` | 配置字段的唯一定义（面板与校验共用） |
| `haiqbot/kernel/control.py` | 面板写操作：令牌认证 / 字段白名单 / 热更 / 审计 |
| `haiqbot/kernel/vault.py` | env / DPAPI / keyring / cmd 四后端 |
| `haiqbot/kernel/jobs.py` | 持久化定时任务（interval / daily，动作白名单） |
| `haiqbot/kernel/secret_index.py` | 凭据槽位索引（CLI 与面板共用） |
| `haiqbot/kernel/channel_control.py` | 频道开关读写与体检（识别 5 类静默失效） |
| `haiqbot/kernel/log_ring.py` | 面板日志环形缓冲（先脱敏再入缓冲） |
| `plugins/painter/broker.py` | 唯一生图出网口（`image:generate` 的提供者） |
| `plugins/chat/broker.py` | 唯一对话出网口（`llm:chat` 的提供者） |
| `plugins/<id>/providers/` | 厂商实现（归插件所有） |
| `haiqbot/services/providers/base.py` | 通用 provider 类型（`ProviderError` / `ImageProvider`） |
| `haiqbot/services/media_store.py` | 图片中转与 GC |
| `haiqbot/app.py` | 装配 + 管线 + 生命周期 |
