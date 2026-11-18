"""Orquestração: harvest (clips prontos) / watch (picos de chat) -> process (corte+render) -> publish."""
from __future__ import annotations

import json
import logging
import math
import os
import random
import shutil
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from . import config as C
from .db import DB
from .download import fetch_url, fetch_vod_section
from .render import probe, render
from .scoring import choose_window, end_at_pause, heuristic_title
from .signals import audio
from .signals.chat import SpikeDetector, TwitchChat
from .signals.transcript import text_between, transcribe

log = logging.getLogger("clipfarm")


class Ctx:
    def __init__(self, config_path: str | None = None):
        self.cfg = C.load(config_path)
        self.data = Path(self.cfg["paths"]["data_dir"])
        self.db = DB(self.data / "clipfarm.db")
        self._twitch = None
        self._allowed_ids: set[str] | None = None

    @property
    def twitch(self):
        if self._twitch is None:
            from .sources.twitch import Twitch

            self._twitch = Twitch(C.env("TWITCH_CLIENT_ID", True), C.env("TWITCH_CLIENT_SECRET", True))
        return self._twitch

    def allowed_ids(self) -> set[str] | None:
        """IDs Twitch da allowlist (None = sem allowlist)."""
        rules = C.channel_rules(self.cfg)
        if not rules:
            return None
        if self._allowed_ids is None:
            self._allowed_ids = {u["id"] for u in self.twitch.users(list(rules)).values()}
        return self._allowed_ids


# ======================================================================= DISCOVER
def discover(ctx: Ctx) -> list[dict]:
    tc = ctx.cfg["sources"]["twitch"]
    out = []
    for name, gid in ctx.twitch.game_ids(tc["games"]).items():
        for s in ctx.twitch.top_streams(gid, ctx.cfg["market"]["languages"], tc["top_streams"], tc["min_viewers"]):
            s["game"] = name
            s["allowed"] = C.is_allowed(ctx.cfg, s["user_login"])
            out.append(s)
    return out


# ======================================================================= HARVEST (clips da Twitch)
def harvest(ctx: Ctx) -> int:
    """Puxa os clips mais vistos do jogo: a própria audiência já marcou os melhores momentos."""
    tc, langs = ctx.cfg["sources"]["twitch"], set(ctx.cfg["market"]["languages"] or [])
    allowed, added = ctx.allowed_ids(), 0
    blocked = {b.lower() for b in (ctx.cfg.get("blocklist") or [])}
    for name, gid in ctx.twitch.game_ids(tc["games"]).items():
        for c in ctx.twitch.top_clips(gid, tc["clips_lookback_hours"], max(1000, tc["clips_max_per_run"] * 4)):
            if c["view_count"] < tc["clips_min_views"]:
                continue  # a ordem por views da Twitch não é exata: parar aqui perdia clips bons mais adiante
            if langs and c.get("language") not in langs:
                continue
            if allowed is not None and c["broadcaster_id"] not in allowed:
                continue
            login = c["broadcaster_name"].lower()
            if login in blocked:
                continue
            off = c.get("vod_offset")
            mid = ctx.db.add_moment(
                source="twitch_clip", platform="twitch", channel=login,
                vod_id=c.get("video_id") or None,
                # na prática o vod_offset da Twitch marca o FIM do clip no VOD (conferido em frames), não o início
                vod_offset_s=(off - c["duration"] / 2) if off is not None else None,
                media_url=c["url"], duration_s=c["duration"],
                score=round(3 * math.log10(c["view_count"] + 1), 2),
                signals={"kind": "viewer_clip", "views": c["view_count"], "clip_title": c["title"],
                         "game": name, "created_at": c["created_at"]},
                title_hint=c["title"], external_id=f"twitch_clip:{c['id']}",
                status="new" if c.get("video_id") else "no_vod")  # sem VOD só há o clip com legenda embutida
            added += mid is not None
            if added >= tc["clips_max_per_run"]:
                return added
    log.info("harvest: %d momentos novos", added)
    return added


