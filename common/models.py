"""Canonical message formats, telemetry models, and Layer 2 <-> Layer 3 translators."""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from common.errors import ErrorCode, IntegrationError

# Mapping between canonical/Layer-3 intent verbs and Layer-2 wire protocol verbs
L3_TO_L2_COMMAND_MAP: dict[str, str] = {
    "GOTO": "WAYPOINT",
    "RETURN_HOME": "RTL",
    "TAKEOFF": "TAKEOFF",
    "LAND": "LAND",
}

L2_TO_L3_COMMAND_MAP: dict[str, str] = {
    "WAYPOINT": "GOTO",
    "RTL": "RETURN_HOME",
    "TAKEOFF": "TAKEOFF",
    "LAND": "LAND",
}


def _reject_non_finite_json(value: str) -> None:
    raise ValueError(f"Non-finite JSON numeric constant rejected: {value}")


def _validate_finite_numbers(obj: Any, context: str = "parameters") -> None:
    """Recursively ensure no NaN/Inf floats exist in dicts/lists."""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise IntegrationError(
                f"Non-finite float value in {context}",
                error_code=ErrorCode.API_MALFORMED_REQUEST,
                layer="common",
            )
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise IntegrationError(
                    f"Non-string dictionary key in {context}",
                    error_code=ErrorCode.API_MALFORMED_REQUEST,
                    layer="common",
                )
            _validate_finite_numbers(v, context=f"{context}.{k}")
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _validate_finite_numbers(item, context=f"{context}[{idx}]")


