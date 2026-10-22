"""haiqbot 命令行。

haiqbot init                生成 haiq.toml 与 secrets/ 骨架
haiq doctor              自检（依赖 / 配置 / 凭据 / 端口）
haiq run [--demo]        启动
haiqbot plugin list|reload|enable|disable   插件管理（有运行实例时自动转发）
haiq webui token [--generate]        生成面板写操作令牌
haiq vault set|list|rm               凭据管理（不回显、不进 shell history）
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from functools import lru_cache
from pathlib import Path

from haiqbot.config import DEFAULT_CONFIG_FILE, dump_default_config, load_config
from haiqbot.core.errors import ConfigError
from haiqbot.kernel.log import configure_logging, get_logger

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
def _cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path or DEFAULT_CONFIG_FILE)
    if target.exists() and not args.force:
        print(f"× {target} 已存在（加 --force 覆盖）")
        return 1
    target.write_text(dump_default_config(), encoding="utf-8")
    secrets_dir = Path(args.secrets_dir or "secrets")
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / ".gitkeep").write_text("", encoding="utf-8")
    print(f"✅ 已生成 {target}")
    print(f"✅ 已创建 {secrets_dir}/（已加入 .gitignore）")
    print("\n下一步：")
    print("  haiq vault set WEFT_IMAGE_API_KEY   # 生图密钥（交互式输入，不回显）")
    print(
        "  haiq doctor                          # 自检\n  haiq channels list                   # 频道开关（应急入口）"
    )
    print("  haiq run --demo                      # 不连 QQ 的本地演示")
    return 0


def _doctor_channels(cfg: object) -> tuple[int, list[dict]]:
    """频道开关体检：返回 (配置条数, 会导致静默失效的 error 列表)。"""
    import asyncio

    from haiqbot.kernel import channel_control as cc
    from haiqbot.kernel.plugin_sources import iter_plugin_classes
    from haiqbot.kernel.store import Store

    class _Registry:
        """doctor 不起完整 runtime，只能做插件名校验（feature 级要面板/运行时）。"""

        def discover(self) -> dict[str, str]:
            return {
                str(getattr(cls, "id", "")): "entrypoint"
                for cls in iter_plugin_classes()
                if getattr(cls, "id", None)
            }

    async def run() -> tuple[int, list[dict]]:
        store = Store(cfg.db_path)  # type: ignore[attr-defined]
        await store.open()
        try:
            runtime = type("RT", (), {})()
            runtime.store = store
            problems = await cc.verify(runtime, _Registry())
            rows = await store.channel_all()
            return len(rows or []), [p for p in problems if p["level"] == "error"]
        finally:
            await store.close()

    return asyncio.run(run())


def _cmd_doctor(args: argparse.Namespace) -> int:
    """环境自检。退出码：0 = 全绿 / 1 = 有警告 / 2 = 有错误。"""
    cfg = load_config(args.config)
    errors = 0
    warnings = 0

    print("haiq doctor")
    print("-" * 46)

    # 配置
    cfg_path = Path(args.config or DEFAULT_CONFIG_FILE)
    if cfg_path.is_file():
        print(f"配置：OK {cfg_path}")
    else:
        print("配置：缺失（用默认值）")
        errors += 1

    # 依赖
    for name in ("httpx", "structlog", "aiosqlite", "starlette", "uvicorn", "websockets"):
        try:
            __import__(name)
            print(f"依赖 {name}：OK")
        except ImportError:
            print(f"依赖 {name}：缺失")
            errors += 1

    # 可选：botpy
    try:
        import botpy  # noqa: F401

        print("依赖 qq-botpy：OK（官方平台可用）")
    except ImportError:
        if cfg.official.enabled:
            print("依赖 qq-botpy：缺失 —— 官方平台已启用但没装 SDK")
            errors += 1
        else:
            print("依赖 qq-botpy：未安装（仅官方平台需要）")

    # 凭据
    from haiqbot.kernel.plugin import resolve_secret_ref
    from haiqbot.kernel.vault import Vault, VaultError

    vault = Vault.from_config(cfg)
    print(f"凭据加密：{vault.enc_kind}（{vault.primary_backend}）")

    vdir = Path(cfg.vault.dir or cfg.secrets_dir)
    if (vdir / "vault.bin").is_file() and not (vdir / "vault.json").is_file():
        print("失效：检测到旧格式 vault.bin，新格式读不了它")
        print("      请执行：haiq secrets reencrypt")
        errors += 1

    # 明文密码文件 = 加密白做：能读到 vault.json 的人也能读到钥匙
    for name in (".vault-pass.txt", "vault-pass.txt", ".passphrase"):
        if (vdir / name).is_file():
            print(f"警告：明文密码文件还在：{vdir / name}")
            print("      它让 vault 加密形同虚设；换密码后立刻删掉")
            print("      轮换：powershell -ExecutionPolicy Bypass -File scripts\\rotate-vault-pass.ps1")
            warnings += 1
            break

    targets: list[tuple[str, str, bool]] = []
    for slot_key, entry in sorted(_slot_index().items()):
        section = cfg.plugin_config.get(entry["plugin"]) or {}
        targets.append(
            (
                resolve_secret_ref(entry["plugin"], entry["name"], entry["slot"], section),
                f"{slot_key}（{entry['slot'].label}）",
                bool(entry["slot"].required),
            )
        )
    targets += [
        (cfg.official.app_secret_ref, "官方 AppSecret", cfg.official.enabled),
        (cfg.onebot.access_token_ref, "OneBot access_token", cfg.onebot.enabled),
        (cfg.webui.token_ref, "面板写操作令牌", cfg.webui.write_enabled),
    ]

    for ref, label, required in targets:
        try:
            resolved = vault.resolve(ref)
        except VaultError as exc:
            print(f"凭据 {label}：读不了（{exc}）")
            errors += 1
            continue
        if resolved.present:
            print(f"凭据 {label}（{ref}）：OK 来源 {resolved.backend}")
        elif required:
            print(f"凭据 {label}（{ref}）：缺失（必需）")
            errors += 1
        else:
            print(f"凭据 {label}（{ref}）：未设置（可选）")

    # 平台
    print(
        f"官方平台：{'启用' if cfg.official.enabled else '关闭'}"
        f"{'' if not cfg.official.enabled else ('，app_id OK' if cfg.official.app_id else '，app_id 缺失')}"
    )
    if cfg.official.enabled and not cfg.official.app_id:
        errors += 1
    print(f"OneBot：{'启用（' + cfg.onebot.mode + '）' if cfg.onebot.enabled else '关闭'}")
    print(f"WebUI：{'http://' + cfg.webui.host + ':' + str(cfg.webui.port) if cfg.webui.enabled else '关闭'}")

    # 本地绑定检查
    if cfg.webui.host not in ("127.0.0.1", "localhost", "::1"):
        print("警告：WebUI 监听了非本机地址；公网暴露只应反代 /i/*")
        warnings += 1
    if (
        cfg.onebot.enabled
        and cfg.onebot.mode == "server"
        and cfg.onebot.host not in ("127.0.0.1", "localhost")
    ):
        print("警告：OneBot 反向 WS 监听了非本机地址，请务必设置 access_token")
        warnings += 1
    if cfg.onebot.enabled and cfg.onebot.mode == "http" and cfg.onebot.host not in ("127.0.0.1", "localhost"):
        # 协议端在容器里时监听 0.0.0.0 是正常且必需的；
        # 只有"对公网敞开又完全没鉴权"才算警告。
        try:
            has_token = vault.resolve(cfg.onebot.access_token_ref).present
        except Exception:  # noqa: BLE001
            has_token = False
        if has_token or cfg.onebot.webhook_secret_ref:
            print("提示：OneBot HTTP 上报监听非本机地址（容器部署常见），鉴权已启用")
        else:
            print("警告：OneBot HTTP 上报监听非本机地址，且没有 token / webhook_secret_ref")
            warnings += 1

    # 系统代理
    try:
        import urllib.request

        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001
        proxies = {}
    if proxies:
        without_bypass = {k: v for k, v in proxies.items() if k != "no"}
        if without_bypass:
            print(f"警告：检测到系统代理 {without_bypass}；本地 http://127.0.0.1 请求可能被代理拦截")
            print("      验证 /i/* 时请用 trust_env=False 的客户端，或把 127.0.0.1 加入代理例外")
            print("      更要紧：协议端（尤其跑在容器里的 NapCat）的媒体上传也会被代理劫走，")
            print("              表现为「文字正常、发图失败」（rich media transfer failed）——先关代理再排障")
            warnings += 1
        if "no" in proxies:
            print(f"    代理例外(no_proxy): {proxies['no']}")
    else:
        print("系统代理：未设置 OK")

    # 频道开关（默认全关）：最容易踩的坑是"配了却不生效"
    try:
        total, bad = _doctor_channels(cfg)
        if total == 0:
            print("警告：没有任何频道配置 —— 按默认全关，所有群/私聊都不会响应")
            warnings += 1
        elif bad:
            print(f"警告：{len(bad)} 条频道配置会导致「配了不生效」：")
            for item in bad[:5]:
                print(f"      {item['channel']}：{item['message']}")
            warnings += 1
        else:
            print(f"频道开关：{total} 条配置，无静默失效 OK")
    except Exception as exc:  # noqa: BLE001 - 体检失败不该让 doctor 崩
        print(f"频道开关：检查失败（{type(exc).__name__}: {exc}）")
        warnings += 1

    print("-" * 46)
    if errors:
        print(f"结果：{errors} 个错误，{warnings} 个警告")
    elif warnings:
        print(f"结果：{warnings} 个警告")
    else:
        print("结果：全部通过")
    return 2 if errors else (1 if warnings else 0)


def _cmd_run(args: argparse.Namespace) -> int:
    from haiqbot.app import App

    cfg = load_config(args.config)
    configure_logging(cfg.log_level, cfg.log_json)

    if args.demo:
        app = App(cfg)
        result = asyncio.run(app.demo())
        print()
        for line in result["transcript"]:
            print(line)
        print()
        return 0

    app = App(cfg)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        print("\n收到中断，正在退出…")
        try:
            asyncio.run(app.shutdown())
        except Exception:  # noqa: BLE001
            pass
    return 0


def _remote_control(cfg, path: str, body: dict | None = None) -> tuple[int, dict] | None:
    """向**运行中**的实例发一个写请求。

    返回 (状态码, 响应体)；连不上（没有实例在跑）返回 None。
    必须显式禁用代理：系统代理会把 127.0.0.1 也劫走，导致本地请求 502。
    """
    import urllib.error
    import urllib.request

    from haiqbot.kernel.vault import Vault

    vault = Vault.from_config(cfg)
    token = (vault.get(cfg.webui.token_ref) if cfg.webui.token_ref else None) or ""
    url = f"http://{cfg.webui.host}:{cfg.webui.port}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body or {}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except Exception:  # noqa: BLE001
            payload = {"error": f"HTTP {exc.code}"}
        return exc.code, payload
    except (urllib.error.URLError, OSError):
        return None


def _plugin_list(cfg, args: argparse.Namespace) -> int:
    from haiqbot.app import App

    async def _run() -> int:
        app = App(cfg)
        await app.setup(enable_adapters=False)
        assert app.registry is not None
        try:
            rows = app.registry.status()
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
                return 0
            print(f"{'ID':<12}{'版本':<9}{'状态':<8}来源")
            print("-" * 72)
            for row in rows:
                if row.get("error"):
                    state = "错误"
                elif row.get("loaded"):
                    state = "运行中"
                elif row.get("enabled"):
                    state = "未加载"
                else:
                    state = "未启用"
                version = row.get("version") or "-"
                name = row.get("name") or ""
                label = f"{row['id']}" + (f"（{name}）" if name and name != row["id"] else "")
                print(f"{label:<12}{version:<9}{state:<8}{row['source']}")
                if row.get("error"):
                    print(f"{'':<12}↳ {row['error'][:70]}")
        finally:
            await app.shutdown()
        return 0

    return asyncio.run(_run())


def _plugin_offline(cfg, args: argparse.Namespace) -> int:
    """没有运行中的实例时：reload 只做加载校验，enable/disable 只改配置文件。"""
    from haiqbot.app import App
    from haiqbot.kernel.config_writer import write_config

    async def _run() -> int:
        app = App(cfg)
        await app.setup(enable_adapters=False)
        assert app.registry is not None
        try:
            if args.action == "reload":
                try:
                    await app.registry.load(args.id)
                except Exception as exc:  # noqa: BLE001
                    print(f"× {args.id} 加载失败：{type(exc).__name__}: {exc}")
                    return 1
                print(f"✅ {args.id} 离线加载校验通过（插件本身没问题）")
                print("   ⚠ 这只验证了能否加载；正在运行的实例要生效，请用面板『重载』或重启进程")
                return 0

            enabled = list(cfg.plugins_enabled)
            if args.action == "enable":
                if args.id not in enabled:
                    enabled.append(args.id)
            else:
                enabled = [x for x in enabled if x != args.id]
            if not cfg.config_path:
                print("× 没有定位到配置文件，无法写入")
                return 1
            write_config(cfg.config_path, {"plugins.enabled": enabled})
            print(f"✅ 已写入 {cfg.config_path}：{args.action} {args.id}")
            print("   ⚠ 需重启进程生效")
            return 0
        finally:
            await app.shutdown()

    return asyncio.run(_run())


def _cmd_channels(args: argparse.Namespace) -> int:
    """频道开关的**应急入口**：列表 / 开启 / 关闭。

    日常应该用面板；这里存在的意义是「面板不可用时还能把群救回来」——
    只依赖 vault 密码与数据库，不需要服务在跑。
    """
    import asyncio

    from haiqbot.kernel import channel_control as cc
    from haiqbot.kernel.plugin_sources import iter_plugin_classes
    from haiqbot.kernel.store import Store

    cfg = load_config(args.config)

    class _Registry:
        """只提供 discover()，够 channel_control 做插件名校验即可。"""

        def discover(self) -> dict[str, str]:
            return {
                str(getattr(cls, "id", "")): "entrypoint"
                for cls in iter_plugin_classes()
                if getattr(cls, "id", None)
            }

    class _Audit:
        async def record(self, **_kw: object) -> None:
            return None

    async def run() -> int:
        store = Store(cfg.db_path)
        await store.open()
        try:
            runtime = type("RT", (), {})()
            runtime.store = store
            runtime.audit = _Audit()
            runtime.channel_gate = None
            registry = _Registry()

            if args.action == "verify":
                problems = await cc.verify(runtime, registry)
                marks = {"error": "x", "warn": "!", "info": "."}
                for item in problems:
                    print(f"{marks.get(item['level'], '?')} {item['channel']}: {item['message']}")
                bad = sum(1 for item in problems if item["level"] == "error")
                if not problems:
                    rows = await store.channel_all()
                    print(f"OK：{len(rows)} 条频道配置都说得通（没有静默失效）")
                    return 0
                print(f"\n共 {len(problems)} 条提示，其中 {bad} 条会真正导致「配了不生效」")
                return 1 if bad else 0

            if args.action == "list":
                rows = await store.channel_all()
                if not rows:
                    print("（没有任何频道配置 —— 按默认全关，所有群都不会响应）")
                    print("  开启示例：haiq channels enable onebot:group:674425800 --plugins echo,help")
                    return 0
                print(f"{'频道':<34}{'状态':<6}已开启")
                for row in rows:
                    names = row.get("plugins") or []
                    state = "开" if row.get("enabled", 1) else "关"
                    print(f"{row['channel']:<34}{state:<6}{', '.join(map(str, names)) or '（无插件）'}")
                return 0

            targets = [t for t in (args.targets or []) if t]
            if not targets:
                print("x 需要指定频道，例如：haiq channels enable onebot:group:674425800 --plugins echo")
                return 1

            picked = [x.strip() for x in (args.plugins or "").split(",") if x.strip()]
            known = sorted(registry.discover())

            for channel in targets:
                if args.action == "enable":
                    if picked:
                        out = await cc.set_features(runtime, registry, channel, replace=picked, actor="cli")
                    else:
                        out = await cc.set_features(runtime, registry, channel, replace=known, actor="cli")
                else:  # disable
                    if picked:
                        out = await cc.set_features(runtime, registry, channel, off=picked, actor="cli")
                    else:
                        out = await cc.set_features(runtime, registry, channel, replace=[], actor="cli")

                changed = out["changed"]
                if changed:
                    print(f"ok {channel}：变更 {', '.join(changed)}")
                else:
                    print(f"=  {channel}：没有变化")
                print(f"   当前开启：{', '.join(out['enabled']) or '（无）'}")
            return 0
        except cc.ChannelControlError as exc:
            print(f"x {exc.message}")
            return 1
        finally:
            await store.close()

    return asyncio.run(run())


def _cmd_plugin(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    # list 是给人看的表格，别让启动/停止日志混进来
    configure_logging("WARNING" if args.action == "list" else cfg.log_level, cfg.log_json)

    if args.action == "list":
        return _plugin_list(cfg, args)

    if not args.id:
        print(f"× 需要插件 id：haiqbot plugin {args.action} <id>")
        return 1

    # 优先交给运行中的实例 —— 只有它握着真正的总线与插件实例
    remote = _remote_control(cfg, f"/api/plugin/{args.id}/{args.action}")
    if remote is not None:
        status, payload = remote
        if status == 200:
            print(f"✅ {args.id}：{payload.get('note') or payload}")
            return 0
        print(f"× {args.id}：{payload.get('error') or f'HTTP {status}'}")
        return 1

    print("· 未检测到运行中的实例，转为离线处理")
    return _plugin_offline(cfg, args)


def _cmd_webui(args: argparse.Namespace) -> int:
    import secrets

    from haiqbot.kernel.vault import Vault

    cfg = load_config(args.config)
    ref = cfg.webui.token_ref
    if not ref:
        print("× haiq.toml 里没有配置 [webui].token_ref")
        return 1

    vault = Vault.from_config(cfg)
    current = vault.get(ref)

    if args.generate:
        token = secrets.token_urlsafe(24)
        backend = vault.set(ref, token)
        print(f"✅ 已生成并写入 vault（键 {ref}，后端 {backend}）")
        print(f"   令牌：{token}")
        print("   复制到面板顶部的「写操作令牌」输入框即可。此处只显示这一次。")
        return 0

    if current:
        print(f"✅ {ref} 已设置（{len(current)} 字符，不回显）")
        print("   忘了就用 `haiq webui token --generate` 重新生成")
        return 0
    print(f"× {ref} 未设置 —— 面板写操作目前不可用（403）")
    print("   生成：haiq webui token --generate")
    return 1


def _cmd_vault(args: argparse.Namespace) -> int:
    """兼容入口。新写法请用 `haiq secrets`（支持 painter.api_key 这种槽位名）。"""
    print("提示：haiq vault 已由 haiq secrets 取代（槽位名更好用）", file=sys.stderr)
    from haiqbot.kernel.vault import Vault, VaultError

    cfg = load_config(args.config)
    vault = Vault.from_config(cfg)

    if args.action == "list":
        keys = vault.keys()
        if not keys:
            print("（空）")
        for key in keys:
            print(f"{key}  （已设置）")
        return 0

    if args.action == "set":
        if not args.key:
            print("× 用法：haiq vault set <KEY>")
            return 1
        if args.stdin:
            value = sys.stdin.readline().strip()
        else:
            value = getpass.getpass(f"请输入 {args.key} 的值（不回显）：").strip()
        if not value:
            print("× 空值，已取消")
            return 1
        try:
            backend = vault.set(args.key, value)
        except VaultError as exc:
            print(f"× {exc}")
            return 1
        print(f"✅ 已写入 {args.key}（后端：{backend}）")
        return 0

    if args.action in ("rm", "delete"):
        if not args.key:
            print("× 用法：haiq vault rm <KEY>")
            return 1
        removed = vault.delete(args.key)
        print("✅ 已删除" if removed else "– 未找到")
        return 0 if removed else 1

    print(f"× 未知动作 {args.action}")
    return 1


# --------------------------------------------------------------------------- #
def _secrets_passwd(cfg, vdir: Path) -> int:
    """轮换 vault 口令：旧口令解开 → 新口令重写。

    存在的理由：`reencrypt` 只能从 DPAPI 遗留文件迁移，一旦那份 `.bak` 没了，
    口令就永远改不了。这里不依赖任何遗留文件 —— 走的是「解密-再加密」。

    真值只在本进程内存里过一遍，不打印、不落盘。
    """
    import getpass
    import os

    from haiqbot.kernel.vault import VAULT_FILENAME, FileBackend, VaultError

    target = vdir / VAULT_FILENAME
    if not target.is_file():
        print(f"x 没有找到 {target}")
        return 1
    if cfg.vault.enc == "none":
        print('x 当前 enc="none"（明文存储），没有口令可换')
        print('   先把 haiq.toml 的 [vault] enc 改成 "aead"，用 secrets set 重新录一遍凭据')
        return 1

    ref = cfg.vault.passphrase_ref
    old = os.environ.get(ref) or getpass.getpass("当前口令（不回显）：").strip()
    if not old:
        print("x 空口令")
        return 1
    try:
        current = FileBackend(target, enc=cfg.vault.enc, passphrase=lambda: old)
        values = {k: current.get(k) for k in current.keys()}
    except VaultError as exc:
        print(f"x 旧口令打不开：{exc}")
        return 1
    values = {k: v for k, v in values.items() if v}
    if not values:
        print("x vault 里没有可用条目")
        return 1

    new = getpass.getpass("新口令（不回显，建议 32 位以上随机）：").strip()
    if len(new) < 12:
        print("x 新口令太短（至少 12 位）")
        return 1
    if getpass.getpass("再输一次：").strip() != new:
        print("x 两次输入不一致，已取消")
        return 1
    if new == old:
        print("x 新旧口令相同，没有意义")
        return 1

    staged = vdir / (VAULT_FILENAME + ".new")
    try:
        fresh = FileBackend(staged, enc=cfg.vault.enc, passphrase=lambda: new)
        for key, value in values.items():
            fresh.set(key, value)
        # 回读校验：写坏 / 口令错都在这里暴露，绝不直接覆盖生产文件
        verify = FileBackend(staged, enc=cfg.vault.enc, passphrase=lambda: new)
        probe = sorted(values)[0]
        if verify.get(probe) != values[probe]:
            raise VaultError("回读校验失败")
    except Exception as exc:  # noqa: BLE001
        staged.unlink(missing_ok=True)
        print(f"x 写入新 vault 失败：{exc}")
        return 1

    backup = vdir / (VAULT_FILENAME + ".bak")
    target.replace(backup)
    staged.replace(target)
    print(f"OK 已轮换 {len(values)} 条凭据")
    print(f"    旧文件 → {backup.name}（只有旧口令能解，确认无误后可删）")
    print(f"    接着：把新口令设置到环境变量 {ref}，并删掉明文密码文件")
    return 0


def _secrets_reencrypt(cfg, vdir: Path, force: bool = False) -> int:
    """把旧版 DPAPI 文件迁移到统一的 vault.json（enc 由配置决定）。"""
    import os

    from haiqbot.kernel.vault import LEGACY_FILENAME, VAULT_FILENAME, DpapiFileBackend, FileBackend

    legacy = vdir / LEGACY_FILENAME
    target = vdir / VAULT_FILENAME
    if not legacy.is_file():
        print(f"x 没有找到旧文件 {legacy}（可能已经迁移过，或本来就没配过凭据）")
        return 1
    if target.is_file() and not force:
        print(f"x {target} 已存在；确认要覆盖请加 --force")
        return 1

    try:
        old = DpapiFileBackend(legacy)
        keys = old.keys()
        values = {k: old.get(k) for k in keys}
    except Exception as exc:  # noqa: BLE001
        print(f"x 旧文件读不出来（需要同一台机器、同一个 Windows 账户）：{exc}")
        return 1

    values = {k: v for k, v in values.items() if v}
    if not values:
        print("x 旧文件里没有可用条目")
        return 1

    ref = cfg.vault.passphrase_ref
    if cfg.vault.enc == "aead" and not os.environ.get(ref):
        pw = getpass.getpass("设置 vault 密码（不回显）：").strip()
        pw2 = getpass.getpass("再输一次：").strip()
        if not pw or pw != pw2:
            print("x 两次输入不一致或为空，已取消")
            return 1
        os.environ[ref] = pw

    try:
        backend = FileBackend(target, enc=cfg.vault.enc, passphrase=lambda: os.environ.get(ref) or None)
        for key, value in values.items():
            backend.set(key, value)
    except Exception as exc:  # noqa: BLE001
        print(f"x 写入新文件失败：{exc}")
        return 1

    backup = legacy.parent / (legacy.name + ".bak")
    if not backup.exists():
        legacy.replace(backup)
        print(f"OK 旧文件已备份为 {backup.name}")

    print(f"OK 已迁移 {len(values)} 个凭据 -> {target}（加密方式：{cfg.vault.enc}）")
    for key in sorted(values):
        print(f"   {key}（{len(values[key])} 字符）")
    return 0


@lru_cache(maxsize=1)
def _slot_index() -> dict[str, dict]:
    """{插件.槽位: 信息}，只通过 entry point 发现。"""
    from haiqbot.kernel.plugin import SecretSlot
    from haiqbot.kernel.plugin_sources import iter_plugin_classes

    found: dict[str, dict] = {}
    for cls in iter_plugin_classes():
        pid = getattr(cls, "id", None)
        slots = getattr(cls, "secrets", None) or {}
        if not pid or not isinstance(slots, dict):
            continue
        for name, slot in slots.items():
            if isinstance(slot, SecretSlot):
                found[f"{pid}.{name}"] = {"plugin": pid, "name": name, "slot": slot}
    return found


def _resolve_target(cfg, arg: str) -> tuple[str, str]:
    """`painter.api_key` -> 物理键名；裸键名原样返回。"""
    from haiqbot.kernel.plugin import resolve_secret_ref

    if "." in arg:
        entry = _slot_index().get(arg)
        if entry:
            section = cfg.plugin_config.get(entry["plugin"]) or {}
            return (
                resolve_secret_ref(entry["plugin"], entry["name"], entry["slot"], section),
                arg,
            )
    return arg, arg


def _cmd_secrets(args: argparse.Namespace) -> int:
    """面向人的凭据管理。参数既可以是槽位名（painter.api_key），也可以是裸键名。"""
    import os

    from haiqbot.kernel.vault import VAULT_FILENAME, Vault, VaultError

    cfg = load_config(args.config)
    vdir = Path(cfg.vault.dir or cfg.secrets_dir)
    ref = cfg.vault.passphrase_ref

    if args.action == "reencrypt":
        return _secrets_reencrypt(cfg, vdir, bool(getattr(args, "force", False)))

    if args.action == "passwd":
        return _secrets_passwd(cfg, vdir)

    if cfg.vault.enc == "aead" and not os.environ.get(ref):
        print(f"x vault 已加密但没有密码：请先设置环境变量 {ref}")
        print("   （或先运行 `haiq secrets reencrypt` 完成迁移并设置密码）")
        return 1

    vault = Vault.from_config(cfg)

    if args.action == "list":
        index = _slot_index()
        shown: set[str] = set()
        print(f"凭据存储：{vdir / VAULT_FILENAME}（加密：{vault.enc_kind}）")
        if index:
            print("  槽位                              物理键名 / 来源")
            for slot_key in sorted(index):
                entry = index[slot_key]
                section = cfg.plugin_config.get(entry["plugin"]) or {}
                from haiqbot.kernel.plugin import resolve_secret_ref

                physical = resolve_secret_ref(entry["plugin"], entry["name"], entry["slot"], section)
                shown.add(physical)
                item = vault.resolve(physical)
                if item.present:
                    mark = "v"
                else:
                    mark = "-" if entry["slot"].required else "."
                suffix = "" if entry["slot"].required else "（可选）"
                label = entry["slot"].label
                print(f"  {mark} {slot_key:<30} {label:<18} {physical} <- {item.backend or '未设置'}{suffix}")
        core_targets = [
            (cfg.official.app_secret_ref, "官方 AppSecret"),
            (cfg.onebot.access_token_ref, "OneBot access_token"),
            (cfg.webui.token_ref, "面板写操作令牌"),
        ]
        core_shown = [(ref, label) for ref, label in core_targets if ref]
        if core_shown:
            print("  内核段凭据")
            for core_ref, core_label in core_shown:
                shown.add(core_ref)
                item = vault.resolve(core_ref)
                mark = "v" if item.present else "-"
                print(f"  {mark} {core_label:<30} {'':<18} {core_ref} <- {item.backend or '未设置'}")

        orphans = [k for k in vault.keys() if k not in shown]
        if orphans:
            print("  未被任何槽位引用（孤儿键）：")
            for key in orphans:
                print(f"    - {key}")
        if not index and not orphans:
            print("  （空）")
        return 0

    if args.action == "set" and getattr(args, "missing", False):
        from haiqbot.kernel.plugin import resolve_secret_ref

        index = _slot_index()
        pending = []
        for slot_key in sorted(index):
            entry = index[slot_key]
            section = cfg.plugin_config.get(entry["plugin"]) or {}
            physical = resolve_secret_ref(entry["plugin"], entry["name"], entry["slot"], section)
            if not vault.resolve(physical).present:
                pending.append((slot_key, physical, entry["slot"]))
        if not pending:
            print("OK 所有槽位都已设置")
            return 0
        print(f"有 {len(pending)} 个槽位未设置（直接回车跳过该槽位）：")
        for slot_key, physical, slot in pending:
            hint = "" if slot.required else "（可选）"
            value = getpass.getpass(f"  {slot_key} · {slot.label}{hint}：").strip()
            if not value:
                print("    跳过")
                continue
            backend = vault.set(physical, value)
            print(f"    OK {physical}（{backend}）")
        return 0

    if args.action == "set":
        if not args.key:
            print("x 用法：haiq secrets set <槽位名|键名>，或用 --missing 逐个补")
            return 1
        physical, shown_name = _resolve_target(cfg, args.key)
        if args.stdin:
            value = sys.stdin.readline().strip()
        else:
            value = getpass.getpass(f"请输入 {shown_name} 的值（不回显）：").strip()
        if not value:
            print("x 空值，已取消")
            return 1
        try:
            backend_name = vault.set(physical, value)
        except VaultError as exc:
            print(f"x {exc}")
            return 1
        print(f"OK 已写入 {physical}（后端：{backend_name}）")
        return 0

    if args.action in ("rm", "delete"):
        if not args.key:
            print("x 用法：haiq secrets rm <槽位名|键名>")
            return 1
        physical, _ = _resolve_target(cfg, args.key)
        removed = vault.delete(physical)
        print("OK 已删除" if removed else f"- 未找到 {physical}")
        return 0 if removed else 1

    if args.action == "export":
        print("# 警告：下面是明文凭据。请立即转入密码管理器。", file=sys.stderr)
        try:
            for key in vault.keys():
                value = vault.get(key)
                if value:
                    print(f"{key}={value}")
        except VaultError as exc:
            print(f"x {exc}", file=sys.stderr)
            return 1
        return 0

    print("x 未知操作")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="haiqbot",
        description="haiqbot — QQ Bot 框架（OneBot v11 / 官方 OpenAPI）",
    )
    parser.add_argument("--config", help="配置文件路径（默认 haiq.toml）")
    parser.add_argument("--version", action="store_true", help="显示版本")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="生成配置与 secrets 目录")
    p_init.add_argument("--path", help="配置输出路径")
    p_init.add_argument("--secrets-dir", help="凭据目录")
    p_init.add_argument("--force", action="store_true", help="覆盖已存在的配置")
    p_init.set_defaults(func=_cmd_init)

    p_doc = sub.add_parser("doctor", help="自检")
    p_doc.set_defaults(func=_cmd_doctor)

    p_run = sub.add_parser("run", help="启动")
    p_run.add_argument("--demo", action="store_true", help="本地演示：不连 QQ、不烧额度")
    p_run.set_defaults(func=_cmd_run)

    p_channels = sub.add_parser("channels", help="频道开关（应急入口）")
    p_channels.add_argument("action", choices=["list", "verify", "enable", "disable"])
    p_channels.add_argument("targets", nargs="*", help="频道标识，如 onebot:group:674425800")
    p_channels.add_argument("--plugins", help="逗号分隔：painter,chat:ask（留空 = 全部已装插件）")
    p_channels.set_defaults(func=_cmd_channels)

    p_plugin = sub.add_parser("plugin", help="插件管理")
    p_plugin.add_argument("action", choices=["list", "reload", "enable", "disable"])
    p_plugin.add_argument("id", nargs="?")
    p_plugin.add_argument("--json", action="store_true")
    p_plugin.set_defaults(func=_cmd_plugin)

    p_webui = sub.add_parser("webui", help="面板写操作令牌")
    p_webui.add_argument("action", choices=["token"], nargs="?", default="token")
    p_webui.add_argument("--generate", action="store_true", help="生成新令牌并写入 vault")
    p_webui.set_defaults(func=_cmd_webui)

    p_vault = sub.add_parser("vault", help="凭据管理")
    p_vault.add_argument("action", choices=["set", "list", "rm", "delete"])
    p_vault.add_argument("key", nargs="?")
    p_vault.add_argument("--stdin", action="store_true", help="从 stdin 读取（CI 用）")
    p_vault.set_defaults(func=_cmd_vault)

    p_secrets = sub.add_parser("secrets", help="凭据管理（list/set/rm/export/reencrypt/passwd）")
    p_secrets.add_argument(
        "action",
        choices=["list", "set", "rm", "delete", "export", "reencrypt", "passwd"],
    )
    p_secrets.add_argument("key", nargs="?")
    p_secrets.add_argument("--stdin", action="store_true", help="从 stdin 读取（CI 用）")
    p_secrets.add_argument("--missing", action="store_true", help="只逐个提示尚未设置的槽位（配合 set）")
    p_secrets.add_argument("--force", action="store_true", help="迁移时覆盖已有 vault.json")
    p_secrets.set_defaults(func=_cmd_secrets)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Windows 控制台默认 GBK，遇到 emoji 会直接崩；统一用 UTF-8 兜底
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    if getattr(args, "version", False):
        from haiqbot import __version__

        print(f"haiqbot {__version__}")
        return 0

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        # 配置坏了只该看到一句人话，不该是 traceback
        print(f"× 配置有问题：{exc}")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
