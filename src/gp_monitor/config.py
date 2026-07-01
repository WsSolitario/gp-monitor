"""Configuración del agente gp-monitor.

Carga en este orden de prioridad (último gana):
  1. Defaults internos
  2. config/config.yaml (junto al ejecutable o en cwd)
  3. Variables de entorno (GP_MONITOR_*)
"""

from __future__ import annotations

import logging
import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config") / "config.yaml"
ALT_CONFIG_PATHS = [
    Path("config.yaml"),
    Path("config") / "config.example.yaml",
]


@dataclass
class Config:
    """Configuración del agente."""

    api_url: str = "https://api.dev.gp.ssdevsolutions.com"
    enrollment_token: str = ""

    name: str = ""
    hostname: str = ""
    agency: str = ""
    environment: str = "production"
    description: str = ""

    heartbeat_interval_seconds: int = 60
    http_timeout_seconds: int = 15

    log_level: str = "INFO"
    log_file: Optional[str] = None

    state_dir: str = "C:/ProgramData/gp-monitor"

    # ─── extra ──────────────────────────────────────────────────────────────
    extra: dict = field(default_factory=dict)

    # ─── helpers ────────────────────────────────────────────────────────────
    @property
    def resolved_hostname(self) -> str:
        return self.hostname.strip() or socket.gethostname()

    @property
    def resolved_name(self) -> str:
        return self.name.strip() or self.resolved_hostname

    def state_dir_path(self) -> Path:
        p = Path(self.state_dir)
        if p.is_absolute():
            return p
        # Si es relativo, lo anclamos al directorio del ejecutable.
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        return (base / p).resolve()

    def to_dict(self) -> dict:
        return {
            "api_url": self.api_url,
            "enrollment_token": "***" if self.enrollment_token else "",
            "name": self.name,
            "hostname": self.hostname,
            "agency": self.agency,
            "environment": self.environment,
            "description": self.description,
            "heartbeat_interval_seconds": self.heartbeat_interval_seconds,
            "http_timeout_seconds": self.http_timeout_seconds,
            "log_level": self.log_level,
            "log_file": self.log_file,
            "state_dir": self.state_dir,
        }


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            logger.warning("config.yaml root must be a mapping; ignoring")
            return {}
        return data
    except yaml.YAMLError as exc:
        logger.warning("Error parseando %s: %s", path, exc)
        return {}
    except OSError as exc:
        logger.warning("No se pudo leer %s: %s", path, exc)
        return {}


def _apply_env_overrides(cfg: Config) -> None:
    """Override config fields con variables GP_MONITOR_*."""
    env = os.environ

    def _set(name: str, attr: str, cast=str):
        v = env.get(name)
        if v is not None and v != "":
            try:
                setattr(cfg, attr, cast(v))
            except (TypeError, ValueError):
                logger.warning("Variable %s con valor inválido: %r", name, v)

    _set("GP_MONITOR_API_URL", "api_url")
    _set("GP_MONITOR_ENROLLMENT_TOKEN", "enrollment_token")

    _set("GP_MONITOR_NAME", "name")
    _set("GP_MONITOR_AGENCY", "agency")
    _set("GP_MONITOR_ENVIRONMENT", "environment")
    _set("GP_MONITOR_DESCRIPTION", "description")

    _set("GP_MONITOR_HEARTBEAT_INTERVAL_SECONDS", "heartbeat_interval_seconds", int)
    _set("GP_MONITOR_HTTP_TIMEOUT_SECONDS", "http_timeout_seconds", int)

    _set("GP_MONITOR_LOG_LEVEL", "log_level")
    _set("GP_MONITOR_LOG_FILE", "log_file")

    _set("GP_MONITOR_STATE_DIR", "state_dir")


def load_config(explicit_path: Optional[Path] = None) -> Config:
    """Carga la configuración desde YAML + .env + ENV vars."""
    # Cargar .env si existe (no falla si no está).
    load_dotenv(dotenv_path=Path(".env"), override=False)

    yaml_path: Optional[Path] = None
    if explicit_path is not None:
        yaml_path = explicit_path
    elif DEFAULT_CONFIG_PATH.is_file():
        yaml_path = DEFAULT_CONFIG_PATH
    else:
        for p in ALT_CONFIG_PATHS:
            if p.is_file():
                yaml_path = p
                break

    raw = _load_yaml(yaml_path) if yaml_path else {}
    if yaml_path:
        logger.debug("Configuración cargada desde %s", yaml_path)

    cfg = Config()

    # Aplicar YAML
    for k, v in raw.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
        else:
            cfg.extra[k] = v

    # Override con ENV vars
    _apply_env_overrides(cfg)

    # Normalizar api_url (quitar slash final)
    cfg.api_url = cfg.api_url.rstrip("/")

    # Validaciones básicas
    if not cfg.api_url:
        raise ValueError("api_url es requerido (config.yaml o GP_MONITOR_API_URL).")
    if not cfg.api_url.startswith(("http://", "https://")):
        raise ValueError(f"api_url debe empezar con http:// o https://: {cfg.api_url!r}")

    if cfg.heartbeat_interval_seconds < 10:
        logger.warning("heartbeat_interval_seconds=%d es muy bajo; ajustando a 10",
                       cfg.heartbeat_interval_seconds)
        cfg.heartbeat_interval_seconds = 10

    return cfg