def translate_command_to_l2(
    command_type: str,
    parameters: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Translate a canonical command + parameter dict into Layer 2 `(verb, params_str)`.

    - ``GOTO`` -> ``("WAYPOINT", compact_json)``
    - ``RETURN_HOME`` -> ``("RTL", compact_json_or_empty)``
    - ``TAKEOFF``, ``LAND``, and other verbs pass through with compact JSON params.
    """
    if not isinstance(command_type, str) or not command_type.strip():
        raise IntegrationError(
            "command_type must be a non-empty string",
            error_code=ErrorCode.L2_INVALID_MESSAGE_FORMAT,
            layer="common",
        )
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise IntegrationError(
            "parameters must be a dictionary",
            error_code=ErrorCode.API_MALFORMED_REQUEST,
            layer="common",
        )

    _validate_finite_numbers(parameters, context="parameters")
    l2_verb = L3_TO_L2_COMMAND_MAP.get(command_type, command_type)

    if not parameters:
        return l2_verb, ""

    try:
        params_str = json.dumps(
            parameters,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise IntegrationError(
            f"Failed to serialize command parameters: {exc}",
            error_code=ErrorCode.API_MALFORMED_REQUEST,
            layer="common",
        ) from exc

    return l2_verb, params_str


def translate_l2_to_l3(l2_verb: str, params_str: str = "") -> dict[str, Any]:
    """Translate a verified Layer 2 `(verb, params_str)` into a Layer 3 command dict.

    - ``WAYPOINT`` -> ``{"type": "GOTO", ...}``
    - ``RTL`` -> ``{"type": "RETURN_HOME", ...}``
    - Always forces ``"type"`` to the mapped command verb so payload parameters
      cannot spoof or override the authenticated Layer 2 verb.
    """
    if not isinstance(l2_verb, str) or not l2_verb:
        return {"type": ""}

    l3_type = L2_TO_L3_COMMAND_MAP.get(l2_verb, l2_verb)
    result: dict[str, Any] = {}

    if isinstance(params_str, str) and params_str.strip():
        try:
            parsed = json.loads(
                params_str,
                parse_constant=_reject_non_finite_json,
            )
            if isinstance(parsed, dict):
                # Never allow embedded "type" field in params to override Layer 2 verb
                for key, val in parsed.items():
                    if key != "type":
                        result[key] = val
            else:
                result["raw_params"] = parsed
        except (json.JSONDecodeError, ValueError):
            result["raw_params"] = params_str

    result["type"] = l3_type
    return result


@dataclass
class CommandRequest:
    """Common external command message envelope."""

    command_type: str
    parameters: dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    sender_id: str = "gcs-01"
    recipient_id: str = "uav-01"
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CommandRequest":
        if not isinstance(data, dict):
            raise IntegrationError(
                "Command request payload must be a JSON object",
                error_code=ErrorCode.API_MALFORMED_REQUEST,
                layer="api",
            )
        command_type = data.get("command_type", data.get("type", ""))
        if not isinstance(command_type, str) or not command_type.strip():
            raise IntegrationError(
                "Missing or invalid 'command_type'",
                error_code=ErrorCode.API_MALFORMED_REQUEST,
                layer="api",
            )

        parameters = data.get("parameters")
        if parameters is None:
            # Support flat command payloads like {"type": "GOTO", "x": 50, ...}
            reserved = {
                "command_type",
                "type",
                "message_id",
                "sender_id",
                "recipient_id",
                "timestamp",
                "metadata",
            }
            parameters = {k: v for k, v in data.items() if k not in reserved}
        elif not isinstance(parameters, dict):
            raise IntegrationError(
                "'parameters' must be an object",
                error_code=ErrorCode.API_MALFORMED_REQUEST,
                layer="api",
            )

        ts = data.get("timestamp", time.time())
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(float(ts)):
            raise IntegrationError(
                "'timestamp' must be a finite number",
                error_code=ErrorCode.API_MALFORMED_REQUEST,
                layer="api",
            )

        msg_id = data.get("message_id") or str(uuid.uuid4())
        sender_id = data.get("sender_id") or "gcs-01"
        recipient_id = data.get("recipient_id") or "uav-01"
        metadata = data.get("metadata") or {}
        if not isinstance(msg_id, str) or not isinstance(sender_id, str) or not isinstance(recipient_id, str):
            raise IntegrationError(
                "message_id, sender_id, and recipient_id must be strings",
                error_code=ErrorCode.API_MALFORMED_REQUEST,
                layer="api",
            )
        if not isinstance(metadata, dict):
            raise IntegrationError(
                "'metadata' must be an object",
                error_code=ErrorCode.API_MALFORMED_REQUEST,
                layer="api",
            )

        _validate_finite_numbers(parameters, context="parameters")
        _validate_finite_numbers(metadata, context="metadata")

        return cls(
            command_type=command_type,
            parameters=parameters,
            message_id=msg_id,
            sender_id=sender_id,
            recipient_id=recipient_id,
            timestamp=float(ts),
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChannelDecision:
    """Layer 1 anti-jamming channel selection outcome."""

    allowed: bool
    selected_channel: int
    observed_state: int
    jammer_type: str
    reward: int
    attempts: int
    hop_history: list[dict[str, Any]]
    cumulative_pdr: float
    error_code: str = ErrorCode.OK.value
    reason: str = ""
    latency_ms: float = 0.0
    layer: str = "layer1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CryptoDecision:
    """Layer 2 post-quantum cryptographic protection and verification outcome."""

    allowed: bool
    session_id_hex: str
    epoch: int
    sequence: int
    sender_id: str
    recipient_id: str
    l2_wire_verb: str
    wire_size_bytes: int
    error_code: str = ErrorCode.OK.value
    reason: str = ""
    latency_ms: float = 0.0
    layer: str = "layer2"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuthorizationDecision:
    """Layer 3 intent-based authorization outcome."""

    allowed: bool
    result: str
    reason: str
    uav_state_before: str
    uav_state_after: str
    state_message: str
    l3_command: dict[str, Any]
    error_code: str = ErrorCode.OK.value
    latency_ms: float = 0.0
    layer: str = "layer3"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TelemetryFrame:
    """Unified UAV telemetry and security posture snapshot."""

    timestamp: float
    uav_id: str
    mission_state: str
    position: dict[str, float]
    speed: float
    current_waypoint: str
    armed: bool
    in_air: bool
    active_channel: int
    pdr_percent: float
    link_connected: bool
    session_established: bool
    session_epoch: int
    last_sequence: int
    accepted_commands: int
    rejected_commands: int
    pending_unreal_commands: int
    unreal_connected: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CommandResponse:
    """Unified integrated security pipeline response."""

    message_id: str
    accepted: bool
    executed: bool
    failed_layer: str | None
    error_code: str
    reason: str
    timestamp: float
    layer1: ChannelDecision | None = None
    layer2: CryptoDecision | None = None
    layer3: AuthorizationDecision | None = None
    telemetry: TelemetryFrame | None = None
    total_latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "accepted": self.accepted,
            "executed": self.executed,
            "failed_layer": self.failed_layer,
            "error_code": self.error_code,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "layer1": self.layer1.to_dict() if self.layer1 else None,
            "layer2": self.layer2.to_dict() if self.layer2 else None,
            "layer3": self.layer3.to_dict() if self.layer3 else None,
            "telemetry": self.telemetry.to_dict() if self.telemetry else None,
            "total_latency_ms": self.total_latency_ms,
        }
