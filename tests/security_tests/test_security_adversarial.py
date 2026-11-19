"""Adversarial security tests verifying fail-closed behavior across L1, L2, and L3."""

from __future__ import annotations

import json
import struct
import time
from pathlib import Path

from common.errors import ErrorCode
from communication.pipeline import ThreeLayerSecurityPipeline
from configuration.settings import load_config
from protocol.constants import (  # type: ignore
    MSG_TYPE_COMMAND,
    PROTOCOL_VERSION,
)
from protocol.crypto_core import (  # type: ignore
    aes_gcm_encrypt,
    sig_generate_keypair,
    sig_sign,
)
from protocol.messages import (  # type: ignore
    CommandPayload,
    MessageHeader,
    build_packet,
)


def _setup_pipeline(tmp_path: Path) -> ThreeLayerSecurityPipeline:
    cfg = load_config()
    cfg.audit_log_file = str(tmp_path / "security_audit.jsonl")
    pipeline = ThreeLayerSecurityPipeline(cfg)
    pipeline.establish_session()
    return pipeline


def test_replay_attack_rejected_at_layer2(tmp_path: Path) -> None:
    pipeline = _setup_pipeline(tmp_path)
    wire_pkt, send_dec = pipeline.layer2.protect_command("TAKEOFF", '{"altitude":60}')
    assert wire_pkt is not None and send_dec.allowed is True

    # First delivery succeeds through L1 -> L2 -> L3 -> Bridge
    first = pipeline.process_wire_packet(wire_pkt)
    assert first.accepted is True
    assert first.executed is True
    assert pipeline.layer3.get_state() == "CRUISE"

    # Replaying identical packet must be rejected at Layer 2
    replay = pipeline.process_wire_packet(wire_pkt)
    assert replay.accepted is False
    assert replay.executed is False
    assert replay.failed_layer == "layer2"
    assert replay.error_code in (
        ErrorCode.L2_REPLAY_DETECTED.value,
        ErrorCode.L2_NONCE_ERROR.value,
    )
    assert len(pipeline.bridge.executed_commands) == 1


def test_ciphertext_tampering_rejected_at_layer2(tmp_path: Path) -> None:
    pipeline = _setup_pipeline(tmp_path)
    wire_pkt, _ = pipeline.layer2.protect_command("TAKEOFF", '{"altitude":60}')
    assert wire_pkt is not None

    # Flip one byte in encrypted ciphertext / GCM tag
    tampered = bytearray(wire_pkt)
    tampered[-5] ^= 0xFF

    resp = pipeline.process_wire_packet(bytes(tampered))
    assert resp.accepted is False
    assert resp.executed is False
    assert resp.failed_layer == "layer2"
    assert resp.error_code == ErrorCode.L2_AUTHENTICATION_FAILED.value
    assert pipeline.layer3.get_state() == "IDLE"
    assert len(pipeline.bridge.executed_commands) == 0


def test_forged_signature_with_rogue_key_rejected(tmp_path: Path) -> None:
    pipeline = _setup_pipeline(tmp_path)
    sess = pipeline.layer2.sender.session
    assert sess is not None

    rogue_keys = sig_generate_keypair()
    seq = sess.next_send_sequence()
    nonce = sess.next_sender_nonce()
    payload = CommandPayload(
        sender_id=pipeline.layer2.ground_station_id,
        recipient_id=pipeline.layer2.uav_id,
        command="TAKEOFF",
        params='{"altitude":60}',
        timestamp=time.time(),
    )
    forged_sig = sig_sign(
        payload.canonical_bytes_for_signing(sess.session_id, seq),
        rogue_keys.secret_key,
    )
    signed = CommandPayload(
        sender_id=payload.sender_id,
        recipient_id=payload.recipient_id,
        command=payload.command,
        params=payload.params,
        timestamp=payload.timestamp,
        signature=forged_sig,
    )
    header = MessageHeader(
        version=PROTOCOL_VERSION,
        msg_type=MSG_TYPE_COMMAND,
        session_id=sess.session_id,
        sequence=seq,
    )
    _, ct = aes_gcm_encrypt(
        sess.get_sender_enc_key(),
        signed.to_json_bytes(),
        header.to_bytes(),
        nonce=nonce,
    )
    forged_pkt = build_packet(header, nonce, ct)

    resp = pipeline.process_wire_packet(forged_pkt)
    assert resp.accepted is False
    assert resp.executed is False
    assert resp.failed_layer == "layer2"
    assert resp.error_code == ErrorCode.L2_AUTHENTICATION_FAILED.value
    assert pipeline.layer3.get_state() == "IDLE"


