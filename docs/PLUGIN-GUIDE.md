# 写一个 haiqbot 插件

## 最小插件

```python
# myplugin/__init__.py
from haiqbot import Context, Plugin, on_command


class Hello(Plugin):
    id = "hello"
    name = "打招呼"
    description = "示例插件"
    capabilities: list[str] = []  # 不声明就不发句柄

    @on_command("hi", aliases=["你好"], usage="hi <名字>", priority=10)
    async def hi(self, ctx: Context, name: str = "") -> None:
        await ctx.reply.text(f"你好，{name or '陌生人'}！")
```

```toml
# myplugin/plugin.toml
id = "hello"
name = "打招呼"
version = "0.1.0"
api_version = 1
isolation = "inline"
capabilities = []
```

把目录放进 `haiq.toml` 的 `plugin_dirs` 指向的地方。默认是两个（不存在的会被静默跳过）：

| 目录 | 放什么 | 谁维护 |
|---|---|---|
| `plugins/` | 框架自带的最小集：`echo`（回显）、`help`（列命令） | 框架，升级时整目录替换 |
| `plugins-extra/` | **你自己的**插件 | 你，框架升级不会碰 |

**内核里不放插件。** `src/haiqbot/` 下没有任何插件代码；上面两个目录里的插件走完全同一条
加载路径（`source=local`），所以「写插件」不需要碰内核，也不需要重新安装框架。

两个会「静默失效」的坑，踩了不会报错，只会表现为插件全没了：

1. **`plugin_dirs` 必须写在 `haiq.toml` 里任何 `[section]` 之前。** 写进段内会变成该段的
   子键，被静默忽略。`haiq doctor` 会告警这种情况。
2. **相对路径按启动时的工作目录解析。** 用 NSSM 服务 / 计划任务 / 从别处启动时很容易
   指错目录。`haiq doctor` 会打印实际发现到的插件来源（`本地目录 N 个（a,b,c）`），
   先跑它再排查别的。

## 从写代码到跑起来（本地目录开发循环）

**最快的方式是一条命令**（建目录 + 写清单 + 加进 `enabled` 一次做完）：

```powershell
haiq plugin new hello
```

它会在 `plugins/` 下生成 `hello/{__init__.py,plugin.toml}`，里面已经有一个能跑的
`@on_command("hello")`，并把 `hello` 写进 `[plugins] enabled`。想放到自己的目录就加
`--dir plugins-extra`。

手动创建则要两步：

1. 建目录（自己的插件放 `plugins-extra/`）：

   ```
   plugins-extra/hello/__init__.py
   plugins-extra/hello/plugin.toml
   ```

2. 把 `hello` 加进 `haiq.toml` 的 `[plugins] enabled`。

3. 重启 `haiq run`。日志里应该出现：

   ```
   plugin_loaded   plugin=hello rules=1 source=local version=0.1.0
   ```

   `source=local` 说明走的是本地目录；如果是 `entrypoint`，说明装了同名的 pip 包插件，
   两者都会加载，容易互相干扰。

4. 去面板「白名单与风控」把这个插件勾进目标群，再在群里试命令。

**改代码后必须重启。** 插件是启动时 `import` 的，没有热重载；改 `plugin.toml`
里的 `name` / `version` / `description` / `isolation` 同样要重启。
（面板的「插件 → 重载」只重载单个插件，同样会重新 import。）

排查对照：

| 现象 | 优先级最高的一步 |
|---|---|
| 命令完全没反应 | 面板「插件与能力」看该插件是否 `loaded`；再看目标群的 `plugins` 列表有没有它 |
| 日志 `plugin_local_import_failed` | 你的 `__init__.py` 在导入期抛异常了，日志里带异常类型和路径 |
| 日志 `插件来源 0 个` | `plugin_dirs` 写错位置或路径不存在（见上一节） |
| 被拒绝加载并提示 `api_version` | 插件的 `api_version` 与内核不一致，日志会写清期望值 |
| `plugin.toml_invalid` | 清单不是合法 UTF-8 TOML（BOM 已容忍，语法错要自己修） |

## 触发器

| 装饰器 | 说明 |
|---|---|
| `@on_command("画图", aliases=["draw"])` | 命令；兼容 `/画图` 与 `画图` |
| `@on_message(priority=-50)` | 任意消息；默认不阻断后续匹配 |
| `@on_regex(r"^投票 (\w+)$")` | 正则；handler 可声明 `match` 形参 |
| `@on_notice(notice_type="group_increase")` | 通知事件（入群/退群、撤回、禁言、管理员变更、戳一戳…） |
| `@on_request(request_type="group")` | 请求事件（加好友、入群申请、群邀请）；**默认 `min_role=ADMIN`** |
| `@on_meta()` | 元事件（OneBot 只有 `lifecycle`；心跳不进管线） |

