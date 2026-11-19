"""Adapter wrapping the unmodified Layer 3 intent authorization repository."""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path
from typing import Any

from jsonschema import validate as jsonschema_validate

from common.errors import ErrorCode
from common.models import AuthorizationDecision
from configuration.settings import Layer3Config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAYER3_DIR = _REPO_ROOT / "vendor" / "layer3_intent"
if str(_LAYER3_DIR) not in sys.path:
    sys.path.insert(0, str(_LAYER3_DIR))

from integration_interface import authorize_command  # type: ignore  # noqa: E402
from mission.state_machine import MissionStateMachine  # type: ignore  # noqa: E402
from security.security_logger import log_security_event  # type: ignore  # noqa: E402


def _has_non_finite_float(obj: Any) -> bool:
    if isinstance(obj, float):
        return not math.isfinite(obj)
    if isinstance(obj, dict):
        return any(_has_non_finite_float(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_non_finite_float(v) for v in obj)
    return False


class Layer3IntentAdapter:
    """Validates commands against mission constraints, geofence, and UAV state."""

    def __init__(
        self,
        config: Layer3Config | None = None,
        project_root: Path | None = None,
    ) -> None:
        self.config = config or Layer3Config()
        self.root = project_root or _REPO_ROOT
        self.mission_path = self._resolve(self.config.mission_file)
        self.schema_path = self._resolve(self.config.schema_file)

        with self.schema_path.open("r", encoding="utf-8") as fh:
            self.schema: dict[str, Any] = json.load(fh)
        with self.mission_path.open("r", encoding="utf-8") as fh:
            self.mission: dict[str, Any] = json.load(fh)

        # Validate mission configuration against intent_schema.json on startup
        jsonschema_validate(instance=self.mission, schema=self.schema)
        self.state_machine = MissionStateMachine()

    def _resolve(self, p: str | Path) -> Path:
        path = Path(p)
        if path.is_absolute():
            return path
        return (self.root / path).resolve()

    def get_state(self) -> str:
        return self.state_machine.get_state()

    def reset_state(self) -> str:
        self.state_machine = MissionStateMachine()
        return self.state_machine.get_state()

    def complete_landing(self) -> tuple[bool, str]:
        """Transition state machine from LANDING -> LANDED upon touchdown."""
        return self.state_machine.transition("LAND_COMPLETE")

    @staticmethod
    def _map_reason_to_error_code(reason: str) -> ErrorCode:
        r = reason.lower()
        if "geofence" in r:
            return ErrorCode.L3_GEOFENCE_VIOLATION
        if "altitude" in r and "exceeds" in r:
            return ErrorCode.L3_ALTITUDE_VIOLATION
        if "speed" in r and "exceeds" in r:
            return ErrorCode.L3_SPEED_VIOLATION
        if "waypoint is not authorized" in r:
            return ErrorCode.L3_WAYPOINT_UNAUTHORIZED
        if "not allowed in" in r or "not allowed while" in r:
            return ErrorCode.L3_STATE_VIOLATION
        if "unknown command type" in r or "not allowed by current mission policy" in r:
            return ErrorCode.L3_UNAUTHORIZED_COMMAND
        return ErrorCode.L3_INVALID_PARAMETERS

    def authorize(self, l3_command: dict[str, Any]) -> AuthorizationDecision:
        """Evaluate command against Layer 3 rules and state machine."""
        start = time.perf_counter()
        state_before = self.state_machine.get_state()

        if _has_non_finite_float(l3_command):
            reason = "Command contains non-finite numeric values"
            latency_ms = round((time.perf_counter() - start) * 1000.0, 4)
            return AuthorizationDecision(
                allowed=False,
                result="REJECT",
                reason=reason,
                uav_state_before=state_before,
                uav_state_after=state_before,
                state_message="",
                l3_command=l3_command if isinstance(l3_command, dict) else {},
                error_code=ErrorCode.L3_INVALID_PARAMETERS.value,
                latency_ms=latency_ms,
            )

        decision = authorize_command(l3_command, self.mission, self.state_machine)
        result_str = decision["result"]
        reason = decision["reason"]
        state_after = decision["state"]
        state_msg = decision.get("state_message", "")
        allowed = result_str == "ACCEPT"

        if isinstance(l3_command, dict):
            log_security_event(l3_command, result_str, reason, state_after)

        error_code = (
            ErrorCode.OK
            if allowed
            else self._map_reason_to_error_code(reason)
        )
        latency_ms = round((time.perf_counter() - start) * 1000.0, 4)
        return AuthorizationDecision(
            allowed=allowed,
            result=result_str,
            reason=reason,
            uav_state_before=state_before,
            uav_state_after=state_after,
            state_message=state_msg,
            l3_command=l3_command if isinstance(l3_command, dict) else {},
            error_code=error_code.value,
            latency_ms=latency_ms,
        )
