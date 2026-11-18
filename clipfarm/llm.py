"""(Opcional) Claude como editor: dá nota ao momento, ajusta o corte e escreve título/hook."""
from __future__ import annotations

import base64
import json
import re

from .config import env

PROMPT = """Você é editor de um canal de cortes de GTA 6 no TikTok/YouTube Shorts (público {lang}).
Avalie se este trecho de live vira um corte viral (humor, susto, reação forte, jogada absurda).

Streamer: {channel}
Tipo de sinal: {kind}
Mensagens do chat no momento: {chat}

Transcrição com tempos (segundos, relativos ao arquivo):
{transcript}

Janela proposta: {start:.1f}s a {end:.1f}s (pico em {peak:.1f}s). Duração máx: {max_len}s.

Regras:
- Nota 0–10 (10 = altamente compartilhável).
- Rejeite (keep=false) só quando for claro que é cutscene/história do jogo SEM nenhuma fala ou reação do streamer,
  ou tela parada. Você não vê o vídeo e a transcrição mistura jogo e streamer: se o chat riu forte e há qualquer
  reação do streamer, na dúvida mantenha (keep=true).
- Pode ajustar start/end para começar no setup, mas o fim deve incluir a reação completa do streamer ao payoff
  (não corte no meio de uma fala); em geral mantenha entre 20 e 40 segundos.
- Título (máx 60 caracteres): é a legenda da publicação. Escreva como um jovem de 16 a 22 anos comentaria o vídeo
  com um amigo: curto, falado, minúsculas, sem ponto final, direto no que aconteceu NESTE trecho.
  Bom: "a lucia já começou brigando", "ele bateu o carro em 10 segundos", "o jason esqueceu que tem filho kkkk",
  "mano ele respondeu a mina assim", "não tankei essa resposta".
  Quando o momento é só absurdo ou sem explicação, o título pode ser apenas "?" ou "???" (ou "kkkkkkk" se for só risada).
  Gíria só se couber naturalmente (mano, kkkk, tankar, ficar gag, aura); nunca force nem empilhe gírias.
  Ruim: "A reação dele no GTA 6" (genérico), "Lucia responde X e o chat explode de rir" (narrado, engessado).
  Não descreva a reação do chat nem do streamer, não cite "GTA 6" nem o nome do streamer (isso vai nas hashtags),
  não use "explode", "hilário", "inacreditável", "você não vai acreditar".
  Use nomes e fatos que aparecem na transcrição; não invente o que não está nela.
- Manchete (headline, máx 45 caracteres): fica escrita no vídeo o tempo todo, para quem assiste sem som. Frase
  informativa e direta sobre o que acontece, sem gíria e sem "kkkk"; pode citar o streamer pelo nome e o jogo.
  Ex.: "GABE É O PRIMEIRO BR A VER ESSE EVENTO NO GTA 6", "ALANZOKA MATA UM GOLFINHO SEM QUERER".
  O fato da manchete precisa acontecer logo no começo do corte ou ocupar a maior parte dele. Nunca escreva
  manchete sobre algo que só aparece nos segundos finais: nesse caso, ajuste o start para começar perto desse momento.
  Quem vê só este corte, sem ter assistido à live, precisa entender a manchete e ver aquilo acontecer. Se houver
  imagens anexas, são frames do trecho em ordem (jogo + câmera do streamer): use o que elas mostram para conferir o
  que acontece de fato. Na dúvida, descreva de forma simples o que se vê e se ouve, em vez de interpretar.
- Nome de personagem do jogo (título, manchete e hashtags): só use se o nome for DITO na transcrição ou aparecer
  escrito nos frames deste trecho. Não deduza quem é pela aparência nem pelo contexto: na dúvida, escreva "ela",
  "ele", "a mina", "o cara". O nome do streamer pode ser usado sempre.
- Hashtags: 2 ou 3, só nomes próprios deste trecho (personagem, lugar, veículo, missão), minúsculas, sem espaço, com #.
  Nada genérico (#dialogo, #rua, #animais) e nada de #gta6, #fyp, #viral ou o nome do streamer: esses já são adicionados.
- A transcrição pode ter erros e misturar a fala do jogo com a do streamer.

Responda SOMENTE com JSON:
{{"score": float, "keep": bool, "start": float, "end": float, "title": str, "headline": str, "hashtags": [str], "reason": str}}"""


def review(channel: str, kind: str, chat: list[str], transcript: str, start: float, end: float, peak: float,
           max_len: float, model: str, lang: str = "pt-BR", frames: list[bytes] | None = None) -> dict | None:
    key = env("ANTHROPIC_API_KEY")
    if not key:
        return None
    import anthropic  # import tardio

    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model,
        max_tokens=2000,  # o Haiku 5.5 pensa antes de responder; 400 cortava o JSON
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": [
            *({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                           "data": base64.standard_b64encode(f).decode()}} for f in frames or []),
            {"type": "text", "text": PROMPT.format(
                lang=lang, channel=channel, kind=kind, chat=" | ".join(chat[-20:]) or "(sem dados)",
                transcript=transcript or "(sem fala)", start=start, end=end, peak=peak, max_len=max_len)}]}],
    )
    if msg.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    out["start"] = max(0.0, float(out.get("start", start)))
    out["end"] = float(out.get("end", end))
    if out["end"] - out["start"] > max_len or out["end"] <= out["start"]:
        out["start"], out["end"] = start, end
    return out


FACECAM_PROMPT = """Este é um frame de uma live de jogo. Ache a webcam do streamer (o retângulo com a pessoa real,
sobreposto ao jogo). Devolva o retângulo SÓ da imagem da câmera: sem molduras, sem barras de alerta/sub coladas nela.
Coordenadas como fração do frame (0 a 1), origem no canto superior esquerdo.
Responda SOMENTE com JSON: {"found": bool, "x": float, "y": float, "w": float, "h": float}
Se não houver webcam visível, responda {"found": false}."""


def find_facecam(jpeg: bytes, model: str) -> dict | None:
    """Região da facecam vista num frame: {x,y,w,h} em fração; {} se não há câmera; None se não deu para saber.

    O streamer muda a câmera de lugar durante a live, então uma posição fixa por canal erra.
    """
    import base64

    if not env("ANTHROPIC_API_KEY"):
        return None
    import anthropic

    try:
        msg = anthropic.Anthropic().messages.create(
            model=model, max_tokens=2000, output_config={"effort": "low"},
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.standard_b64encode(jpeg).decode()}},
                {"type": "text", "text": FACECAM_PROMPT}]}])
    except anthropic.APIError:
        return None
    m = re.search(r"\{.*\}", "".join(b.text for b in msg.content if b.type == "text"), re.S)
    try:
        d = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None
    if not d:
        return None
    if not d.get("found"):
        return {}
    try:
        x, y, w, h = (min(1.0, max(0.0, float(d[k]))) for k in "xywh")
    except (KeyError, TypeError, ValueError):
        return None
    w, h = min(w, 1 - x), min(h, 1 - y)
    # câmera plausível: nem um pontinho nem a tela toda
    return {"x": x, "y": y, "w": w, "h": h} if 0.05 <= w <= 0.6 and 0.05 <= h <= 0.6 else None
