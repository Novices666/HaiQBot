"""haiqbot 示例插件：最小可用的回声插件，用于验证注册/分发/卸载链路。"""

from __future__ import annotations

from typing import Any

from haiqbot.plugin_api import Context, Plugin, on_command, on_message


class Echo(Plugin):
    id = "echo"
    name = "回声"
    description = "把你说的话原样送回去，用于验证插件链路"

    @on_command(
        "echo",
        aliases=["回声"],
        usage="echo <内容>",
        description="原样回显后面的内容（验证链路用）",
        priority=10,
    )
    async def echo(self, ctx: Context, content: str = "") -> None:
        text = content.strip() or "（空）"
        await ctx.reply.text(text)

    @on_message(priority=-100)
    async def passthrough(self, ctx: Context) -> None:
        """低优先级兜底：只有没人认领时才回声。"""
        if ctx.handled:
            return
        if ctx.event.text:
            ctx.log.debug("echo_passthrough", text=ctx.event.text)

    # -- 面板自定义页（PanelPage 契约的参考实现）--------------------------- #
    # 只需要三个可选成员：panel_meta（导航）/ panel_page（内容）/ panel_action（动作）。
    # 内核不认识 echo，也不知道这个页面长什么样 —— 全靠插件自己声明。

    def panel_meta(self) -> dict[str, Any]:
        return {"title": "回声 · 自检", "order": 90}

    def panel_page(self, panel: Any) -> dict[str, Any]:
        return {
            "title": "回声插件",
            "sections": [
                {
                    "type": "text",
                    "text": "这是插件自己声明的页面：内核不写死任何字段，echo 只提供 schema。",
                },
                {
                    "type": "field_group",
                    "title": "概况",
                    "fields": [
                        {"label": "插件 ID", "value": self.id},
                        {"label": "命令", "value": "echo / 回声"},
                        {"label": "私有配置", "value": str(dict(panel.config)) or "{}"},
                        {"label": "累计事件", "value": int(getattr(panel, "events_total", 0))},
                    ],
                },
                {
                    "type": "readonly_table",
                    "title": "示例表格（数据由插件自己给）",
                    "columns": [
                        {"key": "k", "title": "键"},
                        {"key": "v", "title": "值"},
                    ],
                    "rows": [
                        {"k": "规则数", "v": "2（echo + 兜底回声）"},
                        {"k": "需要凭据", "v": "否"},
                    ],
                },
                {
                    "type": "action_button",
                    "label": "点一下：面板动作链路自检",
                    "action": "ping",
                },
            ],
        }

    def panel_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "ping":
            text = str((payload or {}).get("echo") or "pong：面板动作链路已打通")
            return {"message": text}
        raise KeyError(action)
