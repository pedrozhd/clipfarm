"""TikTok Content Posting API. Docs: https://developers.tiktok.com/doc/content-posting-api-get-started

Dois modos:
- inbox  (escopo video.upload): o vídeo cai nas notificações/rascunhos do app; você finaliza e posta no celular.
          Bom para começar: não depende de auditoria e você revisa a legenda/som no app.
- direct (escopo video.publish): post direto. Sem auditoria do app, tudo fica PRIVADO (SELF_ONLY).
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.parse
from pathlib import Path

import requests

BASE = "https://open.tiktokapis.com"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = f"{BASE}/v2/oauth/token/"
MB = 1024 * 1024


# ---------------------------------------------------------------- OAuth
def authorize_url(client_key: str, redirect_uri: str, scopes=("user.info.basic", "video.upload", "video.publish")) -> str:
    q = {"client_key": client_key, "response_type": "code", "scope": ",".join(scopes),
         "redirect_uri": redirect_uri, "state": "clipfarm"}
    return f"{AUTH_URL}?{urllib.parse.urlencode(q)}"


def exchange_code(client_key: str, client_secret: str, code: str, redirect_uri: str, token_path: str) -> dict:
    r = requests.post(TOKEN_URL, data={"client_key": client_key, "client_secret": client_secret, "code": code,
                                       "grant_type": "authorization_code", "redirect_uri": redirect_uri},
                      headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    r.raise_for_status()
    return _save(r.json(), token_path)


def _save(tok: dict, token_path: str) -> dict:
    if "access_token" not in tok:
        raise RuntimeError(f"TikTok OAuth falhou: {tok}")
    tok["obtained_at"] = time.time()
    Path(token_path).write_text(json.dumps(tok))
    return tok


def access_token(token_path: str) -> str:
    tok = json.loads(Path(token_path).read_text())
    if time.time() > tok["obtained_at"] + tok.get("expires_in", 86400) - 300:
        r = requests.post(TOKEN_URL, data={"client_key": os.environ["TIKTOK_CLIENT_KEY"],
                                           "client_secret": os.environ["TIKTOK_CLIENT_SECRET"],
                                           "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]},
                          headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
        r.raise_for_status()
        tok = _save(r.json(), token_path)
    return tok["access_token"]


# ---------------------------------------------------------------- upload
def _post(token: str, path: str, body: dict) -> dict:
    r = requests.post(f"{BASE}{path}", json=body, timeout=30,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"})
    d = r.json()
    if r.status_code >= 400 or d.get("error", {}).get("code") not in (None, "ok"):
        raise RuntimeError(f"TikTok {path}: {d.get('error') or r.text}")
    return d.get("data", {})


def _chunks(size: int) -> tuple[int, int]:
    """Arquivo ≤ 64 MB vai inteiro; acima disso, pedaços de 10 MB (o último absorve o resto)."""
    if size <= 64 * MB:
        return size, 1
    chunk = 10 * MB
    return chunk, math.floor(size / chunk)


def _send_file(upload_url: str, path: str, chunk: int, count: int):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            data = f.read(end - start + 1)
            r = requests.put(upload_url, data=data, timeout=300, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(data)),
                "Content-Range": f"bytes {start}-{end}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise RuntimeError(f"TikTok upload chunk {i}: {r.status_code} {r.text[:200]}")


def creator_info(token: str) -> dict:
    return _post(token, "/v2/post/publish/creator_info/query/", {})


def post(token: str, path: str, title: str, mode: str = "inbox", privacy: str = "SELF_ONLY") -> str:
    size = os.path.getsize(path)
    chunk, count = _chunks(size)
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
    if mode == "direct":
        allowed = creator_info(token).get("privacy_level_options", [])
        if privacy not in allowed:
            privacy = "SELF_ONLY" if "SELF_ONLY" in allowed else (allowed or ["SELF_ONLY"])[0]
        data = _post(token, "/v2/post/publish/video/init/", {
            "post_info": {"title": title[:2200], "privacy_level": privacy, "disable_duet": False,
                          "disable_comment": False, "disable_stitch": False, "video_cover_timestamp_ms": 1000},
            "source_info": source})
    else:
        data = _post(token, "/v2/post/publish/inbox/video/init/", {"source_info": source})
    _send_file(data["upload_url"], path, chunk, count)
    return data["publish_id"]


def status(token: str, publish_id: str) -> dict:
    return _post(token, "/v2/post/publish/status/fetch/", {"publish_id": publish_id})
