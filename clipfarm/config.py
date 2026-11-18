from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")  # caminho fixo: o Claude Desktop inicia o processo em outra pasta
except ImportError:  # python-dotenv é opcional
    pass
EXAMPLE = ROOT / "config.example.yaml"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | Path | None = None) -> dict[str, Any]:
    """Carrega config.example.yaml como padrão e sobrescreve com config.yaml (ou o caminho dado)."""
    cfg = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    p = Path(path) if path else ROOT / "config.yaml"
    if p.exists():
        cfg = _merge(cfg, yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    data_dir = Path(cfg["paths"]["data_dir"])
    data_dir = data_dir.expanduser()
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    cfg["paths"]["data_dir"] = str(data_dir)
    for sub in ("raw", "clips", "tmp"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    return cfg


def env(name: str, required: bool = False) -> str | None:
    val = os.getenv(name)
    if required and not val:
        raise RuntimeError(f"Variável de ambiente {name} não definida (veja .env.example)")
    return val


def env_path(name: str, required: bool = False) -> str | None:
    """Caminho vindo do .env; relativo = relativo à pasta do projeto (não ao diretório atual)."""
    val = env(name, required)
    if not val:
        return None
    p = Path(val).expanduser()
    return str(p if p.is_absolute() else ROOT / p)


def channel_rules(cfg: dict) -> dict[str, dict]:
    """login -> regras (credit, facecam). Vazio = sem allowlist."""
    return {c["login"].lower(): c for c in (cfg.get("allowlist") or []) if c.get("login")}


def is_allowed(cfg: dict, login: str) -> bool:
    login = (login or "").lower()
    if login in {b.lower() for b in (cfg.get("blocklist") or [])}:
        return False
    rules = channel_rules(cfg)
    return not rules or login in rules
