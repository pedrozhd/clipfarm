"""Sinal de áudio: curva de volume (gritos, risadas, explosões) para achar o pico exato.

Lê o áudio em streaming (memória constante), então funciona tanto para um clip de 60s
quanto para um VOD de 4h.
"""
from __future__ import annotations

import subprocess

import numpy as np

SR = 8000  # suficiente para energia/volume


def loudness_curve(path: str, hop_s: float = 0.25, win_s: float = 0.5, sr: int = SR) -> tuple[np.ndarray, np.ndarray]:
    """Retorna (tempos, dB RMS) a cada `hop_s`, suavizado numa janela de `win_s`."""
    hop = max(1, int(sr * hop_s))
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    energies, rest = [], np.zeros(0, dtype=np.float32)
    chunk_bytes = hop * 2 * 4096
    while True:
        buf = proc.stdout.read(chunk_bytes)
        if not buf:
            break
        x = np.concatenate([rest, np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0])
        n = len(x) // hop
        if n:
            energies.append((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1))
        rest = x[n * hop:]
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg não conseguiu ler o áudio de {path}")
    if not energies:
        return np.array([0.0]), np.array([-90.0])
    e = np.concatenate(energies)
    k = max(1, round(win_s / hop_s))
    if k > 1 and len(e) > k:
        e = np.convolve(e, np.ones(k) / k, mode="same")
    db = 10 * np.log10(e + 1e-12)
    times = (np.arange(len(e)) + 0.5) * hop / sr
    return times, db


def excitement(times: np.ndarray, db: np.ndarray, smooth_s: float = 1.5) -> np.ndarray:
    """Volume suavizado e normalizado (z-score robusto) — picos = momentos mais intensos."""
    if len(db) < 3:
        return np.zeros_like(db)
    step = times[1] - times[0]
    k = max(1, int(smooth_s / step))
    sm = np.convolve(db, np.ones(k) / k, mode="same")
    return (sm - np.median(sm)) / (np.std(sm) + 1e-6)


def find_peak(times: np.ndarray, curve: np.ndarray, lo: float, hi: float) -> tuple[float, float]:
    """Pico da curva entre lo e hi segundos. Retorna (tempo, intensidade)."""
    mask = (times >= lo) & (times <= hi)
    if not mask.any():
        return (lo + hi) / 2, 0.0
    i = int(np.argmax(np.where(mask, curve, -np.inf)))
    return float(times[i]), float(curve[i])
