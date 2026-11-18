"""Kick — placeholder para a 2ª fonte.

A Kick tem API pública com OAuth (https://docs.kick.com). Para plugar aqui:
  1. top_streams(category) -> lista de dicts {login, user_id, started_at, viewers}
  2. chat: a Kick usa Pusher/WebSocket; adapte um listener igual ao signals/chat.py
  3. download: yt-dlp suporta VODs e clips da Kick (kick.com/video/<id>, kick.com/<canal>?clip=<id>)
A Kick é forte no público BR — vale ser a próxima fonte depois da Twitch.
"""


class Kick:
    def __init__(self, *_, **__):
        raise NotImplementedError("Fonte Kick ainda não implementada — ver docstring do módulo.")
