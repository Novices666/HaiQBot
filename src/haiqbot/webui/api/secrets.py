"""凭据管理：在面板里读写 vault（密码管理器式）。

安全边界（动这个文件之前先读一遍）：
1. **列表接口不返回值**，只给「已设置 / 未设置」。要看某一条的值必须单独请求，
   且每次都会写一条 `webui:secret.reveal` 审计 —— 谁在什么时候看过密钥是可见的。
2. **审计只记哪个键被动过**，`detail` 里绝不允许出现值本体（`test_secrets_api` 盯着）。
3. 写入只进 vault，**绝不回写 haiq.toml** —— 配置里永远只有 `*_ref`。
4. 轮换主口令不会自动更新当前进程里的派生密钥，所以返回 `restart_required`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from haiqbot.kernel.secret_index import resolve_target, slot_index
from haiqbot.webui.api.common import Ctx, audit, fail, ok, read_json

MIN_PASSPHRASE = 12


def _vault(runtime: Any) -> Any:
    vault = getattr(runtime, "vault", None)
    if vault is None:
        raise RuntimeError("运行时没有 vault")
    return vault


def routes(ctx: Ctx) -> list[Any]:
    from starlette.routing import Route

    runtime = ctx.runtime
    cfg = runtime.config

    def _vault_path() -> Path:
        from haiqbot.kernel.vault import VAULT_FILENAME

        vdir = Path(getattr(cfg.vault, "dir", None) or cfg.secrets_dir)
        return vdir / VAULT_FILENAME

    def _entry(
        key: str, physical: str, vault: Any, *, plugin: str | None, label: str, required: bool
    ) -> dict[str, Any]:
        try:
            present = vault.get(physical) is not None
        except Exception:  # noqa: BLE001 - vault 打不开不该让整页 500
            present = False
        return {
            "key": key,
            "ref": physical,
            "plugin": plugin,
            "label": label,
            "required": required,
            "set": present,
        }

    async def list_all(_request: Any) -> Any:
        """槽位清单 + vault 里那些没被任何插件声明的键（如面板令牌）。"""
        vault = _vault(runtime)
        rows: list[dict[str, Any]] = []
        declared: set[str] = set()

        for key, entry in sorted(slot_index(cfg).items()):
            physical, _ = resolve_target(cfg, key)
            declared.add(physical)
            slot = entry["slot"]
            rows.append(
                _entry(
                    key,
                    physical,
                    vault,
                    plugin=entry["plugin"],
                    label=getattr(slot, "label", None) or entry["name"],
                    required=bool(getattr(slot, "required", True)),
                )
            )

        try:
            leftovers = [k for k in vault.keys() if k not in declared]
        except Exception:  # noqa: BLE001
            leftovers = []
        for key in leftovers:
            rows.append(_entry(key, key, vault, plugin=None, label=key, required=False))

        info = {
            "path": str(_vault_path()),
            "enc": str(getattr(vault, "enc_kind", "") or ""),
            "backend": str(getattr(vault, "primary_backend", "") or ""),
        }
        return ok({"items": rows, "vault": info})

    async def one(request: Any) -> Any:
        """单条取值 —— 这是唯一会吐明文值的接口。"""
        key = str(request.path_params.get("key") or "").strip()
        if not key:
            return fail(400, "bad_request", "缺少键名")
        physical, shown = resolve_target(cfg, key)
        vault = _vault(runtime)
        try:
            value = vault.get(physical)
        except Exception as exc:  # noqa: BLE001
            return fail(500, "vault_error", f"读取失败：{type(exc).__name__}: {exc}")
        if value is None:
            return ok({"key": shown, "ref": physical, "set": False, "value": None})
        await audit(ctx, "secret.reveal", {"key": shown, "ref": physical})
        return ok({"key": shown, "ref": physical, "set": True, "value": value})

    async def upsert(request: Any) -> Any:
        try:
            body = await read_json(request)
        except ValueError as exc:
            return fail(400, "bad_request", str(exc))
        key = str(body.get("key") or "").strip()
        value = body.get("value")
        if not key:
            return fail(400, "bad_request", "缺少 key（槽位名如 painter.api_key，或 vault 键名）")
        if not isinstance(value, str) or not value.strip():
            return fail(400, "bad_request", "value 必须是非空字符串")

        physical, shown = resolve_target(cfg, key)
        if not physical:
            return fail(400, "bad_request", "无法解析出 vault 键名")

        vault = _vault(runtime)
        try:
            backend = vault.set(physical, value)
        except Exception as exc:  # noqa: BLE001
            return fail(500, "vault_error", f"写入失败：{type(exc).__name__}: {exc}")
        # 只记「哪个键」和落到哪个后端，不记值
        await audit(ctx, "secret.set", {"key": shown, "ref": physical, "backend": backend})
        return ok({"key": shown, "ref": physical, "set": True, "backend": backend})

    async def remove(request: Any) -> Any:
        key = str(request.path_params.get("key") or "").strip()
        if not key:
            return fail(400, "bad_request", "缺少键名")
        physical, shown = resolve_target(cfg, key)
        vault = _vault(runtime)
        try:
            removed = vault.delete(physical)
        except Exception as exc:  # noqa: BLE001
            return fail(500, "vault_error", f"删除失败：{type(exc).__name__}: {exc}")
        await audit(ctx, "secret.delete", {"key": shown, "ref": physical, "removed": removed})
        return ok({"removed": removed})

    async def passwd(request: Any) -> Any:
        """轮换主口令：旧口令解开 → 新口令重写（与 CLI 的 `secrets passwd` 同语义）。"""
        from haiqbot.kernel.vault import FileBackend, VaultError

        try:
            body = await read_json(request)
        except ValueError as exc:
            return fail(400, "bad_request", str(exc))
        old = str(body.get("old") or "")
        new = str(body.get("new") or "")
        if not old:
            return fail(400, "bad_request", "缺少当前口令")
        if len(new) < MIN_PASSPHRASE:
            return fail(400, "bad_request", f"新口令至少 {MIN_PASSPHRASE} 位")
        if new == old:
            return fail(400, "bad_request", "新旧口令相同，没有意义")

        enc = str(getattr(cfg.vault, "enc", "aead") or "aead")
        if enc == "none":
            return fail(
                400,
                "plaintext_vault",
                '当前 enc="none"（明文存储），没有口令可换',
                hint='先把 haiq.toml 的 [vault] enc 改成 "aead"，再重新录入凭据',
            )

        target = _vault_path()
        if not target.is_file():
            return fail(404, "vault_missing", f"找不到 {target}")

        try:
            current = FileBackend(target, enc=enc, passphrase=lambda: old)
            values = {k: current.get(k) for k in current.keys()}
        except VaultError as exc:
            return fail(403, "bad_passphrase", f"旧口令打不开：{exc}")

        cleaned = {k: v for k, v in values.items() if v}
        if not cleaned:
            return fail(400, "empty_vault", "vault 里没有可用条目")

        staged = target.with_name(target.name + ".new")
        try:
            fresh = FileBackend(staged, enc=enc, passphrase=lambda: new)
            for name, value in cleaned.items():
                fresh.set(name, value)
            staged.replace(target)
        except Exception as exc:  # noqa: BLE001
            staged.unlink(missing_ok=True)
            return fail(500, "vault_error", f"重写失败：{type(exc).__name__}: {exc}")

        await audit(ctx, "secret.passwd", {"entries": len(cleaned)})
        return ok(
            {
                "entries": len(cleaned),
                "restart_required": True,
                "hint": "把新的 VAULT 口令写进环境变量（或 secrets/.vault-pass.txt）后重启服务，"
                "否则当前进程仍用旧口令派生密钥，读取会失败",
            }
        )

    return [
        Route("/api/secrets", list_all, methods=["GET"]),
        Route("/api/secrets", upsert, methods=["POST"]),
        Route("/api/secrets/passwd", passwd, methods=["POST"]),
        Route("/api/secrets/{key:path}", one, methods=["GET"]),
        Route("/api/secrets/{key:path}", remove, methods=["DELETE"]),
    ]
