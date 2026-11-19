"""Standardized error codes and exception classes for the integrated system."""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """Canonical error codes across Layer 1, Layer 2, Layer 3, and External API."""

    OK = "OK"

    # Layer 1: Anti-Jamming RL
    L1_LINK_DOWN = "L1_LINK_DOWN"
    L1_CHANNEL_JAMMED = "L1_CHANNEL_JAMMED"
    L1_INVALID_CHANNEL = "L1_INVALID_CHANNEL"
    L1_INTERNAL_ERROR = "L1_INTERNAL_ERROR"

    # Layer 2: Post-Quantum Cryptography
    L2_NO_ACTIVE_SESSION = "L2_NO_ACTIVE_SESSION"
    L2_SESSION_EXPIRED = "L2_SESSION_EXPIRED"
    L2_HANDSHAKE_FAILED = "L2_HANDSHAKE_FAILED"
    L2_AUTHENTICATION_FAILED = "L2_AUTHENTICATION_FAILED"
    L2_REPLAY_DETECTED = "L2_REPLAY_DETECTED"
    L2_SEQUENCE_ERROR = "L2_SEQUENCE_ERROR"
    L2_NONCE_ERROR = "L2_NONCE_ERROR"
    L2_TRUST_ERROR = "L2_TRUST_ERROR"
    L2_VERSION_ERROR = "L2_VERSION_ERROR"
    L2_INVALID_MESSAGE_FORMAT = "L2_INVALID_MESSAGE_FORMAT"
    L2_STALE_TIMESTAMP = "L2_STALE_TIMESTAMP"
    L2_REKEY_FAILED = "L2_REKEY_FAILED"

    # Layer 3: Intent-Based Command Authorization
    L3_UNAUTHORIZED_COMMAND = "L3_UNAUTHORIZED_COMMAND"
    L3_GEOFENCE_VIOLATION = "L3_GEOFENCE_VIOLATION"
    L3_ALTITUDE_VIOLATION = "L3_ALTITUDE_VIOLATION"
    L3_SPEED_VIOLATION = "L3_SPEED_VIOLATION"
    L3_WAYPOINT_UNAUTHORIZED = "L3_WAYPOINT_UNAUTHORIZED"
    L3_STATE_VIOLATION = "L3_STATE_VIOLATION"
    L3_INVALID_PARAMETERS = "L3_INVALID_PARAMETERS"
    L3_INTERNAL_ERROR = "L3_INTERNAL_ERROR"

    # External API & Simulation Bridge
    API_AUTH_FAILED = "API_AUTH_FAILED"
    API_TIMEOUT = "API_TIMEOUT"
    API_MALFORMED_REQUEST = "API_MALFORMED_REQUEST"
    SIM_EXECUTION_FAILED = "SIM_EXECUTION_FAILED"
    INTERNAL_FAILURE = "INTERNAL_FAILURE"


class IntegrationError(Exception):
    """Base exception carrying a structured ErrorCode."""

    def __init__(
        self,
        message: str,
        error_code: ErrorCode = ErrorCode.INTERNAL_FAILURE,
        layer: str = "integration",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.layer = layer
