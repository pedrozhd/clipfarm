"""Testes offline (sem Twitch/YouTube/TikTok): detector de chat, pico de áudio, janela e render.

Rode: python -m pytest -q tests   (ou: python tests/test_offline.py)
"""
from __future__ import annotations

import random
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clipfarm import config as C  # noqa: E402
from clipfarm import pipeline as P  # noqa: E402
from clipfarm.render import build_ass, probe, render  # noqa: E402
from clipfarm.scoring import choose_window  # noqa: E402
from clipfarm.signals import audio  # noqa: E402
from clipfarm.signals.chat import SpikeDetector  # noqa: E402
from clipfarm.signals.transcript import Segment, Word  # noqa: E402

DET = C.load()["detection"]
TMP = Path(tempfile.mkdtemp(prefix="clipfarm_test_"))


def make_video(path: Path, dur: float = 90, burst_at: float = 60) -> Path:
    """Gameplay falso 1920x1080 com 'facecam' no canto e um grito (burst de áudio) em burst_at."""
    if path.exists():
        return path
    a = (f"aevalsrc='0.03*sin(2*PI*220*t)+0.02*(random(0)-0.5)"
         f"+if(between(t,{burst_at - 1},{burst_at + 1.5}),0.7*sin(2*PI*660*t),0)':s=48000:d={dur}")
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={dur}",
        "-f", "lavfi", "-i", f"color=c=0x2E86AB:size=460x324:rate=30:duration={dur}",
        "-f", "lavfi", "-i", a,
        "-filter_complex", "[0:v][1:v]overlay=0:0[v]", "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def fake_transcriber(_path, *_a, **_k):
    script = [(52.0, "olha", "aquele", "carro", "ali"), (55.5, "vou", "pegar", "ele", "agora"),
              (58.8, "NÃO", "ACREDITO", "MANO"), (61.5, "kkkkk", "que", "isso")]
    segs, words = [], []
    for t0, *ws in script:
        t = t0
        for w in ws:
            words.append(Word(t, t + 0.38, w))
            t += 0.45
        segs.append(Segment(t0, t, " ".join(ws)))
    return segs, words


# ------------------------------------------------------------------ chat
def test_chat_spike_detects_laugh_burst():
    random.seed(1)
    det = SpikeDetector("streamer", **DET)
    spikes, t = [], 1_000_000.0
    for _ in range(300):  # 5 min de chat normal: ~4 msgs / 10s
        t += random.uniform(1.5, 3.5)
        s = det.add(t, random.choice(["salve", "que jogo lindo", "gg", "boa", "?"]))
        spikes += [s] if s else []
    assert not spikes, "não deveria disparar em chat normal"
    for _ in range(45):  # explosão de risada
        t += 0.2
        s = det.add(t, random.choice(["KKKKKKKK", "KEKW", "kkkkkkk mano", "LUL", "hahahaha"]))
        spikes += [s] if s else []
    for _ in range(20):
        t += 2
        s = det.add(t, "boa")
        spikes += [s] if s else []
    assert len(spikes) == 1, spikes
    sp = spikes[0]
    assert sp.kind == "laugh" and sp.z >= DET["z_threshold"] and sp.laugh_ratio > 0.8


def test_chat_hype_kind():
    det = SpikeDetector("s", **DET)
    t, spikes = 0.0, []
    for i in range(200):
        t += 2.5
        det.add(t, "oi")
    for _ in range(40):
        t += 0.2
        s = det.add(t, random.choice(["POG", "PogChamp", "W", "INSANO", "😱"]))
        spikes += [s] if s else []
    for _ in range(10):
        t += 3
        s = det.add(t, "oi")
        spikes += [s] if s else []
    assert spikes and spikes[0].kind == "hype"


def test_chat_messages_returns_on_busy_chat():
    """Chat que nunca silencia ainda devolve o controle (senão o watch nunca faz refresh)."""
    import socket
    import threading
    import time

    from clipfarm.signals.chat import TwitchChat

    srv, cli = socket.socketpair()
    stop = threading.Event()

    def flood():
        while not stop.is_set():
            srv.sendall(b":u!u@u PRIVMSG #canal :kkkk\r\n")
            time.sleep(0.05)

    threading.Thread(target=flood, daemon=True).start()
    chat = TwitchChat.__new__(TwitchChat)  # sem conectar na Twitch
    chat.sock, chat._buf, chat.joined = cli, b"", set()
    t0 = time.time()
    got = sum(1 for _ in chat.messages(timeout=1.0, max_s=1.0))
    stop.set()
    assert got > 0 and time.time() - t0 < 3, (got, time.time() - t0)


