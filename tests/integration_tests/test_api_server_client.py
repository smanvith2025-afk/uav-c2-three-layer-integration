"""Integration tests for HTTP/REST and TCP API servers, sample client, and timeouts."""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from api.client import SecurityPipelineClient
from api.server import SecurityBridgeServer
from common.errors import ErrorCode
from communication.pipeline import ThreeLayerSecurityPipeline
from configuration.settings import load_config
from examples.sample_client.run_sample_mission import execute_sample_mission


def test_http_and_tcp_sample_mission(tmp_path: Path) -> None:
    cfg = load_config()
    cfg.audit_log_file = str(tmp_path / "audit.jsonl")
    pipeline = ThreeLayerSecurityPipeline(cfg)

    with SecurityBridgeServer(pipeline, http_port=0, tcp_port=0) as srv:
        http_host, http_port = srv.http_address
        tcp_host, tcp_port = srv.tcp_address

        http_client = SecurityPipelineClient("http", http_host, http_port, timeout=5.0)
        res_http = execute_sample_mission(http_client)
        assert res_http == {"accepted": 5, "rejected": 6}

        pipeline.layer3.reset_state()
        tcp_client = SecurityPipelineClient("tcp", tcp_host, tcp_port, timeout=5.0)
        res_tcp = execute_sample_mission(tcp_client)
        assert res_tcp == {"accepted": 5, "rejected": 6}


def test_api_key_authentication_enforced(tmp_path: Path) -> None:
    cfg = load_config()
    cfg.audit_log_file = str(tmp_path / "audit.jsonl")
    cfg.api.require_api_key = True
    cfg.api.api_key = "test-runtime-secret-token"
    pipeline = ThreeLayerSecurityPipeline(cfg)

    with SecurityBridgeServer(pipeline, http_port=0, tcp_port=0) as srv:
        http_host, http_port = srv.http_address
        tcp_host, tcp_port = srv.tcp_address

        unauth_http = SecurityPipelineClient("http", http_host, http_port)
        resp_http = unauth_http.health()
        assert resp_http.get("http_status") == 401
        assert resp_http.get("error_code") == ErrorCode.API_AUTH_FAILED.value

        auth_http = SecurityPipelineClient(
            "http", http_host, http_port, api_key="test-runtime-secret-token"
        )
        assert auth_http.health()["status"] == "ok"

        unauth_tcp = SecurityPipelineClient("tcp", tcp_host, tcp_port)
        resp_tcp = unauth_tcp.health()
        assert resp_tcp.get("error_code") == ErrorCode.API_AUTH_FAILED.value

        auth_tcp = SecurityPipelineClient(
            "tcp", tcp_host, tcp_port, api_key="test-runtime-secret-token"
        )
        assert auth_tcp.health()["status"] == "ok"


def test_malformed_http_and_tcp_inputs_handled_safely(tmp_path: Path) -> None:
    cfg = load_config()
    cfg.audit_log_file = str(tmp_path / "audit.jsonl")
    pipeline = ThreeLayerSecurityPipeline(cfg)

    with SecurityBridgeServer(pipeline, http_port=0, tcp_port=0) as srv:
        http_host, http_port = srv.http_address
        url = f"http://{http_host}:{http_port}/api/v1/command"
        req = urllib.request.Request(
            url,
            data=b"{invalid json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=3.0)
        assert exc_info.value.code == 400

        # Non-finite JSON constant (NaN) rejected safely
        req_nan = urllib.request.Request(
            url,
            data=b'{"command_type":"TAKEOFF","parameters":{"altitude":NaN}}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_nan:
            urllib.request.urlopen(req_nan, timeout=3.0)
        assert exc_nan.value.code == 400


def test_server_and_client_timeout_handling(tmp_path: Path) -> None:
    cfg = load_config()
    cfg.audit_log_file = str(tmp_path / "audit.jsonl")
    cfg.api.request_timeout_seconds = 0.2
    pipeline = ThreeLayerSecurityPipeline(cfg)

    with SecurityBridgeServer(pipeline, http_port=0, tcp_port=0) as srv:
        tcp_host, tcp_port = srv.tcp_address
        # Connect and send incomplete length prefix, verify server times out cleanly
        with socket.create_connection((tcp_host, tcp_port), timeout=2.0) as sock:
            sock.sendall(b"\x00\x00\x00\x20")  # declares 32 bytes, sends nothing
            time.sleep(0.35)
            data = sock.recv(1024)
            assert len(data) > 4
            assert b"API_TIMEOUT" in data

        # Subsequent normal client connection still works without server wedging
        client = SecurityPipelineClient("tcp", tcp_host, tcp_port, timeout=2.0)
        assert client.health()["status"] == "ok"

    # Client-side timeout against silent blackhole socket
    blackhole = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blackhole.bind(("127.0.0.1", 0))
    blackhole.listen(1)
    port = blackhole.getsockname()[1]

    def _accept_and_hold() -> None:
        conn, _ = blackhole.accept()
        time.sleep(0.4)
        conn.close()

    t = threading.Thread(target=_accept_and_hold, daemon=True)
    t.start()
    try:
        slow_client = SecurityPipelineClient("tcp", "127.0.0.1", port, timeout=0.1)
        with pytest.raises((socket.timeout, TimeoutError)):
            slow_client.health()
    finally:
        blackhole.close()
