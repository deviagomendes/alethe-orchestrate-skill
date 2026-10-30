"""HTTP client for the local jevos runtime (/health and /v1/systemone). Standard library only."""
import json
import urllib.error
import urllib.request
from typing import Any, Dict

from .contracts import DecisionError

MAX_RESPONSE_BYTES = 1 << 20


class Client:
    def __init__(self, endpoint: str, timeout: float = 5.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        # No proxy: the runtime is on this machine, and a proxy must never see a task.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, path: str, payload: Any = None) -> Dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint + path, data=data, headers={"Content-Type": "application/json"} if data else {}
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, OSError, ValueError) as error:  # URLError covers HTTPError and timeouts
            raise DecisionError(f"decision runtime unreachable: {error}") from error
        if len(body) > MAX_RESPONSE_BYTES:
            raise DecisionError("decision runtime answered with too much data")
        try:
            result = json.loads(body.decode("utf-8"))
        except ValueError as error:
            raise DecisionError("decision runtime answered with invalid JSON") from error
        if not isinstance(result, dict):
            raise DecisionError("decision runtime answered with an unexpected shape")
        return result

    def health(self) -> Dict[str, Any]:
        return self._call("/health")

    def systemone(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("/v1/systemone", payload)
