"""凭据库（vault）。



设计约束：

- 配置文件里只有键名（`*_ref`），真值全在这里；

- `get()` 是唯一出口，取出时自动登记到日志脱敏名单；

- 后端优先级：环境变量 → DPAPI 加密文件 → keyring → 明文文件（仅最后手段，会大声告警）。

"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .log import get_logger, register_secret

log = get_logger(__name__)


# 统一文件格式（所有平台一致）。旧版 DPAPI 文件名为 vault.bin，仅迁移时读。

VAULT_FILENAME = "vault.json"

LEGACY_FILENAME = "vault.bin"


# scrypt 参数：64 MiB / r=8 / p=1，单次解密约 50~120ms，对启动与写入都可接受。

SCRYPT_N = 1 << 15

SCRYPT_R = 8

SCRYPT_P = 1

SCRYPT_DKLEN = 32

# OpenSSL 默认 maxmem 是 32 MiB，n=2^15/r=8 需要 64 MiB —— 必须显式放开。

SCRYPT_MAXMEM = 256 * 1024 * 1024


# AEAD 附加数据：当前值与历史值。老 vault 是用历史值加密的，必须继续认。
_AAD = b"haiqbot.vault.v2"
_AAD_LEGACY = (b"weft.vault.v2",)


class VaultError(RuntimeError):
    """凭据不可用。"""


@runtime_checkable
class Backend(Protocol):
    name: str

    writable: bool

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> bool: ...

    def keys(self) -> list[str]: ...


class EnvBackend:
    """环境变量后端。只读，用于容器化部署。"""

    name = "env"

    writable = False

    def get(self, key: str) -> str | None:

        return os.environ.get(key) or None

    def set(self, key: str, value: str) -> None:

        raise VaultError("env 后端只读；请用 `haiq vault set` 写入加密库，或直接设置环境变量")

    def delete(self, key: str) -> bool:

        raise VaultError("env 后端只读")

    def keys(self) -> list[str]:

        return []


# --------------------------------------------------------------------------- #

# DPAPI（Windows CryptProtectData，绑定当前用户，无需额外依赖）

# --------------------------------------------------------------------------- #


def _dpapi_available() -> bool:

    return sys.platform == "win32"


if _dpapi_available():  # pragma: no cover - 平台分支
    import ctypes
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def _to_blob(data: bytes) -> tuple[_BLOB, Any]:

        buf = ctypes.create_string_buffer(data, len(data))

        return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _from_blob(blob: _BLOB) -> bytes:

        try:
            return ctypes.string_at(blob.pbData, blob.cbData)

        finally:
            ctypes.windll.kernel32.LocalFree(blob.pbData)

    def dpapi_encrypt(data: bytes) -> bytes:

        blob_in, _keep = _to_blob(data)

        blob_out = _BLOB()

        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(blob_out),
        )

        if not ok:
            raise VaultError(f"DPAPI 加密失败（GetLastError={ctypes.GetLastError()}）")

        return _from_blob(blob_out)

    def dpapi_decrypt(data: bytes) -> bytes:

        blob_in, _keep = _to_blob(data)

        blob_out = _BLOB()

        description = wintypes.LPWSTR()

        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            ctypes.byref(description),
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(blob_out),
        )

        if description:
            ctypes.windll.kernel32.LocalFree(description)

        if not ok:
            raise VaultError(f"DPAPI 解密失败（GetLastError={ctypes.GetLastError()}）")

        return _from_blob(blob_out)


else:  # pragma: no cover - 平台分支

    def dpapi_encrypt(data: bytes) -> bytes:

        raise VaultError("当前平台不支持 DPAPI")

    def dpapi_decrypt(data: bytes) -> bytes:

        raise VaultError("当前平台不支持 DPAPI")


class DpapiFileBackend:
    """`secrets/vault.bin`：DPAPI 加密的 JSON 字典。"""

    name = "dpapi"

    writable = True

    def __init__(self, path: Path) -> None:

        self.path = path

        self._cache: dict[str, str] | None = None

        self._stamp: int | None = None

    def _disk_stamp(self) -> int | None:

        try:
            return self.path.stat().st_mtime_ns

        except OSError:
            return None

    def _load(self) -> dict[str, str]:

        # 文件被别的进程改过（`haiq vault set` / `haiq webui token --generate`）就重读。

        # 不这么做的话，运行中的实例会一直用启动时那份缓存：凭据改了却不生效，

        # 面板还会莫名其妙地 401。

        stamp = self._disk_stamp()

        if self._cache is not None and stamp == self._stamp:
            return self._cache

        if stamp is None:
            self._cache = {}

            self._stamp = None

            return self._cache

        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))

            payload = dpapi_decrypt(base64.b64decode(envelope["data"]))

            self._cache = json.loads(payload.decode("utf-8"))

        except VaultError:
            raise

        except Exception as exc:  # noqa: BLE001
            raise VaultError(
                f"vault 文件损坏或不属于当前用户：{self.path.name}（{type(exc).__name__}）"
            ) from exc

        self._stamp = stamp

        return self._cache

    def _save(self, data: dict[str, str]) -> None:

        self.path.parent.mkdir(parents=True, exist_ok=True)

        blob = dpapi_encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8"))

        envelope = {"v": 1, "backend": "dpapi", "data": base64.b64encode(blob).decode("ascii")}

        tmp = self.path.with_suffix(".tmp")

        tmp.write_text(json.dumps(envelope), encoding="utf-8")

        try:
            tmp.chmod(0o600)

        except OSError:
            pass

        tmp.replace(self.path)

        self._cache = data

        self._stamp = self._disk_stamp()

    def get(self, key: str) -> str | None:

        return self._load().get(key)

    def set(self, key: str, value: str) -> None:

        data = dict(self._load())

        data[key] = value

        self._save(data)

    def delete(self, key: str) -> bool:

        data = dict(self._load())

        if key not in data:
            return False

        del data[key]

        self._save(data)

        return True

    def keys(self) -> list[str]:

        return sorted(self._load().keys())


class PlainFileBackend:
    """非 Windows 的退路：明文 JSON + 0600，启动时大声告警。"""

    name = "file"

    writable = True

    def __init__(self, path: Path) -> None:

        self.path = path

    def _load(self) -> dict[str, str]:

        if not self.path.is_file():
            return {}

        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: dict[str, str]) -> None:

        self.path.parent.mkdir(parents=True, exist_ok=True)

        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        try:
            self.path.chmod(0o600)

        except OSError:
            pass

    def get(self, key: str) -> str | None:

        return self._load().get(key)

    def set(self, key: str, value: str) -> None:

        data = self._load()

        data[key] = value

        self._save(data)

    def delete(self, key: str) -> bool:

        data = self._load()

        if key not in data:
            return False

        del data[key]

        self._save(data)

        return True

    def keys(self) -> list[str]:

        return sorted(self._load().keys())


@dataclass(frozen=True)
class Resolved:
    """一次解析的结果。**刻意不带值** —— 面板与 doctor 只该看到这些。"""

    key: str

    backend: str | None

    present: bool


class FileBackend:
    """统一文件后端：JSON 信封 + `enc` 决定内层加密。



    - `enc="aead"`：scrypt(密码) -> AES-256-GCM。跨平台、可迁移到任意机器。

    - `enc="none"`：明文 base64。只在显式降级时使用，必须由调用方告警。



    所有平台走同一份代码 —— 这里没有、也不该有 `if win32` 之类的分支。

    """

    name = "file"

    writable = True

    def __init__(self, path: str | Path, *, enc: str = "aead", passphrase: Any = None) -> None:

        if enc not in ("aead", "none"):
            raise VaultError(f"未知的 vault 加密方式：{enc}（只支持 aead / none）")

        self.path = Path(path)

        self.enc = enc

        # 密码来源是可调用对象：每次解密都现取，改环境变量后无需重启。

        self._passphrase = passphrase or (lambda: None)

        self._cache: dict[str, str] | None = None

        self._stamp: int | None = None

    # -- 内部 ---------------------------------------------------------- #

    def _disk_stamp(self) -> int | None:

        try:
            return self.path.stat().st_mtime_ns

        except OSError:
            return None

    def _derive(self, salt: bytes, kdf: dict[str, Any]) -> bytes:

        passphrase = self._passphrase()

        if not passphrase:
            raise VaultError("vault 已加密但未提供密码：请设置环境变量 HAIQ_VAULT_PASSPHRASE")

        return hashlib.scrypt(
            str(passphrase).encode("utf-8"),
            salt=salt,
            n=int(kdf.get("n", SCRYPT_N)),
            r=int(kdf.get("r", SCRYPT_R)),
            p=int(kdf.get("p", SCRYPT_P)),
            dklen=SCRYPT_DKLEN,
            maxmem=SCRYPT_MAXMEM,
        )

    def _decrypt(self, envelope: dict[str, Any]) -> dict[str, str]:

        if int(envelope.get("v") or 0) < 2:
            raise VaultError("这是旧版 DPAPI 凭据文件，请执行 `haiq secrets reencrypt` 迁移到新格式")

        enc = envelope.get("enc")

        raw = base64.b64decode(envelope.get("data") or "")

        if enc == "none":
            return json.loads(raw.decode("utf-8"))

        if enc != "aead":
            raise VaultError(f"未知的 vault 加密方式：{enc}")

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        kdf = envelope.get("kdf") or {}

        key = self._derive(base64.b64decode(kdf.get("salt") or ""), kdf)

        nonce = base64.b64decode(envelope.get("nonce") or "")

        # AAD 是**已落盘数据的契约**，不随产品改名。改名前的库用旧 AAD 加密，
        # 所以解密按「新 → 旧」顺序试；下次写入自动升级成新 AAD（惰性迁移）。
        aead = AESGCM(key)
        plain: bytes | None = None
        for aad in (_AAD,) + _AAD_LEGACY:
            try:
                plain = aead.decrypt(nonce, raw, aad)
                break
            except Exception:  # noqa: BLE001 - InvalidTag 说明这条 AAD 不对，换下一条
                continue
        if plain is None:
            raise VaultError("vault 文件无法解密（口令不对，或文件被损坏/篡改）")

        return json.loads(plain.decode("utf-8"))

    def _encrypt(self, data: dict[str, str]) -> dict[str, Any]:

        plain = json.dumps(data, ensure_ascii=False).encode("utf-8")

        if self.enc == "none":
            return {"v": 2, "enc": "none", "data": base64.b64encode(plain).decode("ascii")}

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        salt = os.urandom(16)

        kdf = {
            "algo": "scrypt",
            "salt": base64.b64encode(salt).decode("ascii"),
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
        }

        key = self._derive(salt, kdf)

        nonce = os.urandom(12)

        blob = AESGCM(key).encrypt(nonce, plain, _AAD)

        return {
            "v": 2,
            "enc": "aead",
            "kdf": kdf,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "data": base64.b64encode(blob).decode("ascii"),
        }

    def _load(self) -> dict[str, str]:

        stamp = self._disk_stamp()

        if self._cache is not None and stamp == self._stamp:
            return self._cache

        if stamp is None:
            self._cache = {}

            self._stamp = None

            return self._cache

        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))

            data = self._decrypt(envelope)

            if not isinstance(data, dict):
                raise ValueError("vault 内容不是 JSON 对象")

        except VaultError:
            raise

        except Exception as exc:  # noqa: BLE001
            raise VaultError(f"vault 文件无法解析：{self.path.name}（{type(exc).__name__}）") from exc

        self._cache = {str(k): str(v) for k, v in data.items()}

        self._stamp = stamp

        return self._cache

    def _save(self, data: dict[str, str]) -> None:

        cleaned = {str(k): str(v) for k, v in data.items()}

        envelope = self._encrypt(cleaned)

        self.path.parent.mkdir(parents=True, exist_ok=True)

        tmp = self.path.with_suffix(".tmp")

        tmp.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

        try:
            tmp.chmod(0o600)

        except OSError:
            pass

        tmp.replace(self.path)

        self._cache = cleaned

        self._stamp = self._disk_stamp()

    # -- 协议 ---------------------------------------------------------- #

    def get(self, key: str) -> str | None:

        return self._load().get(key)

    def set(self, key: str, value: str) -> None:

        data = dict(self._load())

        data[key] = value

        self._save(data)

    def delete(self, key: str) -> bool:

        data = dict(self._load())

        if key not in data:
            return False

        del data[key]

        self._save(data)

        return True

    def keys(self) -> list[str]:

        return sorted(self._load().keys())


class KeyringBackend:
    """系统钥匙串（可选依赖）。"""

    name = "keyring"

    writable = True

    def __init__(self, service: str = "haiqbot") -> None:

        self.service = service

        try:
            import keyring  # noqa: F401

            self._kr = keyring

            self.available = True

        except Exception:  # noqa: BLE001
            self._kr = None

            self.available = False

    def get(self, key: str) -> str | None:

        if not self.available:
            return None

        try:
            return self._kr.get_password(self.service, key)

        except Exception:  # noqa: BLE001
            return None

    def set(self, key: str, value: str) -> None:

        if not self.available:
            raise VaultError("keyring 未安装")

        self._kr.set_password(self.service, key, value)

    def delete(self, key: str) -> bool:

        if not self.available:
            return False

        try:
            self._kr.delete_password(self.service, key)

            return True

        except Exception:  # noqa: BLE001
            return False

    def keys(self) -> list[str]:

        return []


class CmdBackend:
    """外部命令后端：`cmd <key>` 输出到 stdout 即凭据值。"""

    name = "cmd"

    writable = False

    def __init__(self, command: list[str] | None = None) -> None:

        self.command = command or []

    def get(self, key: str) -> str | None:

        if not self.command:
            return None

        try:
            out = subprocess.run(  # noqa: S603
                [*self.command, key], capture_output=True, text=True, timeout=10, check=False
            )

        except Exception:  # noqa: BLE001
            return None

        value = out.stdout.strip()

        return value or None

    def set(self, key: str, value: str) -> None:

        raise VaultError("cmd 后端只读")

    def delete(self, key: str) -> bool:

        raise VaultError("cmd 后端只读")

    def keys(self) -> list[str]:

        return []


class Vault:
    """按优先级串联多个后端。"""

    def __init__(self, backends: list[Backend] | None = None) -> None:

        self.backends: list[Backend] = backends or [EnvBackend()]

    @classmethod
    def default(cls, secrets_dir: str | Path = "secrets") -> Vault:
        """兼容层：统一链，不区分平台。



        新代码请用 `Vault.from_config(cfg)`（会读 `[vault]` 配置）。

        """

        ref = "HAIQ_VAULT_PASSPHRASE"

        return cls(
            [
                EnvBackend(),
                FileBackend(
                    Path(secrets_dir) / VAULT_FILENAME,
                    enc="aead",
                    passphrase=lambda: os.environ.get(ref) or None,
                ),
            ]
        )

    @classmethod
    def from_config(cls, cfg: Any) -> Vault:
        """按 `[vault]` 配置装配 —— 所有平台完全一致，没有平台分支。"""

        vcfg = getattr(cfg, "vault", None)

        secrets_dir = Path(getattr(vcfg, "dir", None) or getattr(cfg, "secrets_dir", "secrets"))

        enc = str(getattr(vcfg, "enc", "aead") or "aead")

        ref = str(getattr(vcfg, "passphrase_ref", "HAIQ_VAULT_PASSPHRASE"))

        return cls(
            [
                EnvBackend(),
                FileBackend(
                    secrets_dir / VAULT_FILENAME,
                    enc=enc,
                    passphrase=lambda: os.environ.get(ref) or None,
                ),
            ]
        )

    # -- 诊断 ------------------------------------------------------------- #

    @property
    def enc_kind(self) -> str:

        for backend in self.backends:
            enc = getattr(backend, "enc", None)

            if enc:
                return str(enc)

        return "n/a"

    def resolve(self, key: str) -> Resolved:
        """只回答「有没有 / 从哪来」，绝不返回真值。"""

        value, backend = self._lookup(key)

        return Resolved(key=key, backend=backend, present=value is not None)

    # -- 读取 ------------------------------------------------------------- #

    def _lookup(self, key: str) -> tuple[str | None, str | None]:

        for backend in self.backends:
            try:
                value = backend.get(key)

            except VaultError:
                raise

            except Exception as exc:  # noqa: BLE001
                log.warning("vault_backend_error", backend=backend.name, error=type(exc).__name__)

                continue

            if value:
                register_secret(value)

                return value, backend.name

        return None, None

    def get(self, key: str, default: str | None = None) -> str | None:

        value, _ = self._lookup(key)

        return value if value else default

    def require(self, key: str) -> str:

        value = self.get(key)

        if not value:
            raise VaultError(f"缺少凭据 {key}。请执行 `haiq vault set {key}` 或设置同名环境变量。")

        return value

    # -- 写入 ------------------------------------------------------------- #

    def _writable(self, wanted: str | None = None) -> Backend:

        for backend in self.backends:
            if not backend.writable:
                continue

            if wanted and backend.name != wanted:
                continue

            return backend

        raise VaultError("没有可写的凭据后端")

    def set(self, key: str, value: str, backend: str | None = None) -> str:

        target = self._writable(backend)

        target.set(key, value)

        register_secret(value)

        log.info("vault_set", key=key, backend=target.name)

        return target.name

    def delete(self, key: str) -> bool:

        removed = False

        for backend in self.backends:
            if not backend.writable:
                continue

            try:
                removed = backend.delete(key) or removed

            except VaultError:
                continue

            except Exception:  # noqa: BLE001
                continue

        return removed

    def keys(self) -> list[str]:

        seen: set[str] = set()

        for backend in self.backends:
            try:
                seen.update(backend.keys())

            except Exception:  # noqa: BLE001
                continue

        return sorted(seen)

    @property
    def primary_backend(self) -> str:

        return self._writable().name
