# haiqbot

**QQ Bot 框架（OneBot v11 + 官方 OpenAPI 双通道）+ 插件生态。**

> 把协议通道与插件编织在一起。

---

## 一句话卖点

**插件永远拿不到密钥，也永远不需要知道自己在哪个平台。**

- 凭据只活在内核的 provider 里；插件只能拿到受限的**基础设施句柄**（`ctx.net` / `ctx.kv` / `ctx.storage` / `ctx.schedule` / `ctx.moderation`）和**服务句柄**（`ctx.service("…")`）。
- 平台差异收敛到**能力协商**（`ctx.reply.capabilities`）。插件里不写 `if platform == "qq"`，框架按目标平台自动降级（发图 → 发链接 → 文本提示）。

## 状态

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 骨架 / 配置 / 脱敏日志 / vault / store | ✅ |
| M1 | core（Segment·Event·Channel·Reply）/ bus / matcher | ✅ |
| M2 | registry / plugin / capabilities / governance | ✅ |
| M3 | mock adapter + e2e | ✅ |
| M4 | official adapter + media_store + image_broker + painter | ✅ |
| M5 | onebot11 adapter + help/admin | ✅ |
| M6 | webui 管控面板（只读看板 + 写操作令牌）+ CLI | ✅ |
| M7 | Dockerfile / compose / NSSM / systemd / `haiqbot init` | ✅ |
| M8 | 子进程隔离（插件 ABI v2） | ⏳ 预留 |

## 快速开始

```bash
uv sync --extra dev          # 装依赖
uv run haiqbot init             # 生成 haiq.toml + secrets/ 骨架
uv run haiq doctor           # 自检
uv run haiq run --demo       # 不连 QQ、不烧额度的本地演示
```

真实环境：

```bash
# 生图凭据（写入 DPAPI 加密库，不回显、不进 shell history）
uv run haiq vault set WEFT_IMAGE_API_KEY

# QQ 官方平台
uv run haiq vault set WEFT_QQ_APP_SECRET
# 然后在 haiq.toml 里填 [official] app_id 并置 enabled = true
uv run haiq run
```

## 写一个插件

```python
from haiqbot import Context, Plugin, ServiceSpec, on_command


class Painter(Plugin):
    id = "painter"
    name = "生图"
    # 自己提供 image:generate，也自己消费；插件之间只认这个名字，内核不认识"生图"
    provides = {
        "image:generate": ServiceSpec(
            name="image:generate",
            factory="build_image_service",
            version=1,
            methods=("generate",),
        )
    }
    services = {"image:generate": "1"}

    @on_command("画图", aliases=["draw"], usage="画图 <描述>")
    async def draw(self, ctx: Context, prompt: str) -> None:
        if not ctx.reply.capabilities.image:
            await ctx.reply.text("当前平台不支持图片")
            return
        ref = await ctx.service("image:generate").generate(prompt)  # 无 key、无 endpoint
        await ctx.reply.image(ref)
```

详见 [docs/PLUGIN-GUIDE.md](docs/PLUGIN-GUIDE.md) 与 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 安全模型

| 闸 | 落地 |
|---|---|
| 不进仓库 | 真值只进 `secrets/`（已 gitignore）+ gitleaks pre-commit |
| 不进日志 | structlog 脱敏 processor；vault 取值即登记，日志出现即打码 |
| 不进 URL | 生图结果一律落地中转，对外只发 `/i/{随机ID}`，无参数、不可枚举 |
| 不裸奔入口 | 媒体服务与 OneBot 反向 WS 仅监听 `127.0.0.1`，公网只反代 `/i/*` |

## License

MIT
