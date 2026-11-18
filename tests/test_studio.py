"""Teste ponta a ponta do Studio: sobe o servidor, faz upload, e usa o conector MCP como o Claude usaria.

Rode: python tests/test_studio.py
"""
from __future__ import annotations

import asyncio
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp import Client  # noqa: E402

from clipfarm.render import probe  # noqa: E402
from clipfarm.studio.analyze import parse_vtt  # noqa: E402
from clipfarm.studio.server import create_app  # noqa: E402
from tests.test_offline import fake_transcriber, make_video  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="clipfarm_studio_"))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def text_of(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def data_of(result) -> dict:
    import json

    if result.structured_content:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(text_of(result))


def test_parse_vtt_rolling():
    vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
olha<00:00:01.500><c> aquele</c><00:00:02.000><c> carro</c>

00:00:03.000 --> 00:00:03.010
olha aquele carro

00:00:03.010 --> 00:00:05.000
olha aquele carro
vou pegar ele
"""
    segs = parse_vtt(vtt)
    assert [s.text for s in segs] == ["olha aquele carro", "vou pegar ele"], segs


def test_studio_end_to_end():
    port = free_port()
    cfg = TMP / "config.yaml"
    cfg.write_text(f"paths: {{data_dir: '{TMP / 'data'}'}}\n"
                   f"studio: {{public_url: 'http://127.0.0.1:{port}', export_dir: '{TMP / 'cortes'}'}}\n")
    app = create_app(str(cfg), transcriber=fake_transcriber)
    user = app.state.store.create_user("Teste", handle="@cortesgta6br")
    tok = user["token"]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if httpx.get(f"{base}/health").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)

    # upload pelo painel (como o usuário faria)
    video = make_video(TMP / "fake.mp4")
    r = httpx.post(f"{base}/u/{tok}/upload", files={"arquivo": ("live.mp4", video.read_bytes(), "video/mp4")})
    assert r.status_code == 200, r.text
    media_id = r.json()["media_id"]
    assert httpx.post(f"{base}/u/nao-existe/upload", files={"arquivo": ("x.mp4", b"x")}).status_code == 404

    async def flow():
        async with Client(f"{base}/u/{tok}/mcp") as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"analisar_video", "status_video", "criar_cortes", "status_cortes", "variar_corte",
                    "ajustar_corte", "ver_frame", "ler_transcricao", "meu_painel"} <= names, names
            prompts = {p.name for p in (await c.list_prompts()).prompts}
            assert "gerar_cortes" in prompts

            bad = await c.call_tool("analisar_video", {"url": "http://169.254.169.254/latest"})
            assert bad.is_error and "não suportada" in text_of(bad)

            st = {}
            for _ in range(10):
                st = data_of(await c.call_tool("status_video", {"media_id": media_id, "aguardar_s": 20}))
                if st["status"] in ("ready", "failed"):
                    break
            assert st["status"] == "ready", st
            top = st["candidatos"][0]
            assert abs(top["pico_s"] - 60) < 3, top
            assert "ACREDITO" in top["fala"]

            tr = text_of(await c.call_tool("ler_transcricao", {"media_id": media_id, "inicio_s": 50, "fim_s": 65}))
            assert "[59s]" in tr or "[58s]" in tr, tr

            frame = await c.call_tool("ver_frame", {"media_id": media_id, "tempo_s": 30})
            assert frame.content[0].type == "image" and len(frame.content[0].data) > 1000

            await c.call_tool("configurar_pagina", {"arroba": "@cortesgta6br", "credito_padrao": "twitch.tv/teste"})
            res = data_of(await c.call_tool("criar_cortes", {"media_id": media_id, "cortes": [
                {"inicio_s": 50, "fim_s": 66, "titulo": "Ele não acreditou", "gancho": "ELE NÃO ESPERAVA ISSO",
                 "estilo": "destaque", "layout": "meme"},
                {"inicio_s": 51, "fim_s": 65, "titulo": "Facecam", "gancho": "olha a reação",
                 "estilo": "verde", "layout": "facecam", "facecam": {"x": 0, "y": 0, "w": 0.24, "h": 0.3}},
            ]}))
            ids = res["ids"]
            invalid = await c.call_tool("criar_cortes", {"media_id": media_id, "cortes": [
                {"inicio_s": 10, "fim_s": 200, "titulo": "longo demais"}]})
            assert invalid.is_error

            done = {}
            for _ in range(10):
                done = data_of(await c.call_tool("status_cortes", {"ids": ids, "aguardar_s": 30}))
                if done["pendentes"] == 0:
                    break
            assert all(x["status"] == "done" for x in done["cortes"]), done
            var = data_of(await c.call_tool("variar_corte", {"clip_id": ids[0], "estilos": ["minimal", "meme"],
                                                              "layouts": ["blur"]}))
            adj = data_of(await c.call_tool("ajustar_corte", {"clip_id": ids[0], "fim_s": 64,
                                                               "gancho": "NINGUÉM ESPERAVA"}))
            all_ids = var["ids"] + [adj["id"]]
            for _ in range(10):
                done2 = data_of(await c.call_tool("status_cortes", {"ids": all_ids, "aguardar_s": 30}))
                if done2["pendentes"] == 0:
                    break
            assert all(x["status"] == "done" for x in done2["cortes"]), done2
            return done["cortes"] + done2["cortes"]

        # token inválido
    clips = asyncio.run(flow())

    async def bad_token():
        async with Client(f"{base}/u/token-errado/mcp") as c:
            r = await c.call_tool("meu_painel", {})
            assert r.is_error and "Token inválido" in text_of(r)

    asyncio.run(bad_token())

    for cl in clips:
        r = httpx.get(cl["download"])
        assert r.status_code == 200 and len(r.content) > 10_000
        out = TMP / f"dl_{cl['id']}.mp4"
        out.write_bytes(r.content)
        info = probe(str(out))
        assert (info["w"], info["h"]) == (1080, 1920), info
        import subprocess

        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "8.5", "-i", str(out), "-frames:v", "1",
                        str(TMP / f"frame_{cl['id']}.png")], check=True)
    page = httpx.get(f"{base}/u/{tok}").text
    assert "Baixar" in page and "@cortesgta6br" in page
    assert httpx.get(f"{base}/u/{tok}/c/9999.mp4").status_code == 404
    server.should_exit = True
    th.join(timeout=10)
    print("frames em", TMP)


if __name__ == "__main__":
    test_parse_vtt_rolling()
    print("ok parse_vtt")
    test_studio_end_to_end()
    print("ok studio e2e")
