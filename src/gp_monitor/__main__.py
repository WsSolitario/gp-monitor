"""Entry point CLI: `gp-monitor <comando>`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from gp_monitor import __version__
from gp_monitor.config import load_config


def _configure_utf8_io() -> None:
    """Reconfigura stdout/stderr a UTF-8.

    Sin esto, los prints con caracteres unicode (✓ ✗ …) lanzan
    UnicodeEncodeError en consolas Windows que usan cp1252.
    El bug aparecia al hacer `python -m gp_monitor start` desde
    un proceso con codificacion por defecto cp1252.

    Seguro de llamar: si reconfigure no esta disponible (Python < 3.7),
    intenta via sys.setdefaultencoding (deprecated pero funcional).
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        # Python 3.7+: API oficial
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
                continue
            except Exception:
                pass
        # Fallback para Python 3.6 (no deberia aplicar porque requires-python >= 3.9)
        try:
            stream.encoding = "utf-8"  # type: ignore[attr-defined]
        except Exception:
            pass


_configure_utf8_io()


def _setup_logging(level: str, log_file: Optional[str] = None) -> None:
    """Wrapper backwards-compat: delega a agent.setup_logging."""
    from gp_monitor.agent import setup_logging
    from pathlib import Path
    setup_logging(level, log_file, Path("."))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gp-monitor",
        description="Agente Python para monitoreo de servidores (gp-it)",
    )
    p.add_argument("--config", "-c", type=Path, help="Ruta a config.yaml")
    p.add_argument("--version", action="version", version=f"gp-monitor {__version__}")

    sub = p.add_subparsers(dest="command", required=False)

    sub.add_parser("run", help="Correr el agente en foreground (Ctrl+C para parar)")
    sub.add_parser("heartbeat", help="Enviar un único heartbeat y salir (debug)")

    sub.add_parser("install", help="Instalar como servicio de Windows")
    sub.add_parser("uninstall", help="Desinstalar el servicio de Windows")
    sub.add_parser("start", help="Arrancar el servicio de Windows")
    sub.add_parser("stop", help="Detener el servicio de Windows")
    sub.add_parser("status", help="Ver estado del servicio de Windows")

    sub.add_parser("version", help="Mostrar versión")

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    # ─── Comandos que NO requieren config ───────────────────────────────────
    if args.command in (None, "version"):
        print(f"gp-monitor {__version__}")
        return 0

    if args.command == "install":
        from gp_monitor.windows_service import install_service
        return install_service(config_path=args.config)

    if args.command == "uninstall":
        from gp_monitor.windows_service import uninstall_service
        return uninstall_service()

    if args.command == "start":
        from gp_monitor.windows_service import start_service
        return start_service()

    if args.command == "stop":
        from gp_monitor.windows_service import stop_service
        return stop_service()

    if args.command == "status":
        from gp_monitor.windows_service import service_status
        return service_status()

    # ─── Comandos que requieren config ──────────────────────────────────────
    try:
        config = load_config(explicit_path=args.config)
    except (ValueError, OSError) as exc:
        print(f"✗ Error cargando configuración: {exc}", file=sys.stderr)
        return 2

    _setup_logging(config.log_level, config.log_file)
    logger = logging.getLogger("gp_monitor.cli")

    if args.command == "run":
        from gp_monitor.agent import run_foreground
        logger.info("Arrancando agente en foreground…")
        return run_foreground(config)

    if args.command == "heartbeat":
        from gp_monitor.agent import run_once
        logger.info("Enviando un único heartbeat…")
        rc = run_once(config)
        if rc == 0:
            print("✓ Heartbeat enviado correctamente.")
        elif rc == 2:
            print("✗ No se pudo hacer enrollment.")
        else:
            print("✗ La API rechazó el heartbeat.")
        return rc

    # Si llegamos aquí, el comando es desconocido
    print("✗ Comando desconocido. Ejecuta `gp-monitor --help`.")
    return 2


if __name__ == "__main__":
    sys.exit(main())