def test_stale_timestamp_command_rejected(tmp_path: Path) -> None:
    pipeline = _setup_pipeline(tmp_path)
    sess = pipeline.layer2.sender.session
    assert sess is not None

    seq = sess.next_send_sequence()
    nonce = sess.next_sender_nonce()
    stale_ts = time.time() - 300.0  # 5 minutes old (> 60s max skew)
    payload = CommandPayload(
        sender_id=pipeline.layer2.ground_station_id,
        recipient_id=pipeline.layer2.uav_id,
        command="TAKEOFF",
        params='{"altitude":60}',
        timestamp=stale_ts,
    )
    sig = sig_sign(
        payload.canonical_bytes_for_signing(sess.session_id, seq),
        pipeline.layer2._gs_keys.secret_key,
    )
    signed = CommandPayload(
        sender_id=payload.sender_id,
        recipient_id=payload.recipient_id,
        command=payload.command,
        params=payload.params,
        timestamp=payload.timestamp,
        signature=sig,
    )
    header = MessageHeader(
        version=PROTOCOL_VERSION,
        msg_type=MSG_TYPE_COMMAND,
        session_id=sess.session_id,
        sequence=seq,
    )
    _, ct = aes_gcm_encrypt(
        sess.get_sender_enc_key(),
        signed.to_json_bytes(),
        header.to_bytes(),
        nonce=nonce,
    )
    stale_pkt = build_packet(header, nonce, ct)

    resp = pipeline.process_wire_packet(stale_pkt)
    assert resp.accepted is False
    assert resp.executed is False
    assert resp.failed_layer == "layer2"
    assert resp.error_code == ErrorCode.L2_STALE_TIMESTAMP.value


def test_invalid_protocol_version_and_revoked_peer_rejected(tmp_path: Path) -> None:
    pipeline = _setup_pipeline(tmp_path)
    wire_pkt, _ = pipeline.layer2.protect_command("TAKEOFF", '{"altitude":60}')
    assert wire_pkt is not None

    # Version downgrade byte -> VersionError
    bad_ver = bytearray(wire_pkt)
    bad_ver[0] = 1
    r_ver = pipeline.process_wire_packet(bytes(bad_ver))
    assert r_ver.accepted is False
    assert r_ver.failed_layer == "layer2"
    assert r_ver.error_code == ErrorCode.L2_VERSION_ERROR.value

    # Revoke ground station in TrustStore -> TrustError
    pipeline.layer2.trust_store.revoke_peer(pipeline.layer2.ground_station_id)
    r_rev = pipeline.process_wire_packet(wire_pkt)
    assert r_rev.accepted is False
    assert r_rev.failed_layer == "layer2"
    assert r_rev.error_code == ErrorCode.L2_TRUST_ERROR.value


def test_expired_session_rejected_fail_closed(tmp_path: Path) -> None:
    pipeline = _setup_pipeline(tmp_path)
    assert pipeline.layer2.sender.session is not None
    pipeline.layer2.sender.session.expire()

    resp = pipeline.process_command({"command_type": "TAKEOFF", "parameters": {"altitude": 50}})
    assert resp.accepted is False
    assert resp.executed is False
    assert resp.failed_layer == "layer2"
    assert resp.error_code == ErrorCode.L2_SESSION_EXPIRED.value


