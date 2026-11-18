"""Modo pessoal: MCP local (sem HTTP, sem token) + publicação/agendamento com publicadores falsos.

Rode: python tests/test_local.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp import Client  # noqa: E402

from clipfarm.studio.server import Studio  # noqa: E402
from tests.test_offline import fake_transcriber, make_video  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="clipfarm_local_"))


def data_of(r):
    if r.structured_content:
        sc = r.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads("".join(getattr(c, "text", "") for c in r.content))


def err_of(r) -> str:
    assert r.is_error, r
    return "".join(getattr(c, "text", "") for c in r.content)


def test_local_generate_and_publish():
    cfg = TMP / "config.yaml"
    cfg.write_text(f"paths: {{data_dir: '{TMP / 'data'}'}}\nstudio: {{export_dir: '{TMP / 'cortes'}'}}\n")
    sent = []

    def fake_pub(name):
        def pub(post, clip):
            assert Path(clip["path"]).exists()
            sent.append((name, post["clip_id"], post["caption"]))
            return f"{name}-{post['id']}"
        return pub

    st = Studio(str(cfg), transcriber=fake_transcriber,
                publishers={"youtube": fake_pub("youtube"), "instagram": fake_pub("instagram")})
    st.worker.start()

    # vídeo "subido" pelo usuário local
    user = st.local_user()
    m = st.store.add_media(user["id"], "upload", "live.mp4", st.base_dir, title="live.mp4", status="uploading")
    shutil.copy(make_video(TMP / "fake.mp4"), Path(m["dir"]) / "source.mp4")
    st.store.set_media(m["id"], status="queued")

    async def flow():
        async with Client(st.mcp) as c:  # conexão em processo = igual ao stdio (sem request HTTP)
            await c.call_tool("configurar_pagina", {"arroba": "@cortesgta6br"})
            for _ in range(10):
                s = data_of(await c.call_tool("status_video", {"media_id": m["id"], "aguardar_s": 20}))
                if s["status"] == "ready":
                    break
            assert s["status"] == "ready", s
            ids = data_of(await c.call_tool("criar_cortes", {"media_id": m["id"], "cortes": [
                {"inicio_s": 50, "fim_s": 66, "titulo": "Ele não acreditou", "gancho": "ELE NÃO ESPERAVA",
                 "layout": "meme", "credito": "twitch.tv/teste"},
                {"inicio_s": 52, "fim_s": 64, "titulo": "Segundo corte", "layout": "blur"}]}))["ids"]
            for _ in range(10):
                done = data_of(await c.call_tool("status_cortes", {"ids": ids, "aguardar_s": 30}))
                if done["pendentes"] == 0:
                    break
            for cl in done["cortes"]:
                assert cl["status"] == "done" and Path(cl["arquivo"]).exists(), cl
                assert Path(cl["arquivo"]).parent == TMP / "cortes"
            assert "download" not in done["cortes"][0]  # modo local entrega arquivo, não URL

            contas = data_of(await c.call_tool("contas_conectadas", {}))
            assert contas["conectadas"] == {"youtube": True, "tiktok": False, "instagram": True}, contas

            # publicar agora em 2 plataformas
            pub = data_of(await c.call_tool("publicar_corte", {"clip_id": ids[0],
                                                                "plataformas": ["youtube", "instagram"]}))
            assert len(pub["posts"]) == 2
            # duplicado, plataforma desconectada, horário no passado
            assert "já está" in err_of(await c.call_tool("publicar_corte", {"clip_id": ids[0],
                                                                            "plataformas": ["youtube"]}))
            assert "conecte antes" in err_of(await c.call_tool("publicar_corte", {"clip_id": ids[1],
                                                                                  "plataformas": ["tiktok"]}))
            assert "passado" in err_of(await c.call_tool("publicar_corte", {
                "clip_id": ids[1], "plataformas": ["youtube"], "quando": "2020-01-01 10:00"}))

            # agendar para amanhã 18h (horário de Brasília) e cancelar
            amanha = (datetime.now(ZoneInfo("America/Sao_Paulo")) + timedelta(days=1)).strftime("%Y-%m-%d")
            ag = data_of(await c.call_tool("publicar_corte", {"clip_id": ids[1], "plataformas": ["youtube"],
                                                               "quando": f"{amanha} 18:00",
                                                               "legenda": "Legenda manual #gta6"}))
            post = ag["posts"][0]
            assert post["status"] == "scheduled" and post["quando"].endswith("18:00"), post
            utc = st.store.post(post["post_id"])["publish_at"]
            assert utc.endswith("21:00:00+00:00"), utc  # 18h BRT = 21h UTC

            for _ in range(15):
                sp = data_of(await c.call_tool("status_publicacoes", {"clip_id": ids[0]}))
                if all(p["status"] == "ok" for p in sp["posts"]):
                    break
                await asyncio.sleep(2)
            assert all(p["status"] == "ok" for p in sp["posts"]), sp
            assert any(p.get("link", "").startswith("https://youtube.com/shorts/") for p in sp["posts"])

            canc = data_of(await c.call_tool("cancelar_publicacao", {"post_id": post["post_id"]}))
            assert canc["status"] == "canceled"
            assert "não dá" in err_of(await c.call_tool("cancelar_publicacao", {"post_id": post["post_id"]}))

    asyncio.run(flow())
    st.worker.stop()
    assert sorted(s[0] for s in sent) == ["instagram", "youtube"], sent
    cap = sent[0][2]
    assert "Ele não acreditou" in cap and "twitch.tv/teste" in cap and "#gta6" in cap, cap
    print("cortes em", TMP / "cortes", [p.name for p in (TMP / "cortes").iterdir()])


if __name__ == "__main__":
    test_local_generate_and_publish()
    print("ok local + publicação")
