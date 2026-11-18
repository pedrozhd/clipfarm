"""Análise de vídeo longo (VOD, live gravada, podcast, upload): acha candidatos a corte.

Estratégia barata:
  1. baixa SÓ o áudio (ou extrai do upload)
  2. transcrição: legenda automática do YouTube quando existir; senão Whisper (modelo rápido)
  3. curva de volume -> picos (grito, risada, explosão)
  4. cada pico vira candidato com trecho da fala; quem decide o corte final é o Claude, no chat
O vídeo em si só é baixado depois, e só os trechos escolhidos.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

from ..signals import audio
from ..signals.transcript import Segment

ALLOWED_HOSTS = ("youtube.com", "youtu.be", "twitch.tv", "kick.com")

HYPE_WORDS = re.compile(
    r"\b(n[ãa]o acredito|meu deus|caralho|que isso|olha isso|mano|corre|socorro|morri|kkk+|hahaha+|"
    r"wtf|oh my god|no way|bro|what|let'?s go|vai vai|nossa|puta|eita|ai meu)\b", re.I)


class SourceError(ValueError):
    pass


def check_url(url: str, allowed: tuple[str, ...] = ALLOWED_HOSTS) -> str:
    """Só aceita plataformas conhecidas (evita SSRF e uso do yt-dlp como proxy genérico)."""
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not any(host == h or host.endswith("." + h) for h in allowed):
        raise SourceError(f"URL não suportada. Aceito: {', '.join(allowed)}")
    return url.strip()


def _ytdlp(args: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    r = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist",
                        "--use-extractors", "youtube.*,twitch.*,kick.*", *args], capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp: {r.stderr.strip()[-400:]}")
    return r


def metadata(url: str) -> dict:
    d = json.loads(_ytdlp(["-J", "--", url], timeout=120).stdout)
    return {"title": d.get("title"), "duration": d.get("duration"), "uploader": d.get("uploader"),
            "webpage_url": d.get("webpage_url", url), "extractor": d.get("extractor_key"),
            "is_live": d.get("is_live")}


def fetch_audio(url: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    _ytdlp(["-f", "bestaudio/best", "-x", "--audio-format", "m4a", "-o", str(out_dir / "audio.%(ext)s"), "--", url])
    return out_dir / "audio.m4a"


def fetch_section(url: str, start: float, end: float, out: Path) -> Path:
    """Baixa só o trecho [start, end] do vídeo em até 1080p."""
    def fmt(t):
        t = max(0, int(t))
        return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"

    _ytdlp(["-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
            "--download-sections", f"*{fmt(start)}-{fmt(end + 1)}", "--force-keyframes-at-cuts",
            "-o", str(out), "--", url])
    return out


def extract_audio(video: Path, out: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "aac", "-b:a", "64k", str(out)], check=True, stdout=subprocess.DEVNULL)
    return out


# ---------------------------------------------------------------- legenda automática (YouTube)
_TS = r"(\d+):(\d{2}):(\d{2})\.(\d{3})"


def _sec(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_vtt(text: str) -> list[Segment]:
    """Converte VTT (inclusive o 'rolante' do YouTube) em segmentos sem repetição."""
    segs: list[Segment] = []
    last = ""
    for block in re.split(r"\n\s*\n", text):
        m = re.search(_TS + r"\s+-->\s+" + _TS, block)
        if not m:
            continue
        start, end = _sec(*m.groups()[:4]), _sec(*m.groups()[4:])
        body = block[m.end():].strip().splitlines()
        lines = [re.sub(r"<[^>]+>", "", ln).strip() for ln in body]
        lines = [ln for ln in lines if ln]
        if not lines:
            continue
        txt = lines[-1]  # no VTT rolante a última linha é a nova
        if txt == last or (last and txt in last):
            continue
        if last and txt.startswith(last):
            txt = txt[len(last):].strip()
        last = lines[-1]
        if txt:
            segs.append(Segment(start, end, txt))
    return segs


def youtube_captions(url: str, out_dir: Path, lang: str) -> list[Segment] | None:
    try:
        _ytdlp(["--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", f"{lang}.*,{lang}",
                "--sub-format", "vtt", "-o", str(out_dir / "subs.%(ext)s"), "--", url], timeout=120)
    except Exception:  # noqa: BLE001
        return None
    files = sorted(out_dir.glob("subs*.vtt"))
    if not files:
        return None
    segs = parse_vtt(files[0].read_text(encoding="utf-8", errors="ignore"))
    return segs or None


# ---------------------------------------------------------------- candidatos
def find_candidates(audio_path: str, segments: list[Segment], duration: float, n: int = 25,
                    min_gap_s: float = 40, pre_s: float = 22, post_s: float = 10) -> list[dict]:
    times, db = audio.loudness_curve(audio_path, hop_s=0.5, win_s=1.0)
    curve = audio.excitement(times, db, smooth_s=3.0)

    # bônus por fala "exaltada" perto do ponto
    bonus = np.zeros_like(curve)
    for s in segments:
        hits = len(HYPE_WORDS.findall(s.text))
        if hits:
            mask = (times >= s.start - 2) & (times <= s.end + 2)
            bonus[mask] += 0.6 * min(hits, 3)
    score = curve + bonus

    picked: list[int] = []
    for i in np.argsort(-score):
        t = times[i]
        if t < 8 or t > duration - 3:
            continue
        if all(abs(t - times[j]) >= min_gap_s for j in picked):
            picked.append(int(i))
        if len(picked) >= n:
            break

    out = []
    for rank, i in enumerate(sorted(picked, key=lambda j: -score[j]), 1):
        peak = float(times[i])
        start, end = max(0.0, peak - pre_s), min(duration, peak + post_s)
        excerpt = " ".join(s.text for s in segments if s.end > start - 3 and s.start < end + 2)
        out.append({"rank": rank, "pico_s": round(peak, 1), "inicio_s": round(start, 1), "fim_s": round(end, 1),
                    "intensidade": round(float(score[i]), 2), "fala": excerpt[:600]})
    return out


def transcript_window(segments: list[dict], start: float, end: float, max_chars: int = 6000) -> str:
    rows = [f"[{s['start']:.0f}s] {s['text']}" for s in segments if s["end"] > start and s["start"] < end]
    text = "\n".join(rows)
    return text[:max_chars] + ("\n…(cortado; peça um intervalo menor)" if len(text) > max_chars else "")
