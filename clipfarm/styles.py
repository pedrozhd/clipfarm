"""Presets de legenda. Cores ASS: &HAABBGGRR."""
from __future__ import annotations

STYLES: dict[str, dict] = {
    # identidade Flagra Neon: palavra ativa em ciano (#19E3FF), tarja do gancho magenta (#FF2D87)
    "flagra": {"font": "Poppins", "font_size": 124, "upper": True, "words_per_caption": 3,
               "caption_color": "&H00FFFFFF", "highlight_color": "&H00FFE319", "outline": 7, "shadow": 3,
               "pop": True, "y_offset": 0, "hook_color": "&H00872DFF", "hook_font": "Anton", "hook_scale": 0.95},
    # palavra ativa em amarelo, 3 palavras, caixa alta (estilo "Hormozi")
    "destaque": {"font": "Poppins", "font_size": 100, "upper": True, "words_per_caption": 3,
                 "caption_color": "&H00FFFFFF", "highlight_color": "&H0000E5FF", "outline": 7, "shadow": 3,
                 "pop": True, "y_offset": 0},
    # 2 palavras grandes, ativa em verde
    "verde": {"font": "Poppins", "font_size": 112, "upper": True, "words_per_caption": 2,
              "caption_color": "&H00FFFFFF", "highlight_color": "&H005AFF00", "outline": 8, "shadow": 3,
              "pop": True, "y_offset": 0},
    # frase corrida, discreta, no terço inferior
    "minimal": {"font": "Inter", "font_size": 64, "upper": False, "words_per_caption": 6,
                "caption_color": "&H00FFFFFF", "highlight_color": None, "outline": 4, "shadow": 1,
                "pop": False, "y_offset": 220},
    # fonte condensada com contorno grosso, cara de meme
    "meme": {"font": "Anton", "font_size": 118, "upper": True, "words_per_caption": 4,
             "caption_color": "&H00FFFFFF", "highlight_color": None, "outline": 10, "shadow": 0,
             "pop": False, "y_offset": 0},
}

LAYOUTS = ("auto", "blur", "facecam", "pip", "crop", "meme")


def resolve(rc: dict, style: str | None) -> dict:
    """Mescla o preset escolhido sobre a config de render."""
    out = dict(rc)
    out.update({"upper": True, "outline": 7, "shadow": 3, "pop": True, "y_offset": 0})
    if style:
        if style not in STYLES:
            raise ValueError(f"estilo desconhecido: {style}. Opções: {', '.join(STYLES)}")
        out.update(STYLES[style])
    return out