通用参数：`priority`（越大越先）、`block`（命中后是否阻断后续）、`require_at`、
`min_role`（`Role.USER / ADMIN / OWNER`）、`platforms`、`channel_kinds`。

通知/请求还可以按 `notice_type` / `sub_type` / `request_type` 细分过滤，字符串或列表都行：

```python
from haiqbot import on_notice, on_request


@on_notice(notice_type="group_increase")
async def welcome(self, ctx, event):
    await ctx.reply.text(f"欢迎 {event.actor.id} 进群～")


@on_notice(notice_type="group_recall")
async def on_recall(self, ctx, event):
    # notice_id 就是被撤回的消息 ID；operator_id != actor.id 说明是管理员撤的
    ctx.log.info("recall", msg=event.notice_id, by=event.operator_id)


@on_request(request_type="group", sub_type="add")
async def auto_approve(self, ctx, event):
    # 审批是特权动作：默认只对 ADMIN 生效
    await ctx.moderation.approve(event, approve=True)
```

### 通知/请求事件里谁是谁

| 字段 | 含义 |
|---|---|
| `actor` | **当事人**（入群的人、被禁言的人、被撤回消息的作者）。`min_role` 卡的是他 |
| `operator_id` | **谁执行的**（管理员踢人时不是本人）；等于 `actor.id` 表示本人操作自己 |
| `notice_id` | 相关 ID；撤回事件里就是被撤回的 `message_id` |
| `event.raw` | 平台原始 payload（要平台特有字段时用它，别指望内核归一化） |

## 群管理动作（需要声明 `moderation` 能力）

```python
class Guard(Plugin):
    id = "guard"
    capabilities = ["moderation"]

    @on_command("禁言", min_role=Role.ADMIN)
    async def mute(self, ctx, target: str = "", seconds: str = "600"):
        if not ctx.moderation.supports("mute"):
            return await ctx.reply.text("当前平台不支持禁言")
        await ctx.moderation.mute(ctx.event.channel, target, int(seconds or 600))
```

| 方法 | 说明 |
|---|---|
| `ctx.moderation.recall(channel, message_id)` | 撤回 |
| `ctx.moderation.mute(channel, user_id, seconds)` | 禁言；`0` = 解除 |
| `ctx.moderation.kick(channel, user_id, reject_add_request=False)` | 踢人 |
| `ctx.moderation.member_list(channel, limit=100)` | 成员列表（`user_id` / `name` / `role`） |
| `ctx.moderation.approve(request_event, approve, reason="")` | 批准/拒绝加好友或入群申请 |
| `ctx.moderation.supports("mute")` | 先探测再决定文案，别靠 try/except |

所有动作都会落一条 `moderation:*` 审计（谁在哪个群对谁做了什么），平台不支持时抛
`CapabilityUnavailable` 而不是静默返回 False —— 静默失败会让插件以为已经执行了。

## 参数映射

命令后面的 token 按位置填进 handler 形参，**最后一个形参吸收剩余全部文本**：

```python
@on_command("画图")
async def draw(self, ctx, prompt: str): ...


# /画图 一只 橘色 的猫  →  prompt = "一只 橘色 的猫"
```

## `require_at` 与多轮上下文

`@on_message(require_at=True)` 只在「消息里 @ 了机器人」时命中 —— 闲聊类插件必须这么写，
否则群里每句话它都要接。

同一个能力可以有两种入口，**上下文语义不同**。`assistant` 插件的对话侧就是这么分的：

| 入口 | 装饰器 | 上下文 |
|---|---|---|
| `@机器人 我叫小明` | `@on_message(require_at=True, priority=-30, block=True)` | **带历史**：保留最近 `history_turns` 轮（1 轮 = 一问一答），超出的丢弃 |
| `/问 我叫什么` | `@on_command("问", aliases=["ask"])` | **不带历史**：单轮问答，说完就忘 |
| `/重置对话` | `@on_command("重置对话")` | 清掉该会话的历史 |

为什么这样分：`/问` 是「查一下」，用户期望干净的一次问答；`@机器人` 是「聊天」，
期待它记得上文。混成一个入口就会出现「我问个公式，它却接着上一句」的困惑。

实现细节（`assistant` 的对话侧可直接抄）：

- 历史存在 `ctx.kv` 里，key 是 `hist:{channel}:{actor}` —— **按频道和用户分开**，
  群里 A 的上下文不会串到 B；
