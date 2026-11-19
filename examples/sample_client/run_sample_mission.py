"""Working sample client exercising the external HTTP/REST and TCP APIs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.client import SecurityPipelineClient
from api.server import SecurityBridgeServer
from communication.pipeline import ThreeLayerSecurityPipeline
from configuration.settings import load_config


def execute_sample_mission(client: SecurityPipelineClient) -> dict[str, int]:
    """Run handshake, valid mission commands, adversarial rejections, and Unreal sync."""
    commands_path = REPO_ROOT / "examples" / "sample_commands" / "mission_commands.json"
    with commands_path.open("r", encoding="utf-8") as fh:
        samples = json.load(fh)

    health = client.health()
    assert health.get("status") == "ok", f"Health check failed: {health}"

    hs = client.establish_session()
    assert hs.get("status") == "established", f"Handshake failed: {hs}"

    accepted_count = 0
    rejected_count = 0

    valid_cmds = samples["valid_mission_sequence"]
    # Run TAKEOFF + two GOTOs first, test adversarial attacks mid-flight, then finish RETURN_HOME + LAND
    for item in valid_cmds[:3]:
        resp = client.send_command(item["command_type"], item["parameters"])
        assert resp["accepted"] is True, f"Expected ACCEPT for {item}: {resp}"
        accepted_count += 1
        print(f"[ACCEPT] {item['command_type']:12s} | ch={resp['layer1']['selected_channel']} | seq={resp['layer2']['sequence']} | state={resp['telemetry']['mission_state']}")

    # Poll pending commands as Unreal Engine / Cosys-AirSim would do
    pending = client.poll_pending_commands().get("commands", [])
    print(f"[UNREAL] Polled {len(pending)} approved command(s) ready for flight execution")
    for cmd in pending:
        ack = client.ack_command(cmd["command_id"])
        assert ack.get("acknowledged") is True

    # Simulate Unreal Engine pushing back telemetry update
    telem_ack = client.push_unreal_telemetry(
        {"x": 75.0, "y": 80.0, "z_altitude": 90.0, "speed": 15.0, "in_air": True, "armed": True}
    )
    assert telem_ack.get("status") == "updated"

    # Send adversarial commands while cruising
    for item in samples["adversarial_rejected_commands"]:
        resp = client.send_command(item["command_type"], item["parameters"])
        assert resp["accepted"] is False, f"Expected REJECT for {item['label']}"
        assert resp["executed"] is False
        rejected_count += 1
        print(f"[REJECT] {item['label']} -> failed_layer={resp['failed_layer']}, code={resp['error_code']}")

    # Finish RETURN_HOME and LAND
    for item in valid_cmds[3:]:
        resp = client.send_command(item["command_type"], item["parameters"])
        assert resp["accepted"] is True, f"Expected ACCEPT for {item}: {resp}"
        accepted_count += 1
        print(f"[ACCEPT] {item['command_type']:12s} | ch={resp['layer1']['selected_channel']} | seq={resp['layer2']['sequence']} | state={resp['telemetry']['mission_state']}")

    for cmd in client.poll_pending_commands().get("commands", []):
        ack = client.ack_command(cmd["command_id"])
        assert ack.get("acknowledged") is True

    touchdown = client.complete_landing()
    assert touchdown.get("accepted") is True
    print(f"[UNREAL] Touchdown confirmed -> state={touchdown['state']}")

    return {"accepted": accepted_count, "rejected": rejected_count}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sample client for UAV C2 Security API")
    parser.add_argument("--connect", action="store_true", help="Connect to an already-running external server")
    parser.add_argument("--protocol", choices=("http", "tcp"), default="http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args(argv)

    if args.connect:
        client = SecurityPipelineClient(
            protocol=args.protocol,
            host=args.host,
            port=args.port,
            api_key=args.api_key,
        )
        summary = execute_sample_mission(client)
        print(f"Sample mission finished ({args.protocol.upper()}): {summary}")
        return 0

    cfg = load_config()
    pipeline = ThreeLayerSecurityPipeline(cfg)
    with SecurityBridgeServer(pipeline, http_port=0, tcp_port=0) as srv:
        http_host, http_port = srv.http_address
        tcp_host, tcp_port = srv.tcp_address
        print(f"--- Testing Sample Client via HTTP ({http_host}:{http_port}) ---")
        http_client = SecurityPipelineClient(protocol="http", host=http_host, port=http_port)
        res_http = execute_sample_mission(http_client)
        print(f"HTTP sample result: {res_http}")

        # Reset mission state for second client pass via TCP
        pipeline.layer3.reset_state()
        print(f"\n--- Testing Sample Client via TCP ({tcp_host}:{tcp_port}) ---")
        tcp_client = SecurityPipelineClient(protocol="tcp", host=tcp_host, port=tcp_port)
        res_tcp = execute_sample_mission(tcp_client)
        print(f"TCP sample result: {res_tcp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
