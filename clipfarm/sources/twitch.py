"""Twitch Helix: lives por jogo, clips mais vistos, VOD atual. Docs: https://dev.twitch.tv/docs/api/reference"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Iterator

import requests

API = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Twitch:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_exp = 0.0
        self.s = requests.Session()

    # ---------- auth / http ----------
    def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self.s.post(
            TOKEN_URL,
            params={"client_id": self.client_id, "client_secret": self.client_secret,
                    "grant_type": "client_credentials"},
            timeout=20,
        )
        r.raise_for_status()
        d = r.json()
        self._token, self._token_exp = d["access_token"], time.time() + d["expires_in"]
        return self._token

    def _get(self, path: str, params: dict | list) -> dict:
        for attempt in range(4):
            r = self.s.get(
                f"{API}/{path}",
                params=params,
                headers={"Client-Id": self.client_id, "Authorization": f"Bearer {self._auth()}"},
                timeout=20,
            )
            if r.status_code == 401:
                self._token = None
                continue
            if r.status_code == 429:  # rate limit: espera até o reset
                reset = float(r.headers.get("Ratelimit-Reset", time.time() + 5))
                time.sleep(max(1.0, reset - time.time()))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def _paginate(self, path: str, params: dict, limit: int) -> Iterator[dict]:
        got, cursor = 0, None
        while got < limit:
            p = dict(params, first=min(100, limit - got))
            if cursor:
                p["after"] = cursor
            d = self._get(path, p)
            for item in d.get("data", []):
                yield item
                got += 1
            cursor = (d.get("pagination") or {}).get("cursor")
            if not cursor or not d.get("data"):
                break

    # ---------- endpoints ----------
    def game_ids(self, names: list[str]) -> dict[str, str]:
        d = self._get("games", [("name", n) for n in names])
        return {g["name"]: g["id"] for g in d.get("data", [])}

    def users(self, logins: list[str]) -> dict[str, dict]:
        out = {}
        for i in range(0, len(logins), 100):
            d = self._get("users", [("login", l) for l in logins[i:i + 100]])
            out.update({u["login"].lower(): u for u in d.get("data", [])})
        return out

    def top_streams(self, game_id: str, languages: list[str] | None, limit: int, min_viewers: int = 0) -> list[dict]:
        """Lives do jogo, já ordenadas por espectadores (desc)."""
        streams = []
        for lang in (languages or [None]):
            p = {"game_id": game_id, "type": "live"}
            if lang:
                p["language"] = lang
            for s in self._paginate("streams", p, limit):
                if s["viewer_count"] < min_viewers:
                    break
                streams.append(s)
        streams.sort(key=lambda s: s["viewer_count"], reverse=True)
        return streams[:limit]

    def top_clips(self, game_id: str, hours: float, limit: int) -> list[dict]:
        """Clips criados pelos espectadores nas últimas `hours`, ordenados por views (desc)."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        return list(self._paginate(
            "clips", {"game_id": game_id, "started_at": rfc3339(start), "ended_at": rfc3339(end)}, limit))

    def archive_vod(self, user_id: str) -> dict | None:
        """VOD da live atual/mais recente (existe durante a live se o canal salva VODs)."""
        d = self._get("videos", {"user_id": user_id, "type": "archive", "first": 1})
        return (d.get("data") or [None])[0]