- 只在 `use_history=True` 的路径上 `_remember()`，单轮入口不写库；
- `history_turns` 是面板可调的配置项，`0` 表示完全不带上下文。

> `require_at` 依赖适配器把「是否 @ 了机器人」算出来（OneBot 侧扫消息段的 `at` 并比对 `self_id`）。
> 如果协议端只上报纯文本、没有消息段，`require_at` 永远不会命中 —— 这时改用 `/命令` 入口。

## Context 里有什么

| 属性 | 说明 |
|---|---|
| `ctx.event` | `MessageEvent`（`channel` / `actor` / `segments` / `msg_id`） |
| `ctx.reply` | 回复句柄：`text` / `ack` / `image` / `markdown` / `segments` |
| `ctx.reply.capabilities` | 平台能力（决定要不要降级） |
| `ctx.reply.remaining()` | 被动窗口剩余秒数，`None` = 无限制 |
| `ctx.service("…")` | 服务句柄（只能取自己在 `services` 里声明过的名字） |
| `ctx.platform` | 平台查询/轻量动作（需声明 `platform`） |
| `ctx.net` | 受限 HTTP（域名白名单） |
| `ctx.storage` | 插件私有文件区（需声明 `storage:media`） |
| `ctx.kv` | 插件命名空间键值（需声明 `kv`） |
| `ctx.schedule` | 定时任务（需声明 `schedule`） |
| `ctx.introspect` | 只读：查看所有插件与命令 |
| `ctx.log` | 已挂脱敏 processor 的结构化 logger |
| `ctx.config` | 插件自己的配置（`data/plugins/<插件id>.toml`，见下一节） |

**没有**：vault、env、任何密钥。

## 按功能粒度开关（`feature`）

频道开关的最小单位不是插件，而是 `插件id:feature`。想让「这群只许对话、不许画图」能做到，
就得在装饰器上声明 `feature=`：

```python
@on_command("问", feature="ask")
async def ask(self, ctx: Context, q: str = "") -> None: ...


@on_message(require_at=True, feature="ask")
async def talk(self, ctx: Context) -> None: ...


@on_command("画图", feature="draw")
async def draw(self, ctx: Context, prompt: str = "") -> None: ...
```

之后频道配置有两种写法：

| 写法 | 含义 |
| :--- | :--- |
| `assistant` | 整个插件的所有规则都放行 |
| `assistant:ask,assistant:reset` | 只放行声明了 `ask` / `reset` 的规则，`draw` 静默不响应 |

三条规矩：

- **不写 `feature` 就等于没有独立开关**，只能整插件开/关；
- feature 名是**数据契约**：改名会让老配置失效，`haiq channels verify` 报 `unknown_feature`；
- 同一插件里可以复用（`/问` 和 `@机器人` 都归 `ask`）。

## 在控制台里配置你的插件

插件参数落在**插件自己的文件**：data/plugins/<插件id>.toml（扁平 key/value）。
主 haiq.toml 只留注册信息（plugin_dirs + [plugins] enabled），所以：

- 卸载插件 = 删目录 + 删一个文件，主配置不用动；
- 默认值只有一处来源（你的 config_schema()），文件里只写「覆盖过的值」；
- 面板「插件设置」和 haiq plugin config 写的都是这个文件。

声明 `config_schema()`，控制台「插件」页会自动渲染成表单（不声明则退化为自由键值编辑）：

```python
class MyPlugin(Plugin):
    id = "my"

    @classmethod
    def config_schema(cls) -> list[dict]:
        return [
            {"key": "greeting", "label": "打招呼", "kind": "str", "default": "嗨"},
            {"key": "limit", "label": "每人每天上限", "kind": "int", "default": 5},
            {"key": "enabled", "label": "启用", "kind": "bool", "default": True},
        ]

    @on_command("hello")
    async def hello(self, ctx: Context) -> None:
        await ctx.reply.text(ctx.config.get("greeting", "嗨"))
```

`kind` 取 `str` / `int` / `float` / `bool`；可选 `choices` 给下拉候选。
值存在 `haiq.toml` 的 `[plugins.my]`，控制台改完会**就地把 `ctx.config` 更新**，
所以请在每个 handler 里现取现用（不要只在 `on_load` 里读一次）。

## 能力声明

内核只保留 5 个**基础设施**能力：

```toml
capabilities = ["net:http", "storage:media", "kv", "schedule", "moderation", "platform"]
```

未声明 = 句柄为 `None`，用前判空：

```python
if ctx.kv is None:
    await ctx.reply.text("kv 未启用")
    return
```

