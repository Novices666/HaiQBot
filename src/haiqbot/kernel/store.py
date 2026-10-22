"""持久化存储（SQLite / aiosqlite）。

四张业务表 + 一张媒体表：
- kv          插件私有键值
- quota       按主体按天的配额计数
- dedupe      msg_id 幂等去重（带过期）
- media       图片中转元数据（id → 文件路径）
- audit       审计流水（谁在哪个频道触发了什么）
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    plugin_id TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT NOT NULL,
    PRIMARY KEY (plugin_id, key)
);

CREATE TABLE IF NOT EXISTS quota (
    subject TEXT NOT NULL,
    day     TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (subject, day)
);

CREATE TABLE IF NOT EXISTS dedupe (
    msg_id     TEXT PRIMARY KEY,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dedupe_expires ON dedupe(expires_at);

CREATE TABLE IF NOT EXISTS media (
    id         TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    mime       TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    channel    TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_expires ON media(expires_at);

CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    trace_id  TEXT,
    plugin_id TEXT,
    action    TEXT,
    channel   TEXT,
    actor     TEXT,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);

-- 能力调用流水：每次生图 / 对话记一条，用于调优
CREATE TABLE IF NOT EXISTS invocation (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    trace_id    TEXT,
    plugin_id   TEXT,
    capability  TEXT NOT NULL,
    provider    TEXT,
    model       TEXT,
    request     TEXT,
    detail      TEXT,
    duration_ms INTEGER,
    status      TEXT,
    error       TEXT,
    channel     TEXT,
    actor       TEXT,
    media_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_invocation_ts ON invocation(ts);
CREATE INDEX IF NOT EXISTS idx_invocation_plugin ON invocation(plugin_id, ts);

-- 群维度配置：某一群对全局默认值的覆盖
CREATE TABLE IF NOT EXISTS channel_config (
    channel      TEXT PRIMARY KEY,
    enabled      INTEGER,
    quota_user   INTEGER,
    quota_chan   INTEGER,
    plugins      TEXT,
    extra_admins TEXT,
    note         TEXT,
    updated_at   REAL NOT NULL
);

"""


