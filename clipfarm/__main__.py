"""CLI: python -m clipfarm <comando>"""
from __future__ import annotations

import argparse
import logging
import sys

from . import config as C
from . import pipeline as P


def main(argv=None):
    ap = argparse.ArgumentParser(prog="clipfarm", description="Farm de clips: lives -> cortes 9:16 -> TikTok/YouTube")
    ap.add_argument("-c", "--config", help="caminho do config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("discover", help="lista as maiores lives do jogo agora")
    sub.add_parser("harvest", help="importa os clips mais vistos da Twitch como momentos")
    w = sub.add_parser("watch", help="escuta o chat ao vivo e registra picos")
    w.add_argument("--minutes", type=float, help="parar depois de N minutos")
    p = sub.add_parser("process", help="baixa, corta, legenda e renderiza momentos pendentes")
    p.add_argument("--limit", type=int, default=10)
    loc = sub.add_parser("add-local", help="adiciona um vídeo local como momento (teste)")
    loc.add_argument("path")
    loc.add_argument("--channel", default="local")
    r = sub.add_parser("review", help="lista/aprova/rejeita clips renderizados")
    r.add_argument("action", nargs="?", choices=["list", "approve", "reject"], default="list")
    r.add_argument("ids", nargs="*", type=int)
    r.add_argument("--title", help="novo título ao aprovar")
    sub.add_parser("publish", help="publica clips aprovados (respeita limite diário)")
    sub.add_parser("run", help="loop: harvest + process + publish")
    sub.add_parser("auth-youtube", help="login OAuth no canal do YouTube")
    sub.add_parser("auth-tiktok", help="login OAuth na conta do TikTok")
    sub.add_parser("stats", help="resumo do funil")
    pn = sub.add_parser("painel", help="painel local: ver os cortes, copiar legenda, marcar postados")
    pn.add_argument("--port", type=int, default=8766)

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    ctx = P.Ctx(a.config)

    if a.cmd == "discover":
        for s in P.discover(ctx):
            flag = "✔" if s["allowed"] else "·"
            print(f"{flag} {s['viewer_count']:>7}  {s['user_login']:<22} {s['language']}  {s['title'][:70]}")
    elif a.cmd == "harvest":
        print(f"{P.harvest(ctx)} momentos novos")
    elif a.cmd == "watch":
        P.watch(ctx, a.minutes)
    elif a.cmd == "process":
        print(f"clips gerados: {P.process(ctx, a.limit)}")
    elif a.cmd == "add-local":
        mid = ctx.db.add_moment(source="local", platform="local", channel=a.channel, media_url=a.path,
                                score=5, signals={"kind": "reaction"}, external_id=f"local:{a.path}")
        print(f"momento {mid}")
    elif a.cmd == "review":
        if a.action == "list":
            for c in ctx.db.clips("review"):
                print(f"[{c['id']}] {c['score']:.1f}  {c['channel']:<18} {c['end_s'] - c['start_s']:.0f}s  "
                      f"{c['title']}\n      {c['path']}")
        else:
            for cid in a.ids:
                ctx.db.set_clip(cid, "approved" if a.action == "approve" else "rejected", a.title)
            print(f"{a.action}: {a.ids}")
    elif a.cmd == "publish":
        print(f"{P.publish(ctx)} posts enviados")
    elif a.cmd == "run":
        P.run(ctx)
    elif a.cmd == "auth-youtube":
        from .publish import youtube

        youtube.service(C.env("YOUTUBE_CLIENT_SECRETS", True), str(ctx.data / "youtube_token.json"))
        print("YouTube autenticado.")
    elif a.cmd == "auth-tiktok":
        from .publish import tiktok

        key, secret = C.env("TIKTOK_CLIENT_KEY", True), C.env("TIKTOK_CLIENT_SECRET", True)
        redirect = C.env("TIKTOK_REDIRECT_URI", True)
        print("1) Abra e autorize:\n", tiktok.authorize_url(key, redirect))
        url = input("2) Cole aqui a URL para onde você foi redirecionado: ").strip()
        from urllib.parse import parse_qs, urlparse

        code = parse_qs(urlparse(url).query).get("code", [url])[0]
        tiktok.exchange_code(key, secret, code, redirect, str(ctx.data / "tiktok_token.json"))
        print("TikTok autenticado.")
    elif a.cmd == "painel":
        import uvicorn

        from .panel import create_app

        print(f"painel: http://127.0.0.1:{a.port}")
        uvicorn.run(create_app(ctx), host="127.0.0.1", port=a.port, log_level="warning")
    elif a.cmd == "stats":
        q = ctx.db.conn.execute
        print("momentos:", dict(q("SELECT status, COUNT(*) FROM moments GROUP BY status").fetchall()))
        print("por fonte:", dict(q("SELECT source, COUNT(*) FROM moments GROUP BY source").fetchall()))
        print("clips:", dict(q("SELECT status, COUNT(*) FROM clips GROUP BY status").fetchall()))
        print("posts:", [tuple(r) for r in q("SELECT platform, status, COUNT(*) FROM posts GROUP BY 1,2")])
    return 0


if __name__ == "__main__":
    sys.exit(main())