> `image:generate`、`llm:chat` 这类**不是能力，是服务** —— 由插件自己 `provides`，
> 用 `ctx.service("image:generate")` 取。见下面「服务与契约」。

## 富消息段

```python
await ctx.reply.text("普通文本")
await ctx.reply.image(ref)  # ImageRef 或 URL
await ctx.reply.record("https://x/a.silk")  # 语音
await ctx.reply.video("https://x/a.mp4")  # 视频
await ctx.reply.markdown("**加粗**", keyboard=rows)  # 官方 markdown + 按钮
await ctx.reply.forward([ForwardNode(segments=(Segment.text("第一句"),))])
```

也可以直接拼 `Segment` 再交给 `ctx.reply.segments([...])`：

| Segment | 说明 | OneBot | QQ 官方 |
|---|---|---|---|
| `Segment.record(url/path)` | 语音 | ✅ `record` 段 | ✅ `/files` `file_type=3`（要 silk） |
| `Segment.video(url/path)` | 视频 | ✅ `video` 段 | ✅ `/files` `file_type=2`（要 mp4） |
| `Segment.face(14)` | 平台表情 | ✅ `face` 段 | ❌ |
| `Segment.markdown(text)` | markdown 正文 | 降级成纯文本 | ✅ `msg_type=2` |
| `Segment.keyboard(rows)` | 按钮行 | **丢弃并告警** | ✅ 套 `content.rows` 外壳 |
| `Segment.forward(nodes)` | 合并转发 | ✅ `send_*_forward_msg` | ❌ |

四条约定：

1. **降级不丢内容**：markdown 在 OneBot 变成纯文本；合并转发在官方侧变成逐条文本。
2. **`path` 型媒体没人替你搬文件**：OneBot 的 `file` 是给协议端自己读的 —— NapCat 跑在
   Docker 里就读不到宿主机路径。实测：传 Windows 路径 → `retcode 1200`；放进容器挂载目录
   再传 `/app/napcat/config/x.mp4` → 成功。跨机器/容器部署请优先用 `url=`。
3. **按钮 schema 归平台**：内核只套 `keyboard.content.rows` 这层外壳，`render_data` /
   `action` / `permission` 原样透传 —— 官方改版时插件自己改，不用等内核发新版本。
4. **能力先探测**：`ctx.reply.capabilities.record` / `.markdown` / `.button` / `.forward`，
   别硬发（`Reply` 的糖方法已经替你降级了，自己拼 Segment 才需要判断）。

## 生图的正确姿势

```python
@on_command("画图", priority=20)
async def draw(self, ctx, prompt: str):
    if not prompt.strip():
        return await ctx.reply.text("用法：画图 <描述>")
    if ctx.reply.almost_expired(margin=30):
        return await ctx.reply.text("这条消息快过期了，重新 @我 一次")

    await ctx.reply.ack("🎨 排队中…")  # 两枪第一枪

    try:
        ref = await ctx.service("image:generate").generate(prompt)  # 无 key、无 endpoint
    except Exception:
        ctx.log.exception("draw_failed")  # message 里不含密钥
        return await ctx.reply.text("生成失败，请稍后再试")

    await ctx.reply.image(ref)  # 两枪第二枪，自动按能力降级
```

## 凭据：声明槽位 + 用句柄

插件**不写键名**，只声明「我要一个凭据」：

```python
from haiqbot import Plugin, ProviderContext, SecretSlot


class Painter(Plugin):
    id = "painter"

    secrets = {
        "api_key": SecretSlot(
            label="生图 API Key",  # 面板与报错里给人看的名字
            ref="HAIQ_IMAGE_API_KEY",  # 可省略；省略时推导为 haiqbot.painter.api_key
            description="OpenAI 兼容的密钥",
        ),
    }

    def build_image_service(self, pctx: ProviderContext):
        provider = OpenAICompatProvider(pctx.slot("api_key").headers, cfg)
        ...
```

`SecretHandle` 的方法：

| 方法 | 用途 |
|---|---|
| `is_set()` | 只问「有没有」，不取明文（用于启动期降级） |
| `headers(header=, scheme=)` | 生成鉴权头（默认 `Authorization: Bearer …`） |
| `query(param=)` | 生成查询参数（`?key=…` 这类网关） |
| `text()` | 明文，包成 `SecretValue`（print / 日志只显示占位符） |
| `require()` | 明文，缺失时抛 `CapabilityUnavailable`（含 label，不含物理键名） |
| `bytes()` | 二进制（HMAC 验签 / 对称加密） |
| `json()` | 解析成 dict（Google service account 这类凭据文件） |

