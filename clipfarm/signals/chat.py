"""Sinal de chat: picos de volume de mensagens + risadas/hype.

O chat é o melhor "detector de emoção" barato que existe: quando algo engraçado ou
impactante acontece, a taxa de mensagens explode e aparecem KEKW / KKKKK / POG.
"""
from __future__ import annotations

import math
import random
import re
import select
import socket
import time
from collections import deque
from dataclasses import dataclass, field

LAUGH_RE = re.compile(r"(k{4,}|(ha){3,}|(rs){3,}|(ja){3,}|\blo+l+\b|\blmf?ao+\b)", re.I)


@dataclass
class Spike:
    channel: str
    ts: float                 # epoch do fim da janela que disparou
    kind: str                 # laugh | hype | reaction
    z: float
    msgs: int
    laugh_ratio: float
    hype_ratio: float
    score: float
    sample: list[str] = field(default_factory=list)


class SpikeDetector:
    """Agrupa mensagens em janelas fixas e compara cada janela com a linha de base recente."""

    def __init__(self, channel: str, window_s=10, baseline_windows=30, z_threshold=3.0,
                 min_msgs=12, cooldown_s=90, laugh_terms=(), hype_terms=(), min_emotion=0.0, **_):
        self.channel = channel
        self.window_s = window_s
        self.z_threshold = z_threshold
        self.min_msgs = min_msgs
        self.cooldown_s = cooldown_s
        self.min_emotion = min_emotion
        self.laugh_terms = {t.lower() for t in laugh_terms}
        self.hype_terms = {t.lower() for t in hype_terms}
        self.history: deque[int] = deque(maxlen=baseline_windows)
        self._win_start: float | None = None
        self._count = self._laugh = self._hype = 0
        self._sample: deque[str] = deque(maxlen=25)
        self._last_spike = -1e18

    def _classify(self, text: str) -> tuple[bool, bool]:
        low = text.lower()
        tokens = set(re.findall(r"\w+|[^\w\s]", low))
        laugh = bool(LAUGH_RE.search(low)) or bool(tokens & self.laugh_terms) or any(
            t in low for t in self.laugh_terms if not t.isalnum())
        hype = bool(tokens & self.hype_terms) or any(t in low for t in self.hype_terms if not t.isalnum())
        return laugh, hype

    def add(self, ts: float, text: str) -> Spike | None:
        """Adiciona uma mensagem; devolve um Spike quando uma janela fecha em pico."""
        spike = None
        if self._win_start is None:
            self._win_start = ts
        while ts >= self._win_start + self.window_s:  # fecha janelas (inclusive vazias)
            spike = self._close() or spike
            self._win_start += self.window_s
        self._count += 1
        laugh, hype = self._classify(text)
        self._laugh += laugh
        self._hype += hype
        self._sample.append(text[:120])
        return spike

    def _close(self) -> Spike | None:
        n, end_ts = self._count, self._win_start + self.window_s
        spike = None
        if len(self.history) >= max(6, self.history.maxlen // 3):
            mean = sum(self.history) / len(self.history)
            var = sum((x - mean) ** 2 for x in self.history) / len(self.history)
            std = max(math.sqrt(var), 1.0, 0.25 * mean)  # piso evita z gigante em chat parado
            z = (n - mean) / std
            lr, hr = (self._laugh / n, self._hype / n) if n else (0.0, 0.0)
            if (z >= self.z_threshold and n >= self.min_msgs and end_ts - self._last_spike >= self.cooldown_s
                    and max(lr, hr) >= self.min_emotion):  # só msg sem risada/hype = sub, pergunta, conversa
                kind = "laugh" if lr >= 0.25 and lr >= hr else "hype" if hr >= 0.25 else "reaction"
                score = round(z + 3 * lr + 2 * hr + math.log10(n), 2)
                spike = Spike(self.channel, end_ts, kind, round(z, 2), n, round(lr, 2), round(hr, 2),
                              score, list(self._sample))
                self._last_spike = end_ts
        self.history.append(n)
        self._count = self._laugh = self._hype = 0
        return spike


class TwitchChat:
    """Cliente IRC anônimo (somente leitura) — não precisa de token."""

    HOST, PORT = "irc.chat.twitch.tv", 6667

    def __init__(self):
        self.sock = socket.create_connection((self.HOST, self.PORT), timeout=30)
        nick = f"justinfan{random.randint(10000, 99999)}"
        self._send(f"NICK {nick}")
        self.joined: set[str] = set()
        self._buf = b""
        self._last_join = 0.0

    def _send(self, line: str):
        self.sock.sendall((line + "\r\n").encode())

    def join(self, channel: str):
        channel = channel.lower()
        if channel in self.joined:
            return
        wait = 0.6 - (time.time() - self._last_join)  # ~20 joins / 10s
        if wait > 0:
            time.sleep(wait)
        self._send(f"JOIN #{channel}")
        self.joined.add(channel)
        self._last_join = time.time()

    def part(self, channel: str):
        channel = channel.lower()
        if channel in self.joined:
            self._send(f"PART #{channel}")
            self.joined.discard(channel)

    def messages(self, timeout: float = 1.0, max_s: float = 5.0):
        """Gera (canal, texto, epoch) até `timeout` sem dados ou `max_s` no total."""
        deadline = time.time() + max_s  # chat lotado nunca fica `timeout` em silêncio: devolve o controle ao loop
        while time.time() < deadline:
            ready, _, _ = select.select([self.sock], [], [], timeout)
            if not ready:
                return
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("IRC desconectado")
            self._buf += chunk
            *lines, self._buf = self._buf.split(b"\r\n")
            now = time.time()
            for raw in lines:
                line = raw.decode("utf-8", "ignore")
                if line.startswith("PING"):
                    self._send("PONG :tmi.twitch.tv")
                    continue
                # :user!user@user.tmi.twitch.tv PRIVMSG #canal :mensagem
                parts = line.split(" ", 3)
                if len(parts) == 4 and parts[1] == "PRIVMSG":
                    yield parts[2].lstrip("#"), parts[3][1:], now
