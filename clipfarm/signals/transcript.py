"""Transcrição com timestamps por palavra (faster-whisper) — base da legenda e do corte."""
from __future__ import annotations

import functools
from dataclasses import dataclass


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str


# shortcut: lista fixa de palavras que não existem em português; trocar por detecção de idioma por segmento se falhar
_EN = frozenset("the you and is it we what that this of are was they get got with on my your he she too here there "
                "have not don't it's i'm that's shit fuck yeah".split())


def looks_english(text: str) -> bool:
    """Fala do jogo (em inglês) que o Whisper transcreve mesmo com idioma forçado."""
    toks = [t.strip(".,!?…\"'").lower() for t in text.split()]
    hits = sum(t in _EN for t in toks)
    return hits >= 2 and hits / len(toks) >= 0.3


@functools.cache  # um modelo por (nome, device, compute_type): análise usa 'base', legenda usa 'small'
def _model(name: str, device: str, compute_type: str):
    from faster_whisper import WhisperModel  # import tardio: dependência pesada

    return WhisperModel(name, device=device, compute_type=compute_type)


def transcribe(path: str, language: str = "pt", model: str = "small", device: str = "auto",
               compute_type: str = "int8", prompt: str | None = None,
               fixes: dict[str, str] | None = None) -> tuple[list[Segment], list[Word]]:
    """prompt: vocabulário que o Whisper deve reconhecer; fixes: {errado: certo} aplicado palavra a palavra."""
    m = _model(model, device, compute_type)
    fixes = {k.lower(): v for k, v in (fixes or {}).items()}
    segs, _ = m.transcribe(path, language=language, initial_prompt=prompt, word_timestamps=True, vad_filter=False,  # com VAD a 1ª palavra após um silêncio saía segundos adiantada
                          
                           condition_on_previous_text=False)
    segments, words = [], []
    for s in segs:
        if language != "en" and looks_english(s.text):
            continue
        segments.append(Segment(s.start, s.end, s.text.strip()))
        for w in s.words or []:
            text = w.word.strip()
            core = text.strip(".,!?…\"'")
            if core.lower() in fixes:
                text = text.replace(core, fixes[core.lower()])
            if text:
                words.append(Word(w.start, w.end, text))
    return segments, words


def text_between(segments: list[Segment], start: float, end: float) -> str:
    return " ".join(f"[{s.start:.0f}s] {s.text}" for s in segments if s.end > start and s.start < end)
