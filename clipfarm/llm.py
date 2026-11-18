"""(Opcional) Claude como editor: dá nota ao momento, ajusta o corte e escreve título/hook."""
from __future__ import annotations

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
- Hashtags: 2 ou 3, só nomes próprios deste trecho (personagem, lugar, veículo, missão), minúsculas, sem espaço, com #.
  Nada genérico (#dialogo, #rua, #animais) e nada de #gta6, #fyp, #viral ou o nome do streamer: esses já são adicionados.
- A transcrição pode ter erros e misturar a fala do jogo com a do streamer.

Responda SOMENTE com JSON:
{{"score": float, "keep": bool, "start": float, "end": float, "title": str, "hashtags": [str], "reason": str}}"""


def review(channel: str, kind: str, chat: list[str], transcript: str, start: float, end: float, peak: float,
           max_len: float, model: str, lang: str = "pt-BR") -> dict | None:
    key = env("ANTHROPIC_API_KEY")
    if not key:
        return None
    import anthropic  # import tardio

    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=model,
        max_tokens=2000,  # o Haiku 5.5 pensa antes de responder; 400 cortava o JSON
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": PROMPT.format(
            lang=lang, channel=channel, kind=kind, chat=" | ".join(chat[-20:]) or "(sem dados)",
            transcript=transcript or "(sem fala)", start=start, end=end, peak=peak, max_len=max_len)}],
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
