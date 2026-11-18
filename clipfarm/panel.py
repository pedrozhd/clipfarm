"""Painel local do modo automático: ver os cortes, simular a prévia no celular, copiar a legenda e marcar postados.

Só escuta em 127.0.0.1. `python -m clipfarm painel`
"""
from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

PLATFORMS = ("tiktok", "instagram", "youtube")


class Mark(BaseModel):
    platform: str
    posted: bool


def create_app(ctx) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    lock = threading.Lock()  # uma conexão SQLite para as threads do servidor

    @app.get("/", response_class=HTMLResponse)
    def index():
        return PAGE.replace("__HANDLE__", ctx.cfg["render"].get("handle") or "")

    @app.get("/api/clips")
    def clips():
        with lock:
            rows, posted = ctx.db.clips(), ctx.db.posted_map()
        return [{"id": c["id"], "channel": c["channel"], "title": c["title"], "caption": c["description"],
                 "seconds": round((c["end_s"] or 0) - (c["start_s"] or 0)), "score": c["score"],
                 "created": c["created_at"], "file": Path(c["path"]).name, "exists": Path(c["path"]).exists(),
                 "posted": posted.get(c["id"], [])} for c in rows]

    @app.get("/v/{cid}.mp4")
    def video(cid: int):
        with lock:
            row = ctx.db.conn.execute("SELECT path FROM clips WHERE id=?", (cid,)).fetchone()
        if not row or not Path(row["path"]).exists():
            raise HTTPException(404)
        return FileResponse(row["path"], media_type="video/mp4", filename=Path(row["path"]).name)

    @app.post("/api/clips/{cid}/posted")
    def mark(cid: int, m: Mark):
        if m.platform not in PLATFORMS:
            raise HTTPException(400, "plataforma inválida")
        with lock:
            if not ctx.db.conn.execute("SELECT 1 FROM clips WHERE id=?", (cid,)).fetchone():
                raise HTTPException(404)
            if m.posted:
                ctx.db.add_post(cid, m.platform, "manual", "ok")
            else:
                ctx.db.remove_post(cid, m.platform)
            return {"posted": ctx.db.posted_map().get(cid, [])}

    return app


