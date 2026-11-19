"""Main integration entry point for the three-layer UAV C2 security system."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

from api.server import SecurityBridgeServer
from communication.pipeline import ThreeLayerSecurityPipeline
from configuration.settings import load_config


def run_verify(config_path: str | None = None) -> int:
    """Verify initialization and end-to-end command pipeline across all 3 layers."""
    cfg = load_config(config_path)
    pipeline = ThreeLayerSecurityPipeline(cfg)
    sess = pipeline.establish_session()
    print(f"[VERIFY] Layer 2 PQC session established: id={sess['session_id_hex'][:12]}... epoch={sess['epoch']}")

    r1 = pipeline.process_command({"command_type": "TAKEOFF", "parameters": {"altitude": 50}})
    r2 = pipeline.process_command(
        {
            "command_type": "GOTO",
            "parameters": {"waypoint": "WP1", "x": 25, "y": 30, "altitude": 50, "speed": 10},
        }
    )
    r3 = pipeline.process_command(
        {
            "command_type": "GOTO",
            "parameters": {"waypoint": "WP2", "x": 250, "y": 30, "altitude": 50, "speed": 10},
        }
    )
    r4 = pipeline.process_command({"command_type": "LAND", "parameters": {}})

    assert r1.accepted and r1.executed, f"TAKEOFF failed: {r1.reason}"
    assert r2.accepted and r2.executed, f"GOTO failed: {r2.reason}"
    assert not r3.accepted and r3.failed_layer == "layer3", f"Geofence check failed: {r3.reason}"
    assert r4.accepted and r4.executed, f"LAND failed: {r4.reason}"

    print("[VERIFY] All 3 layers initialized and verified:")
    print(f"  - Layer 1 PDR: {pipeline.layer1.cumulative_pdr}% (jammer={cfg.layer1.jammer_type})")
    print(f"  - Layer 2 Session Epoch: {pipeline.layer2.get_session_info()['epoch']}, Seq: {pipeline.layer2.get_session_info()['recv_sequence']}")
    print(f"  - Layer 3 Mission State: {pipeline.layer3.get_state()}")
    print(f"  - Accepted: {pipeline.accepted_commands}, Rejected: {pipeline.rejected_commands}")
    return 0


def run_demo(config_path: str | None = None) -> int:
    """Run an end-to-end demonstration of valid commands, jamming, PQC attacks, and intent checks."""
    cfg = load_config(config_path)
    pipeline = ThreeLayerSecurityPipeline(cfg)

    print("=" * 72)
    print("  THREE-LAYER UAV C2 SECURITY INTEGRATION DEMONSTRATION")
    print("=" * 72)

    # 1. Pre-handshake fail-safe check
    pre_hs = pipeline.process_command({"command_type": "TAKEOFF", "parameters": {"altitude": 50}})
    print(f"\n[1] Pre-handshake TAKEOFF -> accepted={pre_hs.accepted}, layer={pre_hs.failed_layer}, code={pre_hs.error_code}")

    # 2. Establish PQC session (ML-KEM-768 + ML-DSA-65 + HKDF-SHA-256)
    sess = pipeline.establish_session()
    print(f"\n[2] Handshake Established -> session={sess['session_id_hex'][:16]}..., epoch={sess['epoch']}, latency={sess['latency_ms']} ms")

    # 3. Valid mission sequence
    scenarios = [
        ("GOTO while IDLE (should reject at L3)", {"command_type": "GOTO", "parameters": {"waypoint": "WP1", "x": 20, "y": 20, "altitude": 50, "speed": 10}}),
        ("Valid TAKEOFF to 80m", {"command_type": "TAKEOFF", "parameters": {"altitude": 80}}),
        ("Valid GOTO WP2 (50, 50)", {"command_type": "GOTO", "parameters": {"waypoint": "WP2", "x": 50, "y": 50, "altitude": 80, "speed": 12}}),
        ("Geofence violation x=150 (should reject at L3)", {"command_type": "GOTO", "parameters": {"waypoint": "WP2", "x": 150, "y": 50, "altitude": 80, "speed": 10}}),
        ("Altitude violation 180m (should reject at L3)", {"command_type": "GOTO", "parameters": {"waypoint": "WP2", "x": 40, "y": 40, "altitude": 180, "speed": 10}}),
        ("Unauthorized command ARM (should reject at L2)", {"command_type": "ARM", "parameters": {}}),
        ("Valid RETURN_HOME", {"command_type": "RETURN_HOME", "parameters": {}}),
    ]

    for idx, (label, cmd) in enumerate(scenarios, start=3):
        resp = pipeline.process_command(cmd)
        ch = resp.layer1.selected_channel if resp.layer1 else -1
        seq = resp.layer2.sequence if resp.layer2 else 0
        print(
            f"\n[{idx}] {label}\n"
            f"     accepted={resp.accepted} executed={resp.executed} "
            f"failed_layer={resp.failed_layer} code={resp.error_code}\n"
            f"     channel={ch} seq={seq} state={resp.telemetry.mission_state if resp.telemetry else 'N/A'} "
            f"reason={resp.reason}"
        )

    # 4. In-session rekey
    rekey_info = pipeline.rekey_session()
    print(f"\n[10] In-Session PQC Rekey -> new_epoch={rekey_info['epoch']}, latency={rekey_info['latency_ms']} ms")

    # 5. Simulated RF Jamming Blackout & Recovery
    pipeline.layer1.force_jam_next(3)
    jammed = pipeline.process_command({"command_type": "LAND", "parameters": {}})
    print(f"\n[11] Total RF Jamming Blackout -> accepted={jammed.accepted}, failed_layer={jammed.failed_layer}, code={jammed.error_code}")

    land = pipeline.process_command({"command_type": "LAND", "parameters": {}})
    print(f"\n[12] Link Recovered LAND -> accepted={land.accepted}, executed={land.executed}, state={land.telemetry.mission_state if land.telemetry else 'N/A'}")

    ok_touchdown, msg = pipeline.complete_landing()
    print(f"\n[13] Touchdown LAND_COMPLETE -> accepted={ok_touchdown}, msg={msg}, state={pipeline.layer3.get_state()}")

    print("\n" + "=" * 72)
    print("FINAL TELEMETRY:")
    print(json.dumps(pipeline.get_telemetry().to_dict(), indent=2))
    print("=" * 72)
    return 0


def run_server(config_path: str | None = None, http_port: int | None = None, tcp_port: int | None = None) -> int:
    """Start external HTTP/REST and TCP servers for Unreal Engine 5.8 / Cosys-AirSim."""
    cfg = load_config(config_path)
    if http_port is not None:
        cfg.api.http_port = http_port
    if tcp_port is not None:
        cfg.api.tcp_port = tcp_port

    pipeline = ThreeLayerSecurityPipeline(cfg)
    pipeline.establish_session()

    server = SecurityBridgeServer(pipeline)
    server.start()

    print("UAV C2 Three-Layer Security Bridge Server Started")
    print(f"  HTTP/REST API : http://{server.http_address[0]}:{server.http_address[1]}/api/v1/")
    print(f"  TCP Frame API : {server.tcp_address[0]}:{server.tcp_address[1]}")
    print(f"  L1 Jammer     : {cfg.layer1.jammer_type} ({cfg.layer1.num_channels} channels)")
    print(f"  L2 Session    : {pipeline.layer2.get_session_info()['session_id_hex'][:16]}... (epoch 0)")
    print(f"  L3 Mission    : {pipeline.layer3.mission.get('mission_id')} (state={pipeline.layer3.get_state()})")
    print("Press Ctrl+C to shut down cleanly.")

    stop_requested = False

    def _handle_sig(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    try:
        while not stop_requested:
            time.sleep(0.5)
    finally:
        print("\nShutting down server...")
        server.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Three-Layer UAV Command & Control Security Integration System"
    )
    parser.add_argument(
        "--mode",
        choices=("demo", "server", "verify"),
        default="demo",
        help="Execution mode (default: demo)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Optional path to custom JSON configuration file",
    )
    parser.add_argument("--http-port", type=int, default=None, help="Override HTTP server port")
    parser.add_argument("--tcp-port", type=int, default=None, help="Override TCP server port")
    args = parser.parse_args(argv)

    if args.mode == "verify":
        return run_verify(args.config)
    if args.mode == "server":
        return run_server(args.config, args.http_port, args.tcp_port)
    return run_demo(args.config)


if __name__ == "__main__":
    sys.exit(main())
