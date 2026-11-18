"""Instagram Reels via Instagram Graph API (Facebook Login for Business), com upload resumível de arquivo local.

Pré-requisitos (uma vez):
  - Conta Instagram Profissional (Criador ou Empresa) ligada a uma Página do Facebook.
  - App em developers.facebook.com com o produto Instagram; você como admin do app.
  - Token com permissões instagram_basic, instagram_content_publish, pages_read_engagement
    (gere no Graph API Explorer e rode `python -m clipfarm.studio auth instagram`).
Fluxo: cria container REELS (upload_type=resumable) -> envia o arquivo para rupload -> espera FINISHED -> publica.
Docs: https://developers.facebook.com/documentation/instagram-platform/content-publishing/resumable-uploads
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

VERSION = "v26.0"
GRAPH = f"https://graph.facebook.com/{VERSION}"
RUPLOAD = f"https://rupload.facebook.com/ig-api-upload/{VERSION}"


def _check(r: requests.Response, what: str) -> dict:
    try:
        d = r.json()
    except ValueError:
        d = {"raw": r.text[:300]}
    if r.status_code >= 400 or "error" in d:
        raise RuntimeError(f"Instagram {what}: {d.get('error', d)}")
    return d


# ---------------------------------------------------------------- conta
def setup(token: str, token_path: str, app_id: str | None = None, app_secret: str | None = None) -> dict:
    """Troca por token de longa duração (se app_id/secret) e descobre o ID da conta do Instagram."""
    if app_id and app_secret:
        d = _check(requests.get(f"{GRAPH}/oauth/access_token", timeout=30, params={
            "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": app_secret,
            "fb_exchange_token": token}), "troca de token")
        token = d["access_token"]
    pages = _check(requests.get(f"{GRAPH}/me/accounts", timeout=30, params={
        "fields": "name,instagram_business_account{id,username}", "access_token": token}), "páginas")
    accounts = [p["instagram_business_account"] | {"page": p["name"]} for p in pages.get("data", [])
                if p.get("instagram_business_account")]
    if not accounts:
        raise RuntimeError("Nenhuma conta Instagram profissional ligada às suas Páginas do Facebook.")
    acc = accounts[0]
    saved = {"access_token": token, "ig_user_id": acc["id"], "username": acc.get("username"),
             "page": acc["page"], "obtained_at": time.time()}
    Path(token_path).write_text(json.dumps(saved))
    return saved


def load(token_path: str) -> dict:
    p = Path(token_path)
    if not p.exists():
        raise RuntimeError("Instagram não conectado: rode `python -m clipfarm.studio auth instagram`.")
    return json.loads(p.read_text())


# ---------------------------------------------------------------- publicação
def post(token: str, ig_user_id: str, path: str, caption: str, share_to_feed: bool = True,
         timeout_s: int = 900) -> str:
    size = os.path.getsize(path)
    if size > 300 * 1024 * 1024:
        raise RuntimeError("Reels aceita no máximo 300 MB")
    container = _check(requests.post(f"{GRAPH}/{ig_user_id}/media", timeout=60, params={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200],
        "share_to_feed": "true" if share_to_feed else "false", "access_token": token}), "criar container")
    cid = container["id"]

    with open(path, "rb") as f:
        up = requests.post(f"{RUPLOAD}/{cid}", data=f, timeout=600, headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)})
    d = _check(up, "upload")
    if not d.get("success", True):
        raise RuntimeError(f"Instagram upload: {d}")

    deadline = time.time() + timeout_s
    while True:
        st = _check(requests.get(f"{GRAPH}/{cid}", timeout=30, params={"fields": "status_code,status"},
                                 headers={"Authorization": f"OAuth {token}"}), "status")
        code = st.get("status_code")
        if code in ("FINISHED", "PUBLISHED"):
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram processamento: {st.get('status') or code}")
        if time.time() > deadline:
            raise RuntimeError("Instagram: processamento do vídeo passou do tempo limite")
        time.sleep(5)

    pub = _check(requests.post(f"{GRAPH}/{ig_user_id}/media_publish", timeout=60, data={"creation_id": cid},
                               headers={"Authorization": f"OAuth {token}"}), "publicar")
    return pub["id"]
