"""Validador de allowlist de comandos para gp-monitor.

Lee `policy.toml` (junto al codigo fuente) y expone `is_allowed()` para
verificar si un comando puede ejecutarse sin `allow_arbitrary=True`.

Sintaxis de policy.toml: una lista de bloques [[allowed]] con:
  - pattern: prefijo del comando (case-insensitive). Coincide si el
             comando (trimmed) empieza con `pattern` o con `pattern + ' '`.
  - description: texto legible para el dashboard
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yaml

logger = logging.getLogger("gp_monitor.allowlist")

# Paths donde buscar policy.toml, en orden de prioridad
_DEFAULT_POLICY_PATHS = [
    Path("policy.toml"),
    Path(__file__).resolve().parent.parent.parent / "policy.toml",
    # Para installs del sistema (egg / wheel) sin acceso al repo
    Path("/etc/gp-monitor/policy.toml"),
]


@dataclass
class AllowedCommand:
    pattern: str          # prefijo del comando
    description: str
    regex: re.Pattern     # compilado (case-insensitive)


class CommandAllowlist:
    """Carga policy.toml y valida comandos contra la lista."""

    def __init__(self, allowed: List[AllowedCommand]):
        self.allowed = allowed

    @classmethod
    def load(cls, explicit_path: Optional[Path] = None) -> "CommandAllowlist":
        path = explicit_path
        if path is None:
            for candidate in _DEFAULT_POLICY_PATHS:
                if candidate.is_file():
                    path = candidate
                    break

        if path is None:
            logger.warning(
                "policy.toml no encontrado en ninguna ruta estandar; "
                "allowlist queda vacia (todos los free-form seran rechazados)."
            )
            return cls(allowed=[])

        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.error("Error leyendo %s: %s", path, exc)
            return cls(allowed=[])

        allowed_list = raw.get("allowed") or []
        allowed: List[AllowedCommand] = []
        for item in allowed_list:
            pattern = (item.get("pattern") or "").strip()
            description = (item.get("description") or "").strip()
            if not pattern:
                continue
            # Coincide si el comando (trimmed + lowercase) empieza con
            # `pattern` o `pattern + ' '`. Esto evita que "Get-ProcessMalware"
            # matchee "Get-Process".
            try:
                regex = re.compile(
                    r"^\s*" + re.escape(pattern) + r"(\s|$)",
                    re.IGNORECASE | re.DOTALL,
                )
            except re.error as exc:
                logger.error("Pattern invalido en policy.toml: %r (%s)", pattern, exc)
                continue
            allowed.append(AllowedCommand(
                pattern=pattern,
                description=description,
                regex=regex,
            ))

        logger.info("Allowlist cargada: %d comandos desde %s", len(allowed), path)
        return cls(allowed=allowed)

    def is_allowed(self, command: str) -> bool:
        """True si `command` empieza con uno de los patrones."""
        if not command or not self.allowed:
            return False
        for ac in self.allowed:
            if ac.regex.match(command):
                return True
        return False

    def list_descriptions(self) -> List[dict]:
        """Devuelve la allowlist como [{pattern, description}] para la UI."""
        return [
            {"pattern": ac.pattern, "description": ac.description}
            for ac in self.allowed
        ]

    def __len__(self) -> int:
        return len(self.allowed)
