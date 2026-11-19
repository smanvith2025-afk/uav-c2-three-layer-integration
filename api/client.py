"""Python client for the HTTP/REST and TCP external security API."""

from __future__ import annotations

import json
import socket
import struct
import urllib.error
import urllib.request
from typing import Any


class SecurityPipelineClient:
    """Reference client for Unreal Engine / Cosys-AirSim external integration."""

    def __init__(
        self,
        protocol: str = "http",
        host: str = "127.0.0.1",
        port: int = 8765,
        timeout: float = 5.0,
        api_key: str | None = None,
    ) -> None:
        if protocol not in ("http", "tcp"):
            raise ValueError("protocol must be 'http' or 'tcp'")
        self.protocol = protocol
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.api_key = api_key

    def _http_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"http://{self.host}:{self.port}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        data: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            data = json.dumps(body, allow_nan=False).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
                parsed.setdefault("http_status", exc.code)
                return parsed
            except json.JSONDecodeError:
                return {"http_status": exc.code, "error": raw}

    def _tcp_request(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        req_obj: dict[str, Any] = {
            "action": action,
            "payload": payload or {},
        }
        if self.api_key:
            req_obj["api_key"] = self.api_key

        raw = json.dumps(req_obj, allow_nan=False).encode("utf-8")
        frame = struct.pack("!I", len(raw)) + raw

        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            sock.sendall(frame)
            len_hdr = self._recv_exact(sock, 4)
            resp_len = struct.unpack("!I", len_hdr)[0]
            resp_raw = self._recv_exact(sock, resp_len)
            envelope = json.loads(resp_raw.decode("utf-8"))
            return envelope.get("data", envelope)

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("TCP connection closed prematurely")
            buf.extend(chunk)
        return bytes(buf)

    def health(self) -> dict[str, Any]:
        if self.protocol == "http":
            return self._http_request("GET", "/api/v1/health")
        return self._tcp_request("health")

    def get_status(self) -> dict[str, Any]:
        if self.protocol == "http":
            return self._http_request("GET", "/api/v1/status")
        return self._tcp_request("status")

    def get_telemetry(self) -> dict[str, Any]:
        if self.protocol == "http":
            return self._http_request("GET", "/api/v1/telemetry")
        return self._tcp_request("telemetry")

    def establish_session(self) -> dict[str, Any]:
        if self.protocol == "http":
            return self._http_request("POST", "/api/v1/session/handshake", {})
        return self._tcp_request("handshake")

    def rekey_session(self) -> dict[str, Any]:
        if self.protocol == "http":
            return self._http_request("POST", "/api/v1/session/rekey", {})
        return self._tcp_request("rekey")

    def terminate_session(self) -> dict[str, Any]:
        if self.protocol == "http":
            return self._http_request("POST", "/api/v1/session/terminate", {})
        return self._tcp_request("terminate")

    def send_command(
        self,
        command_type: str,
        parameters: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "command_type": command_type,
            "parameters": parameters or {},
        }
        if message_id:
            body["message_id"] = message_id
        if self.protocol == "http":
            return self._http_request("POST", "/api/v1/command", body)
        return self._tcp_request("command", body)

    def poll_pending_commands(self) -> dict[str, Any]:
        if self.protocol == "http":
            return self._http_request("GET", "/api/v1/commands/pending")
        return self._tcp_request("pending_commands")

    def ack_command(self, command_id: str) -> dict[str, Any]:
        body = {"command_id": command_id}
        if self.protocol == "http":
            return self._http_request("POST", "/api/v1/commands/ack", body)
        return self._tcp_request("ack_command", body)

    def complete_landing(self) -> dict[str, Any]:
        if self.protocol == "http":
            return self._http_request("POST", "/api/v1/commands/land_complete", {})
        return self._tcp_request("land_complete")

    def push_unreal_telemetry(self, telemetry: dict[str, Any]) -> dict[str, Any]:
        if self.protocol == "http":
            return self._http_request("POST", "/api/v1/telemetry/update", telemetry)
        return self._tcp_request("telemetry_update", telemetry)
