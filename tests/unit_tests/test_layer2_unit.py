"""Unit tests for Layer 2 Post-Quantum Cryptographic Security adapter."""

from __future__ import annotations

from common.errors import ErrorCode
from configuration.settings import Layer2Config
from layer2_adapter.adapter import Layer2PQCAdapter


def test_layer2_handshake_command_rekey_terminate() -> None:
    adapter = Layer2PQCAdapter(Layer2Config())
    assert adapter.is_session_established() is False

    hs_info = adapter.establish_session()
    assert hs_info["established"] is True
    assert hs_info["epoch"] == 0
    assert len(hs_info["session_id_hex"]) == 32

    ok, cmd, decision = adapter.process_command("TAKEOFF", '{"altitude":50}')
    assert ok is True
    assert decision.allowed is True
    assert decision.error_code == ErrorCode.OK.value
    assert decision.sequence == 1
    assert cmd is not None
    assert cmd["command"] == "TAKEOFF"

    rekey_info = adapter.rekey_session()
    assert rekey_info["established"] is True
    assert rekey_info["epoch"] == 1

    ok2, cmd2, dec2 = adapter.process_command("WAYPOINT", '{"x":10,"y":20}')
    assert ok2 is True
    assert dec2.epoch == 1
    assert dec2.sequence == 3  # seq 2 was REKEY_INIT

    term_info = adapter.terminate_session()
    assert term_info["established"] is False


def test_layer2_rejects_command_without_session() -> None:
    adapter = Layer2PQCAdapter(Layer2Config())
    ok, cmd, decision = adapter.process_command("TAKEOFF", '{"altitude":50}')
    assert ok is False
    assert cmd is None
    assert decision.allowed is False
    assert decision.error_code == ErrorCode.L2_NO_ACTIVE_SESSION.value


def test_layer2_rejects_unlisted_command_verb() -> None:
    adapter = Layer2PQCAdapter(Layer2Config())
    adapter.establish_session()
    ok, cmd, decision = adapter.process_command("ARM", "{}")
    assert ok is False
    assert cmd is None
    assert decision.allowed is False
    assert decision.error_code == ErrorCode.L2_INVALID_MESSAGE_FORMAT.value