四条规则：

1. **没声明就取不到**：`pctx.slot("x")` 若没写进 `secrets`，直接抛 `CapabilityDenied`。
2. **句柄是惰性的**：每次调用重新解析 vault，所以**换 key 不需要重载插件**，下一句话就生效。
3. **键名优先级**：`[plugins.<id>] <slot>_ref`（覆盖）> `SecretSlot.ref`（插件绑定）> 推导 `haiqbot.<id>.<slot>`。
4. **`Context`（handler 层）没有 `slot()`** —— 业务代码拿不到凭据，这是刻意的边界。

用户侧的写入口：

```bash
haiq secrets                      # 槽位 / 状态 / 来源 / 物理键名
haiq secrets set assistant.image_api_key  # 用槽位名，不用记物理键名
haiq secrets set --missing        # 只提示缺失项
haiq secrets passwd               # 轮换口令：旧口令解开 -> 新口令重写
```

口令轮换不依赖任何遗留文件；Windows 上可以直接跑
`scripts\rotate-vault-pass.ps1`，它会顺带帮你删掉明文密码文件。

## 平台查询（`ctx.platform`）

要声明 `capabilities = ["platform"]`。查询类动作不改别人状态，不审计；
但**会改现状的动作一律留审计**（下表标「有审计」），审计失败只记日志、不阻断动作。
和其它能力一样先用 `supports()` 探测，平台不支持时抛 `CapabilityUnavailable`。

| 方法 | 说明 | OneBot | 官方 |
|---|---|---|---|
| `get_message(msg_id)` | 取单条消息 | ✅ `get_msg` | ❌ |
| `get_forward(msg_id)` | 取合并转发的节点 | ✅ `get_forward_msg` | ❌ |
| `group_list()` | 机器人在的群列表 | ✅ `get_group_list` | ❌ |
| `group_info()` | 当前群信息 | ✅ `get_group_info` | ❌ |
| `member_info(user_id)` | 群成员信息 | ✅ `get_group_member_info` | ❌ |
| `stranger_info(user_id)` | 陌生人信息 | ✅ `get_stranger_info` | ❌ |
| `set_group_card(user_id, card)` | 改群名片（有审计） | ✅ `set_group_card` | ❌ |
| `upload_group_file(path, name)` | 上传群文件（有审计） | ✅ `upload_group_file` | ❌ |
| `emoji_like(msg_id, code)` | 给消息加表情回应 | ✅ `set_msg_emoji_like` | ❌ |
| `login_info()` | 机器人自己的账号 | ✅ `get_login_info` | ❌ |
| `status()` | 在线状态 | ✅ `get_status` | ❌ |
| `friend_list()` | 好友列表 | ✅ `get_friend_list` | ❌ |
| `essence_list()` | 群精华消息 | ✅ `get_essence_msg_list` | ❌ |
| `set_group_name(name)` | 改群名（有审计） | ✅ `set_group_name` | ❌ |
| `leave_group()` | **机器人退群**（危险，有审计） | ✅ `set_group_leave` | ❌ |
| `set_group_whole_ban(enable)` | 全员禁言（有审计） | ✅ `set_group_whole_ban` | ❌ |
| `send_like(user_id, times)` | 点赞，1~10 次（有审计） | ✅ `send_like` | ❌ |
| `version_info()` | 协议端版本（排障用） | ✅ `get_version_info` | ❌ |
| `at_all_remain()` | @全体 剩余次数 | ✅ `get_group_at_all_remain` | ❌ |
| `honor_list(kind="all")` | 群荣誉：`all`/`legend`/`strong_newbie`/`emotion` | ✅ `get_group_honor_info` | ❌ |
| `fetch_image(file_id)` | 取图片，返回 `url`/`path`/`local` | ✅ `get_image` | ❌ |
| `fetch_record(file_id)` | 取语音（转 mp3），同上 | ✅ `get_record` | ❌ |
| `mark_read(msg_id)` | 标记已读（无对外影响，不审计） | ✅ `mark_msg_as_read` | ❌ |
| `set_essence(msg_id)` | 设精华（有审计） | ✅ `set_essence_msg` | ❌ |
| `del_essence(msg_id)` | 移精华（有审计） | ✅ `delete_essence_msg` | ❌ |
| `set_admin(user_id, enable)` | 设/撤管理员（**提权**，有审计） | ✅ `set_group_admin` | ❌ |
| `set_title(user_id, title)` | 设专属头衔（有审计） | ✅ `set_group_special_title` | ❌ |
| `set_portrait(path)` | 改群头像（有审计；`path` 是协议端能读到的） | ✅ `set_group_portrait` | ❌ |
| `set_anonymous(enable)` | 开/关匿名（有审计） | ✅ `set_group_anonymous` | ❌ |
| `ban_anonymous(flag, sec)` | 禁言匿名成员（有审计） | ✅ `set_group_anonymous_ban` | ❌ |
| `user_status(user_id)` | 在线 / 忙碌状态 | ✅ `get_user_status` | ❌ |
| `unidirectional_friends()` | 单向好友列表 | ✅ `get_unidirectional_friend_list` | ❌ |
| `sign_info()` | 群签到信息 | ✅ `get_group_sign` | ❌ |
| `sign_in()` | 打卡（有审计） | ✅ `send_group_sign` | ❌ |
| `group_files()` | 群文件根目录 | ✅ `get_group_root_files` | ❌ |
| `folder_files(folder_id)` | 某个文件夹的内容 | ✅ `get_group_files_by_folder` | ❌ |
| `file_url(file_id, busid)` | 文件下载地址 | ✅ `get_group_file_url` | ❌ |
| `file_system_info()` | 群文件空间用量 | ✅ `get_group_file_system_info` | ❌ |
| `create_folder(name, parent_id)` | 建文件夹（有审计） | ✅ `create_group_file_folder` | ❌ |
| `delete_file(file_id, busid)` | 删文件（有审计） | ✅ `delete_group_file` | ❌ |
| `delete_folder(folder_id)` | 删文件夹（**连内容一起**，有审计） | ✅ `delete_group_folder` | ❌ |
| `can_send_image()` | 协议端当前能否发图 | ✅ `can_send_image` | ❌ |
| `can_send_record()` | 协议端当前能否发语音 | ✅ `can_send_record` | ❌ |
| `system_messages()` | 加群请求 / 被邀请记录（扩展） | ✅ `get_group_system_msg` | ❌ |

