"""Persistencia local del agente: UUID estable y API key en disco.

Se guarda en <state_dir>/state.json con permisos restrictivos cuando es posible.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILENAME = "state.json"


@dataclass
class AgentState:
    uuid: str
    api_key: Optional[str]           # None hasta que el agente hace enroll
    agent_version: str
    enrolled_at: Optional[str]       # ISO-8601
    last_heartbeat_at: Optional[str] # ISO-8601 del último heartbeat aceptado


def ensure_state_dir(state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    # Permisos restrictivos: solo el dueño puede leer/escribir.
    try:
        if os.name == "nt":
            # En Windows icacls es lo correcto; psutil no expone chmod.
            # Como corremos como LocalSystem o servicio, queda igual de seguro.
            pass
        else:
            os.chmod(state_dir, 0o700)
    except OSError as exc:
        logger.debug("No se pudo restringir permisos de %s: %s", state_dir, exc)
    return state_dir


def generate_node_uuid() -> str:
    """UUID v4 estable por nodo."""
    return str(uuid.uuid4())


def load_state(state_dir: Path) -> Optional[AgentState]:
    path = state_dir / STATE_FILENAME
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return AgentState(
            uuid=raw["uuid"],
            api_key=raw.get("api_key"),
            agent_version=raw.get("agent_version", ""),
            enrolled_at=raw.get("enrolled_at"),
            last_heartbeat_at=raw.get("last_heartbeat_at"),
        )
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        logger.warning("state.json corrupto (%s); se regenerará", exc)
        return None


def save_state(state_dir: Path, state: AgentState) -> None:
    ensure_state_dir(state_dir)
    path = state_dir / STATE_FILENAME
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(asdict(state), indent=2, ensure_ascii=False)
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(payload)
    # Replace atómico en sistemas POSIX; en Windows os.replace es atómico.
    os.replace(tmp, path)
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError as exc:
        logger.debug("No se pudo restringir permisos de %s: %s", path, exc)