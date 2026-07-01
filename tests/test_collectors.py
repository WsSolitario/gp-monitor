"""Tests básicos de collectors (sin necesidad de API real)."""

from __future__ import annotations

import sys
from pathlib import Path

# Permitir `python -m pytest` desde la raíz del proyecto.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gp_monitor.collectors import (  # noqa: E402
    NetworkRateCollector,
    collect_metrics,
    get_agent_version,
    get_os_info,
)


def test_get_os_info() -> None:
    info = get_os_info()
    assert isinstance(info, dict)
    assert "name" in info and info["name"]
    assert "version" in info
    assert "release" in info
    assert "arch" in info


def test_get_agent_version() -> None:
    v = get_agent_version()
    assert isinstance(v, str) and len(v) > 0


def test_collect_metrics_returns_known_keys() -> None:
    metrics = collect_metrics(net=NetworkRateCollector())
    expected = {
        "cpu_usage", "memory_usage", "disk_usage",
        "load_avg_1m", "load_avg_5m", "load_avg_15m",
        "network_rx_bps", "network_tx_bps",
        "uptime_seconds",
    }
    assert expected.issubset(metrics.keys()), (
        f"Faltan keys: {expected - set(metrics.keys())}"
    )


def test_collect_metrics_does_not_raise() -> None:
    # El colector debe ser totalmente defensivo.
    for _ in range(3):
        metrics = collect_metrics(net=NetworkRateCollector())
        # cpu/memory/disco suelen estar; los demás pueden ser None en Windows.
        assert metrics["cpu_usage"] is None or 0 <= metrics["cpu_usage"] <= 100
        assert metrics["memory_usage"] is None or 0 <= metrics["memory_usage"] <= 100
        assert metrics["disk_usage"] is None or 0 <= metrics["disk_usage"] <= 100


def test_network_collector_first_sample_is_zero() -> None:
    net = NetworkRateCollector()
    sample = net.sample()
    assert sample == {"network_rx_bps": 0, "network_tx_bps": 0}