不传 `channel` 时用**当前事件**的频道，所以插件里通常不用传：

```python
@on_command("群信息", min_role=Role.ADMIN)
async def info(self, ctx: Context) -> None:
    if not ctx.platform.supports("group_info"):
        await ctx.reply.text("当前平台不支持查询群信息")
        return
    data = await ctx.platform.group_info()  # 自动用当前群
    members = await ctx.platform.member_info(ctx.event.actor.id)
    await ctx.reply.text(f"{data.get('group_name')} / {members.get('card') or members.get('nickname')}")
```

写操作（`set_group_card` / `upload_group_file`）走和发消息同一个**出站闸门**
（同频道串行 + 最小间隔 + 全局限速），所以不用担心刷。

`fetch_image` / `fetch_record` 的返回值会被归一化成 `{url, path, local}`：
`file` 是 http(s) 直链时给 `url`；是**协议端所在机器**的绝对路径时给 `path` 且
`local=True`。容器部署（NapCat 跑在 docker 里）时宿主侧读不到那个路径——
要么给容器配共享卷，要么只用 `url`。原始字段（`file` / `file_size` 等）原样保留。

群文件系列返回 `{"files": [...], "folders": [...]}`：协议端原始字段（`file_id` /
`file_name` / `file_size` / `busid` / `upload_time` / `uploader_name` …）原样保留，
另外补了短别名 `id` / `name` / `size` / `count`，日常用别名就够。删文件 / 取下载地址
必须带 `busid`——它来自文件条目本身，别自己猜。

`delete_folder()` 会先试 `delete_group_folder`，遇到「不支持的 API」自动重试老名字
`delete_group_file_folder`——两种协议端都能用。

**`supports()` 的真实含义**：它只保证「本适配器实现了这个动作」，不保证当前协议端实现了。
扩展动作（`user_status` / `sign_info` / `sign_in`）属于 go-cqhttp 遗留，
NapCat 4.18 实测返回 `retcode=1404 不支持的 API`，调用会抛 `AdapterError`——
用之前自己 try/except，或第一次失败后把结果缓存下来别再试。

## 服务与契约（插件之间互相调用）

概念已经合并：**能力只剩内核自带的基础设施句柄**，业务能力（生图、对话、账本、题库）
一律由插件以**服务**的形式提供。

| | 基础设施能力 | 服务 |
|---|---|---|
| 谁定义 | **内核**（net / storage / kv / schedule / moderation） | **插件自己**（`image:generate` / `llm:chat` / `economy` …） |
| 怎么拿 | `ctx.kv`、`ctx.net`、`ctx.storage`… | `ctx.service("名字")` |
| 缺了会怎样 | 句柄为 `None`，插件**降级**继续跑 | 依赖方进 **blocked 三态**（对象在、面板可见、规则不注册），依赖恢复后自动重试 |
| 写在哪个类属性 | `capabilities = [...]` | 提供方 `provides = {...}`，消费方 `services = {...}` |

