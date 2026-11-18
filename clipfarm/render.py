"""Render vertical 9:16: legenda palavra-a-palavra, gancho (headline), crédito e @ da página."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from .signals.transcript import Word
from .styles import resolve


def probe(path: str) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
         "-of", "json", path], capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), {})
    return {"w": v.get("width", 1920), "h": v.get("height", 1080),
            "duration": float(d["format"].get("duration", 0)),
            "audio": any(s["codec_type"] == "audio" for s in d["streams"])}


# ---------------------------------------------------------------- legendas ASS
def _ts(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def _bgr(ass_color: str) -> str:
    """&HAABBGGRR -> &HBBGGRR& (formato do override \\c)."""
    return f"&H{ass_color.replace('&H', '').replace('&', '')[-6:]}&"


def _clean(w: str, upper: bool = True) -> str:
    w = w.replace("\\", "").replace("{", "").replace("}", "").replace("\n", " ")
    return w.upper() if upper else w


def chunk_words(words: list[Word], per: int, max_chars: int = 18, gap: float = 0.6) -> list[list[Word]]:
    chunks, cur = [], []
    for w in words:
        too_long = sum(len(x.text) + 1 for x in cur) + len(w.text) > max_chars
        if cur and (len(cur) >= per or too_long or w.start - cur[-1].end > gap):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


# posições em 1080x1920: legenda, crédito (x, y, alinhamento), @ da página, área do gancho (centro y)
LAYOUT_POS = {  # @ e crédito logo abaixo do vídeo: a base da tela fica coberta pela UI do TikTok/Reels
    "blur": {"cap": (540, 1440), "credit": (540, 1335, 5), "handle": (540, 1290, 5), "hook_y": 270},
    "facecam": {"cap": (540, 820), "credit": (90, 625, 1), "handle": (990, 625, 3), "hook_y": 1560},
    "crop": {"cap": (540, 1300), "credit": (540, 290, 5), "handle": (540, 245, 5), "hook_y": 440},
    "pip": {"cap": (540, 1330), "credit": (540, 1225, 5), "handle": (540, 1180, 5), "hook_y": 760},
    "meme": {"cap": (540, 1420), "credit": (540, 1252, 5), "handle": (540, 1210, 5), "hook_y": 330},
}


def build_ass(words: list[Word], start: float, end: float, rc: dict, layout: str, credit: str | None,
              hook: str | None = None, handle: str | None = None, style: str | None = None) -> str:
    st = resolve(rc, style)
    W, H = st["width"], st["height"]
    font, size, upper = st["font"], st["font_size"], st["upper"]
    base = _bgr(st["caption_color"])
    hi = _bgr(st["highlight_color"]) if st.get("highlight_color") else None
    pos = LAYOUT_POS[layout]
    cx, cy = pos["cap"][0], pos["cap"][1] + st["y_offset"]
    hook_box = 1 if layout == "meme" else 3  # meme: texto puro no fundo; demais: tarja opaca
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{size},{st['caption_color']},&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,"
        f"1,{st['outline']},{st['shadow']},5,60,60,0,1",
        f"Style: Credit,Poppins,{int(rc['font_size'] * 0.40)},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,"
        f"100,100,0,0,1,3,1,5,30,30,0,1",
        f"Style: Handle,Poppins,{int(rc['font_size'] * 0.46)},&H40FFFFFF,&H000000FF,&H80000000,&H00000000,-1,0,0,0,"
        f"100,100,0,0,1,2,0,5,30,30,0,1",
        f"Style: Hook,{st.get('hook_font', 'Poppins')},{int(rc['font_size'] * st.get('hook_scale', 0.88 if layout == 'meme' else 0.66))},&H00FFFFFF,&H000000FF,{st.get('hook_color', '&H00000000')},&H00000000,-1,0,0,0,"
        f"100,100,0,0,{hook_box},{18 if hook_box == 3 else 0},0,5,70,70,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    dur = end - start
    if hook and layout == "facecam":
        # manchete na faixa entre a câmera e o jogo (não cobre o gameplay); a legenda desce junto
        cy += BAND_H
        lines.insert(lines.index("[Events]") - 1, f"Style: Band,{st.get('hook_font', 'Poppins')},78,&H00FFFFFF,&H000000FF,"
                     f"&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,0,0,5,100,100,0,1")
        lines.append(f"Dialogue: 2,{_ts(0)},{_ts(dur)},Band,,0,0,0,,"
                     f"{{\\an5\\q0\\pos({W // 2},{H // 3 + BAND_H // 2 - 3})}}{_clean(hook, True)}")
    elif hook:
        lines.append(f"Dialogue: 2,{_ts(0)},{_ts(dur)},Hook,,0,0,0,,"
                     f"{{\\an5\\q0\\pos({W // 2},{pos['hook_y']})}}{_clean(hook, False)}")
    if credit:
        x, y, an = pos["credit"]
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(dur)},Credit,,0,0,0,,{{\\an{an}\\pos({x},{y})}}{_clean(credit, False)}")
    if handle:
        x, y, an = pos["handle"]
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(dur)},Handle,,0,0,0,,{{\\an{an}\\pos({x},{y})}}{_clean(handle, False)}")

    rel = [Word(w.start - start, w.end - start, w.text) for w in words if w.end > start and w.start < end]
    max_chars = 18 if st["words_per_caption"] <= 4 else 34
    chunks = chunk_words(rel, st["words_per_caption"], max_chars=max_chars)
    for ci, ch in enumerate(chunks):
        chunk_end = ch[-1].end + 0.25
        if ci + 1 < len(chunks):
            chunk_end = min(chunk_end, chunks[ci + 1][0].start)
        if not hi:  # sem destaque: um evento por bloco
            text = " ".join(_clean(x.text, upper) for x in ch)
            lines.append(f"Dialogue: 0,{_ts(max(0, ch[0].start))},{_ts(min(dur, chunk_end))},Cap,,0,0,0,,"
                         f"{{\\an5\\pos({cx},{cy})}}{text}")
            continue
        for wi, w in enumerate(ch):
            t0 = w.start
            t1 = ch[wi + 1].start if wi + 1 < len(ch) else chunk_end
            if t1 <= t0:
                continue
            parts = [f"{{\\c{hi}}}{_clean(x.text, upper)}{{\\c{base}}}" if j == wi else _clean(x.text, upper)
                     for j, x in enumerate(ch)]
            pop = "\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)" if wi == 0 and st["pop"] else ""
            lines.append(f"Dialogue: 0,{_ts(max(0, t0))},{_ts(min(dur, t1))},Cap,,0,0,0,,"
                         f"{{\\an5\\pos({cx},{cy}){pop}}}{' '.join(parts)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- vídeo
def _even(x: float) -> int:
    return max(2, int(x) // 2 * 2)


BAND_H = 190  # altura da faixa da manchete no layout facecam (px em 1080x1920)


def video_filter(layout: str, w: int, h: int, W: int, H: int, fps: int, facecam: dict | None,
                 pip_zoom: float = 1.5, band: bool = False) -> str:
    if layout == "pip" and facecam:
        # jogo em tela cheia (recorte 9:16 central) + câmera pequena no canto de cima: pouco zoom = câmera nítida
        fx, fy = _even(w * facecam["x"]), _even(h * facecam["y"])
        fw, fh = _even(w * facecam["w"]), _even(h * facecam["h"])
        cw = min(w, _even(h * W / H))
        z = min(pip_zoom, (W - 80) / fw)  # nunca mais larga que a tela
        return (f"[0:v]fps={fps},split=2[a][b];"
                f"[a]crop={fw}:{fh}:{fx}:{fy},scale={_even(fw * z)}:{_even(fh * z)}[cam];"
                f"[b]crop={cw}:{h}:{(w - cw) // 2}:0,scale={W}:{H}[game];"
                f"[game][cam]overlay=40:180,setsar=1[base]")
    if layout == "facecam" and facecam:
        top_h = _even(H / 3)                      # 640 em 1080x1920
        bot_h = H - top_h - (BAND_H if band else 0)
        fx, fy = _even(w * facecam["x"]), _even(h * facecam["y"])
        fw, fh = _even(w * facecam["w"]), _even(h * facecam["h"])
        gw = min(w, _even(h * W / bot_h))
        parts = (f"[0:v]fps={fps},split=2[a][b];"
                 f"[a]crop={fw}:{fh}:{fx}:{fy},scale={W}:{top_h}:force_original_aspect_ratio=increase,"
                 f"crop={W}:{top_h}[cam];"
                 f"[b]crop={gw}:{h}:{(w - gw) // 2}:0,scale={W}:{bot_h}[game];")
        if not band:
            return parts + "[cam][game]vstack=inputs=2,setsar=1[base]"
        # faixa escura com filete magenta (cores da marca) entre a câmera e o jogo
        return (parts + f"color=c=0x0D0B1A:s={W}x{BAND_H}:r={fps},"
                f"drawbox=x=0:y={BAND_H - 6}:w={W}:h=6:color=0xFF2D87:t=fill[band];"
                f"[cam][band][game]vstack=inputs=3:shortest=1,setsar=1[base]")
    if layout == "crop":
        cw = min(w, _even(h * W / H))
        return f"[0:v]fps={fps},crop={cw}:{h}:{(w - cw) // 2}:0,scale={W}:{H},setsar=1[base]"
    if layout == "meme":
        # fundo preto, vídeo inteiro no meio, espaço em cima para o gancho
        fg_h = min(_even(W * h / w), _even(H * 0.62))
        fg_w = min(W, _even(fg_h * w / h))
        y = 560 if fg_h <= 760 else max(0, (H - fg_h) // 2 + 120)
        return (f"[0:v]fps={fps},scale={fg_w}:{fg_h}[fg];color=c=black:s={W}x{H}:r={fps}[bg];"
                f"[bg][fg]overlay=(W-w)/2:{y}:shortest=1,setsar=1[base]")
    # blur: fundo desfocado + gameplay com zoom leve (4:3 central) no meio
    cw = min(w, _even(h * 4 / 3))
    fg_h = _even(W * h / cw)
    y = (H - fg_h) // 2 - 120 if fg_h < H * 0.8 else (H - fg_h) // 2
    return (f"[0:v]fps={fps},split=2[a][b];"
            f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=24:2,"
            f"eq=brightness=-0.12[bg];"
            f"[b]crop={cw}:{h}:{(w - cw) // 2}:0,scale={W}:{fg_h}[fg];"
            f"[bg][fg]overlay=0:{y},setsar=1[base]")


def render(src: str, out: str, start: float, end: float, words: list[Word], rc: dict,
           layout: str = "auto", facecam: dict | None = None, credit: str | None = None,
           hook: str | None = None, handle: str | None = None, style: str | None = None) -> str:
    info = probe(src)
    if layout == "auto":
        layout = "facecam" if facecam else "blur"
    if layout in ("facecam", "pip") and not facecam:
        layout = "blur"
    W, H = rc["width"], rc["height"]
    vf = video_filter(layout, info["w"], info["h"], W, H, rc["fps"], facecam, rc.get("pip_zoom", 1.5),
                      band=bool(hook))
    with tempfile.TemporaryDirectory() as tmp:
        ass = build_ass(words, start, end, rc, layout, credit, hook=hook, handle=handle, style=style)
        (Path(tmp) / "subs.ass").write_text(ass, encoding="utf-8")
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
               "-i", str(Path(src).resolve()), "-filter_complex", f"{vf};[base]ass=subs.ass[v]",
               "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", rc["preset"], "-crf", str(rc["crf"]),
               "-pix_fmt", "yuv420p", "-profile:v", "high", "-r", str(rc["fps"]),
               "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-movflags", "+faststart"]
        if info["audio"] and rc.get("loudnorm", True):
            cmd += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
        cmd.append(str(Path(out).resolve()))
        r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg falhou: {r.stderr.strip()[-600:]}")
    return out


def thumbnail(video: str, out: str, at: float = 1.0) -> str:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", video, "-frames:v", "1",
                    "-vf", "scale=360:-2", out], check=True, stdout=subprocess.DEVNULL)
    return out
