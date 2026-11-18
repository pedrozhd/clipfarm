from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS moments (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,          -- twitch_clip | chat_spike | local
    platform      TEXT NOT NULL,          -- twitch | kick | local
    channel       TEXT NOT NULL,
    vod_id        TEXT,
    vod_offset_s  REAL,                   -- centro estimado do momento no VOD
    media_url     TEXT,                   -- URL do clip pronto (twitch_clip) ou arquivo local
    duration_s    REAL,
    score         REAL DEFAULT 0,
    signals       TEXT DEFAULT '{}',      -- JSON com sinais (chat, views, tipo)
    title_hint    TEXT,
    external_id   TEXT UNIQUE,            -- dedupe (id do clip, canal+offset etc.)
    status        TEXT DEFAULT 'new',     -- new | rendered | rejected | failed | no_vod
    error         TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
    id           INTEGER PRIMARY KEY,
    moment_id    INTEGER NOT NULL REFERENCES moments(id),
    path         TEXT NOT NULL,
    start_s      REAL, end_s REAL,
    title        TEXT, description TEXT,
    score        REAL,
    status       TEXT DEFAULT 'review',   -- review | approved | rejected | published
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY,
    clip_id     INTEGER NOT NULL REFERENCES clips(id),
    platform    TEXT NOT NULL,            -- youtube | tiktok
    remote_id   TEXT,
    status      TEXT,                     -- ok | failed
    error       TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (clip_id, platform)
);
CREATE INDEX IF NOT EXISTS ix_moments_status ON moments(status, score DESC);
CREATE INDEX IF NOT EXISTS ix_moments_chan ON moments(channel, vod_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---------- moments ----------
    def near_duplicate(self, channel: str, vod_id: str | None, offset: float | None, tol: float = 45) -> bool:
        if not vod_id or offset is None:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM moments WHERE channel=? AND vod_id=? AND ABS(vod_offset_s-?)<? LIMIT 1",
            (channel, vod_id, offset, tol),
        ).fetchone()
        return row is not None

    def add_moment(self, **m) -> int | None:
        """Insere um momento; retorna None se for duplicado."""
        if self.near_duplicate(m["channel"], m.get("vod_id"), m.get("vod_offset_s")):
            return None
        m.setdefault("signals", {})
        m["signals"] = json.dumps(m["signals"], ensure_ascii=False)
        m["created_at"] = now()
        cols = ",".join(m)
        try:
            cur = self.conn.execute(
                f"INSERT INTO moments ({cols}) VALUES ({','.join('?' * len(m))})", tuple(m.values())
            )
        except sqlite3.IntegrityError:
            return None
        self.conn.commit()
        return cur.lastrowid

    def pending_moments(self, limit: int, min_age_s: float = 0) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM moments WHERE status='new' "
            "AND (source!='chat_spike' OR (julianday('now')-julianday(created_at))*86400 >= ?) "
            "ORDER BY score DESC LIMIT ?",
            (min_age_s, limit),
        ).fetchall()

    def set_moment(self, mid: int, status: str, error: str | None = None):
        self.conn.execute("UPDATE moments SET status=?, error=? WHERE id=?", (status, error, mid))
        self.conn.commit()

    # ---------- clips ----------
    def add_clip(self, **c) -> int:
        c["created_at"] = now()
        cur = self.conn.execute(
            f"INSERT INTO clips ({','.join(c)}) VALUES ({','.join('?' * len(c))})", tuple(c.values())
        )
        self.conn.commit()
        return cur.lastrowid

    def clips(self, status: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT c.*, m.channel, m.source FROM clips c JOIN moments m ON m.id=c.moment_id"
        if status:
            return self.conn.execute(q + " WHERE c.status=? ORDER BY c.score DESC", (status,)).fetchall()
        return self.conn.execute(q + " ORDER BY c.id DESC").fetchall()

    def set_clip(self, cid: int, status: str, title: str | None = None):
        if title:
            self.conn.execute("UPDATE clips SET status=?, title=? WHERE id=?", (status, title, cid))
        else:
            self.conn.execute("UPDATE clips SET status=? WHERE id=?", (status, cid))
        self.conn.commit()

    # ---------- posts ----------
    def posted(self, clip_id: int, platform: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM posts WHERE clip_id=? AND platform=? AND status='ok'", (clip_id, platform)
        ).fetchone() is not None

    def posts_today(self, platform: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE platform=? AND status='ok' AND date(created_at)=date('now')",
            (platform,),
        ).fetchone()[0]

    def remove_post(self, clip_id: int, platform: str):
        self.conn.execute("DELETE FROM posts WHERE clip_id=? AND platform=?", (clip_id, platform))
        self.conn.commit()

    def posted_map(self) -> dict[int, list[str]]:
        """clip_id -> plataformas em que já foi postado."""
        out: dict[int, list[str]] = {}
        for cid, plat in self.conn.execute("SELECT clip_id, platform FROM posts WHERE status='ok'"):
            out.setdefault(cid, []).append(plat)
        return out

    def add_post(self, clip_id: int, platform: str, remote_id: str | None, status: str, error: str | None = None):
        self.conn.execute(
            "INSERT INTO posts (clip_id, platform, remote_id, status, error, created_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(clip_id, platform) DO UPDATE SET remote_id=excluded.remote_id, status=excluded.status, "
            "error=excluded.error, created_at=excluded.created_at",
            (clip_id, platform, remote_id, status, error, now()),
        )
        self.conn.commit()
