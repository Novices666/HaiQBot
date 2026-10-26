"""命令工作台。

两条完全不同的路，刻意不合并：

1. **预演**（`POST /api/workbench/parse`）：回答"我发这句话，谁会接管、参数被
   解析成什么、为什么别人没接管"。**不调用** `rule.invoker` —— 一旦调用就等于
   真的执行了 handler，工作台就变成了"远程执行面板"，那是另一个性质的东西
   （权限、幂等、副作用全都不成立）。
2. **真发**（`POST /api/workbench/send`）：只把**文本**发到指定频道，
   **不触发任何插件**。用途是验证链路本身（协议端 → 出站闸门 → 群），
   这是排障时最省事的一步。必须显式传 `confirm=true`（防误触），
   只读模式下直接拒绝。

真发走 `adapter.send_text`，所以照样经过出站闸门（同频道串行 + 最小间隔 +
全局限速 + 频控退避），不是绕后门直发。
"""

from __future__ import annotations

from typing import Any

from haiqbot.core.channel import Actor, ChannelId, ChannelKind, Role
from haiqbot.core.event import EventKind, MessageEvent
from haiqbot.core.segment import Segment
from haiqbot.webui.api.common import Ctx, audit, fail, ok, read_json

_ROLE_NAMES = {1: "USER", 2: "ADMIN", 3: "OWNER"}


def _role(value: Any) -> Role:
    """Role 是 IntEnum：USER=1 / ADMIN=2 / OWNER=3（不是 0 起）。"""
    try:
        return Role(int(value))
    except (TypeError, ValueError):
        return Role.USER


def _channel_kind(value: Any) -> ChannelKind:
    try:
        return ChannelKind(str(value or "group"))
    except ValueError:
        return ChannelKind.GROUP


