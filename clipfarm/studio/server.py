"""ClipFarm Studio: ferramentas MCP para o Claude gerar e publicar cortes, + painel web local.

Modos:
  mcp     (stdio)  conector local do Claude Desktop / Cowork — uso pessoal, sem servidor público
  painel  (http)   só o painel em http://127.0.0.1:<porta> (preview, upload, download)
  serve   (http)   MCP remoto em /u/<token>/mcp (ex.: via túnel) + painel
"""
from __future__ import annotations

import contextlib
import functools
import html
import json
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anyio
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from .. import __version__
from .. import config as C
from ..styles import LAYOUTS, STYLES
from . import analyze as A
from .store import Store
from .worker import PLATFORMS, Worker

log = logging.getLogger("clipfarm.studio")

INSTRUCTIONS = """\
ClipFarm gera cortes verticais 9:16 (legenda palavra a palavra, gancho no topo, @ da página) a partir de \
vídeos longos — VOD da Twitch/Kick, YouTube ou arquivo enviado no painel — e publica/agenda no YouTube Shorts, \
TikTok e Instagram Reels.

Gerar:
1. analisar_video(url) — ou o usuário sobe o arquivo no painel (meu_painel) e você usa o media_id.
2. status_video(media_id, aguardar_s=45) até status=ready. VOD longo leva minutos; avise o usuário do andamento.
3. Leia os candidatos (picos de volume + fala exaltada); use ler_transcricao para contexto e ver_frame para \
ver o enquadramento (se houver facecam, layout="facecam" com a região em fração 0–1).
4. Escolha os melhores: algo acontece nos 2 primeiros segundos, termina logo após o payoff, 15–45s (máx 90s).
5. criar_cortes com titulo e gancho (headline curta, até ~8 palavras). status_cortes até prontos.
6. variar_corte gera versões com outros ganchos/estilos/layouts para teste A/B; ajustar_corte refaz um corte.

Publicar:
7. contas_conectadas para ver o que está ligado. publicar_corte(clip_id, plataformas, legenda, quando) — \
quando = 'AAAA-MM-DD HH:MM' no fuso local; vazio = agora. Ao agendar vários, espalhe ao longo do dia \
(intervalos de 1–3h) em vez de postar tudo junto. Confirme com o usuário antes de publicar.

Regras: só conteúdo que o usuário tem direito de usar (dele ou de criadores que autorizam clipagem). \
Evite cortes que sejam só cutscene/história de jogo sem reação (spoiler e risco de remoção)."""


class Facecam(BaseModel):
    x: float = Field(ge=0, le=1, description="borda esquerda da facecam, fração da largura")
    y: float = Field(ge=0, le=1, description="borda superior, fração da altura")
    w: float = Field(gt=0, le=1, description="largura, fração")
    h: float = Field(gt=0, le=1, description="altura, fração")


class Corte(BaseModel):
    inicio_s: float = Field(ge=0, description="início no vídeo original, em segundos")
    fim_s: float = Field(gt=0, description="fim no vídeo original, em segundos")
    titulo: str = Field(max_length=150, description="título/legenda do post")
    gancho: str | None = Field(None, max_length=90, description="headline curta exibida no vídeo")
    estilo: str = Field("flagra", description=f"estilo da legenda: {', '.join(STYLES)}")
    layout: str = Field("auto", description=f"layout: {', '.join(LAYOUTS)}")
    facecam: Facecam | None = Field(None, description="região da facecam (para layout facecam)")
    credito: str | None = Field(None, max_length=60, description="crédito ao criador original; vazio = sem crédito")


def _token(req) -> str | None:
    tok = req.scope.get("clipfarm_token") if req is not None else None
    if not tok and req is not None:
        auth = req.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
    return tok


