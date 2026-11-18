"""Download de clips e trechos de VOD com yt-dlp (só baixa a janela necessária)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _fmt(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def fetch_url(url: str, out: Path) -> Path:
    """Clip pronto (ex.: https://clips.twitch.tv/...) ou arquivo local (file:// ou caminho)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if url.startswith("file://") or Path(url).exists():
        shutil.copy(url.removeprefix("file://"), out)
        return out
    _ytdlp([url, "-o", str(out)])
    return out


def fetch_vod_section(vod_id: str, start: float, end: float, out: Path, platform: str = "twitch") -> Path:
    url = f"https://www.twitch.tv/videos/{vod_id}" if platform == "twitch" else vod_id
    # sem --force-keyframes-at-cuts: ele recodifica o trecho e a facecam saía em blocos; o início cai
    # no keyframe anterior (alguns segundos antes) e o pico exato é reencontrado pelo áudio
    _ytdlp([url, "--download-sections", f"*{_fmt(start)}-{_fmt(end)}", "-o", str(out)])
    return out


def _ytdlp(args: list[str]):
    cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "-f", "best[height<=1080]/best",
           "--merge-output-format", "mp4", "--remux-video", "mp4", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp falhou: {r.stderr.strip()[-400:]}")