### 提供方：写清契约

```python
from haiqbot import Plugin
from haiqbot.kernel.plugin import ServiceSpec


class EconomyService:
    def add(self, uid: str, amount: int) -> int: ...
    def balance(self, uid: str) -> int: ...


class Economy(Plugin):
    id = "economy"
    version = "0.1.0"
    provides = {
        "economy": ServiceSpec(
            name="economy",
            factory="build",  # 本类里构造服务的方法名（必填）
            version=1,  # 契约版本，改签名就加一
            methods=("add", "balance"),  # 声明了就必须真的有
        )
    }

    def build(self) -> EconomyService:
        return EconomyService()
```

`methods` 会在**提供方加载时**逐个校验：少一个就整插件加载失败，而不是等消费方运行时
`AttributeError`。

### 消费方：声明依赖

```python
class Shop(Plugin):
    id = "shop"
    requires = ["economy"]  # 只影响加载顺序（拓扑排序）
    services = {"economy": "1"}  # 硬约束：最低契约版本

    async def on_load(self, ctx):
        # 依赖在 on_load 之前已核对，这里可以放心用
        self.balance = ctx.service("economy").balance("u1")
```

三条规则：

1. **没声明就取不到**：`ctx.service("quiz")` 若没写进 `services`，直接抛
   `CapabilityDenied` —— 和能力句柄同一套边界。
2. **版本不够就不加载**：`services = {"economy": ">=2"}` 而提供方只到 v1，插件不加载，
   面板上这条插件的 `error` 会写清楚"需要 v2，当前 v1"。
3. **提供方重载/卸载后代理自动失效**：`ctx.service()` 返回的是惰性代理，每次调用都
   重新解析，被卸载后立刻抛 `CapabilityUnavailable` —— 这就是热插拔为真的前提。

### 依赖成环

`a.requires = ["b"]` 且 `b.requires = ["a"]` 时，拓扑排序抛 `ContractError`（带环路径），
registry 把错误挂到 `__dependencies__` 并**继续启动**，成环的那几个插件保持未加载。
成环的那几个插件面板上会显示 `blocked` 原因，修好依赖后 `resume_blocked()` 会自动补加载。

## 打包成 pip 插件

```toml
[project.entry-points."haiqbot.plugins"]
hello = "myplugin:Hello"
```

装好后 `haiq.toml` 的 `plugins.enabled` 里加上 `"hello"` 即可。

## 仓库里的插件：框架最小集 vs 你自己的

**框架只带两个最小插件**，在 `plugins/` 里。判断标准：陌生人装好框架、不接任何外部
服务，是否仍然想要它？答案是「是」的才留在框架目录。

| 插件 | 目录 | 提供的服务 | 订阅的服务 / 设施 | 凭据槽位 | 命令 |
|---|---|---|---|---|---|
| `echo` | `plugins/` | — | — | — | `/echo` |
| `help` | `plugins/` | — | — | — | `/help` |
| `assistant` | `plugins-extra/` | `image:generate` v1 + `llm:chat` v1 | 两个服务都自用、`kv`、`moderation` | `assistant.image_api_key` → `HAIQ_IMAGE_API_KEY`、`assistant.chat_api_key` → `HAIQ_CHAT_API_KEY` | `/问`、`/画图`、`@机器人`、`/重置对话` |
| `guard` | `plugins-extra/` | — | `moderation` | — | 刷屏限流 / 广告过滤 |
| `admin` | `plugins-extra/` | — | — | — | `/plugins`、`/status`（需 ADMIN） |

右边四个**不属于框架**：它们绑定具体厂商的 API Key，或写死了我自己的取舍（限流阈值、
违禁词表）。`plugins-extra/README.md` 说明了怎么整包搬走。

槽位名写法是 `<插件 id>.<槽位名>`，例如 `haiq secrets set assistant.image_api_key`。

## 插件自包含：配置 + 厂商实现

插件的**配置模型**和**厂商实现**都归插件自己，内核不认识"生图"或"对话"：

```
plugins-extra/assistant/image/
  __init__.py          # Plugin 子类（声明 secrets / provides / config_schema）
  config.py            # ImageCfg（Pydantic，字段全是生图语义）
  broker.py            # ImageBroker（业务编排）
  providers/
    openai_compat.py   # 厂商实现（只有插件自己 import）
    mock.py            # 演示/测试用
```