# ======================================================================= WATCH (chat ao vivo)
def watch(ctx: Ctx, minutes: float | None = None):
    """Escuta o chat das maiores lives e registra picos de emoção como momentos."""
    det_cfg = ctx.cfg["detection"]
    detectors: dict[str, SpikeDetector] = {}
    meta: dict[str, dict] = {}
    chat, last_refresh = TwitchChat(), 0.0
    deadline = time.time() + minutes * 60 if minutes else None

    def refresh():
        nonlocal last_refresh
        live = {s["user_login"].lower(): s for s in discover(ctx) if s["allowed"]}
        for login in list(chat.joined - set(live)):
            chat.part(login)
            detectors.pop(login, None)
        for login, s in live.items():
            if login not in chat.joined:
                chat.join(login)
                detectors[login] = SpikeDetector(login, **det_cfg)
            meta.setdefault(login, {})["user_id"] = s["user_id"]
        last_refresh = time.time()
        log.info("watch: %d canais: %s", len(live), ", ".join(list(live)[:10]))

    def vod_for(login: str) -> dict | None:
        m = meta[login]
        if "vod" not in m or time.time() - m.get("vod_at", 0) > 600:
            m["vod"], m["vod_at"] = ctx.twitch.archive_vod(m["user_id"]), time.time()
        return m["vod"]

    refresh()
    while deadline is None or time.time() < deadline:
        if time.time() - last_refresh > 300:
            refresh()
        try:
            for ch, text, ts in chat.messages(1.0):
                det = detectors.get(ch)
                spike = det.add(ts, text) if det else None
                if not spike:
                    continue
                vod = vod_for(ch)
                from .sources.twitch import parse_ts

                offset = None
                if vod:
                    offset = spike.ts - parse_ts(vod["created_at"]).timestamp() \
                        - det_cfg["chat_delay_s"] - det_cfg["window_s"] / 2
                mid = ctx.db.add_moment(
                    source="chat_spike", platform="twitch", channel=ch,
                    vod_id=vod["id"] if vod else None, vod_offset_s=offset, score=spike.score,
                    signals=asdict(spike), external_id=f"spike:{ch}:{int(spike.ts)}",
                    status="new" if vod else "no_vod")
                log.info("PICO %s [%s] z=%.1f msgs=%d risos=%.0f%% -> momento %s",
                         ch, spike.kind, spike.z, spike.msgs, spike.laugh_ratio * 100, mid)
        except (ConnectionError, OSError) as e:
            log.warning("chat caiu (%s), reconectando…", e)
            time.sleep(5)
            chat = TwitchChat()
            refresh()


# ======================================================================= PROCESS
def process(ctx: Ctx, limit: int = 10) -> list[int]:
    done = []
    for m in ctx.db.pending_moments(limit, ctx.cfg["clip"]["process_delay_s"]):
        try:
            cid = process_moment(ctx, m)
            if cid:
                done.append(cid)
        except Exception as e:  # noqa: BLE001 — um momento ruim não derruba o lote
            log.exception("momento %s falhou", m["id"])
            ctx.db.set_moment(m["id"], "failed", str(e)[:500])
    return done