def test_sequence_gap_anomaly_rejected(tmp_path: Path) -> None:
    pipeline = _setup_pipeline(tmp_path)
    sess = pipeline.layer2.sender.session
    assert sess is not None

    # Force a sequence jump exceeding MAX_SEQUENCE_GAP (100)
    sess.send_sequence = 500
    wire_pkt, _ = pipeline.layer2.protect_command("TAKEOFF", '{"altitude":60}')
    assert wire_pkt is not None

    resp = pipeline.process_wire_packet(wire_pkt)
    assert resp.accepted is False
    assert resp.failed_layer == "layer2"
    assert resp.error_code == ErrorCode.L2_SEQUENCE_ERROR.value


def test_layer3_policy_geofence_and_state_attacks_blocked(tmp_path: Path) -> None:
    pipeline = _setup_pipeline(tmp_path)

    # GOTO while IDLE -> L3_STATE_VIOLATION
    r_idle = pipeline.process_command(
        {
            "command_type": "GOTO",
            "parameters": {"waypoint": "WP1", "x": 25, "y": 25, "altitude": 50, "speed": 10},
        }
    )
    assert r_idle.accepted is False
    assert r_idle.failed_layer == "layer3"
    assert r_idle.error_code == ErrorCode.L3_STATE_VIOLATION.value

    # TAKEOFF succeeds
    assert pipeline.process_command({"command_type": "TAKEOFF", "parameters": {"altitude": 50}}).accepted

    # TAKEOFF again while CRUISE -> L3_STATE_VIOLATION
    r_double = pipeline.process_command({"command_type": "TAKEOFF", "parameters": {"altitude": 60}})
    assert r_double.accepted is False
    assert r_double.failed_layer == "layer3"
    assert r_double.error_code == ErrorCode.L3_STATE_VIOLATION.value

    # Geofence, altitude, speed, waypoint, and policy-disallowed L2 verb
    attacks = [
        ("GOTO", {"waypoint": "WP1", "x": -1, "y": 50, "altitude": 50, "speed": 10}, ErrorCode.L3_GEOFENCE_VIOLATION.value),
        ("GOTO", {"waypoint": "WP1", "x": 50, "y": 105, "altitude": 50, "speed": 10}, ErrorCode.L3_GEOFENCE_VIOLATION.value),
        ("GOTO", {"waypoint": "WP1", "x": 50, "y": 50, "altitude": 120, "speed": 10}, ErrorCode.L3_ALTITUDE_VIOLATION.value),
        ("GOTO", {"waypoint": "WP1", "x": 50, "y": 50, "altitude": 50, "speed": 25}, ErrorCode.L3_SPEED_VIOLATION.value),
        ("GOTO", {"waypoint": "WP99", "x": 50, "y": 50, "altitude": 50, "speed": 10}, ErrorCode.L3_WAYPOINT_UNAUTHORIZED.value),
        ("EMERGENCY_STOP", {}, ErrorCode.L3_UNAUTHORIZED_COMMAND.value),
    ]
    for verb, params, expected_code in attacks:
        resp = pipeline.process_command({"command_type": verb, "parameters": params})
        assert resp.accepted is False
        assert resp.executed is False
        assert resp.failed_layer == "layer3"
        assert resp.error_code == expected_code
        assert pipeline.layer3.get_state() == "CRUISE"

    assert len(pipeline.bridge.executed_commands) == 1


def test_audit_log_redacts_secrets(tmp_path: Path) -> None:
    pipeline = _setup_pipeline(tmp_path)
    pipeline.process_command({"command_type": "TAKEOFF", "parameters": {"altitude": 50}})

    # Also log synthetic sensitive dictionary to verify redaction filter
    pipeline.audit_logger.log_event(
        "SYNTHETIC_SECRET_TEST",
        {
            "secret_key": "SUPER_SECRET_HEX",
            "shared_secret": b"raw_secret",
            "signature": "deadbeef",
            "safe_field": "visible_value",
        },
    )

    log_path = Path(pipeline.config.audit_log_file)
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "SUPER_SECRET_HEX" not in content
    assert "deadbeef" not in content
    assert "visible_value" in content
    for line in content.strip().splitlines():
        obj = json.loads(line)
        assert "timestamp_utc" in obj
