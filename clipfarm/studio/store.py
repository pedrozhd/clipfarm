"""Persistência do Studio (SQLite em WAL). Usuários, vídeos analisados e cortes."""
from __future__ import annotations

import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL, name TEXT, handle TEXT, credit TEXT,
    plan TEXT DEFAULT 'free', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,                 -- url | upload
    source TEXT NOT NULL,               -- URL ou nome do arquivo
    title TEXT, uploader TEXT, duration_s REAL, transcript_source TEXT,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued | analyzing | ready | failed
    error TEXT, dir TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    media_id INTEGER NOT NULL REFERENCES media(id), parent_id INTEGER,
    start_s REAL NOT NULL, end_s REAL NOT NULL, title TEXT, hook TEXT, style TEXT, layout TEXT,
    facecam TEXT, credit TEXT,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued | rendering | done | failed
    path TEXT, thumb TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    clip_id INTEGER NOT NULL REFERENCES clips(id),
    platform TEXT NOT NULL,             -- youtube | tiktok | instagram
    caption TEXT, publish_at TEXT NOT NULL,   -- UTC ISO
    status TEXT NOT NULL DEFAULT 'scheduled', -- scheduled | publishing | ok | failed | canceled
    remote_id TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_media_q ON media(status, id);
CREATE INDEX IF NOT EXISTS ix_posts_q ON posts(status, publish_at);
CREATE INDEX IF NOT EXISTS ix_clips_q ON clips(status, id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def _x(self, sql: str, args: tuple = ()):
        with self.lock:
            return self.conn.execute(sql, args)

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        row = self._x(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self._x(sql, args).fetchall()]

    # ---------------------------------------------------------- users
    def create_user(self, name: str, handle: str | None = None, credit: str | None = None,
                    plan: str = "free") -> dict:
        token = secrets.token_urlsafe(24)
        self._x("INSERT INTO users (token, name, handle, credit, plan, created_at) VALUES (?,?,?,?,?,?)",
                (token, name, handle, credit, plan, now()))
        return self.user_by_token(token)

    def user_by_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        return self._one("SELECT * FROM users WHERE token=?", (token,))

    def local_user(self, plan: str = "pessoal") -> dict:
        """Usuário único do modo local (Claude Desktop / Cowork)."""
        u = self._one("SELECT * FROM users WHERE name='local' ORDER BY id LIMIT 1")
        return u or self.create_user("local", plan=plan)

    def users(self) -> list[dict]:
        return self._all("SELECT * FROM users ORDER BY id")

    def update_user(self, uid: int, **fields):
        if fields:
            sets = ",".join(f"{k}=?" for k in fields)
            self._x(f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), uid))

    # ---------------------------------------------------------- media
    def add_media(self, user_id: int, kind: str, source: str, base_dir: Path, title: str | None = None,
                  status: str = "queued") -> dict:
        with self.lock:
            cur = self._x("INSERT INTO media (user_id, kind, source, title, status, dir, created_at, updated_at) "
                          "VALUES (?,?,?,?,?,?,?,?)", (user_id, kind, source, title, status, "", now(), now()))
            mid = cur.lastrowid
            d = base_dir / f"u{user_id}" / f"m{mid}"
            d.mkdir(parents=True, exist_ok=True)
            self._x("UPDATE media SET dir=? WHERE id=?", (str(d), mid))
        return self.media(mid)

    def media(self, mid: int, user_id: int | None = None) -> dict | None:
        if user_id is None:
            return self._one("SELECT * FROM media WHERE id=?", (mid,))
        return self._one("SELECT * FROM media WHERE id=? AND user_id=?", (mid, user_id))

    def list_media(self, user_id: int, limit: int = 30) -> list[dict]:
        return self._all("SELECT * FROM media WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))

    def set_media(self, mid: int, **fields):
        fields["updated_at"] = now()
        sets = ",".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE media SET {sets} WHERE id=?", (*fields.values(), mid))

    def claim_media(self) -> dict | None:
        return self._one("UPDATE media SET status='analyzing', updated_at=? WHERE id=(SELECT id FROM media "
                         "WHERE status='queued' ORDER BY id LIMIT 1) RETURNING *", (now(),))

    # ---------------------------------------------------------- clips
    def add_clip(self, **c) -> dict:
        c.setdefault("created_at", now())
        c["updated_at"] = now()
        cur = self._x(f"INSERT INTO clips ({','.join(c)}) VALUES ({','.join('?' * len(c))})", tuple(c.values()))
        return self.clip(cur.lastrowid)

    def clip(self, cid: int, user_id: int | None = None) -> dict | None:
        if user_id is None:
            return self._one("SELECT * FROM clips WHERE id=?", (cid,))
        return self._one("SELECT * FROM clips WHERE id=? AND user_id=?", (cid, user_id))

    def list_clips(self, user_id: int, media_id: int | None = None, ids: list[int] | None = None,
                   limit: int = 60) -> list[dict]:
        q, args = "SELECT * FROM clips WHERE user_id=?", [user_id]
        if media_id is not None:
            q += " AND media_id=?"
            args.append(media_id)
        if ids:
            q += f" AND id IN ({','.join('?' * len(ids))})"
            args += list(ids)
        return self._all(q + " ORDER BY id DESC LIMIT ?", (*args, limit))

    def set_clip(self, cid: int, **fields):
        fields["updated_at"] = now()
        sets = ",".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE clips SET {sets} WHERE id=?", (*fields.values(), cid))

    def claim_clip(self) -> dict | None:
        return self._one("UPDATE clips SET status='rendering', updated_at=? WHERE id=(SELECT id FROM clips "
                         "WHERE status='queued' ORDER BY id LIMIT 1) RETURNING *", (now(),))

    def clips_today(self, user_id: int) -> int:
        return self._x("SELECT COUNT(*) FROM clips WHERE user_id=? AND date(created_at)=date('now')",
                       (user_id,)).fetchone()[0]

    # ---------------------------------------------------------- posts
    def add_post(self, user_id: int, clip_id: int, platform: str, caption: str, publish_at: str) -> dict:
        cur = self._x("INSERT INTO posts (user_id, clip_id, platform, caption, publish_at, created_at, updated_at) "
                      "VALUES (?,?,?,?,?,?,?)", (user_id, clip_id, platform, caption, publish_at, now(), now()))
        return self.post(cur.lastrowid)

    def post(self, pid: int, user_id: int | None = None) -> dict | None:
        if user_id is None:
            return self._one("SELECT * FROM posts WHERE id=?", (pid,))
        return self._one("SELECT * FROM posts WHERE id=? AND user_id=?", (pid, user_id))

    def list_posts(self, user_id: int, clip_id: int | None = None, limit: int = 60) -> list[dict]:
        if clip_id is not None:
            return self._all("SELECT * FROM posts WHERE user_id=? AND clip_id=? ORDER BY id DESC LIMIT ?",
                             (user_id, clip_id, limit))
        return self._all("SELECT * FROM posts WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))

    def set_post(self, pid: int, **fields):
        fields["updated_at"] = now()
        sets = ",".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE posts SET {sets} WHERE id=?", (*fields.values(), pid))

    def claim_post(self) -> dict | None:
        """Próximo post vencido cujo corte já está renderizado."""
        return self._one(
            "UPDATE posts SET status='publishing', updated_at=? WHERE id=(SELECT p.id FROM posts p "
            "JOIN clips c ON c.id=p.clip_id WHERE p.status='scheduled' AND p.publish_at<=? AND c.status='done' "
            "ORDER BY p.publish_at LIMIT 1) RETURNING *", (now(), now()))

    def posts_on_day(self, user_id: int, platform: str, day_utc: str) -> int:
        """Posts feitos ou agendados para a plataforma num dia (UTC, YYYY-MM-DD)."""
        return self._x("SELECT COUNT(*) FROM posts WHERE user_id=? AND platform=? AND status IN "
                       "('scheduled','publishing','ok') AND substr(publish_at,1,10)=?",
                       (user_id, platform, day_utc)).fetchone()[0]

    def reset_running(self):
        """Ao iniciar, devolve para a fila o que ficou travado (outro processo pode estar ativo, então só o antigo)."""
        stale = "(julianday('now') - julianday(updated_at)) * 1440 > ?"
        self._x(f"UPDATE media SET status='queued' WHERE status='analyzing' AND {stale}", (180,))
        self._x(f"UPDATE clips SET status='queued' WHERE status='rendering' AND {stale}", (30,))
        # post interrompido no meio pode ter saído: não republica sozinho
        self._x("UPDATE posts SET status='failed', error='interrompido no meio do envio; confira na plataforma "
                f"antes de reenviar' WHERE status='publishing' AND {stale}", (30,))