def process_moment(ctx: Ctx, m, transcriber=None) -> int | None:
    cfg, cc = ctx.cfg, ctx.cfg["clip"]
    sig = json.loads(m["signals"] or "{}")
    raw = ctx.data / "raw" / f"moment_{m['id']}.mp4"

    # 1) baixa só o trecho necessário
    if m["source"] == "chat_spike":
        pad = cc["download_pad_s"]
        dl_start = max(0.0, m["vod_offset_s"] - pad)
        fetch_vod_section(m["vod_id"], dl_start, m["vod_offset_s"] + pad * 0.5, raw)
        expected = m["vod_offset_s"] - dl_start
        search = (expected - 15, expected + 8)
    else:
        if m["source"] == "twitch_clip" and m["vod_id"] and m["vod_offset_s"] is not None:
            # o clip pronto da Twitch vem com legenda automática embutida; o mesmo trecho do VOD vem limpo
            half = (m["duration_s"] or 30) / 2 + 2
            # 15s a mais no fim: quem clipa para logo após o momento, e a reação do streamer vem em seguida
            fetch_vod_section(m["vod_id"], max(0.0, m["vod_offset_s"] - half), m["vod_offset_s"] + half + 15, raw)
        else:
            fetch_url(m["media_url"], raw)
        dur0 = probe(str(raw))["duration"]
        expected, search = None, (dur0 * 0.3, dur0)
    duration = probe(str(raw))["duration"]

    # 2) pico de áudio (grito/risada/explosão) dentro da região esperada
    times, db = audio.loudness_curve(str(raw))
    curve = audio.excitement(times, db)
    peak_t, peak_val = audio.find_peak(times, curve, *search)

    # 3) transcrição palavra-a-palavra
    segments, words = [], []
    if cfg["whisper"]["enabled"] or transcriber:
        w = cfg["whisper"]
        segments, words = (transcriber or transcribe)(str(raw), cfg["market"]["whisper_language"],
                                                      w["model"], w["device"], w["compute_type"],
                                                      prompt=w.get("prompt"), fixes=w.get("fixes"))

    # 4) janela final
    if m["source"] != "chat_spike" and duration <= cc["max_len_s"]:
        start, end = 0.0, duration  # clip feito pela audiência: já vem na medida
    else:
        start, end = choose_window(duration, peak_t, segments, cc["pre_s"], cc["post_s"],
                                   cc["min_len_s"], cc["max_len_s"])

    # 4b) só o trecho em que a facecam aparece o tempo todo (ela some em cutscene e volta com fade)
    rules = C.channel_rules(cfg).get(m["channel"].lower(), {})
    facecam, lo, hi = rules.get("facecam"), 0.0, duration
    if cfg["llm"]["enabled"] and cfg["llm"].get("find_facecam", True):
        run = facecam_run(str(raw), start, end, cfg["llm"]["model"])
        if run is not None:  # None = não deu para saber: segue com a posição fixa do config
            cam_start, cam_end, seen = run
            if cam_end - cam_start >= cc["min_len_s"]:
                start, end, lo, hi = cam_start, cam_end, cam_start, cam_end
                # meio por cento para dentro: a borda da câmera aparecia como um filete no corte
                facecam = {"x": seen["x"] + 0.005, "y": seen["y"] + 0.006,
                           "w": seen["w"] - 0.010, "h": seen["h"] - 0.012}
            elif cc.get("require_facecam", True):
                ctx.db.set_moment(m["id"], "rejected",
                                  f"facecam visível por só {cam_end - cam_start:.0f}s seguidos neste trecho")
                raw.unlink(missing_ok=True)
                return None
            else:
                facecam = None

    # 5) (opcional) Claude como editor
    kind = sig.get("kind", "reaction")
    title, headline, extra_tags, score = None, None, [], float(m["score"] or 0)
    if cfg["llm"]["enabled"]:
        from .llm import review

        rv = review(m["channel"], kind, sig.get("sample", []), text_between(segments, start - 5, end + 5),
                    start, end, peak_t, cc["max_len_s"], cfg["llm"]["model"],
                    "pt-BR" if cfg["market"]["whisper_language"] == "pt" else cfg["market"]["whisper_language"],
                    frames=[frame_jpeg(str(raw), start + (end - start) * f) for f in (0.15, 0.4, 0.65, 0.9)])
        if rv:
            # clip feito pela audiência já foi escolhido por quem assistiu: o Claude só dá título, não rejeita
            if m["source"] == "chat_spike" and (
                    not rv.get("keep", True) or rv.get("score", 10) < cfg["llm"]["min_score"]):
                ctx.db.set_moment(m["id"], "rejected", f"llm: {rv.get('reason', '')}"[:500])
                raw.unlink(missing_ok=True)
                return None
            title, score = rv.get("title"), rv["score"]
            if min(rv["end"], hi) - max(rv["start"], lo) >= cc["min_len_s"]:  # o Claude não sai do trecho com câmera
                start, end = max(rv["start"], lo), min(rv["end"], hi)
            headline = (rv.get("headline") or "")[:60] or None
            extra_tags = [t for t in rv.get("hashtags") or [] if isinstance(t, str) and t.startswith("#")][:3]

    end = end_at_pause(words, end, hi)

    # 6) render 9:16 com legenda + crédito
    credit = rules.get("credit") or f"twitch.tv/{m['channel']}"
    title = title or heuristic_title(m["channel"], kind, m["title_hint"])
    out = ctx.data / "clips" / f"{m['channel']}_{m['id']}.mp4"
    render(str(raw), str(out), start, end, words, cfg["render"], cfg["render"]["layout"],
           facecam or None, credit, hook=headline, handle=cfg["render"].get("handle"), style=cfg["render"].get("style"))

    tags = " ".join(dict.fromkeys(pick_hashtags(cfg["publish"]) + [f"#{m['channel']}"] + extra_tags))
    desc = f"{title}\n\nCréditos: {credit}\n{tags} #shorts"
    out.with_suffix(".txt").write_text(desc, encoding="utf-8")  # legenda pronta para colar ao postar à mão
    export_copies(cfg["publish"], out, m["channel"])
    auto = cfg["review"].get("auto_approve_score")
    status = "approved" if auto is not None and score >= auto else "review"
    cid = ctx.db.add_clip(moment_id=m["id"], path=str(out), start_s=start, end_s=end, title=title,
                          description=desc, score=score, status=status)
    ctx.db.set_moment(m["id"], "rendered")
    raw.unlink(missing_ok=True)
    log.info("clip %s pronto (%s, %.0fs, pico %.1f@%.1fs): %s", cid, m["channel"], end - start, peak_val,
             peak_t, out.name)
    return cid