PAGE = r"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flagra Neon · cortes</title>
<style>
:root{--night:#0D0B1A;--surface:#17132B;--line:#2C2547;--ink:#F4F1FF;--muted:#A99FC9;--mag:#FF2D87;--cyan:#19E3FF;--orange:#FF9F1C;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--night);color:var(--ink);font:15px/1.5 Inter,system-ui,"Segoe UI",sans-serif}
header{display:flex;flex-wrap:wrap;gap:16px;align-items:center;justify-content:space-between;padding:20px 24px;border-bottom:1px solid var(--line)}
h1{margin:0;font:400 26px/1 Anton,Impact,sans-serif;text-transform:uppercase;letter-spacing:.02em;display:flex;align-items:center;gap:10px}
h1 i{width:12px;height:12px;border-radius:50%;background:var(--mag);box-shadow:0 0 10px var(--mag)}
h1 b{font-weight:400;color:var(--cyan)}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
button{font:inherit;color:inherit;cursor:pointer;border:1px solid var(--line);background:var(--surface);border-radius:8px;padding:6px 12px}
button:hover{border-color:var(--muted)}
button:focus-visible,video:focus-visible{outline:2px solid var(--cyan);outline-offset:2px}
.tabs button[aria-pressed=true]{background:var(--mag);border-color:var(--mag);color:#fff;font-weight:600}
.count{color:var(--muted);font-variant-numeric:tabular-nums}
main{padding:24px;display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:20px}
.empty{grid-column:1/-1;color:var(--muted);padding:48px 0;text-align:center}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
.card.done{opacity:.55}
.thumb{position:relative;aspect-ratio:9/16;background:#000;cursor:pointer;border:0;padding:0;border-radius:0;display:block;width:100%}
.thumb video{width:100%;height:100%;object-fit:cover;display:block}
.thumb span{position:absolute;right:8px;bottom:8px;background:rgba(13,11,26,.8);padding:2px 8px;border-radius:999px;font-size:12px;font-variant-numeric:tabular-nums}
.thumb em{position:absolute;left:8px;top:8px;background:var(--mag);color:#fff;font-style:normal;font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;text-transform:uppercase;letter-spacing:.06em}
.body{padding:12px 14px 14px;display:grid;gap:10px}
.title{font-weight:600;line-height:1.3;overflow-wrap:anywhere}
.meta{color:var(--muted);font-size:13px}
.caption{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12.5px;color:var(--muted);background:var(--night);border:1px solid var(--line);border-radius:8px;padding:8px 10px;max-height:92px;overflow:auto;margin:0;font-family:inherit}
.row{display:flex;gap:6px;flex-wrap:wrap}
.row button{font-size:13px;padding:5px 10px}
.plat[aria-pressed=true]{background:rgba(25,227,255,.14);border-color:var(--cyan);color:var(--cyan);font-weight:600}
a.dl{color:var(--muted);font-size:13px;align-self:center;margin-left:auto}
dialog{border:0;background:transparent;padding:0;max-width:none;max-height:none;color:var(--ink)}
dialog::backdrop{background:rgba(5,4,12,.86)}
.sim{display:flex;gap:24px;align-items:center;flex-wrap:wrap;justify-content:center}
.phone{position:relative;height:min(86vh,800px);aspect-ratio:9/19.5;background:#000;border-radius:38px;border:8px solid #26223a;overflow:hidden;display:flex;align-items:center}
.phone video{width:100%;aspect-ratio:9/16;object-fit:cover;display:block}
.ui{position:absolute;inset:0;pointer-events:none;font-size:13px;color:#fff;text-shadow:0 1px 3px rgba(0,0,0,.8)}
.ui[hidden]{display:none}
.ui .top{position:absolute;top:5%;left:0;right:0;text-align:center;font-weight:600;opacity:.9}
.ui .rail{position:absolute;right:3%;bottom:14%;display:grid;gap:18px;justify-items:center;font-size:11px}
.ui .rail div::before{content:"";display:block;width:34px;height:34px;border-radius:50%;background:rgba(255,255,255,.85);margin:0 auto 3px}
.ui .cap{position:absolute;left:4%;right:20%;bottom:9%;display:grid;gap:4px}
.ui .cap b{font-weight:700}
.ui .cap span{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ui .nav{position:absolute;left:0;right:0;bottom:0;height:6.5%;background:rgba(0,0,0,.72)}
.side{display:grid;gap:10px;max-width:280px}
.side p{margin:0;color:var(--muted);font-size:13px}
</style></head><body>
<header>
  <h1><i></i>Flagra <b>Neon</b></h1>
  <div class="tabs" id="tabs">
    <button data-f="todo" aria-pressed="true">A postar</button>
    <button data-f="done" aria-pressed="false">Postados</button>
    <button data-f="all" aria-pressed="false">Todos</button>
  </div>
  <span class="count" id="count"></span>
</header>
<main id="grid"></main>
<dialog id="dlg"><div class="sim">
  <div class="phone"><video id="pv" controls loop playsinline></video>
    <div class="ui" id="ui"><div class="top" id="uitop"></div>
      <div class="rail"><div>curtir</div><div>comentar</div><div>salvar</div><div>enviar</div></div>
      <div class="cap"><b id="uih"></b><span id="uic"></span></div><div class="nav"></div></div>
  </div>
  <div class="side">
    <div class="row" id="simtabs"><button data-p="tiktok" aria-pressed="true">TikTok</button><button data-p="instagram" aria-pressed="false">Reels</button><button data-p="youtube" aria-pressed="false">Shorts</button><button data-p="" aria-pressed="false">Sem interface</button></div>
    <p>Simulação aproximada das áreas cobertas pela interface de cada app. Serve para ver se a manchete, a legenda e o @ ficam visíveis; a posição real varia por aparelho.</p>
    <button id="close">Fechar</button>
  </div>
</div></dialog>
<script>
const HANDLE = "__HANDLE__", PL = {tiktok: "TikTok", instagram: "Reels", youtube: "Shorts"};
const TOP = {tiktok: "Seguindo   |   Para você", instagram: "Reels", youtube: "Shorts"};
let clips = [], filter = "todo";
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const done = (c) => Object.keys(PL).every((p) => c.posted.includes(p));

async function load() {
  try { clips = await (await fetch("/api/clips")).json(); } catch { return; }
  draw();
}
function draw() {
  const grid = $("grid"), show = clips.filter((c) => filter === "all" || (filter === "done") === done(c));
  $("count").textContent = clips.filter((c) => !done(c)).length + " a postar · " + clips.length + " cortes";
  grid.replaceChildren();
  if (!show.length) grid.append(el("div", "empty", filter === "todo" ? "Nada para postar. Os cortes novos aparecem aqui sozinhos." : "Nenhum corte nesta lista."));
  for (const c of show) {
    const card = el("article", "card" + (done(c) ? " done" : ""));
    const th = el("button", "thumb"); th.setAttribute("aria-label", "Ver prévia: " + c.title);
    if (c.exists) { const v = el("video"); v.src = "/v/" + c.id + ".mp4#t=1"; v.preload = "metadata"; v.muted = true; th.append(v); }
    th.append(el("span", "", c.seconds + "s"));
    if (c.posted.length) th.append(el("em", "", c.posted.length + "/3 postado"));
    th.onclick = () => c.exists && openSim(c);
    const body = el("div", "body");
    body.append(el("div", "title", c.title), el("div", "meta", c.channel + " · nota " + (c.score ?? 0).toFixed(1) + (c.exists ? "" : " · arquivo não encontrado")));
    body.append(el("pre", "caption", c.caption));
    const r1 = el("div", "row"), copy = el("button", "", "Copiar legenda");
    copy.onclick = async () => { try { await navigator.clipboard.writeText(c.caption); copy.textContent = "Copiado"; } catch { copy.textContent = "Selecione e copie o texto acima"; } setTimeout(() => copy.textContent = "Copiar legenda", 1500); };
    r1.append(copy);
    if (c.exists) { const a = el("a", "dl", "Baixar"); a.href = "/v/" + c.id + ".mp4"; a.download = c.file; r1.append(a); }
    const r2 = el("div", "row");
    for (const p of Object.keys(PL)) {
      const b = el("button", "plat", PL[p]); b.setAttribute("aria-pressed", c.posted.includes(p)); b.title = "Marcar como postado no " + PL[p];
      b.onclick = async () => {
        const res = await fetch("/api/clips/" + c.id + "/posted", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({platform: p, posted: !c.posted.includes(p)})});
        if (res.ok) { c.posted = (await res.json()).posted; draw(); }
      };
      r2.append(b);
    }
    body.append(r1, r2); card.append(th, body); grid.append(card);
  }
}
function setSim(p) {
  for (const b of $("simtabs").children) b.setAttribute("aria-pressed", b.dataset.p === p);
  $("ui").hidden = !p; $("uitop").textContent = TOP[p] || "";
}
function openSim(c) {
  $("pv").src = "/v/" + c.id + ".mp4"; $("uih").textContent = HANDLE; $("uic").textContent = c.caption.split("\n")[0] + " " + (c.caption.match(/#\S+/g) || []).join(" ");
  setSim("tiktok"); $("dlg").showModal(); $("pv").play().catch(() => {});
}
$("simtabs").onclick = (e) => e.target.dataset.p !== undefined && setSim(e.target.dataset.p);
$("close").onclick = () => $("dlg").close();
$("dlg").addEventListener("close", () => $("pv").pause());
$("tabs").onclick = (e) => { if (!e.target.dataset.f) return; filter = e.target.dataset.f; for (const b of $("tabs").children) b.setAttribute("aria-pressed", b === e.target); draw(); };
load(); setInterval(() => { if (!$("dlg").open) load(); }, 30000);
</script></body></html>
"""
