"""插件脚手架：证明「能力句柄 + 零凭据」这套约定在外部包里也成立。"""

from __future__ import annotations

from haiqbot import Context, Plugin, on_command, on_message


class Demo(Plugin):
    id = "demo"
    name = "脚手架示例"
    version = "0.1.0"
    description = "外部包形式的 haiqbot 插件"
    # 声明了才发句柄；这里只要 kv
    capabilities = ["kv"]

    async def on_load(self, ctx: Context) -> None:
        count = await ctx.kv.get("load_count", "0")
        await ctx.kv.set("load_count", str(int(count) + 1))
        ctx.log.info("demo_loaded", times=int(count) + 1)

    @on_command("demo", usage="demo", priority=5)
    async def demo(self, ctx: Context) -> None:
        count = await ctx.kv.get("hit_count", "0")
        await ctx.kv.set("hit_count", str(int(count) + 1))
        await ctx.reply.text(f"demo 命中第 {int(count) + 1} 次")

    @on_message(priority=-200)
    async def observe(self, ctx: Context) -> None:
        # 只观察，不回复
        ctx.log.debug("demo_observed", text=ctx.event.text if ctx.event else "")