def routes(ctx: Ctx) -> list[Any]:
    from starlette.routing import Route

    runtime = ctx.runtime

    def _rules() -> list[Any]:
        bus = getattr(runtime, "bus", None)
        rules = getattr(bus, "_rules", None)
        if rules:
            return list(rules)
        out: list[Any] = []
        for handle in ctx.registry.handles().values() if ctx.registry else []:
            out.extend(handle.rules)
        return out

    async def parse(request: Any) -> Any:
        try:
            body = await read_json(request)
        except ValueError as exc:
            return fail(400, "bad_request", str(exc))

        text = str(body.get("text") or "")
        if not text:
            return fail(400, "bad_request", "text 不能为空")

        platform = str(body.get("platform") or "onebot")
        kind = _channel_kind(body.get("channel_kind"))
        channel_id = str(body.get("channel_id") or "0")
        role = _role(body.get("role", 0))
        at_bot = bool(body.get("at_bot", True))

        event = MessageEvent(
            kind=EventKind.MESSAGE,
            channel=ChannelId(platform, kind, channel_id),
            actor=Actor(id=str(body.get("actor") or "webui-probe"), name="工作台预演", role=role),
            segments=[Segment.text(text)],
            msg_id="webui-dryrun",
            at_bot=at_bot,
            bot_id=None,
            window_ttl=None,
            raw={"dry_run": True},
        )

        matched: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for rule in sorted(_rules(), key=lambda r: (-r.priority, r.plugin_id)):
            reason = _skip_reason(rule, event, role)
            if reason:
                skipped.append(
                    {
                        "plugin": rule.plugin_id,
                        "rule": rule.label,
                        "priority": rule.priority,
                        "reason": reason,
                    }
                )
                continue
            outcome = rule.matches(event)
            if not outcome.matched:
                skipped.append(
                    {
                        "plugin": rule.plugin_id,
                        "rule": rule.label,
                        "priority": rule.priority,
                        "reason": "命令词/参数不匹配",
                    }
                )
                continue
            try:
                params = rule.build_kwargs(outcome, text)
            except Exception as exc:  # noqa: BLE001 - 参数解析失败本身就是要看的信息
                params = {"__error__": f"{type(exc).__name__}: {exc}"}
            matched.append(
                {
                    "plugin": rule.plugin_id,
                    "rule": rule.label,
                    "priority": rule.priority,
                    "commands": list(getattr(rule, "commands", ()) or ()),
                    "usage": getattr(rule, "usage", ""),
                    "block": bool(getattr(rule, "block", False)),
                    "params": {k: repr(v) for k, v in (params or {}).items() if k != "ctx"},
                }
            )

        return ok(
            {
                "dry_run": True,
                "note": "只做到解析与匹配，不调用插件、不发消息",
                "event": {
                    "text": text,
                    "platform": platform,
                    "channel_kind": str(kind),
                    "channel_id": channel_id,
                    "role": _ROLE_NAMES.get(int(role), str(role)),
                    "at_bot": at_bot,
                },
                "matched": matched,
                "skipped": skipped,
                "rules_total": len(_rules()),
            }
        )

    def _skip_reason(rule: Any, event: MessageEvent, role: Role) -> str | None:
        events = getattr(rule, "events", None)
        if events is not None and event.kind not in events:
            return "事件类型不匹配"
        platforms = getattr(rule, "platforms", None)
        if platforms is not None and event.channel.platform not in platforms:
            return f"平台不匹配（只接受 {', '.join(sorted(platforms))}）"
        kinds = getattr(rule, "channel_kinds", None)
        if kinds is not None and event.channel.kind not in kinds:
            return f"频道类型不匹配（只接受 {', '.join(k.value for k in kinds)}）"
        min_role = getattr(rule, "min_role", Role.USER)
        if role < min_role:
            need = _ROLE_NAMES.get(int(min_role), str(min_role))
            return f"权限不足（需要 {need}）"
        if getattr(rule, "invoker", None) is None:
            return "规则未绑定 handler（插件未加载完）"
        return None

    async def send(request: Any) -> Any:
        """把一条文本真发到指定频道。**不触发插件**，只验链路。"""
        if not ctx.write_enabled:
            return fail(403, "readonly", "面板处于只读模式，不允许真发")
        try:
            body = await read_json(request)
        except ValueError as exc:
            return fail(400, "bad_request", str(exc))

        # 防误触：默认不真发。前端那个开关就是这个字段。
        if body.get("confirm") is not True:
            return fail(
                400,
                "confirm_required",
                "真发需要显式传 confirm=true",
                hint="这是防误触开关：不带它一律按预演处理，不会发出去",
            )

        text = str(body.get("text") or "").strip()
        if not text:
            return fail(400, "bad_request", "text 不能为空")
        # 面板不是聊天客户端，给个上限，免得把整篇文档塞进去
        if len(text) > 4000:
            return fail(400, "bad_request", "text 太长（上限 4000 字）")

        platform = str(body.get("platform") or "onebot")
        kind = _channel_kind(body.get("channel_kind"))
        channel_id = str(body.get("channel_id") or "").strip()
        if not channel_id or channel_id == "0":
            return fail(400, "bad_request", "channel_id 不能为空")

        adapter = runtime.adapters.get(platform)
        if adapter is None:
            known = ", ".join(sorted(runtime.adapters)) or "（没有已启动的适配器）"
            return fail(404, "unknown_platform", f"平台 {platform} 未启动（可用：{known}）")

        channel = ChannelId(platform, kind, channel_id)
        try:
            message_id = await adapter.send_text(channel, text, plugin_id="webui:workbench")
        except Exception as exc:  # noqa: BLE001 - 失败原因要原样带回面板
            await audit(
                ctx,
                "workbench.send",
                {"channel": str(channel), "ok": False, "error": f"{type(exc).__name__}: {exc}"},
            )
            return fail(
                502,
                "send_failed",
                f"{type(exc).__name__}: {exc}",
                hint="看「日志 · 审计 · 媒体」页的 outbound_* 记录，或 haiq doctor",
            )

        await audit(
            ctx,
            "workbench.send",
            {"channel": str(channel), "ok": True, "message_id": message_id, "chars": len(text)},
        )
        return ok(
            {
                "sent": True,
                "message_id": message_id,
                "channel": str(channel),
                "chars": len(text),
            }
        )

    return [
        Route("/api/workbench/parse", parse, methods=["POST"]),
        Route("/api/workbench/send", send, methods=["POST"]),
    ]
