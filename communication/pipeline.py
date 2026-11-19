"""Coordinated three-layer UAV C2 security pipeline."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from common.errors import ErrorCode, IntegrationError
from common.logging_utils import SecurityAuditLogger
from common.models import (
    CommandRequest,
    CommandResponse,
    TelemetryFrame,
    translate_command_to_l2,
    translate_l2_to_l3,
)
from communication.sim_bridge import UAVSimulatorBridge
from configuration.settings import SystemConfig, load_config
from layer1_adapter.adapter import Layer1AntiJammingAdapter
from layer2_adapter.adapter import Layer2PQCAdapter
from layer3_adapter.adapter import Layer3IntentAdapter


class ThreeLayerSecurityPipeline:
    """Integrates Layer 1 (RL), Layer 2 (PQC), Layer 3 (Intent), and UAV Bridge."""

    def __init__(self, config: SystemConfig | None = None) -> None:
        self.config = config or load_config()
        self._lock = threading.RLock()

        self.layer1 = Layer1AntiJammingAdapter(self.config.layer1)
        self.layer2 = Layer2PQCAdapter(self.config.layer2)
        self.layer3 = Layer3IntentAdapter(
            self.config.layer3,
            project_root=self.config.project_root,
        )
        self.bridge = UAVSimulatorBridge(uav_id=self.config.layer2.uav_id)
        self.audit_logger = SecurityAuditLogger(
            self.config.resolve_path(self.config.audit_log_file)
        )

        self.accepted_commands: int = 0
        self.rejected_commands: int = 0
        self._l2_msgs_in_epoch: int = 0

    def establish_session(self) -> dict[str, Any]:
        with self._lock:
            info = self.layer2.establish_session()
            self._l2_msgs_in_epoch = 0
            self.audit_logger.log_event("SESSION_ESTABLISHED", info)
            return info

    def rekey_session(self) -> dict[str, Any]:
        with self._lock:
            info = self.layer2.rekey_session()
            self._l2_msgs_in_epoch = 0
            self.audit_logger.log_event("SESSION_REKEYED", info)
            return info

    def terminate_session(self) -> dict[str, Any]:
        with self._lock:
            info = self.layer2.terminate_session()
            self._l2_msgs_in_epoch = 0
            self.audit_logger.log_event("SESSION_TERMINATED", info)
            return info

    def get_telemetry(self) -> TelemetryFrame:
        with self._lock:
            l1 = self.layer1.get_metrics()
            l2 = self.layer2.get_session_info()
            return TelemetryFrame(
                timestamp=time.time(),
                uav_id=self.config.layer2.uav_id,
                mission_state=self.layer3.get_state(),
                position={
                    "x": self.bridge.x,
                    "y": self.bridge.y,
                    "z_altitude": self.bridge.z_altitude,
                },
                speed=self.bridge.speed,
                current_waypoint=self.bridge.current_waypoint,
                armed=self.bridge.armed,
                in_air=self.bridge.in_air,
                active_channel=l1["active_channel"],
                pdr_percent=l1["cumulative_pdr_percent"],
                link_connected=l1["link_connected"],
                session_established=l2["established"],
                session_epoch=l2["epoch"],
                last_sequence=l2["recv_sequence"],
                accepted_commands=self.accepted_commands,
                rejected_commands=self.rejected_commands,
                pending_unreal_commands=len(self.bridge.peek_pending_commands()),
                unreal_connected=self.bridge.unreal_connected,
            )

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "layer1": self.layer1.get_metrics(),
                "layer2": self.layer2.get_session_info(),
                "layer3": {
                    "mission_id": self.layer3.mission.get("mission_id"),
                    "mission": self.layer3.mission.get("mission"),
                    "state": self.layer3.get_state(),
                    "max_altitude": self.layer3.mission.get("max_altitude"),
                    "max_speed": self.layer3.mission.get("max_speed"),
                    "geofence": self.layer3.mission.get("geofence"),
                },
                "telemetry": self.get_telemetry().to_dict(),
            }

    def push_unreal_telemetry(self, telemetry_data: dict[str, Any]) -> TelemetryFrame:
        with self._lock:
            self.bridge.update_from_unreal_telemetry(telemetry_data)
            if telemetry_data.get("landed") and self.layer3.get_state() == "LANDING":
                self.layer3.complete_landing()
                self.bridge.mark_landed()
            self.audit_logger.log_event("UNREAL_TELEMETRY_UPDATE", telemetry_data)
            return self.get_telemetry()

    def complete_landing(self) -> tuple[bool, str]:
        with self._lock:
            ok, msg = self.layer3.complete_landing()
            if ok:
                self.bridge.mark_landed()
            self.audit_logger.log_event(
                "LANDING_COMPLETED",
                {"accepted": ok, "message": msg, "state": self.layer3.get_state()},
            )
            return ok, msg

    def _maybe_auto_rekey(self) -> None:
        interval = self.config.layer2.auto_rekey_interval_messages
        if interval <= 0 or not self.layer2.is_session_established():
            return
        if self._l2_msgs_in_epoch >= interval:
            self.rekey_session()

    def process_command(self, raw_request: CommandRequest | dict[str, Any]) -> CommandResponse:
        """Process a command sequentially through Layer 1 -> Layer 2 -> Layer 3 -> UAV Bridge."""
        start_total = time.perf_counter()
        with self._lock:
            try:
                req = (
                    raw_request
                    if isinstance(raw_request, CommandRequest)
                    else CommandRequest.from_dict(raw_request)
                )
            except IntegrationError as exc:
                self.rejected_commands += 1
                resp = CommandResponse(
                    message_id=str(uuid.uuid4()),
                    accepted=False,
                    executed=False,
                    failed_layer="api",
                    error_code=exc.error_code.value,
                    reason=str(exc),
                    timestamp=time.time(),
                    telemetry=self.get_telemetry(),
                    total_latency_ms=round((time.perf_counter() - start_total) * 1000.0, 4),
                )
                self.audit_logger.log_event("COMMAND_REJECTED", resp.to_dict())
                return resp

            # --------------------------------------------------------------
            # Layer 1: Anti-Jamming RL Channel Selection
            # --------------------------------------------------------------
            l1_decision = self.layer1.allocate_channel()
            if not l1_decision.allowed:
                self.rejected_commands += 1
                resp = CommandResponse(
                    message_id=req.message_id,
                    accepted=False,
                    executed=False,
                    failed_layer="layer1",
                    error_code=l1_decision.error_code,
                    reason=l1_decision.reason,
                    timestamp=time.time(),
                    layer1=l1_decision,
                    telemetry=self.get_telemetry(),
                    total_latency_ms=round((time.perf_counter() - start_total) * 1000.0, 4),
                )
                self.audit_logger.log_event("COMMAND_REJECTED", resp.to_dict())
                return resp

            # --------------------------------------------------------------
            # Layer 2: Post-Quantum Cryptographic Protection & Verification
            # --------------------------------------------------------------
            try:
                l2_verb, params_str = translate_command_to_l2(
                    req.command_type, req.parameters
                )
            except IntegrationError as exc:
                self.rejected_commands += 1
                resp = CommandResponse(
                    message_id=req.message_id,
                    accepted=False,
                    executed=False,
                    failed_layer="layer2",
                    error_code=exc.error_code.value,
                    reason=str(exc),
                    timestamp=time.time(),
                    layer1=l1_decision,
                    telemetry=self.get_telemetry(),
                    total_latency_ms=round((time.perf_counter() - start_total) * 1000.0, 4),
                )
                self.audit_logger.log_event("COMMAND_REJECTED", resp.to_dict())
                return resp

            l2_ok, verified_cmd, l2_decision = self.layer2.process_command(
                l2_verb, params_str
            )
            if l2_ok and l2_decision.allowed:
                self._l2_msgs_in_epoch += 1
                self._maybe_auto_rekey()

            if not l2_ok or not l2_decision.allowed or verified_cmd is None:
                self.rejected_commands += 1
                resp = CommandResponse(
                    message_id=req.message_id,
                    accepted=False,
                    executed=False,
                    failed_layer="layer2",
                    error_code=l2_decision.error_code,
                    reason=l2_decision.reason,
                    timestamp=time.time(),
                    layer1=l1_decision,
                    layer2=l2_decision,
                    telemetry=self.get_telemetry(),
                    total_latency_ms=round((time.perf_counter() - start_total) * 1000.0, 4),
                )
                self.audit_logger.log_event("COMMAND_REJECTED", resp.to_dict())
                return resp

            # --------------------------------------------------------------
            # Layer 3: Intent-Based Command Authorization
            # --------------------------------------------------------------
            l3_command = translate_l2_to_l3(
                verified_cmd["command"], verified_cmd["params"]
            )
            l3_decision = self.layer3.authorize(l3_command)
            if not l3_decision.allowed:
                self.rejected_commands += 1
                resp = CommandResponse(
                    message_id=req.message_id,
                    accepted=False,
                    executed=False,
                    failed_layer="layer3",
                    error_code=l3_decision.error_code,
                    reason=l3_decision.reason,
                    timestamp=time.time(),
                    layer1=l1_decision,
                    layer2=l2_decision,
                    layer3=l3_decision,
                    telemetry=self.get_telemetry(),
                    total_latency_ms=round((time.perf_counter() - start_total) * 1000.0, 4),
                )
                self.audit_logger.log_event("COMMAND_REJECTED", resp.to_dict())
                return resp

            # --------------------------------------------------------------
            # UAV Simulator / Unreal Bridge Dispatch
            # --------------------------------------------------------------
            self.bridge.execute_approved_command(
                message_id=req.message_id,
                l3_command=l3_command,
                sequence=l2_decision.sequence,
                channel=l1_decision.selected_channel,
            )
            if l3_command.get("type") == "LAND" and self.config.layer3.auto_complete_landing:
                self.layer3.complete_landing()
                self.bridge.mark_landed()

            self.accepted_commands += 1
            resp = CommandResponse(
                message_id=req.message_id,
                accepted=True,
                executed=True,
                failed_layer=None,
                error_code=ErrorCode.OK.value,
                reason=l3_decision.reason,
                timestamp=time.time(),
                layer1=l1_decision,
                layer2=l2_decision,
                layer3=l3_decision,
                telemetry=self.get_telemetry(),
                total_latency_ms=round((time.perf_counter() - start_total) * 1000.0, 4),
            )
            self.audit_logger.log_event("COMMAND_ACCEPTED", resp.to_dict())
            return resp

    def process_wire_packet(
        self,
        raw_packet: bytes,
        message_id: str | None = None,
    ) -> CommandResponse:
        """Process a pre-built Layer 2 wire packet through L1 -> L2 Receiver -> L3 -> Bridge.

        Used for adversarial wire-level security verification (tampering, replay,
        spoofing, stale timestamp, sequence anomalies) without bypassing L1 or L3.
        """
        start_total = time.perf_counter()
        msg_id = message_id or str(uuid.uuid4())
        with self._lock:
            l1_decision = self.layer1.allocate_channel()
            if not l1_decision.allowed:
                self.rejected_commands += 1
                resp = CommandResponse(
                    message_id=msg_id,
                    accepted=False,
                    executed=False,
                    failed_layer="layer1",
                    error_code=l1_decision.error_code,
                    reason=l1_decision.reason,
                    timestamp=time.time(),
                    layer1=l1_decision,
                    telemetry=self.get_telemetry(),
                    total_latency_ms=round((time.perf_counter() - start_total) * 1000.0, 4),
                )
                self.audit_logger.log_event("WIRE_PACKET_REJECTED", resp.to_dict())
                return resp

            l2_ok, verified_cmd, l2_decision = self.layer2.verify_packet(raw_packet)
            if not l2_ok or not l2_decision.allowed or verified_cmd is None:
                self.rejected_commands += 1
                resp = CommandResponse(
                    message_id=msg_id,
                    accepted=False,
                    executed=False,
                    failed_layer="layer2",
                    error_code=l2_decision.error_code,
                    reason=l2_decision.reason,
                    timestamp=time.time(),
                    layer1=l1_decision,
                    layer2=l2_decision,
                    telemetry=self.get_telemetry(),
                    total_latency_ms=round((time.perf_counter() - start_total) * 1000.0, 4),
                )
                self.audit_logger.log_event("WIRE_PACKET_REJECTED", resp.to_dict())
                return resp

            l3_command = translate_l2_to_l3(
                verified_cmd["command"], verified_cmd["params"]
            )
            l3_decision = self.layer3.authorize(l3_command)
            if not l3_decision.allowed:
                self.rejected_commands += 1
                resp = CommandResponse(
                    message_id=msg_id,
                    accepted=False,
                    executed=False,
                    failed_layer="layer3",
                    error_code=l3_decision.error_code,
                    reason=l3_decision.reason,
                    timestamp=time.time(),
                    layer1=l1_decision,
                    layer2=l2_decision,
                    layer3=l3_decision,
                    telemetry=self.get_telemetry(),
                    total_latency_ms=round((time.perf_counter() - start_total) * 1000.0, 4),
                )
                self.audit_logger.log_event("WIRE_PACKET_REJECTED", resp.to_dict())
                return resp

            self.bridge.execute_approved_command(
                message_id=msg_id,
                l3_command=l3_command,
                sequence=l2_decision.sequence,
                channel=l1_decision.selected_channel,
            )
            self.accepted_commands += 1
            resp = CommandResponse(
                message_id=msg_id,
                accepted=True,
                executed=True,
                failed_layer=None,
                error_code=ErrorCode.OK.value,
                reason=l3_decision.reason,
                timestamp=time.time(),
                layer1=l1_decision,
                layer2=l2_decision,
                layer3=l3_decision,
                telemetry=self.get_telemetry(),
                total_latency_ms=round((time.perf_counter() - start_total) * 1000.0, 4),
            )
            self.audit_logger.log_event("WIRE_PACKET_ACCEPTED", resp.to_dict())
            return resp
