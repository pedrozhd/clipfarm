"""Escolhe o recorte final: começa antes do pico (contexto), termina logo após (payoff)."""
from __future__ import annotations

from .signals.transcript import Segment


def choose_window(duration: float, peak_t: float, segments: list[Segment], pre_s: float, post_s: float,
                  min_len: float, max_len: float, snap_s: float = 4.0) -> tuple[float, float]:
    start = max(0.0, peak_t - pre_s)
    end = min(duration, peak_t + post_s)

    # Alinha o início ao começo de uma fala e o fim ao final de uma fala (não corta no meio da frase)
    starts = [s.start for s in segments if start - snap_s <= s.start <= start + snap_s / 2]
    if starts:
        start = max(0.0, min(starts, key=lambda t: abs(t - start)) - 0.15)
    ends = [s.end for s in segments if end - snap_s / 2 <= s.end <= end + snap_s]
    if ends:
        end = min(duration, min(ends, key=lambda t: abs(t - end)) + 0.35)

    if end - start > max_len:  # mantém o pico, corta contexto
        start = max(0.0, end - max_len)
    if end - start < min_len:
        pad = (min_len - (end - start)) / 2
        start, end = max(0.0, start - pad), min(duration, end + pad)
    return round(start, 2), round(end, 2)


def end_at_pause(words, end: float, duration: float, gap: float = 0.45, max_extra: float = 6.0) -> float:
    """Estende o fim até a próxima pausa na fala, para o corte não acabar no meio de uma frase."""
    t = end
    for w in words:
        if w.end <= t:
            continue
        if w.start - t > gap or w.end > end + max_extra:
            break
        t = w.end
    return round(min(duration, t + 0.35), 2) if t > end else end


def heuristic_title(channel: str, kind: str, hint: str | None, game: str = "GTA 6") -> str:
    if hint and len(hint) > 6:
        return f"{hint.strip()[:80]} | {channel} {game}"
    templates = {
        "laugh": f"{channel} chorou de rir no {game}",
        "hype": f"{channel} não acreditou nisso no {game}",
        "reaction": f"A reação do {channel} no {game}",
    }
    return templates.get(kind, f"{channel} no {game}")