def test_chat_spike_needs_emotion():
    """Explosão de mensagens sem risada/hype (sub, pergunta) só vira pico se min_emotion permitir."""
    def burst(min_emotion):
        det, t, spikes = SpikeDetector("s", **{**DET, "min_emotion": min_emotion}), 0.0, []
        for step, n in ((2.5, 200), (0.2, 45), (3, 10)):
            for _ in range(n):
                t += step
                s = det.add(t, "que isso aconteceu")
                spikes += [s] if s else []
        return spikes

    assert burst(0.0) and not burst(0.25)


def test_clip_keeps_only_stretch_with_facecam():
    from clipfarm.pipeline import longest_cam_run as r

    a, far = {"x": .78, "y": .25, "w": .2, "h": .2}, {"x": .1, "y": .1, "w": .2, "h": .2}
    ts = [1.5, 4.5, 7.5, 10.5, 13.5, 16.5]
    assert r(ts, [a] * 6, 0, 18) == (0, 18, a)                    # câmera o tempo todo: corte inteiro
    assert r(ts, [{}, {}, {}, a, a, a], 0, 18) == (11.5, 18, a)   # volta com fade: começa 1s depois de vê-la
    assert r(ts, [a, a, a, {}, {}, {}], 0, 18) == (0, 7.5, a)     # some no meio: para onde ainda estava
    assert r(ts, [a, a, far, far, far, far], 0, 18)[2] == far     # mudou de lugar: fica o trecho maior
    assert r(ts, [{}] * 6, 0, 18) == (0, 0, {}) and r(ts, [None] * 6, 0, 18) is None
    assert r(ts, [a, a, far, a, a, a], 0, 18) == (0, 18, a)       # uma leitura errada isolada não quebra o trecho


def test_end_extends_to_speech_pause():
    from clipfarm.scoring import end_at_pause

    words = [Word(9.0, 9.8, "ele"), Word(9.9, 10.6, "abriu"), Word(10.7, 11.2, "a"), Word(11.2, 11.8, "boca"),
             Word(14.0, 14.5, "depois")]
    assert end_at_pause(words, 10.0, 60) == 12.15      # termina a frase em andamento, para na pausa
    assert end_at_pause(words, 12.5, 60) == 12.5       # já está numa pausa: não mexe
    assert end_at_pause(words, 10.0, 11.0) == 11.0     # nunca passa do fim do arquivo


def test_english_game_dialogue_is_dropped():
    from clipfarm.signals.transcript import looks_english

    assert looks_english("Is it in the water too?") and looks_english("Shit, we got it.")
    assert not looks_english("Mano, olha o que ele fez com o carro!")
    assert not looks_english("Não acredito, the best!") and not looks_english("")


def test_transcribe_applies_word_fixes():
    import types

    from clipfarm.signals import transcript as T

    w = lambda a, b, t: types.SimpleNamespace(start=a, end=b, word=t)  # noqa: E731
    seg = types.SimpleNamespace(start=0, end=2, text="fala a chate.", words=[w(0, 1, " fala"), w(1, 1.2, " a"), w(1.2, 2, " Chate.")])
    seen = {}

    class FakeModel:
        def __init__(self, *_a, **_k): pass

        def transcribe(self, _path, **kw):
            seen.update(kw)
            return [seg], None

    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = FakeModel
    real = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = fake
    try:
        T._model.cache_clear()
        _, words = T.transcribe("x.mp4", prompt="Lucia, chat", fixes={"chate": "chat"})
        assert [x.text for x in words] == ["fala", "a", "chat."] and seen["initial_prompt"] == "Lucia, chat"
    finally:
        T._model.cache_clear()
        if real is not None:
            sys.modules["faster_whisper"] = real
        else:
            sys.modules.pop("faster_whisper", None)


