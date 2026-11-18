# ClipFarm — cortes de GTA 6 no automático

Lives → detecção de momentos (chat + áudio + fala) → corte 9:16 com legenda → revisão → TikTok e YouTube Shorts.

Dois modos, para uso pessoal:

- **Studio (conversando com o Claude)** — você manda o link do VOD (ou sobe o arquivo), o Claude escolhe os momentos, o ClipFarm corta, legenda e salva os vídeos numa pasta do seu PC, e publica/agenda no YouTube Shorts, TikTok e Instagram Reels. Ver [Studio](#studio--conversando-com-o-claude).
- **Farm (automático)** — roda sozinho: escuta lives, detecta picos, corta e publica. Seções abaixo.

```
            ┌──────────── FONTES ─────────────┐
            │ Twitch Helix: lives por jogo,   │
            │ clips mais vistos, VOD atual    │   (Kick: próxima fonte)
            └───────┬──────────────┬──────────┘
                    │              │
        harvest (a cada 30 min)  watch (contínuo)
        clips que a audiência    chat IRC ao vivo:
        já criou, por views      pico de msgs + KKKK/KEKW/POG
                    │              │
                    └──── moments (SQLite) ────┘
                                 │
                             process
          yt-dlp baixa só o trecho → pico de áudio (grito/risada)
          → Whisper (palavra a palavra) → janela setup+payoff
          → [opcional] Claude dá nota, ajusta corte, escreve título
          → ffmpeg 1080x1920: blur | facecam empilhada | crop
            + legenda estilo Shorts + crédito do streamer
                                 │
                           clips (review)
                    você aprova / rejeita / troca título
                                 │
                             publish
          YouTube Data API (Shorts)  ·  TikTok Content Posting API
          com limite diário por plataforma
```

## Por que esse desenho

- **O chat é o detector de emoção.** Quando algo acontece, a taxa de mensagens explode. O `SpikeDetector` compara cada janela de 10s com os últimos ~5 min (z-score) e classifica em `laugh` (KKKK, KEKW, LUL, hahaha), `hype` (POG, W, INSANO, 😱) ou `reaction`.
- **Os clips da Twitch são curadoria grátis.** Os espectadores já clipam os melhores momentos; o `harvest` puxa os mais vistos das últimas horas. É o caminho mais rápido para ter conteúdo no dia 1.
- **O áudio acha o segundo exato.** O chat reage com alguns segundos de atraso; o pico de volume no trecho baixado marca o payoff, e o corte começa ~22s antes (setup) e termina ~8s depois.
- **A fala define as bordas.** O corte é alinhado ao começo e ao fim das frases (Whisper), e a mesma transcrição vira a legenda palavra a palavra.
- **Revisão humana antes de postar.** Qualidade e risco (spoiler, direitos) pedem um olho humano no começo. Depois, `review.auto_approve_score` automatiza os clips com nota alta.

## Setup

Requisitos: Python 3.11+, **ffmpeg** no PATH (com libass), GPU NVIDIA ajuda no Whisper (opcional).

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env                                   # preencha as chaves
python tests/test_offline.py                           # valida render/detecção sem internet
```

Chaves:

| Serviço | Onde | Observação |
|---|---|---|
| Twitch | dev.twitch.tv/console/apps | client credentials; suficiente para lives, clips e VODs |
| YouTube | Google Cloud → OAuth "Desktop app" + YouTube Data API v3 | sem auditoria, upload só como **privado** |
| TikTok | developers.tiktok.com → Login Kit + Content Posting API | sem auditoria, post direto só **privado**; o modo `inbox` manda para rascunhos |
| Claude (opcional) | console.anthropic.com | nota + título; ative em `llm.enabled` |

Fonte da legenda: o padrão é **Poppins** (instale no sistema) — ou troque `render.font`.

## Comandos

```bash
python -m clipfarm discover          # maiores lives do jogo agora (✔ = na allowlist)
python -m clipfarm harvest           # importa clips mais vistos da Twitch
python -m clipfarm watch             # escuta chats ao vivo (deixe rodando num terminal)
python -m clipfarm process           # baixa, corta, legenda, renderiza
python -m clipfarm review            # lista clips para revisar
python -m clipfarm review approve 3 7 --title "Novo título"
python -m clipfarm review reject 4
python -m clipfarm publish           # posta aprovados (respeita daily_cap)
python -m clipfarm run               # loop: harvest + process + publish
python -m clipfarm stats             # funil: momentos → clips → posts
python -m clipfarm add-local video.mp4 --channel teste   # testa com qualquer vídeo
```

Produção típica: um terminal com `watch`, outro com `run`. Agende no Agendador de Tarefas/cron ou orquestre pelo n8n chamando os comandos.

## Configuração que mais importa

- `sources.twitch.games`: `"Grand Theft Auto VI"`. Até o lançamento, teste com `"Grand Theft Auto V"` (GTA RP tem volume de sobra para calibrar).
- `market.languages`: `["pt"]` para o mercado BR, `["en"]` para o global — melhor um canal por idioma.
- `allowlist`: canais com permissão. Para cada um, `facecam` (região em fração do frame) ativa o layout com a câmera em cima e o jogo embaixo — o formato que mais retém.
- `detection.z_threshold` / `min_msgs`: sensibilidade. Comece em 3.0 e ajuste olhando `stats` e os clips rejeitados.

## Riscos e regras do jogo (leia antes de escalar)

1. **Direitos do streamer.** A live é conteúdo do streamer. Clipar sem permissão = DMCA/strike. Use a `allowlist` com quem permite clipagem ou roda programa de clipping (Whop Content Rewards, Vyro etc.). Isso também é a fonte de receita mais previsível: os programas pagam por mil views.
2. **Rockstar/Take-Two.** A política oficial de vídeos da Rockstar é para uso não comercial, remove spoilers de história (finais, cutscenes isoladas) e qualquer material vazado antes do lançamento, e se reserva o direito de derrubar a qualquer momento. Na semana de lançamento quase tudo será modo história: foque em reação do streamer + gameplay, nunca cutscene pura. O prompt do Claude já rejeita isso.
3. **Monetização das plataformas.** O YouTube (política de conteúdo reutilizado/inautêntico) e o Creator Rewards do TikTok penalizam reupload sem valor agregado. Legenda, corte, título e compilações ajudam; reupload cru não.
4. **Auditorias de API.** YouTube e TikTok só liberam post público por API depois de auditoria (semanas). Peça já. Até lá: YouTube privado → publicar no Studio; TikTok `inbox` → finalizar no app.
5. **"Farm" ≠ muitas contas iguais.** Postar o mesmo conteúdo em várias contas cai em spam nas duas plataformas. Melhor 1–3 canais com identidade (ex.: um PT-BR, um EN).

## Roadmap

- [ ] Kick como fonte (forte no público BR) — `sources/kick.py`
- [ ] Gravação contínua (buffer) via streamlink para canais que não salvam VOD (`status = no_vod` no `stats`)
- [ ] Detecção de risada por modelo de áudio (YAMNet/PANNs) além do volume
- [ ] Detecção automática da facecam (visão) em vez de coordenada manual
- [ ] Painel de revisão web (aprovar no celular) e métricas de views por clip para recalibrar o score
- [ ] Compilações diárias ("Top 10 momentos do GTA 6 hoje") para o YouTube longo

---

## Studio — conversando com o Claude

Roda **no seu PC** como conector local do Claude Desktop (o Cowork também enxerga). Sem servidor, sem domínio.
O trabalho pesado (download, transcrição, áudio, render, upload) fica com o ClipFarm; a escolha dos momentos, títulos e ganchos fica com o Claude.

```
Você: "gera 8 cortes desse VOD e agenda pra semana"
  Claude ── analisar_video ──────▶ baixa SÓ o áudio → legenda do YouTube ou Whisper → 25 candidatos
         ── status_video ────────▶ (picos de volume + fala exaltada, com o trecho falado)
         ── ver_frame / ler_transcricao (acha a facecam, confere contexto)
         ── criar_cortes ────────▶ baixa só os trechos → legenda palavra a palavra → 9:16
                                   salva em ~/ClipFarm/cortes/0001_titulo.mp4
         ── publicar_corte ──────▶ agora ou "2026-11-20 18:00" → YouTube / TikTok / Instagram
```

### Instalar (uma vez)

Requisitos: Python 3.11+ e **ffmpeg** no PATH (Windows: `winget install Gyan.FFmpeg`; Mac: `brew install ffmpeg`).

```bash
cd clipfarm
python -m venv .venv && .venv\Scripts\activate        # Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy config.example.yaml config.yaml                   # Mac/Linux: cp
copy .env.example .env
python -m clipfarm.studio claude-config                # imprime o bloco JSON com os caminhos certos
```

No Claude Desktop: **Configurações → Desenvolvedor → Editar configuração**, cole o bloco dentro de `claude_desktop_config.json` e reinicie o Claude. Pronto: peça *"Use o ClipFarm e gere 8 cortes deste VOD: <link>"*, ou use o prompt `gerar_cortes`.

Enquanto o Claude estiver aberto, o painel fica em `http://127.0.0.1:8765/u/<token>` (o Claude te passa o link com `meu_painel`): upload de arquivos, preview, download e agenda. Sem o Claude: `python -m clipfarm.studio painel`.

Fontes das legendas: instale **Poppins**, **Anton** e **Inter** (Google Fonts) no sistema; sem elas o ffmpeg usa uma fonte padrão.

### Conectar as contas para publicar

| Plataforma | Uma vez | Como sai |
|---|---|---|
| YouTube | Google Cloud → YouTube Data API v3 → OAuth "Desktop app" → salve o JSON em `secrets/` → `python -m clipfarm.studio auth youtube` | **privado** até o projeto do Google passar na auditoria; publique no YouTube Studio com 1 clique |
| TikTok | app em developers.tiktok.com (Login Kit + Content Posting API) → `auth tiktok` | modo `inbox`: cai nas notificações do app e você finaliza (som, capa) no celular |
| Instagram | conta Profissional ligada a uma Página do Facebook + app em developers.facebook.com → `auth instagram` (token do Graph API Explorer) | Reel publicado direto; refaça o `auth` a cada ~60 dias |

O agendamento é do próprio ClipFarm: o post sai no horário **se o Claude Desktop (ou o `painel`) estiver aberto**. Com o PC desligado, sai quando abrir de novo.

### Ferramentas que o Claude usa

| Ferramenta | O que faz |
|---|---|
| `analisar_video(url)` | YouTube, VOD da Twitch, Kick |
| `status_video(media_id, aguardar_s)` | candidatos com tempo + fala quando a análise termina |
| `ler_transcricao`, `ver_frame` | contexto de um trecho; imagem do frame (facecam) |
| `criar_cortes(media_id, cortes[])` | início, fim, título, gancho, estilo, layout, facecam, crédito |
| `status_cortes` | caminho do arquivo + preview no painel |
| `variar_corte`, `ajustar_corte` | pack de variações (A/B) e editor |
| `configurar_pagina(arroba)` | @ que vai em todo vídeo |
| `contas_conectadas`, `publicar_corte`, `status_publicacoes`, `cancelar_publicacao` | publicação e agenda |
| `clips_em_alta` | radar dos clips mais vistos da Twitch (precisa das chaves da Twitch no `.env`) |

Estilos: `flagra` (padrão, identidade Flagra Neon), `destaque`, `verde`, `minimal`, `meme`. Layouts: `blur`, `facecam`, `crop`, `meme` (fundo preto, gancho grande em cima).

### Testes

```bash
python tests/test_offline.py   # detecção, áudio, render
python tests/test_local.py     # modo Claude Desktop: gerar + publicar/agendar (publicadores simulados)
python tests/test_studio.py    # modo HTTP (serve)
```