def longest_cam_run(ts: list[float], looks: list[dict | None], start: float, end: float,
                    tol: float = 0.04) -> tuple[float, float, dict] | None:
    """Maior trecho contínuo com a facecam no mesmo lugar: (início, fim, posição). None se nada pôde ser visto."""
    if all(x is None for x in looks):
        return None
    best, i = (start, start, {}), 0
    while i < len(ts):
        a = looks[i]
        if not a:
            i += 1
            continue
        same = lambda k: k < len(ts) and bool(looks[k]) and abs(a["x"] - looks[k]["x"]) < tol             and abs(a["y"] - looks[k]["y"]) < tol  # noqa: E731
        j = i
        while same(j + 1) or same(j + 2):  # uma amostra fora do padrão entre duas iguais é erro de leitura
            j += 1 if same(j + 1) else 2
        t0 = start if i == 0 else ts[i] + 1.0        # 1s depois: a câmera ainda pode estar entrando em fade
        t1 = end if j == len(ts) - 1 else ts[j]      # para na última amostra em que ela ainda estava lá
        if t1 - t0 > best[1] - best[0]:
            best = (t0, t1, a)
        i = j + 1
    return best


def facecam_run(path: str, start: float, end: float, model: str, step: float = 3.0):
    """Olha um frame a cada `step` segundos e devolve o maior trecho com a câmera visível (ver longest_cam_run)."""
    from concurrent.futures import ThreadPoolExecutor

    from .llm import find_facecam

    ts = [start + step / 2 + i * step for i in range(max(1, int((end - start) // step)))]
    with ThreadPoolExecutor(4) as ex:
        looks = list(ex.map(lambda t: find_facecam(frame_jpeg(path, t), model), ts))
    return longest_cam_run(ts, looks, start, end)


def frame_jpeg(path: str, at: float) -> bytes:
    """Um frame do vídeo em JPEG (720p), para o Claude localizar a facecam."""
    return subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.2f}", "-i", path, "-frames:v", "1",
                           "-vf", "scale=1280:-2", "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                          capture_output=True, check=True).stdout


def export_copies(pc: dict, out: Path, channel: str):
    """Copia o corte e a legenda para <export_dir>/<plataforma>/<canal>/, uma pasta por rede, para postar à mão."""
    if not pc.get("export_dir"):
        return
    for plat in pc.get("export_platforms", ("tiktok", "instagram", "youtube")):
        dest = Path(pc["export_dir"]).expanduser() / plat / channel
        dest.mkdir(parents=True, exist_ok=True)
        for f in (out, out.with_suffix(".txt")):
            (dest / f.name).unlink(missing_ok=True)
            try:
                os.link(f, dest / f.name)  # mesmo arquivo em 3 pastas sem ocupar 3x o disco
            except OSError:  # outro disco ou sistema sem hardlink
                shutil.copy2(f, dest / f.name)


def pick_hashtags(pc: dict) -> list[str]:
    """Sorteia algumas hashtags de cada grupo (jogo, cortes, virais) para variar entre os posts."""
    pools = pc.get("hashtag_pools")
    if not pools:
        return list(pc["hashtags"])
    return [t for p in pools for t in random.sample(p["tags"], min(p["pick"], len(p["tags"])))]


# ======================================================================= PUBLISH
def publish(ctx: Ctx) -> int:
    pc, sent = ctx.cfg["publish"], 0
    clips = ctx.db.clips("approved")
    if not clips:
        return 0
    targets = [p for p in ("youtube", "tiktok") if pc[p]["enabled"]]
    yt = tk = None
    for clip in clips:
        for plat in targets:
            if ctx.db.posted(clip["id"], plat) or ctx.db.posts_today(plat) >= pc[plat]["daily_cap"]:
                continue
            try:
                if plat == "youtube":
                    from .publish import youtube

                    yt = yt or youtube.service(C.env("YOUTUBE_CLIENT_SECRETS", True),
                                               str(ctx.data / "youtube_token.json"))
                    rid = youtube.upload(yt, clip["path"], clip["title"], clip["description"],
                                         pc["youtube"]["tags"], pc["youtube"]["category_id"],
                                         pc["youtube"]["privacy"])
                else:
                    from .publish import tiktok

                    tk = tk or tiktok.access_token(str(ctx.data / "tiktok_token.json"))
                    caption = clip["description"].replace("\n\n", "\n")
                    rid = tiktok.post(tk, clip["path"], caption, pc["tiktok"]["mode"], pc["tiktok"]["privacy"])
                ctx.db.add_post(clip["id"], plat, rid, "ok")
                sent += 1
                log.info("publicado %s no %s: %s", clip["id"], plat, rid)
            except Exception as e:  # noqa: BLE001
                log.exception("falha publicando %s no %s", clip["id"], plat)
                ctx.db.add_post(clip["id"], plat, None, "failed", str(e)[:500])
        if all(ctx.db.posted(clip["id"], p) for p in targets):
            ctx.db.set_clip(clip["id"], "published")
    return sent


# ======================================================================= LOOP
def run(ctx: Ctx, harvest_every_min: float = 30, sleep_s: float = 60):
    last = 0.0
    while True:
        if ctx.cfg["sources"]["twitch"]["enabled"] and time.time() - last > harvest_every_min * 60:
            try:
                harvest(ctx)
            except Exception:  # noqa: BLE001
                log.exception("harvest falhou")
            last = time.time()
        process(ctx)
        publish(ctx)
        time.sleep(sleep_s)