class Store:
    """单一 SQLite 连接的薄封装。所有方法都是 async。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> Store:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        await self._migrate()
        return self

    async def _migrate(self) -> None:
        """老库补列。`CREATE TABLE IF NOT EXISTS` 不会给已存在的表加字段。"""
        async with self.db.execute("PRAGMA table_info(media)") as cur:
            columns = {row["name"] for row in await cur.fetchall()}
        if "plugin_id" not in columns:
            await self.db.execute("ALTER TABLE media ADD COLUMN plugin_id TEXT")
            await self.db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> Store:
        return await self.open()

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store 未打开；请先 await store.open()")
        return self._db

    # -- kv --------------------------------------------------------------- #
    async def kv_get(self, plugin_id: str, key: str) -> str | None:
        async with self.db.execute(
            "SELECT value FROM kv WHERE plugin_id=? AND key=?", (plugin_id, key)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def kv_set(self, plugin_id: str, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO kv(plugin_id,key,value) VALUES(?,?,?) "
            "ON CONFLICT(plugin_id,key) DO UPDATE SET value=excluded.value",
            (plugin_id, key, value),
        )
        await self.db.commit()

    async def kv_all(self, plugin_id: str) -> dict[str, str]:
        async with self.db.execute("SELECT key,value FROM kv WHERE plugin_id=?", (plugin_id,)) as cur:
            rows = await cur.fetchall()
        return {r["key"]: r["value"] for r in rows}

    async def kv_delete(self, plugin_id: str, key: str) -> None:
        await self.db.execute("DELETE FROM kv WHERE plugin_id=? AND key=?", (plugin_id, key))
        await self.db.commit()

    # -- quota ------------------------------------------------------------ #
    async def quota_incr(self, subject: str, day: str, amount: int = 1) -> int:
        await self.db.execute(
            "INSERT INTO quota(subject,day,count) VALUES(?,?,?) "
            "ON CONFLICT(subject,day) DO UPDATE SET count=count+excluded.count",
            (subject, day, amount),
        )
        await self.db.commit()
        return await self.quota_get(subject, day)

    async def quota_get(self, subject: str, day: str) -> int:
        async with self.db.execute(
            "SELECT count FROM quota WHERE subject=? AND day=?", (subject, day)
        ) as cur:
            row = await cur.fetchone()
        return int(row["count"]) if row else 0

    # -- dedupe ----------------------------------------------------------- #
    async def mark_seen(self, msg_id: str, ttl: float = 600.0) -> bool:
        """首次见到返回 True 并登记；已存在返回 False。"""
        now = time.time()
        try:
            await self.db.execute("INSERT INTO dedupe(msg_id,expires_at) VALUES(?,?)", (msg_id, now + ttl))
        except aiosqlite.IntegrityError:
            return False
        await self.db.commit()
        return True

    async def gc_dedupe(self) -> int:
        cur = await self.db.execute("DELETE FROM dedupe WHERE expires_at < ?", (time.time(),))
        await self.db.commit()
        return cur.rowcount or 0

    # -- media ------------------------------------------------------------ #
    async def media_add(
        self,
        media_id: str,
        path: str,
        mime: str,
        ttl: float,
        channel: str | None = None,
        plugin_id: str | None = None,
    ) -> None:
        now = time.time()
        await self.db.execute(
            "INSERT OR REPLACE INTO media(id,path,mime,created_at,expires_at,channel,plugin_id) "
            "VALUES(?,?,?,?,?,?,?)",
            (media_id, path, mime, now, now + ttl, channel, plugin_id),
        )
        await self.db.commit()

    async def media_get(self, media_id: str) -> dict[str, Any] | None:
        async with self.db.execute("SELECT * FROM media WHERE id=?", (media_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def media_expired(self) -> list[dict[str, Any]]:
        async with self.db.execute("SELECT * FROM media WHERE expires_at < ?", (time.time(),)) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def media_delete(self, media_id: str) -> None:
        await self.db.execute("DELETE FROM media WHERE id=?", (media_id,))
        await self.db.commit()

    async def media_count(self) -> int:
        async with self.db.execute("SELECT COUNT(*) AS n FROM media") as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # -- audit ------------------------------------------------------------ #
    async def audit(
        self,
        *,
        trace_id: str | None,
        plugin_id: str | None,
        action: str,
        channel: str | None,
        actor: str | None,
        detail: str | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO audit(ts,trace_id,plugin_id,action,channel,actor,detail) VALUES(?,?,?,?,?,?,?)",
            (time.time(), trace_id, plugin_id, action, channel, actor, detail),
        )
        await self.db.commit()

    async def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- invocation（能力调用流水） ----------------------------------------- #
    async def invocation_add(
        self,
        *,
        capability: str,
        plugin_id: str | None = None,
        trace_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        request: str | None = None,
        detail: str | None = None,
        duration_ms: int | None = None,
        status: str = "ok",
        error: str | None = None,
        channel: str | None = None,
        actor: str | None = None,
        media_id: str | None = None,
    ) -> int:
        cur = await self.db.execute(
            "INSERT INTO invocation(ts,trace_id,plugin_id,capability,provider,model,request,"
            "detail,duration_ms,status,error,channel,actor,media_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                trace_id,
                plugin_id,
                capability,
                provider,
                model,
                request,
                detail,
                duration_ms,
                status,
                error,
                channel,
                actor,
                media_id,
            ),
        )
        await self.db.commit()
        return int(cur.lastrowid or 0)

    async def invocation_recent(
        self,
        limit: int = 100,
        *,
        plugin_id: str | None = None,
        capability: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM invocation"
        clauses: list[str] = []
        args: list[Any] = []
        if plugin_id:
            clauses.append("plugin_id=?")
            args.append(plugin_id)
        if capability:
            clauses.append("capability=?")
            args.append(capability)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        async with self.db.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def invocation_delete(
        self,
        *,
        ids: list[int] | None = None,
        before: float | None = None,
        plugin_id: str | None = None,
        capability: str | None = None,
    ) -> int:
        """删除流水。给了 ids 就按 id 删；否则必须至少带一个过滤条件。"""
        if ids:
            placeholders = ",".join("?" * len(ids))
            cur = await self.db.execute(f"DELETE FROM invocation WHERE id IN ({placeholders})", list(ids))
            await self.db.commit()
            return cur.rowcount or 0

        clauses: list[str] = []
        args: list[Any] = []
        if before is not None:
            clauses.append("ts < ?")
            args.append(before)
        if plugin_id:
            clauses.append("plugin_id=?")
            args.append(plugin_id)
        if capability:
            clauses.append("capability=?")
            args.append(capability)
        if not clauses:
            raise ValueError("拒绝无条件的全表删除；请给出时间窗或插件过滤")
        cur = await self.db.execute("DELETE FROM invocation WHERE " + " AND ".join(clauses), args)
        await self.db.commit()
        return cur.rowcount or 0

    async def invocation_stats(self, *, hours: float = 24) -> list[dict[str, Any]]:
        since = time.time() - hours * 3600
        async with self.db.execute(
            "SELECT capability, status, COUNT(*) AS n, AVG(duration_ms) AS avg_ms, "
            "MAX(duration_ms) AS max_ms FROM invocation WHERE ts >= ? "
            "GROUP BY capability, status",
            (since,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # -- channel_config（群维度配置） --------------------------------------- #
    _CHANNEL_FIELDS = ("enabled", "quota_user", "quota_chan", "plugins", "extra_admins", "note")
    # 这两列在库里是 JSON 字符串，对外一律是 list —— 调用方不该关心存储格式
    _CHANNEL_JSON = ("plugins", "extra_admins")

    @classmethod
    def _decode_channel(cls, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for key in cls._CHANNEL_JSON:
            raw = out.get(key)
            if isinstance(raw, str):
                try:
                    out[key] = json.loads(raw) if raw else []
                except json.JSONDecodeError:
                    out[key] = []
        return out

    @classmethod
    def _encode_channel(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple, set)):
            return json.dumps(list(value), ensure_ascii=False)
        if isinstance(value, bool):
            return int(value)
        return value

    async def channel_get(self, channel: str) -> dict[str, Any] | None:
        async with self.db.execute("SELECT * FROM channel_config WHERE channel=?", (channel,)) as cur:
            row = await cur.fetchone()
        return self._decode_channel(dict(row)) if row else None

    async def channel_all(self) -> list[dict[str, Any]]:
        async with self.db.execute("SELECT * FROM channel_config ORDER BY updated_at DESC") as cur:
            return [self._decode_channel(dict(r)) for r in await cur.fetchall()]

    async def channel_upsert(self, channel: str, **fields: Any) -> None:
        unknown = set(fields) - set(self._CHANNEL_FIELDS)
        if unknown:
            raise ValueError(f"未知的群配置字段：{sorted(unknown)}")
        current = await self.channel_get(channel) or {}
        merged = {k: fields.get(k, current.get(k)) for k in self._CHANNEL_FIELDS}
        await self.db.execute(
            "INSERT INTO channel_config(channel,enabled,quota_user,quota_chan,plugins,"
            "extra_admins,note,updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(channel) DO UPDATE SET enabled=excluded.enabled, "
            "quota_user=excluded.quota_user, quota_chan=excluded.quota_chan, "
            "plugins=excluded.plugins, extra_admins=excluded.extra_admins, "
            "note=excluded.note, updated_at=excluded.updated_at",
            (
                channel,
                self._encode_channel(merged["enabled"]),
                merged["quota_user"],
                merged["quota_chan"],
                self._encode_channel(merged["plugins"]),
                self._encode_channel(merged["extra_admins"]),
                merged["note"],
                time.time(),
            ),
        )
        await self.db.commit()

    async def channel_delete(self, channel: str) -> bool:
        cur = await self.db.execute("DELETE FROM channel_config WHERE channel=?", (channel,))
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    # -- media 查询 --------------------------------------------------------- #
    async def media_list(self, *, plugin_id: str | None = None, limit: int = 60) -> list[dict[str, Any]]:
        sql = "SELECT * FROM media"
        args: list[Any] = []
        if plugin_id:
            sql += " WHERE plugin_id=?"
            args.append(plugin_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        async with self.db.execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]
