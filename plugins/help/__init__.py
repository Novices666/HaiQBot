"""内置插件：帮助。自动汇总所有插件的命令，不需要手写文档。"""

from __future__ import annotations

from haiqbot.plugin_api import Context, Plugin, on_command


class Help(Plugin):
    id = "help"
    name = "帮助"
    description = "列出所有可用命令"
    capabilities: list[str] = []

    @on_command(
        "help",
        aliases=["帮助", "菜单"],
        usage="help",
        description="列出你当前权限能用到的命令",
        priority=100,
    )
    async def show_help(self, ctx: Context) -> None:
        rows = ctx.introspect.commands() if ctx.introspect else []
        if not rows:
            await ctx.reply.text("还没有任何命令。")
            return

        role = ctx.event.actor.role if ctx.event else 1
        visible = [r for r in rows if int(r["min_role"]) <= int(role)]
        if not visible:
            await ctx.reply.text("当前没有你能用的命令。")
            return

        lines = ["📖 可用命令"]
        current = None
        for row in visible:
            if row["plugin_name"] != current:
                current = row["plugin_name"]
                lines.append(f"\n【{current}】")
            usage = row["usage"] or row["command"]
            desc = row["description"] or ""
            lines.append(f"  /{usage}" + (f" —— {desc}" if desc else ""))
        await ctx.reply.text("\n".join(lines))
