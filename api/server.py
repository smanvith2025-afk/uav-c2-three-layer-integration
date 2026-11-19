"""External HTTP/REST and Length-Prefixed TCP servers for UE 5.8 and Cosys-AirSim."""

from __future__ import annotations

import hmac
import json
import socket
import socketserver
import struct
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from common.errors import ErrorCode, IntegrationError
from communication.pipeline import ThreeLayerSecurityPipeline

MAX_API_BODY_BYTES = 1_048_576


def _reject_nan(val: str) -> None:
    raise ValueError(f"Non-finite JSON constant rejected: {val}")


def _parse_json_body(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    if len(raw) > MAX_API_BODY_BYTES:
        raise IntegrationError(
            f"Request exceeds maximum size ({MAX_API_BODY_BYTES} bytes)",
            error_code=ErrorCode.API_MALFORMED_REQUEST,
            layer="api",
        )
    try:
        obj = json.loads(raw.decode("utf-8"), parse_constant=_reject_nan)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise IntegrationError(
            f"Invalid JSON payload: {exc}",
            error_code=ErrorCode.API_MALFORMED_REQUEST,
            layer="api",
        ) from exc
    if not isinstance(obj, dict):
        raise IntegrationError(
            "JSON root must be an object",
            error_code=ErrorCode.API_MALFORMED_REQUEST,
            layer="api",
        )
    return obj


def _dispatch_action(
    pipeline: ThreeLayerSecurityPipeline,
    action: str,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Shared request dispatcher used by both HTTP/REST and TCP servers."""
    if action == "health":
        return HTTPStatus.OK, {
            "status": "ok",
            "service": "uav-c2-three-layer-integration",
            "error_code": ErrorCode.OK.value,
        }
    if action == "status":
        return HTTPStatus.OK, pipeline.get_status()
    if action == "telemetry":
        return HTTPStatus.OK, pipeline.get_telemetry().to_dict()
    if action == "pending_commands":
        return HTTPStatus.OK, {
            "commands": pipeline.bridge.peek_pending_commands(),
            "error_code": ErrorCode.OK.value,
        }
    if action == "handshake":
        try:
            sess = pipeline.establish_session()
            return HTTPStatus.OK, {
                "status": "established",
                "error_code": ErrorCode.OK.value,
                "session": sess,
            }
        except Exception as exc:
            return HTTPStatus.INTERNAL_SERVER_ERROR, {
                "status": "failed",
                "error_code": ErrorCode.L2_HANDSHAKE_FAILED.value,
                "reason": str(exc),
            }
    if action == "rekey":
        try:
            sess = pipeline.rekey_session()
            return HTTPStatus.OK, {
                "status": "rekeyed",
                "error_code": ErrorCode.OK.value,
                "session": sess,
            }
        except Exception as exc:
            return HTTPStatus.CONFLICT, {
                "status": "failed",
                "error_code": ErrorCode.L2_REKEY_FAILED.value,
                "reason": str(exc),
            }
    if action == "terminate":
        sess = pipeline.terminate_session()
        return HTTPStatus.OK, {
            "status": "terminated",
            "error_code": ErrorCode.OK.value,
            "session": sess,
        }
    if action == "command":
        if not payload:
            return HTTPStatus.BAD_REQUEST, {
                "accepted": False,
                "executed": False,
                "error_code": ErrorCode.API_MALFORMED_REQUEST.value,
                "reason": "Command payload cannot be empty",
            }
        resp = pipeline.process_command(payload)
        status = (
            HTTPStatus.BAD_REQUEST
            if resp.error_code == ErrorCode.API_MALFORMED_REQUEST.value
            else HTTPStatus.OK
        )
        return status, resp.to_dict()
    if action == "ack_command":
        cmd_id = payload.get("command_id", "")
        if not isinstance(cmd_id, str) or not cmd_id:
            return HTTPStatus.BAD_REQUEST, {
                "acknowledged": False,
                "error_code": ErrorCode.API_MALFORMED_REQUEST.value,
                "reason": "Missing command_id",
            }
        found = pipeline.bridge.ack_command(cmd_id)
        return (
            (HTTPStatus.OK if found else HTTPStatus.NOT_FOUND),
            {
                "acknowledged": found,
                "command_id": cmd_id,
                "error_code": ErrorCode.OK.value if found else ErrorCode.API_MALFORMED_REQUEST.value,
            },
        )
    if action == "land_complete":
        ok, msg = pipeline.complete_landing()
        return HTTPStatus.OK, {
            "accepted": ok,
            "message": msg,
            "state": pipeline.layer3.get_state(),
            "error_code": ErrorCode.OK.value if ok else ErrorCode.L3_STATE_VIOLATION.value,
        }
    if action == "telemetry_update":
        try:
            frame = pipeline.push_unreal_telemetry(payload)
            return HTTPStatus.OK, {
                "status": "updated",
                "error_code": ErrorCode.OK.value,
                "telemetry": frame.to_dict(),
            }
        except (TypeError, ValueError) as exc:
            return HTTPStatus.BAD_REQUEST, {
                "status": "failed",
                "error_code": ErrorCode.API_MALFORMED_REQUEST.value,
                "reason": f"Invalid telemetry field: {exc}",
            }

    return HTTPStatus.NOT_FOUND, {
        "error_code": ErrorCode.API_MALFORMED_REQUEST.value,
        "reason": f"Unknown action or endpoint: {action}",
    }


class _IntegratedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        pipeline: ThreeLayerSecurityPipeline,
        request_timeout: float,
        require_api_key: bool,
        api_key: str | None,
    ) -> None:
        self.pipeline = pipeline
        self.request_timeout = float(request_timeout)
        self.require_api_key = bool(require_api_key)
        self.api_key = api_key or ""
        super().__init__(server_address, _HTTPHandler)


class _HTTPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.request.settimeout(self.server.request_timeout)  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        # Silence default stderr access logs to keep CLI output clean
        return

    def _check_auth(self) -> bool:
        srv: _IntegratedHTTPServer = self.server  # type: ignore[assignment]
        if not srv.require_api_key:
            return True
        provided = self.headers.get("X-API-Key", "")
        if not provided or not hmac.compare_digest(provided, srv.api_key):
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {
                    "error_code": ErrorCode.API_AUTH_FAILED.value,
                    "reason": "Missing or invalid X-API-Key header",
                },
            )
            return False
        return True

    def _send_json(self, status_code: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, allow_nan=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if not self._check_auth():
            return
        route_map = {
            "/api/v1/health": "health",
            "/api/v1/status": "status",
            "/api/v1/telemetry": "telemetry",
            "/api/v1/commands/pending": "pending_commands",
        }
        action = route_map.get(self.path.split("?", 1)[0])
        if action is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {
                    "error_code": ErrorCode.API_MALFORMED_REQUEST.value,
                    "reason": f"Endpoint not found: {self.path}",
                },
            )
            return
        status, data = _dispatch_action(self.server.pipeline, action, {})  # type: ignore[attr-defined]
        self._send_json(status, data)

    def do_POST(self) -> None:
        if not self._check_auth():
            return
        route_map = {
            "/api/v1/session/handshake": "handshake",
            "/api/v1/session/rekey": "rekey",
            "/api/v1/session/terminate": "terminate",
            "/api/v1/command": "command",
            "/api/v1/commands/ack": "ack_command",
            "/api/v1/commands/land_complete": "land_complete",
            "/api/v1/telemetry/update": "telemetry_update",
        }
        action = route_map.get(self.path.split("?", 1)[0])
        if action is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {
                    "error_code": ErrorCode.API_MALFORMED_REQUEST.value,
                    "reason": f"Endpoint not found: {self.path}",
                },
            )
            return

        try:
            content_len = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error_code": ErrorCode.API_MALFORMED_REQUEST.value,
                    "reason": "Invalid Content-Length header",
                },
            )
            return

        if content_len < 0 or content_len > MAX_API_BODY_BYTES:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error_code": ErrorCode.API_MALFORMED_REQUEST.value,
                    "reason": "Content-Length out of permitted range",
                },
            )
            return

        try:
            raw_body = self.rfile.read(content_len) if content_len > 0 else b""
            payload = _parse_json_body(raw_body)
        except socket.timeout:
            self._send_json(
                HTTPStatus.REQUEST_TIMEOUT,
                {
                    "error_code": ErrorCode.API_TIMEOUT.value,
                    "reason": "Request body read timed out",
                },
            )
            return
        except IntegrationError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error_code": exc.error_code.value,
                    "reason": str(exc),
                },
            )
            return

        status, data = _dispatch_action(self.server.pipeline, action, payload)  # type: ignore[attr-defined]
        self._send_json(status, data)


class _IntegratedTCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        pipeline: ThreeLayerSecurityPipeline,
        request_timeout: float,
        require_api_key: bool,
        api_key: str | None,
    ) -> None:
        self.pipeline = pipeline
        self.request_timeout = float(request_timeout)
        self.require_api_key = bool(require_api_key)
        self.api_key = api_key or ""
        super().__init__(server_address, _TCPHandler)


class _TCPHandler(socketserver.BaseRequestHandler):
    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                if len(buf) == 0:
                    return None
                raise EOFError("Truncated TCP frame")
            buf.extend(chunk)
        return bytes(buf)

    @staticmethod
    def _send_frame(sock: socket.socket, obj: dict[str, Any]) -> None:
        raw = json.dumps(obj, allow_nan=False).encode("utf-8")
        sock.sendall(struct.pack("!I", len(raw)) + raw)

    def handle(self) -> None:
        srv: _IntegratedTCPServer = self.server  # type: ignore[assignment]
        self.request.settimeout(srv.request_timeout)

        while True:
            try:
                len_bytes = self._recv_exact(self.request, 4)
                if len_bytes is None:
                    return
                frame_len = struct.unpack("!I", len_bytes)[0]
                if frame_len == 0 or frame_len > MAX_API_BODY_BYTES:
                    self._send_frame(
                        self.request,
                        {
                            "ok": False,
                            "http_status": 400,
                            "error_code": ErrorCode.API_MALFORMED_REQUEST.value,
                            "reason": f"Invalid frame length: {frame_len}",
                        },
                    )
                    return
                body_bytes = self._recv_exact(self.request, frame_len)
                if body_bytes is None:
                    return
                req = _parse_json_body(body_bytes)

                if srv.require_api_key:
                    provided = str(req.get("api_key", ""))
                    if not provided or not hmac.compare_digest(provided, srv.api_key):
                        self._send_frame(
                            self.request,
                            {
                                "ok": False,
                                "http_status": 401,
                                "error_code": ErrorCode.API_AUTH_FAILED.value,
                                "reason": "Missing or invalid api_key",
                            },
                        )
                        continue

                action = req.get("action", "")
                payload = req.get("payload", {})
                if not isinstance(action, str) or not isinstance(payload, dict):
                    self._send_frame(
                        self.request,
                        {
                            "ok": False,
                            "http_status": 400,
                            "error_code": ErrorCode.API_MALFORMED_REQUEST.value,
                            "reason": "TCP request requires string 'action' and object 'payload'",
                        },
                    )
                    continue

                status, data = _dispatch_action(srv.pipeline, action, payload)
                self._send_frame(
                    self.request,
                    {
                        "ok": 200 <= status < 300,
                        "http_status": int(status),
                        "error_code": data.get("error_code", ErrorCode.OK.value),
                        "data": data,
                    },
                )
            except socket.timeout:
                self._send_frame(
                    self.request,
                    {
                        "ok": False,
                        "http_status": 408,
                        "error_code": ErrorCode.API_TIMEOUT.value,
                        "reason": "TCP request timed out",
                    },
                )
                return
            except (EOFError, ConnectionResetError, BrokenPipeError, OSError):
                return
            except IntegrationError as exc:
                self._send_frame(
                    self.request,
                    {
                        "ok": False,
                        "http_status": 400,
                        "error_code": exc.error_code.value,
                        "reason": str(exc),
                    },
                )


class SecurityBridgeServer:
    """Runs HTTP/REST and Length-Prefixed TCP interfaces around ThreeLayerSecurityPipeline."""

    def __init__(
        self,
        pipeline: ThreeLayerSecurityPipeline,
        http_host: str | None = None,
        http_port: int | None = None,
        tcp_host: str | None = None,
        tcp_port: int | None = None,
    ) -> None:
        self.pipeline = pipeline
        cfg = pipeline.config.api
        self.http_host = http_host if http_host is not None else cfg.http_host
        self.http_port = http_port if http_port is not None else cfg.http_port
        self.tcp_host = tcp_host if tcp_host is not None else cfg.tcp_host
        self.tcp_port = tcp_port if tcp_port is not None else cfg.tcp_port

        self._http_server: _IntegratedHTTPServer | None = None
        self._tcp_server: _IntegratedTCPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._tcp_thread: threading.Thread | None = None

    @property
    def http_address(self) -> tuple[str, int]:
        if not self._http_server:
            return (self.http_host, self.http_port)
        return self._http_server.server_address[:2]

    @property
    def tcp_address(self) -> tuple[str, int]:
        if not self._tcp_server:
            return (self.tcp_host, self.tcp_port)
        return self._tcp_server.server_address[:2]

    def start(self) -> None:
        cfg = self.pipeline.config.api
        self._http_server = _IntegratedHTTPServer(
            (self.http_host, self.http_port),
            pipeline=self.pipeline,
            request_timeout=cfg.request_timeout_seconds,
            require_api_key=cfg.require_api_key,
            api_key=cfg.api_key,
        )
        self._tcp_server = _IntegratedTCPServer(
            (self.tcp_host, self.tcp_port),
            pipeline=self.pipeline,
            request_timeout=cfg.request_timeout_seconds,
            require_api_key=cfg.require_api_key,
            api_key=cfg.api_key,
        )

        self._http_thread = threading.Thread(
            target=self._http_server.serve_forever,
            name="uav-c2-http-server",
            daemon=True,
        )
        self._tcp_thread = threading.Thread(
            target=self._tcp_server.serve_forever,
            name="uav-c2-tcp-server",
            daemon=True,
        )
        self._http_thread.start()
        self._tcp_thread.start()

    def stop(self) -> None:
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
            self._http_server = None
        if self._tcp_server is not None:
            self._tcp_server.shutdown()
            self._tcp_server.server_close()
            self._tcp_server = None

    def __enter__(self) -> "SecurityBridgeServer":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()
