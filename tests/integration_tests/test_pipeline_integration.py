"""End-to-end integration tests across Layer 1, Layer 2, Layer 3, and UAV Bridge."""

from __future__ import annotations

from pathlib import Path

from common.errors import ErrorCode
from communication.pipeline import ThreeLayerSecurityPipeline
from configuration.settings import load_config


def _make_pipeline(tmp_path: Path, auto_complete_landing: bool = False) -> ThreeLayerSecurityPipeline:
    cfg = load_config()
    cfg.audit_log_file = str(tmp_path / "audit.jsonl")
    cfg.layer3.auto_complete_landing = auto_complete_landing
    return ThreeLayerSecurityPipeline(cfg)


def test_full_mission_lifecycle_and_rekey(tmp_path: Path) -> None:
    pipeline = _make_pipeline(tmp_path)
    sess = pipeline.establish_session()
    assert sess["established"] is True
    assert sess["epoch"] == 0

    # 1. TAKEOFF
    r1 = pipeline.process_command({"command_type": "TAKEOFF", "parameters": {"altitude": 70}})
    assert r1.accepted is True
    assert r1.executed is True
    assert r1.layer1 is not None and r1.layer1.allowed is True
    assert r1.layer2 is not None and r1.layer2.allowed is True
    assert r1.layer3 is not None and r1.layer3.allowed is True
    assert r1.telemetry is not None and r1.telemetry.mission_state == "CRUISE"
    assert r1.telemetry.position["z_altitude"] == 70.0

    # 2. GOTO WP1
    r2 = pipeline.process_command(
        {
            "command_type": "GOTO",
            "parameters": {"waypoint": "WP1", "x": 30, "y": 40, "altitude": 70, "speed": 10},
        }
    )
    assert r2.accepted is True
    assert r2.telemetry is not None
    assert r2.telemetry.position == {"x": 30.0, "y": 40.0, "z_altitude": 70.0}
    assert r2.telemetry.current_waypoint == "WP1"

    # 3. Rekey mid-flight
    rekey = pipeline.rekey_session()
    assert rekey["epoch"] == 1
    assert rekey["established"] is True

    # 4. GOTO WP2 under epoch 1 keys
    r3 = pipeline.process_command(
        {
            "command_type": "GOTO",
            "parameters": {"waypoint": "WP2", "x": 80, "y": 90, "altitude": 90, "speed": 12},
        }
    )
    assert r3.accepted is True
    assert r3.layer2 is not None and r3.layer2.epoch == 1

    # 5. RETURN_HOME
    r4 = pipeline.process_command({"command_type": "RETURN_HOME", "parameters": {}})
    assert r4.accepted is True
    assert r4.telemetry is not None and r4.telemetry.current_waypoint == "HOME"

    # 6. LAND -> LANDING -> LAND_COMPLETE -> LANDED -> second TAKEOFF
    r5 = pipeline.process_command({"command_type": "LAND", "parameters": {}})
    assert r5.accepted is True
    assert pipeline.layer3.get_state() == "LANDING"

    ok_land, _ = pipeline.complete_landing()
    assert ok_land is True
    assert pipeline.layer3.get_state() == "LANDED"

    r6 = pipeline.process_command({"command_type": "TAKEOFF", "parameters": {"altitude": 45}})
    assert r6.accepted is True
    assert pipeline.layer3.get_state() == "CRUISE"
    assert len(pipeline.bridge.executed_commands) == 6


def test_layer_failure_isolation_and_recovery(tmp_path: Path) -> None:
    pipeline = _make_pipeline(tmp_path)
    pipeline.establish_session()

    # Takeoff
    assert pipeline.process_command({"command_type": "TAKEOFF", "parameters": {"altitude": 50}}).accepted

    # Simulate RF link drop -> L1 blocks before L2/L3/Bridge execute
    before_seq = pipeline.layer2.get_session_info()["recv_sequence"]
    pipeline.layer1.set_link_status(False)
    r_down = pipeline.process_command({"command_type": "RETURN_HOME", "parameters": {}})
    assert r_down.accepted is False
    assert r_down.executed is False
    assert r_down.failed_layer == "layer1"
    assert r_down.error_code == ErrorCode.L1_LINK_DOWN.value
    assert pipeline.layer2.get_session_info()["recv_sequence"] == before_seq
    assert len(pipeline.bridge.executed_commands) == 1

    # Recover link -> command succeeds
    pipeline.layer1.set_link_status(True)
    r_up = pipeline.process_command({"command_type": "RETURN_HOME", "parameters": {}})
    assert r_up.accepted is True
    assert r_up.executed is True
    assert len(pipeline.bridge.executed_commands) == 2

    # Simulate L3 geofence rejection -> Bridge never receives command and state remains CRUISE
    r_geo = pipeline.process_command(
        {
            "command_type": "GOTO",
            "parameters": {"waypoint": "WP1", "x": 150, "y": 50, "altitude": 50, "speed": 10},
        }
    )
    assert r_geo.accepted is False
    assert r_geo.executed is False
    assert r_geo.failed_layer == "layer3"
    assert r_geo.error_code == ErrorCode.L3_GEOFENCE_VIOLATION.value
    assert pipeline.layer3.get_state() == "CRUISE"
    assert len(pipeline.bridge.executed_commands) == 2


def test_auto_rekey_triggers_when_threshold_reached(tmp_path: Path) -> None:
    cfg = load_config()
    cfg.audit_log_file = str(tmp_path / "audit.jsonl")
    cfg.layer2.auto_rekey_interval_messages = 2
    pipeline = ThreeLayerSecurityPipeline(cfg)
    pipeline.establish_session()

    assert pipeline.process_command({"command_type": "TAKEOFF", "parameters": {"altitude": 50}}).accepted
    assert pipeline.layer2.get_session_info()["epoch"] == 0

    r2 = pipeline.process_command(
        {
            "command_type": "GOTO",
            "parameters": {"waypoint": "WP1", "x": 20, "y": 20, "altitude": 50, "speed": 10},
        }
    )
    assert r2.accepted is True
    assert pipeline.layer2.get_session_info()["epoch"] == 1
