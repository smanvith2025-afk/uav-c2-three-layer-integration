"""Unit tests for Layer 3 Intent-Based Command Authorization adapter."""

from __future__ import annotations

from common.errors import ErrorCode
from common.models import translate_command_to_l2, translate_l2_to_l3
from layer3_adapter.adapter import Layer3IntentAdapter


def test_layer3_mission_lifecycle_and_state_transitions() -> None:
    adapter = Layer3IntentAdapter()
    assert adapter.get_state() == "IDLE"

    # GOTO before TAKEOFF rejected
    d_bad = adapter.authorize(
        {"type": "GOTO", "waypoint": "WP1", "x": 10, "y": 10, "altitude": 50, "speed": 10}
    )
    assert d_bad.allowed is False
    assert d_bad.error_code == ErrorCode.L3_STATE_VIOLATION.value
    assert adapter.get_state() == "IDLE"

    # TAKEOFF -> CRUISE
    d_takeoff = adapter.authorize({"type": "TAKEOFF", "altitude": 80})
    assert d_takeoff.allowed is True
    assert d_takeoff.error_code == ErrorCode.OK.value
    assert adapter.get_state() == "CRUISE"

    # GOTO -> CRUISE
    d_goto = adapter.authorize(
        {"type": "GOTO", "waypoint": "WP2", "x": 50, "y": 50, "altitude": 80, "speed": 10}
    )
    assert d_goto.allowed is True
    assert adapter.get_state() == "CRUISE"

    # LAND -> LANDING -> LANDED
    d_land = adapter.authorize({"type": "LAND"})
    assert d_land.allowed is True
    assert adapter.get_state() == "LANDING"

    done, msg = adapter.complete_landing()
    assert done is True
    assert "landed" in msg.lower()
    assert adapter.get_state() == "LANDED"


def test_layer3_boundary_constraints_and_nan_guard() -> None:
    adapter = Layer3IntentAdapter()
    adapter.authorize({"type": "TAKEOFF", "altitude": 50})

    d_geo = adapter.authorize(
        {"type": "GOTO", "waypoint": "WP1", "x": 101, "y": 50, "altitude": 50, "speed": 10}
    )
    assert d_geo.allowed is False
    assert d_geo.error_code == ErrorCode.L3_GEOFENCE_VIOLATION.value

    d_alt = adapter.authorize(
        {"type": "GOTO", "waypoint": "WP1", "x": 50, "y": 50, "altitude": 101, "speed": 10}
    )
    assert d_alt.allowed is False
    assert d_alt.error_code == ErrorCode.L3_ALTITUDE_VIOLATION.value

    d_spd = adapter.authorize(
        {"type": "GOTO", "waypoint": "WP1", "x": 50, "y": 50, "altitude": 50, "speed": 16}
    )
    assert d_spd.allowed is False
    assert d_spd.error_code == ErrorCode.L3_SPEED_VIOLATION.value

    d_wp = adapter.authorize(
        {"type": "GOTO", "waypoint": "WP9", "x": 50, "y": 50, "altitude": 50, "speed": 10}
    )
    assert d_wp.allowed is False
    assert d_wp.error_code == ErrorCode.L3_WAYPOINT_UNAUTHORIZED.value

    d_nan = adapter.authorize(
        {"type": "GOTO", "waypoint": "WP1", "x": float("nan"), "y": 50, "altitude": 50, "speed": 10}
    )
    assert d_nan.allowed is False
    assert d_nan.error_code == ErrorCode.L3_INVALID_PARAMETERS.value


def test_command_translator_prevents_type_override() -> None:
    l2_verb, params = translate_command_to_l2(
        "GOTO",
        {"waypoint": "WP1", "x": 10, "y": 20, "altitude": 30, "speed": 5, "type": "TAKEOFF"},
    )
    assert l2_verb == "WAYPOINT"
    l3_cmd = translate_l2_to_l3(l2_verb, params)
    assert l3_cmd["type"] == "GOTO"
    assert l3_cmd["waypoint"] == "WP1"
