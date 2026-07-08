"""Cliente HTTP para gp-it.

Endpoints:
  POST /api/v1/monitor/enroll                                (sin auth, usa enrollmentToken)
  POST /api/v1/monitor/nodes/:nodeUuid/heartbeat             (x-monitor-api-key)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
USER_AGENT = "gp-monitor-agent/1.0 (+https://api.dev.gp.ssdevsolutions.com)"


class MonitorApiError(RuntimeError):
    """Error devuelto por la API o por la red."""

    def __init__(self, message: str, status_code: Optional[int] = None,
                 details: Optional[Any] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.details = details


class MonitorClient:
    """Cliente HTTP para gp-monitor API.

    Configúralo una vez con la URL base y úsalo para enroll + heartbeat.
    """

    def __init__(self, api_url: str, timeout: int = DEFAULT_TIMEOUT,
             verify_ssl: bool = True, api_key: str = "") -> None:
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.api_key = api_key
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })

    # ─── Helpers ─────────────────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        return f"{self.api_url}{path}"

    @staticmethod
    def _raise_for_api_error(resp: requests.Response) -> None:
        """Levanta MonitorApiError si la respuesta no es 2xx."""
        if 200 <= resp.status_code < 300:
            return
        try:
            data = resp.json()
            message = data.get("error") or data.get("message") or resp.text or "Error API"
            details = data.get("details")
        except ValueError:
            message = resp.text or f"HTTP {resp.status_code}"
            details = None
        raise MonitorApiError(message, status_code=resp.status_code, details=details)

    # ─── Enroll ──────────────────────────────────────────────────────────────
    def enroll(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Registra (o re-registra) un nodo. Devuelve {apiKey, nodeUuid}."""
        url = self._url("/api/v1/monitor/enroll")
        logger.debug("POST %s payload=%s", url, {k: v for k, v in payload.items() if k != "enrollmentToken"})
        try:
            resp = self._session.post(
                url, json=payload, timeout=self.timeout, verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise MonitorApiError(f"Error de red en enroll: {exc}") from exc

        self._raise_for_api_error(resp)
        return resp.json()

    # ─── Heartbeat ───────────────────────────────────────────────────────────
    def send_heartbeat(self, node_uuid: str, api_key: str,
                       payload: Dict[str, Any]) -> Dict[str, Any]:
        url = self._url(f"/api/v1/monitor/nodes/{node_uuid}/heartbeat")
        headers = {"x-monitor-api-key": api_key}
        logger.debug("POST %s (cpu=%s mem=%s disk=%s)",
                     url, payload.get("cpuUsage"), payload.get("memoryUsage"), payload.get("diskUsage"))
        try:
            resp = self._session.post(
                url, json=payload, timeout=self.timeout, verify=self.verify_ssl,
                headers=headers,
            )
        except requests.RequestException as exc:
            raise MonitorApiError(f"Error de red en heartbeat: {exc}") from exc

        self._raise_for_api_error(resp)
        return resp.json()

    # ─── Health (opcional, para debugging) ───────────────────────────────────
    def health(self) -> Dict[str, Any]:
        url = self._url("/health")
        try:
            resp = self._session.get(url, timeout=self.timeout, verify=self.verify_ssl)
            self._raise_for_api_error(resp)
            return resp.json()
        except requests.RequestException as exc:
            raise MonitorApiError(f"Error de red en /health: {exc}") from exc

    # ─── Tasks (poll de comandos remotos) ───────────────────────────────────
    def get_pending_tasks(self, node_uuid: str) -> list:
        url = self._url(f"/api/v1/monitor/nodes/{node_uuid}/tasks/pending")
        try:
            resp = self._session.get(
                url, timeout=self.timeout, verify=self.verify_ssl,
                headers={"x-monitor-api-key": self.api_key},
            )
            self._raise_for_api_error(resp)
            data = resp.json()
            return data.get("tasks", [])
        except requests.RequestException as exc:
            raise MonitorApiError(f"Error de red en get_pending_tasks: {exc}") from exc

    def post_task_result(self, node_uuid: str, task_uuid: str, payload: dict) -> dict:
        url = self._url(f"/api/v1/monitor/nodes/{node_uuid}/tasks/{task_uuid}/result")
        try:
            resp = self._session.post(
                url, json=payload, timeout=self.timeout,
                verify=self.verify_ssl,
                headers={"x-monitor-api-key": self.api_key},
            )
            self._raise_for_api_error(resp)
            return resp.json()
        except requests.RequestException as exc:
            raise MonitorApiError(f"Error de red en post_task_result: {exc}") from exc