内核只保留 `haiqbot/services/providers/base.py`（`ProviderError`、`ImageProvider` 协议）。

**所以：不要把厂商实现塞进内核。** 想让插件支持多家厂商，就放自己的 `providers/` 里按
`cfg.provider` 分派 —— `assistant` 的 `image/` 与 `chat/` 两层就是范例。

`haiqbot init` 生成 `[plugins.*]` 段时**扫描插件的 `config_schema()`**，所以默认值只有一处来源：
改 schema 的 `default`，`haiqbot init` 跟着变，不需要动内核代码。

## 开发一个插件的最小清单

1. 继承 `Plugin`，写 `id` / `name` / `description`
2. **要凭据** → `secrets = {"api_key": SecretSlot(label="…")}`，取用时 `pctx.slot("api_key").headers()`
3. **要给别人用** → `provides = {"my:thing": ServiceSpec(name=…, factory=…, version=1, methods=(…))}`
4. **要用别人的** → `services = {"other:thing": "1"}`
5. **要参数** → `config_schema()`（面板与 `haiqbot init` 都靠它）
6. **要命令** → `@on_command` / `@on_message` / `@on_regex` …
7. **自测** → `haiq run --demo`（不连 QQ、不烧额度）+ `pytest`

一个能跑的最小例子：

```python
from haiqbot import Context, Plugin, SecretSlot, on_command


class Weather(Plugin):
    id = "weather"
    name = "天气"
    secrets = {"api_key": SecretSlot(label="天气 API Key")}

    @classmethod
    def config_schema(cls) -> list[dict]:
        return [{"key": "city", "label": "默认城市", "kind": "str", "default": "北京"}]

    @on_command("天气", usage="天气 <城市>")
    async def weather(self, ctx: Context, city: str = "") -> None:
        target = (city or ctx.config.get("city") or "北京").strip()
        # 需要 HTTP 就用 ctx.net（内核管白名单），需要凭据就在 provider 层用 pctx.slot
        await ctx.reply.text(f"{target}：晴，22°C（示例数据）")
```

## 规矩

1. **不要**读 `os.environ` 找密钥 —— 你没有，也不该有。
2. **不要**在日志里打印用户原文以外的敏感内容；`ctx.log` 已脱敏，但别绕开它。
3. **不要**自己起 HTTP 客户端访问需要凭据的服务；用 `ctx.service("…")` / `ctx.net`。
4. 每个 handler 默认 8s 超时，长任务请用 `ctx.schedule` 或先 `ack`。

## 在面板里长出自己的页面（PanelPage）

插件**不需要改内核**就能往面板里加页面，也不需要前端代码。三个可选成员，鸭子类型即可：

```python
class MyPlugin(Plugin):
    id = "my"

    def panel_meta(self) -> dict:  # 可选：只影响导航
        return {"title": "我的页面", "order": 50}

    def panel_page(self, panel) -> dict:  # 也可以是 async
        return {
            "title": "我的页面",
            "sections": [
                {"type": "text", "text": "一句话说明"},
                {"type": "field_group", "title": "概况", "fields": [{"label": "版本", "value": "0.1.0"}]},
                {
                    "type": "readonly_table",
                    "title": "最近",
                    "columns": [{"key": "k", "title": "键"}, {"key": "v", "title": "值"}],
                    "rows": [{"k": "a", "v": 1}],
                },
                {"type": "action_button", "label": "清理缓存", "action": "clear", "confirm": "确定清空？"},
            ],
        }

    async def panel_action(self, action: str, payload: dict) -> dict:
        if action == "clear":
            return {"message": "已清空"}
        raise KeyError(action)
```

规则（都是硬约束）：

1. **前端只渲染它认识的 section**（`text` / `field_group` / `readonly_table` / `action_button`），
   不认识的会降级成只读 JSON —— 插件写坏最多难看，不会白屏。
2. **动作必须先在页面里声明**才能执行。`panel_action` 不是通用方法调用面：
   调 `rm_rf` 这种没声明过的 action 会直接 403。
3. **面板上下文只给两样**：插件自己的 `panel.config` + 只读计数 `events_total` / `handled_total`。
   **不给 vault、不给 store 句柄** —— 要写数据请走自己的能力层，面板不是后门。
4. 面板动作受面板的三道门约束：会话 → 只读模式（`webui.write_enabled`）→ CSRF，
   并且每次执行都会写审计（`panel.action`）。

参考实现：`plugins/echo/__init__.py` 的 `panel_meta/panel_page/panel_action`。
