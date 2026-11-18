"""Workers em background: analisa vídeos, renderiza cortes e publica posts agendados (fila no SQLite)."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Callable

from .. import config as C
from ..render import probe, render, thumbnail
from ..signals.transcript import Segment, Word
from . import analyze as A
from .store import Store

log = logging.getLogger("clipfarm.studio")

Transcriber = Callable[..., tuple[list[Segment], list[Word]]]
Publisher = Callable[[dict, dict], str]  # (post, clip) -> id remoto
PLATFORMS = ("youtube", "tiktok", "instagram")


def default_transcriber() -> Transcriber | None:
    import importlib.util

    if importlib.util.find_spec("faster_whisper") is None:
        return None
    from ..signals.transcript import transcribe

    return transcribe


def words_from_segments(segs: list[dict], start: float, end: float) -> list[Word]:
    """Plano B sem Whisper: distribui as palavras de cada segmento ao longo do tempo dele."""
    out = []
    for s in segs:
        if s["end"] <= start or s["start"] >= end:
            continue
        toks = s["text"].split()
        if not toks:
            continue
        step = (s["end"] - s["start"]) / len(toks)
        for i, t in enumerate(toks):
            out.append(Word(s["start"] + i * step, s["start"] + (i + 0.9) * step, t))
    return out


def slug(text: str | None, n: int = 40) -> str:
    t = unicodedata.normalize("NFKD", text or "corte").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:n] or "corte"


class Worker:
    def __init__(self, store: Store, cfg: dict, transcriber: Transcriber | None = "auto",
                 publishers: dict[str, Publisher] | None = None):
        self.store, self.cfg, self.sc = store, cfg, cfg["studio"]
        self.transcriber = default_transcriber() if transcriber == "auto" else transcriber
        self.publishers = publishers or {}
        self.data = Path(cfg["paths"]["data_dir"])
        self.export_dir = Path(self.sc["export_dir"]).expanduser()
        if not self.export_dir.is_absolute():
            self.export_dir = C.ROOT / self.export_dir
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.started = False

    # ---------------------------------------------------------- ciclo
    def start(self):
        if self.started:
            return
        self.started = True
        self.store.reset_running()
        for target in (self._loop_media, self._loop_clips, self._loop_posts):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self):
        self._stop.set()

    def _loop(self, claim, run, on_error, idle_s: float):
        while not self._stop.is_set():
            item = claim()
            if not item:
                time.sleep(idle_s)
                continue
            try:
                run(item)
            except Exception as e:  # noqa: BLE001 — um item ruim não derruba a fila
                log.exception("falha em %s", item.get("id"))
                on_error(item, str(e)[:500])

    def _loop_media(self):
        self._loop(self.store.claim_media, self.analyze,
                   lambda m, err: self.store.set_media(m["id"], status="failed", error=err), 1)

    def _loop_clips(self):
        self._loop(self.store.claim_clip, self.render_clip,
                   lambda c, err: self.store.set_clip(c["id"], status="failed", error=err), 1)

    def _loop_posts(self):
        self._loop(self.store.claim_post, self.publish,
                   lambda p, err: self.store.set_post(p["id"], status="failed", error=err), 5)

    # ---------------------------------------------------------- análise
    def _transcribe(self, path: str, model: str) -> tuple[list[Segment], list[Word]] | None:
        if not self.transcriber:
            return None
        w = self.cfg["whisper"]
        return self.transcriber(path, self.cfg["market"]["whisper_language"], model, w["device"], w["compute_type"])

    def analyze(self, m: dict):
        d = Path(m["dir"])
        lang = self.cfg["market"]["whisper_language"]
        segs, source = None, None
        if m["kind"] == "url":
            meta = A.metadata(m["source"])
            if meta.get("is_live"):
                raise A.SourceError("A live ainda está no ar; envie o link do VOD quando terminar.")
            if meta.get("duration") and meta["duration"] > self.sc["max_duration_min"] * 60:
                raise A.SourceError(f"Vídeo passa do limite de {self.sc['max_duration_min']} min.")
            self.store.set_media(m["id"], title=meta.get("title"), uploader=meta.get("uploader"),
                                 duration_s=meta.get("duration"))
            audio_path = A.fetch_audio(m["source"], d)
            if "youtube" in (meta.get("extractor") or "").lower():
                segs = A.youtube_captions(m["source"], d, lang)
                source = "legenda do YouTube" if segs else None
        else:
            audio_path = A.extract_audio(d / "source.mp4", d / "audio.m4a")
        duration = probe(str(audio_path))["duration"]
        if segs is None:
            res = self._transcribe(str(audio_path), self.sc["analysis_model"])
            segs, source = (res[0], f"whisper {self.sc['analysis_model']}") if res else ([], "sem transcrição")
        cands = A.find_candidates(str(audio_path), segs, duration, n=self.sc["candidates"])
        analysis = {"segments": [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text} for s in segs],
                    "candidates": cands}
        (d / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False))
        self.store.set_media(m["id"], status="ready", duration_s=duration, transcript_source=source, error=None)
        log.info("mídia %s analisada: %.0fs, %d candidatos (%s)", m["id"], duration, len(cands), source)

    # ---------------------------------------------------------- render
    def render_clip(self, c: dict):
        m = self.store.media(c["media_id"])
        user = self.store._one("SELECT * FROM users WHERE id=?", (c["user_id"],))
        d = Path(m["dir"])
        tmp_section = None
        if m["kind"] == "upload":
            src, offset = d / "source.mp4", 0.0
        else:
            offset = float(max(0, int(c["start_s"] - 1)))
            tmp_section = d / f"sec_{c['id']}.mp4"
            A.fetch_section(m["source"], offset, c["end_s"] + 1, tmp_section)
            src = tmp_section
        start, end = c["start_s"] - offset, c["end_s"] - offset

        # legenda: Whisper melhor no trecho curto; sem Whisper, usa a transcrição da análise
        res = self._transcribe(str(src), self.sc["caption_model"])
        if res:
            words = res[1]
        else:
            analysis = json.loads((d / "analysis.json").read_text()) if (d / "analysis.json").exists() else {}
            words = [Word(w.start - offset, w.end - offset, w.text)
                     for w in words_from_segments(analysis.get("segments", []), c["start_s"], c["end_s"])]

        out = self.export_dir / f"{c['id']:04d}_{slug(c['title'])}.mp4"
        render(str(src), str(out), start, end, words, self.cfg["render"], c["layout"] or "auto",
               json.loads(c["facecam"]) if c["facecam"] else None, c["credit"] or None,
               hook=c["hook"] or None, handle=(user or {}).get("handle"), style=c["style"] or "flagra")
        thumbs = d / "thumbs"
        thumbs.mkdir(exist_ok=True)
        thumb = thumbnail(str(out), str(thumbs / f"{c['id']}.jpg"), at=min(1.5, (end - start) / 2))
        self.store.set_clip(c["id"], status="done", path=str(out), thumb=thumb, error=None)
        if tmp_section:
            tmp_section.unlink(missing_ok=True)
        log.info("corte %s pronto: %s", c["id"], out.name)

    # ---------------------------------------------------------- publicação
    def connected(self) -> dict[str, bool]:
        return {
            "youtube": "youtube" in self.publishers or (self.data / "youtube_token.json").exists(),
            "tiktok": "tiktok" in self.publishers or (self.data / "tiktok_token.json").exists(),
            "instagram": "instagram" in self.publishers or (self.data / "instagram_token.json").exists(),
        }

    def publish(self, p: dict):
        clip = self.store.clip(p["clip_id"])
        if p["platform"] in self.publishers:
            rid = self.publishers[p["platform"]](p, clip)
        else:
            rid = self._publish_real(p, clip)
        self.store.set_post(p["id"], status="ok", remote_id=str(rid), error=None)
        log.info("post %s publicado no %s: %s", p["id"], p["platform"], rid)

    def _publish_real(self, p: dict, clip: dict) -> str:
        pc, plat = self.cfg["publish"], p["platform"]
        if plat == "youtube":
            from ..publish import youtube

            tok = self.data / "youtube_token.json"
            if not tok.exists():
                raise RuntimeError("YouTube não conectado: rode `python -m clipfarm.studio auth youtube`.")
            svc = youtube.service(C.env_path("YOUTUBE_CLIENT_SECRETS", True), str(tok))
            title = (clip["title"] or p["caption"].splitlines()[0])[:95]
            if "#shorts" not in p["caption"].lower():
                title = title[:90] + " #shorts"
            return youtube.upload(svc, clip["path"], title, p["caption"], pc["youtube"]["tags"],
                                  pc["youtube"]["category_id"], pc["youtube"]["privacy"])
        if plat == "tiktok":
            from ..publish import tiktok

            tok = self.data / "tiktok_token.json"
            if not tok.exists():
                raise RuntimeError("TikTok não conectado: rode `python -m clipfarm.studio auth tiktok`.")
            return tiktok.post(tiktok.access_token(str(tok)), clip["path"], p["caption"],
                               pc["tiktok"]["mode"], pc["tiktok"]["privacy"])
        if plat == "instagram":
            from ..publish import instagram

            acc = instagram.load(str(self.data / "instagram_token.json"))
            return instagram.post(acc["access_token"], acc["ig_user_id"], clip["path"], p["caption"],
                                  share_to_feed=pc.get("instagram", {}).get("share_to_feed", True))
        raise RuntimeError(f"plataforma desconhecida: {plat}")
