"""CLI do Studio (uso pessoal).

  python -m clipfarm.studio claude-config      imprime o bloco para colar no claude_desktop_config.json
  python -m clipfarm.studio mcp                o Claude Desktop roda isto (stdio) — você não precisa rodar
  python -m clipfarm.studio painel             só o painel local, sem o Claude
  python -m clipfarm.studio auth youtube|tiktok|instagram
  python -m clipfarm.studio serve              MCP por HTTP (ex.: via túnel); pede token na URL
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .. import config as C


def main(argv=None):
    ap = argparse.ArgumentParser(prog="clipfarm.studio")
    ap.add_argument("-c", "--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mcp", help="servidor MCP local (stdio) para o Claude Desktop/Cowork")
    m.add_argument("--sem-painel", action="store_true")
    p = sub.add_parser("painel", help="painel local + processamento, sem o Claude")
    p.add_argument("--port", type=int)
    s = sub.add_parser("serve", help="MCP por HTTP + painel (para túnel)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    a_ = sub.add_parser("auth", help="conecta uma conta para publicar")
    a_.add_argument("plataforma", choices=["youtube", "tiktok", "instagram"])
    sub.add_parser("claude-config", help="bloco JSON para o claude_desktop_config.json")
    sub.add_parser("token", help="mostra a URL do painel/conector do usuário local")
    a = ap.parse_args(argv)
    # stdio: logs SEMPRE no stderr (o stdout é o canal do protocolo)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if a.cmd == "claude-config":
        cfg_path = str(Path(a.config).resolve()) if a.config else str(C.ROOT / "config.yaml")
        block = {"mcpServers": {"clipfarm": {
            "command": sys.executable,
            "args": ["-m", "clipfarm.studio", "-c", cfg_path, "mcp"],
            "env": {"PYTHONPATH": str(C.ROOT)},
        }}}
        print(json.dumps(block, indent=2, ensure_ascii=False))
        print("\nCole em: Claude Desktop → Configurações → Desenvolvedor → Editar configuração "
              "(claude_desktop_config.json) e reinicie o Claude.", file=sys.stderr)
        return 0

    from .server import Studio

    if a.cmd == "mcp":
        st = Studio(a.config)
        st.run_stdio(None if a.sem_painel else st.sc["painel_port"])
        return 0

    if a.cmd in ("painel", "serve"):
        import uvicorn

        st = Studio(a.config)
        user = st.store.local_user()
        if a.cmd == "painel":
            port = a.port or st.sc["painel_port"]
            print(f"painel: http://127.0.0.1:{port}/u/{user['token']}", file=sys.stderr)
            uvicorn.run(st.http_app(with_mcp=False), host="127.0.0.1", port=port, access_log=False)
        else:
            print(f"MCP: {st.public}/u/{user['token']}/mcp", file=sys.stderr)
            uvicorn.run(st.http_app(with_mcp=True), host=a.host, port=a.port, proxy_headers=True,
                        access_log=False)  # logs com URL têm o token
        return 0

    st = Studio(a.config, transcriber=None)
    data = Path(st.cfg["paths"]["data_dir"])
    if a.cmd == "token":
        u = st.store.local_user()
        print(f"painel:  http://127.0.0.1:{st.sc['painel_port']}/u/{u['token']}")
        print(f"cortes:  {st.worker.export_dir}")
    elif a.cmd == "auth":
        if a.plataforma == "youtube":
            from ..publish import youtube

            youtube.service(C.env_path("YOUTUBE_CLIENT_SECRETS", True), str(data / "youtube_token.json"))
            print("YouTube conectado.")
        elif a.plataforma == "tiktok":
            from urllib.parse import parse_qs, urlparse

            from ..publish import tiktok

            key, secret = C.env("TIKTOK_CLIENT_KEY", True), C.env("TIKTOK_CLIENT_SECRET", True)
            redirect = C.env("TIKTOK_REDIRECT_URI", True)
            print("1) Abra e autorize:\n", tiktok.authorize_url(key, redirect))
            url = input("2) Cole aqui a URL para onde você foi redirecionado: ").strip()
            code = parse_qs(urlparse(url).query).get("code", [url])[0]
            tiktok.exchange_code(key, secret, code, redirect, str(data / "tiktok_token.json"))
            print("TikTok conectado.")
        else:
            from ..publish import instagram

            print("Gere um token no Graph API Explorer (developers.facebook.com/tools/explorer) com as permissões\n"
                  "instagram_basic, instagram_content_publish, pages_show_list, pages_read_engagement.")
            token = input("Cole o token: ").strip()
            acc = instagram.setup(token, str(data / "instagram_token.json"), C.env("META_APP_ID"),
                                  C.env("META_APP_SECRET"))
            print(f"Instagram conectado: @{acc['username']} (página {acc['page']}).")
            if not (C.env("META_APP_ID") and C.env("META_APP_SECRET")):
                print("Aviso: sem META_APP_ID/META_APP_SECRET no .env o token é de curta duração (~1h). "
                      "Preencha e rode de novo para ter ~60 dias.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
