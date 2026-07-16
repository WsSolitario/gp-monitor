"""gp-monitor: agente Python que envía métricas de servidores a gp-it.

Pensado para correr en Windows Server 2019/2022 (también Linux/macOS vía psutil).
La conexión es siempre saliente: el agente hace POST a la API cada N segundos.
No requiere abrir puertos en el servidor donde corre.
"""

__version__ = "1.0.1"
__all__ = ["__version__"]