def test_whisper_model_cache_per_name():
    import types

    from clipfarm.signals import transcript as T

    fake = types.ModuleType("faster_whisper")
    fake.WhisperModel = lambda name, **_: name
    real = sys.modules.get("faster_whisper")
    sys.modules["faster_whisper"] = fake
    try:
        T._model.cache_clear()
        assert T._model("base", "cpu", "int8") == "base"
        assert T._model("small", "cpu", "int8") == "small"
    finally:
        T._model.cache_clear()
        if real is not None:
            sys.modules["faster_whisper"] = real
        else:
            sys.modules.pop("faster_whisper", None)


# ------------------------------------------------------------------ áudio + janela
def test_audio_peak_and_window():
    v = make_video(TMP / "fake.mp4")
    times, db = audio.loudness_curve(str(v))
    peak, val = audio.find_peak(times, audio.excitement(times, db), 30, 90)
    assert abs(peak - 60.25) < 1.5, peak
    assert val > 2
    segs, _ = fake_transcriber(None)
    start, end = choose_window(90, peak, segs, 22, 8, 12, 58)
    assert 36 <= start <= 42 and 66 <= end <= 70 and end - start <= 58, (start, end)


# ------------------------------------------------------------------ render
def test_ass_highlights_each_word():
    _, words = fake_transcriber(None)
    rc = C.load()["render"]
    ass = build_ass(words, 50, 65, rc, "blur", "twitch.tv/teste")
    events = [l for l in ass.splitlines() if l.startswith("Dialogue: 0")]
    assert len(events) == len(words)
    assert "twitch.tv/teste" in ass and f"\\c&H{rc['highlight_color'][-6:]}&" in ass  # cor vem do config
    brand = build_ass(words, 50, 65, rc, "blur", None, hook="gancho", style="flagra")
    assert "\\c&HFFE319&" in brand and "Style: Hook,Anton" in brand and "&H00872DFF" in brand


def test_render_layouts():
    v = make_video(TMP / "fake.mp4")
    _, words = fake_transcriber(None)
    rc = C.load()["render"]
    face = {"x": 0.0, "y": 0.0, "w": 0.24, "h": 0.30}
    for layout in ("blur", "facecam", "pip", "crop"):
        out = TMP / f"out_{layout}.mp4"
        render(str(v), str(out), 50, 66, words, rc, layout, face, "twitch.tv/teste")
        info = probe(str(out))
        assert (info["w"], info["h"]) == (1080, 1920), info
        assert abs(info["duration"] - 16) < 0.5 and info["audio"]
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "9.2", "-i", str(out), "-frames:v", "1",
                        str(TMP / f"frame_{layout}.png")], check=True)


# ------------------------------------------------------------------ pipeline ponta a ponta
def test_pipeline_end_to_end():
    v = make_video(TMP / "fake.mp4")
    cfg_path = TMP / "config.yaml"
    cfg_path.write_text(
        f"paths: {{data_dir: '{TMP / 'data'}'}}\n"
        "whisper: {enabled: false}\n"
        "allowlist: [{login: teste, credit: 'twitch.tv/teste', facecam: {x: 0, y: 0, w: 0.24, h: 0.30}}]\n")
    ctx = P.Ctx(str(cfg_path))
    mid = ctx.db.add_moment(source="local", platform="local", channel="teste", media_url=str(v), score=5,
                            signals={"kind": "laugh"}, external_id="local:e2e")
    m = ctx.db.conn.execute("SELECT * FROM moments WHERE id=?", (mid,)).fetchone()
    cid = P.process_moment(ctx, m, transcriber=fake_transcriber)
    clip = ctx.db.conn.execute("SELECT * FROM clips WHERE id=?", (cid,)).fetchone()
    assert clip["status"] == "review" and Path(clip["path"]).exists()
    assert 25 <= clip["end_s"] - clip["start_s"] <= 58
    assert "twitch.tv/teste" in clip["description"]
    ctx.db.set_clip(cid, "approved")
    assert [c["id"] for c in ctx.db.clips("approved")] == [cid]
    # dedupe por canal + VOD + offset
    a = ctx.db.add_moment(source="chat_spike", platform="twitch", channel="x", vod_id="1", vod_offset_s=100,
                          external_id="a")
    b = ctx.db.add_moment(source="twitch_clip", platform="twitch", channel="x", vod_id="1", vod_offset_s=120,
                          external_id="b")
    assert a and b is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("artefatos em", TMP)
