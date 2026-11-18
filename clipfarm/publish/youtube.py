"""Upload para YouTube (Shorts = vídeo vertical ≤ 3 min). YouTube Data API v3 / videos.insert.

Atenção: projetos de API não auditados só conseguem publicar como PRIVADO.
Peça a auditoria (YouTube API Services – Audit and Quota Extension) para liberar 'public'.
"""
from __future__ import annotations

from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def service(client_secrets: str, token_path: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    tp = Path(token_path)
    if tp.exists():
        creds = Credentials.from_authorized_user_file(str(tp), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(client_secrets, SCOPES)
            creds = flow.run_local_server(port=0)  # abre o navegador para login na conta do canal
        tp.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def upload(svc, path: str, title: str, description: str, tags: list[str], category_id: str = "20",
           privacy: str = "private") -> str:
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {"title": title[:100], "description": description[:4900], "tags": tags,
                    "categoryId": category_id},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    req = svc.videos().insert(part="snippet,status", body=body,
                              media_body=MediaFileUpload(path, mimetype="video/mp4", chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]