class TokenPath:
    """Reescreve /u/<token>/mcp -> /mcp guardando o token no escopo."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            parts = scope["path"].split("/")
            if len(parts) >= 4 and parts[1] == "u" and parts[3] == "mcp":
                scope = dict(scope, path="/mcp", raw_path=b"/mcp", clipfarm_token=parts[2])
        await self.app(scope, receive, send)


class Studio:
    def __init__(self, config_path: str | None = None, transcriber="auto", publishers: dict | None = None):
        self.cfg = C.load(config_path)
        self.sc = self.cfg["studio"]
        self.tz = ZoneInfo(self.sc["timezone"])
        self.base_dir = Path(self.cfg["paths"]["data_dir"]) / "studio"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.base_dir / "studio.db")
        self.worker = Worker(self.store, self.cfg, transcriber, publishers)
        self.public = self.sc["public_url"].rstrip("/")
        self.painel_url: str | None = None  # setado quando o painel local sobe junto do modo stdio
        self.mcp = self._build_mcp()

    # ================================================================ helpers
    def local_user(self) -> dict:
        return self.store.local_user(plan=next(iter(self.sc["plans"])))

    def quota_left(self, user: dict) -> int:
        plans = self.sc["plans"]
        return plans.get(user["plan"], max(plans.values())) - self.store.clips_today(user["id"])

    def fmt_local(self, iso_utc: str) -> str:
        return datetime.fromisoformat(iso_utc).astimezone(self.tz).strftime("%d/%m/%Y %H:%M")

    def parse_when(self, quando: str | None) -> datetime:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        if not quando or quando.strip().lower() in ("agora", "now", "já"):
            return now
        s = quando.strip().replace(" ", "T", 1)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError as e:
            raise ValueError("quando deve ser 'AAAA-MM-DD HH:MM' (fuso local) ou vazio para agora") from e
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        dt = dt.astimezone(timezone.utc).replace(microsecond=0)
        if dt < now - timedelta(minutes=5):
            raise ValueError(f"horário no passado ({dt.astimezone(self.tz):%d/%m %H:%M})")
        if dt > now + timedelta(days=60):
            raise ValueError("agendamento máximo de 60 dias")
        return max(dt, now)

    def default_caption(self, clip: dict) -> str:
        tags = " ".join(self.cfg["publish"]["hashtags"])
        parts = [clip["title"] or ""]
        if clip["credit"]:
            parts.append(f"\n{clip['credit']}")
        parts.append(f"\n{tags}")
        return "\n".join(p for p in parts if p).strip()

    def clip_view(self, c: dict, user: dict, http: bool) -> dict:
        v = {"id": c["id"], "media_id": c["media_id"], "status": c["status"], "titulo": c["title"],
             "gancho": c["hook"], "estilo": c["style"], "layout": c["layout"],
             "duracao_s": round(c["end_s"] - c["start_s"], 1), "inicio_s": c["start_s"], "fim_s": c["end_s"]}
        if c["parent_id"]:
            v["variacao_de"] = c["parent_id"]
        if c["status"] == "done":
            if http:
                v["download"] = f"{self.public}/u/{user['token']}/c/{c['id']}.mp4"
            else:
                v["arquivo"] = c["path"]
                if self.painel_url:
                    v["preview"] = f"{self.painel_url}/u/{user['token']}/c/{c['id']}.mp4"
        if c["status"] == "failed":
            v["erro"] = c["error"]
        return v

    def post_view(self, p: dict) -> dict:
        v = {"post_id": p["id"], "clip_id": p["clip_id"], "plataforma": p["platform"], "status": p["status"],
             "quando": self.fmt_local(p["publish_at"])}
        if p["remote_id"]:
            v["id_remoto"] = p["remote_id"]
            if p["platform"] == "youtube":
                v["link"] = f"https://youtube.com/shorts/{p['remote_id']}"
        if p["error"]:
            v["erro"] = p["error"]
        return v

    def painel_link(self, user: dict) -> str:
        base = self.painel_url or self.public
        return f"{base}/u/{user['token']}"

    # ================================================================ MCP
    def _build_mcp(self) -> MCPServer:
        st, store, worker, sc = self, self.store, self.worker, self.sc
        mcp = MCPServer("clipfarm", title="ClipFarm — cortes e publicação", instructions=INSTRUCTIONS,
                        version=__version__)
        register = mcp.tool

        def tool(**kw):
            """Como mcp.tool, mas erros de validação (ValueError) chegam ao Claude com a mensagem."""
            def deco(fn):
                @functools.wraps(fn)
                async def wrapper(*a, **k):
                    try:
                        return await fn(*a, **k)
                    except ValueError as e:
                        raise ToolError(str(e)) from e
                return register(**kw)(wrapper)
            return deco

        def me(ctx: Context) -> tuple[dict, bool]:
            """(usuário, veio por HTTP?). Sem request HTTP = modo local (stdio) = usuário local."""
            try:
                req = ctx.request_context.request
            except ValueError:
                req = None
            if req is None:
                return st.local_user(), False
            user = store.user_by_token(_token(req))
            if not user:
                raise ValueError("Token inválido. Use a URL do conector que você recebeu.")
            return user, True

        async def wait_for(check, seconds: float):
            deadline = time.monotonic() + max(0.0, min(seconds, 50))
            while True:
                v = check()
                if v is not None or time.monotonic() >= deadline:
                    return v
                await anyio.sleep(2)

        def ready_media(user, media_id) -> dict:
            m = store.media(media_id, user["id"])
            if not m or m["status"] != "ready":
                raise ValueError("vídeo não encontrado ou ainda não analisado")
            return m

        # ------------------------------------------------------------ gerar
        @tool(description="Envia um vídeo (YouTube, VOD da Twitch, Kick) para análise. Retorna media_id.")
        async def analisar_video(url: str, ctx: Context) -> dict:
            user, _ = me(ctx)
            url = A.check_url(url, tuple(sc["allowed_hosts"]))
            m = store.add_media(user["id"], "url", url, st.base_dir)
            return {"media_id": m["id"], "status": "na fila",
                    "proximo_passo": "status_video(media_id, aguardar_s=45) até status=ready"}

        @tool(description="Status da análise. Quando pronto, traz duração e candidatos a corte "
                          "(picos de volume + fala). aguardar_s: espera até N s (máx 50) por mudança.")
        async def status_video(media_id: int, ctx: Context, aguardar_s: int = 30) -> dict:
            user, _ = me(ctx)

            def check():
                m = store.media(media_id, user["id"])
                if not m:
                    raise ValueError("media_id não encontrado")
                return m if m["status"] in ("ready", "failed") else None

            m = await wait_for(check, aguardar_s) or store.media(media_id, user["id"])
            out = {"media_id": m["id"], "status": m["status"], "titulo": m["title"], "canal": m["uploader"],
                   "duracao_s": m["duration_s"], "transcricao": m["transcript_source"]}
            if m["status"] == "failed":
                out["erro"] = m["error"]
            elif m["status"] == "ready":
                an = json.loads((Path(m["dir"]) / "analysis.json").read_text())
                out["candidatos"] = [dict(c, fala=c["fala"][:350]) for c in an["candidates"]]
                out["dica"] = "Ajuste início/fim pela fala; use ler_transcricao para ver o contexto de um trecho."
            else:
                out["dica"] = "Ainda processando; consulte de novo."
            return out

        @tool(description="Lê a transcrição com tempos entre inicio_s e fim_s (máx ~6000 caracteres).")
        async def ler_transcricao(media_id: int, ctx: Context, inicio_s: float = 0, fim_s: float = 600) -> str:
            user, _ = me(ctx)
            m = ready_media(user, media_id)
            an = json.loads((Path(m["dir"]) / "analysis.json").read_text())
            return A.transcript_window(an["segments"], inicio_s, fim_s) or "(sem fala neste trecho)"

        @tool(description="Mostra o frame do vídeo no instante tempo_s (para ver enquadramento/facecam).")
        async def ver_frame(media_id: int, tempo_s: float, ctx: Context) -> Image:
            user, _ = me(ctx)
            m = store.media(media_id, user["id"])
            if not m:
                raise ValueError("media_id não encontrado")

            def grab() -> bytes:
                with tempfile.TemporaryDirectory() as tmp:
                    if m["kind"] == "upload":
                        src, at = str(Path(m["dir"]) / "source.mp4"), tempo_s
                    else:
                        src = str(Path(tmp) / "s.mp4")
                        A.fetch_section(m["source"], tempo_s, tempo_s + 2, Path(src))
                        at = 0.5
                    out = Path(tmp) / "f.jpg"
                    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", src, "-frames:v", "1",
                                    "-vf", "scale=768:-2", "-q:v", "4", str(out)], check=True,
                                   stdout=subprocess.DEVNULL)
                    return out.read_bytes()

            return Image(data=await anyio.to_thread.run_sync(grab), format="jpeg")

        def validate(m: dict, c: Corte):
            if c.fim_s <= c.inicio_s:
                raise ValueError(f"corte '{c.titulo}': fim_s precisa ser maior que inicio_s")
            if not 3 <= c.fim_s - c.inicio_s <= sc["max_clip_s"]:
                raise ValueError(f"corte '{c.titulo}': duração deve ficar entre 3 e {sc['max_clip_s']}s")
            if m["duration_s"] and c.fim_s > m["duration_s"] + 1:
                raise ValueError(f"corte '{c.titulo}': passa do fim do vídeo ({m['duration_s']:.0f}s)")
            if c.estilo not in STYLES:
                raise ValueError(f"estilo inválido: {c.estilo}. Opções: {', '.join(STYLES)}")
            if c.layout not in LAYOUTS:
                raise ValueError(f"layout inválido: {c.layout}. Opções: {', '.join(LAYOUTS)}")

        def queue(user: dict, m: dict, c: Corte, parent_id: int | None = None) -> dict:
            credit = c.credito if c.credito is not None else (
                user.get("credit") or (f"créditos: {m['uploader']}" if m["uploader"] else None))
            return store.add_clip(user_id=user["id"], media_id=m["id"], parent_id=parent_id, start_s=c.inicio_s,
                                  end_s=c.fim_s, title=c.titulo, hook=c.gancho, style=c.estilo, layout=c.layout,
                                  facecam=c.facecam.model_dump_json() if c.facecam else None, credit=credit or None)

        @tool(description="Cria cortes de um vídeo analisado (até 20 por chamada). Renderiza em segundo plano; "
                          "acompanhe com status_cortes.")
        async def criar_cortes(media_id: int, cortes: list[Corte], ctx: Context) -> dict:
            user, _ = me(ctx)
            m = ready_media(user, media_id)
            if not 1 <= len(cortes) <= 20:
                raise ValueError("envie entre 1 e 20 cortes por chamada")
            if st.quota_left(user) < len(cortes):
                raise ValueError(f"limite diário atingido (restam {max(0, st.quota_left(user))})")
            for c in cortes:
                validate(m, c)
            ids = [queue(user, m, c)["id"] for c in cortes]
            return {"ids": ids, "status": "na fila", "proximo_passo": "status_cortes(ids=..., aguardar_s=45)"}

        @tool(description="Status dos cortes (por media_id ou ids). Prontos trazem o arquivo/link.")
        async def status_cortes(ctx: Context, media_id: int | None = None, ids: list[int] | None = None,
                                aguardar_s: int = 30) -> dict:
            user, http = me(ctx)

            def fetch():
                return store.list_clips(user["id"], media_id=media_id, ids=ids)

            def check():
                cs = fetch()
                return cs if cs and all(c["status"] in ("done", "failed") for c in cs) else None

            cs = await wait_for(check, aguardar_s) or fetch()
            pend = sum(c["status"] in ("queued", "rendering") for c in cs)
            return {"cortes": [st.clip_view(c, user, http) for c in cs], "pendentes": pend,
                    "painel": st.painel_link(user), "pasta": str(worker.export_dir)}

        @tool(description="Pack viral: variações de um corte combinando ganchos, estilos e layouts (máx 12).")
        async def variar_corte(clip_id: int, ctx: Context, ganchos: list[str] | None = None,
                               estilos: list[str] | None = None, layouts: list[str] | None = None) -> dict:
            user, _ = me(ctx)
            base = store.clip(clip_id, user["id"])
            if not base:
                raise ValueError("corte não encontrado")
            m = store.media(base["media_id"])
            combos = [(g, s, lay) for g in (ganchos or [base["hook"]]) for s in (estilos or [base["style"]])
                      for lay in (layouts or [base["layout"]])]
            combos = [cb for cb in combos if cb != (base["hook"], base["style"], base["layout"])][:12]
            if not combos:
                raise ValueError("informe ao menos um gancho, estilo ou layout diferente")
            if st.quota_left(user) < len(combos):
                raise ValueError("limite diário atingido")
            novos = []
            for g, s, lay in combos:
                c = Corte(inicio_s=base["start_s"], fim_s=base["end_s"], titulo=base["title"], gancho=g,
                          estilo=s or "flagra", layout=lay or "auto",
                          facecam=Facecam(**json.loads(base["facecam"])) if base["facecam"] else None,
                          credito=base["credit"] or "")
                validate(m, c)
                novos.append(c)
            return {"ids": [queue(user, m, c, parent_id=clip_id)["id"] for c in novos], "status": "na fila"}

        @tool(description="Editor: refaz um corte mudando início/fim, título, gancho, estilo ou layout.")
        async def ajustar_corte(clip_id: int, ctx: Context, inicio_s: float | None = None,
                                fim_s: float | None = None, titulo: str | None = None, gancho: str | None = None,
                                estilo: str | None = None, layout: str | None = None) -> dict:
            user, _ = me(ctx)
            b = store.clip(clip_id, user["id"])
            if not b:
                raise ValueError("corte não encontrado")
            m = store.media(b["media_id"])
            c = Corte(inicio_s=b["start_s"] if inicio_s is None else inicio_s,
                      fim_s=b["end_s"] if fim_s is None else fim_s, titulo=titulo or b["title"],
                      gancho=b["hook"] if gancho is None else (gancho or None), estilo=estilo or b["style"],
                      layout=layout or b["layout"],
                      facecam=Facecam(**json.loads(b["facecam"])) if b["facecam"] else None,
                      credito=b["credit"] or "")
            validate(m, c)
            return {"id": queue(user, m, c, parent_id=clip_id)["id"], "status": "na fila"}

        @tool(description="Lista estilos de legenda e layouts disponíveis.")
        async def listar_estilos() -> dict:
            return {"estilos": {k: {"fonte": v["font"], "palavras_por_vez": v["words_per_caption"],
                                    "destaque": bool(v["highlight_color"])} for k, v in STYLES.items()},
                    "layouts": {"auto": "facecam se informada, senão blur", "blur": "fundo desfocado + vídeo no meio",
                                "facecam": "câmera em cima, jogo embaixo", "crop": "recorte central 9:16",
                                "meme": "fundo preto, gancho grande em cima, vídeo no meio"}}

        @tool(description="Configura o @ que vai em todo vídeo e o crédito padrão.")
        async def configurar_pagina(ctx: Context, arroba: str | None = None,
                                    credito_padrao: str | None = None) -> dict:
            user, _ = me(ctx)
            fields = {}
            if arroba is not None:
                fields["handle"] = arroba.strip()[:40] or None
            if credito_padrao is not None:
                fields["credit"] = credito_padrao.strip()[:60] or None
            store.update_user(user["id"], **fields)
            u = store.user_by_token(user["token"])
            return {"arroba": u["handle"], "credito_padrao": u["credit"]}

        @tool(description="Link do painel (upload, preview, download), pasta dos cortes e cota do dia.")
        async def meu_painel(ctx: Context) -> dict:
            user, _ = me(ctx)
            return {"painel": st.painel_link(user), "pasta_dos_cortes": str(worker.export_dir),
                    "cortes_restantes_hoje": st.quota_left(user), "arroba": user["handle"],
                    "contas": worker.connected()}

        @tool(description="Radar de tendência: clips mais vistos da Twitch num jogo nas últimas horas "
                          "(só metadados — para entender o que está viralizando).")
        async def clips_em_alta(ctx: Context, jogo: str = "Grand Theft Auto VI", idioma: str = "pt",
                                horas: int = 24, limite: int = 15) -> dict:
            me(ctx)
            if not (C.env("TWITCH_CLIENT_ID") and C.env("TWITCH_CLIENT_SECRET")):
                raise ValueError("radar indisponível: preencha TWITCH_CLIENT_ID/SECRET no .env")
            from ..sources.twitch import Twitch

            def run():
                tw = Twitch(C.env("TWITCH_CLIENT_ID"), C.env("TWITCH_CLIENT_SECRET"))
                gid = tw.game_ids([jogo]).get(jogo)
                if not gid:
                    raise ValueError(f"jogo não encontrado na Twitch: {jogo}")
                out = []
                for c in tw.top_clips(gid, min(horas, 168), 100):
                    if idioma and c.get("language") != idioma:
                        continue
                    out.append({"titulo": c["title"], "canal": c["broadcaster_name"], "views": c["view_count"],
                                "duracao_s": c["duration"], "criado_em": c["created_at"], "url": c["url"]})
                    if len(out) >= min(limite, 50):
                        break
                return out

            return {"clips": await anyio.to_thread.run_sync(run),
                    "aviso": "Referência de formato/tema. Só corte canais que autorizam clipagem."}

        # ------------------------------------------------------------ publicar
        @tool(description="Quais contas (YouTube, TikTok, Instagram) estão conectadas para publicar.")
        async def contas_conectadas(ctx: Context) -> dict:
            me(ctx)
            pc = st.cfg["publish"]
            conn = worker.connected()
            notas = {}
            if pc["youtube"]["privacy"] != "public":
                notas["youtube"] = "sobe como privado (projeto do Google sem auditoria); publique no YouTube Studio"
            if pc["tiktok"]["mode"] == "inbox":
                notas["tiktok"] = "vai para as notificações/rascunhos do app; finalize e poste pelo celular"
            faltando = {p: f"python -m clipfarm.studio auth {p}" for p, ok in conn.items() if not ok}
            return {"conectadas": conn, "como_conectar": faltando, "observacoes": notas}

        @tool(description="Publica ou agenda um corte pronto no youtube, tiktok e/ou instagram. "
                          "quando: 'AAAA-MM-DD HH:MM' no fuso local (vazio = agora). legenda: texto do post "
                          "(vazio = título + crédito + hashtags).")
        async def publicar_corte(clip_id: int, plataformas: list[str], ctx: Context, legenda: str | None = None,
                                 quando: str | None = None) -> dict:
            user, _ = me(ctx)
            clip = store.clip(clip_id, user["id"])
            if not clip:
                raise ValueError("corte não encontrado")
            if clip["status"] == "failed":
                raise ValueError("este corte falhou no render; ajuste e gere de novo")
            plats = list(dict.fromkeys(p.strip().lower() for p in plataformas))
            bad = [p for p in plats if p not in PLATFORMS]
            if bad or not plats:
                raise ValueError(f"plataformas válidas: {', '.join(PLATFORMS)}")
            conn = worker.connected()
            missing = [p for p in plats if not conn[p]]
            if missing:
                raise ValueError("conecte antes: " + "; ".join(f"python -m clipfarm.studio auth {p}" for p in missing))
            when = st.parse_when(quando)
            day = when.date().isoformat()
            existing = store.list_posts(user["id"], clip_id)
            for p in plats:
                dup = [x for x in existing if x["platform"] == p and x["status"] in ("scheduled", "publishing", "ok")]
                if dup:
                    raise ValueError(f"corte {clip_id} já está agendado/publicado no {p} (post #{dup[0]['id']})")
                cap = st.cfg["publish"][p]["daily_cap"]
                if store.posts_on_day(user["id"], p, day) >= cap:
                    raise ValueError(f"limite de {cap} posts/dia no {p} para {day}; escolha outro dia")
            caption = (legenda or st.default_caption(clip)).strip()
            posts = [store.add_post(user["id"], clip_id, p, caption, when.isoformat(timespec="seconds"))
                     for p in plats]
            out = {"posts": [st.post_view(p) for p in posts]}
            if clip["status"] != "done":
                out["aviso"] = "o corte ainda está renderizando; sai assim que ficar pronto"
            return out

        @tool(description="Status das publicações (todas ou de um corte).")
        async def status_publicacoes(ctx: Context, clip_id: int | None = None) -> dict:
            user, _ = me(ctx)
            return {"posts": [st.post_view(p) for p in store.list_posts(user["id"], clip_id)]}

        @tool(description="Cancela uma publicação agendada que ainda não saiu.")
        async def cancelar_publicacao(post_id: int, ctx: Context) -> dict:
            user, _ = me(ctx)
            p = store.post(post_id, user["id"])
            if not p:
                raise ValueError("post não encontrado")
            if p["status"] != "scheduled":
                raise ValueError(f"não dá para cancelar: status atual é {p['status']}")
            store.set_post(post_id, status="canceled")
            return st.post_view(store.post(post_id))

        @mcp.prompt(name="gerar_cortes", description="Gera cortes virais de um vídeo do começo ao fim.")
        def gerar_cortes(url: str, quantidade: str = "8", estilo: str = "flagra") -> str:
            return (f"Use o ClipFarm para gerar {quantidade} cortes virais deste vídeo: {url}\n"
                    f"Analise, espere ficar pronto, escolha os melhores momentos (gancho nos 2 primeiros segundos, "
                    f"payoff no fim, 15–45s), crie os cortes com estilo '{estilo}', título e gancho em português "
                    f"e me mostre a lista. Depois proponha uma agenda de postagem para os próximos dias no YouTube, "
                    f"TikTok e Instagram e só agende quando eu aprovar.")

        return mcp

    # ================================================================ HTTP
    def http_app(self, with_mcp: bool = True, start_worker: bool = True):
        st, store, sc = self, self.store, self.sc
        mcp_app = None
        if with_mcp:
            mcp_app = self.mcp.streamable_http_app(
                stateless_http=True, json_response=True, host="0.0.0.0",
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

        @contextlib.asynccontextmanager
        async def lifespan(_app):
            if start_worker:
                st.worker.start()
            if with_mcp:
                async with st.mcp.session_manager.run():
                    yield
            else:
                yield
            if start_worker:
                st.worker.stop()

        api = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

        def user_or_404(token: str) -> dict:
            u = store.user_by_token(token)
            if not u:
                raise HTTPException(404)
            return u

        @api.get("/health")
        def health():
            return {"ok": True, "version": __version__}

        @api.get("/u/{token}", response_class=HTMLResponse)
        def painel(token: str):
            user = user_or_404(token)
            return dashboard_html(st, user)

        @api.post("/u/{token}/upload")
        async def upload(token: str, request: Request, arquivo: UploadFile = File(...)):
            user = user_or_404(token)
            limit = sc["upload_max_mb"] * 1024 * 1024
            m = store.add_media(user["id"], "upload", arquivo.filename or "upload", st.base_dir,
                                title=arquivo.filename, status="uploading")
            dest, size = Path(m["dir"]) / "source.mp4", 0
            with dest.open("wb") as f:
                while chunk := await arquivo.read(4 * 1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        f.close()
                        shutil.rmtree(m["dir"], ignore_errors=True)
                        store.set_media(m["id"], status="failed", error="arquivo acima do limite")
                        raise HTTPException(413, f"limite de {sc['upload_max_mb']} MB")
                    f.write(chunk)
            store.set_media(m["id"], status="queued")
            if "text/html" in request.headers.get("accept", ""):
                return RedirectResponse(f"/u/{token}", status_code=303)
            return JSONResponse({"media_id": m["id"], "status": "na fila"})

        def file_of(token: str, cid: int, kind: str):
            user = user_or_404(token)
            c = store.clip(cid, user["id"])
            p = c and (c["path"] if kind == "mp4" else c["thumb"])
            if not p or not Path(p).exists():
                raise HTTPException(404)
            if kind == "mp4":
                return FileResponse(p, filename=Path(p).name, media_type="video/mp4")
            return FileResponse(p, media_type="image/jpeg")

        @api.get("/u/{token}/c/{cid}.mp4")
        def clip_file(token: str, cid: int):
            return file_of(token, cid, "mp4")

        @api.get("/u/{token}/c/{cid}.jpg")
        def clip_thumb(token: str, cid: int):
            return file_of(token, cid, "jpg")

        if mcp_app is not None:
            api.mount("/", mcp_app)  # /mcp
        return TokenPath(api)

    # ================================================================ stdio (Claude Desktop / Cowork)
    def start_painel(self, port: int) -> str | None:
        """Sobe o painel local numa thread (só 127.0.0.1). Se a porta estiver ocupada, segue sem painel."""
        import socket

        import uvicorn

        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                log.warning("porta %s ocupada; painel não iniciado (outro processo já serve?)", port)
                self.painel_url = f"http://127.0.0.1:{port}"
                return self.painel_url
        server = uvicorn.Server(uvicorn.Config(self.http_app(with_mcp=False, start_worker=False), host="127.0.0.1",
                                               port=port, log_level="warning", access_log=False))
        threading.Thread(target=server.run, daemon=True).start()
        self.painel_url = f"http://127.0.0.1:{port}"
        return self.painel_url

    def run_stdio(self, painel_port: int | None = 8765):
        self.worker.start()
        if painel_port:
            self.start_painel(painel_port)
        self.mcp.run("stdio")


def create_app(config_path: str | None = None, transcriber="auto", start_worker: bool = True,
               publishers: dict | None = None):
    """App HTTP com MCP remoto + painel (usado pelo `serve` e pelos testes)."""
    studio = Studio(config_path, transcriber, publishers)
    app = studio.http_app(with_mcp=True, start_worker=start_worker)
    app.studio = studio
    app.state = type("State", (), {"store": studio.store, "studio": studio})()
    return app


# ==================================================================== painel HTML
def dashboard_html(st: Studio, user: dict) -> str:
    e = html.escape
    store = st.store
    tok = user["token"]
    media = store.list_media(user["id"])
    clips = store.list_clips(user["id"], limit=120)
    posts = store.list_posts(user["id"], limit=80)
    pending = any(m["status"] in ("queued", "analyzing", "uploading") for m in media) or any(
        c["status"] in ("queued", "rendering") for c in clips)
    badge = {"done": "ok", "ready": "ok", "ok": "ok", "failed": "err", "canceled": "err"}
    cards = []
    for c in clips:
        s = c["status"]
        body = (f'<video src="/u/{tok}/c/{c["id"]}.mp4" poster="/u/{tok}/c/{c["id"]}.jpg" controls preload="none">'
                f'</video><a class="btn" href="/u/{tok}/c/{c["id"]}.mp4" download>Baixar</a>'
                if s == "done" else f'<div class="ph">{e(s)}</div>')
        cards.append(f'<div class="card">{body}<div class="meta"><span class="b {badge.get(s, "wait")}">#{c["id"]}'
                     f'</span> {e(c["title"] or "")}<br><small>{e(c["hook"] or "")} · {e(c["style"] or "")} · '
                     f'{e(c["layout"] or "")} · {c["end_s"] - c["start_s"]:.0f}s</small>'
                     f'{"<br><small class=err>" + e(c["error"] or "") + "</small>" if s == "failed" else ""}'
                     f'</div></div>')
    mrows = "".join(
        f'<tr><td>#{m["id"]}</td><td>{e(m["title"] or m["source"])}</td><td>{(m["duration_s"] or 0) / 60:.0f} min'
        f'</td><td><span class="b {badge.get(m["status"], "wait")}">{e(m["status"])}</span> '
        f'<small>{e(m["error"] or "")}</small></td></tr>' for m in media)
    prows = "".join(
        f'<tr><td>#{p["id"]}</td><td>corte #{p["clip_id"]}</td><td>{e(p["platform"])}</td>'
        f'<td>{e(st.fmt_local(p["publish_at"]))}</td><td><span class="b {badge.get(p["status"], "wait")}">'
        f'{e(p["status"])}</span> <small>{e(p["error"] or "")}</small></td></tr>' for p in posts)
    conn = " · ".join(f'{k} {"✔" if v else "—"}' for k, v in st.worker.connected().items())
    refresh = '<meta http-equiv="refresh" content="15">' if pending else ""
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{refresh}<title>ClipFarm</title>
<style>
:root{{--bg:#0d0f12;--card:#171a1f;--tx:#e9edf1;--mut:#8b95a1;--ac:#ffd21f;--ok:#2ecc71;--err:#ff5d5d}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 system-ui,sans-serif}}
main{{max-width:1100px;margin:auto;padding:20px 16px}}h1{{font-size:22px;margin:0 0 4px}}h2{{font-size:17px}}
.top{{display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between;align-items:end;margin-bottom:18px}}
.mut,small{{color:var(--mut)}}code{{background:#22262d;padding:2px 6px;border-radius:6px;word-break:break-all}}
form{{background:var(--card);padding:14px;border-radius:12px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}}
.btn,button{{background:var(--ac);color:#111;border:0;border-radius:8px;padding:8px 14px;font-weight:700;
text-decoration:none;cursor:pointer;display:inline-block}}
.tbl{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;margin:8px 0 22px}}
td{{padding:8px;border-bottom:1px solid #232830}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}}
.card{{background:var(--card);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}}
.card video,.ph{{width:100%;aspect-ratio:9/16;background:#000;display:block}}
.ph{{display:flex;align-items:center;justify-content:center;color:var(--mut)}}
.card .btn{{margin:8px;text-align:center}}.meta{{padding:0 10px 10px}}
.b{{font-size:12px;padding:1px 7px;border-radius:99px;background:#2a2f37}}.ok{{color:var(--ok)}}
.err{{color:var(--err)}}.wait{{color:var(--ac)}}
</style></head><body><main>
<div class="top"><div><h1>ClipFarm</h1><div class="mut">{e(user["handle"] or "sem @ configurado")} ·
{st.quota_left(user)} cortes restantes hoje · contas: {e(conn)}</div></div>
<div class="mut">Pasta dos cortes:<br><code>{e(str(st.worker.export_dir))}</code></div></div>
<form action="/u/{tok}/upload" method="post" enctype="multipart/form-data">
<strong>Subir vídeo</strong><input type="file" name="arquivo" accept="video/*" required>
<button type="submit">Enviar para análise</button>
<span class="mut">Depois peça ao Claude: “gere cortes do vídeo #ID”.</span></form>
<h2>Vídeos</h2><div class="tbl"><table>{mrows or '<tr><td class="mut">Nenhum vídeo ainda.</td></tr>'}</table></div>
<h2>Publicações</h2><div class="tbl"><table>{prows or '<tr><td class="mut">Nada agendado.</td></tr>'}</table></div>
<h2>Cortes</h2><div class="grid">{''.join(cards) or '<p class="mut">Nenhum corte ainda.</p>'}</div>
</main></body></html>"""
