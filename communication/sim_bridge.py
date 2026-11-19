"""Simulated UAV state and command dispatch queue for Unreal Engine / Cosys-AirSim."""

from __future__ import annotations

import collections
import time
from typing import Any


class UAVSimulatorBridge:
    """Maintains simulated UAV flight state and queues approved commands for UE5/AirSim."""

    def __init__(self, uav_id: str = "uav-01") -> None:
        self.uav_id = uav_id
        self.x: float = 0.0
        self.y: float = 0.0
        self.z_altitude: float = 0.0
        self.speed: float = 0.0
        self.current_waypoint: str = "HOME"
        self.armed: bool = False
        self.in_air: bool = False
        self.unreal_connected: bool = False
        self.last_unreal_heartbeat: float | None = None

        self.executed_commands: list[dict[str, Any]] = []
        self._pending_unreal_queue: collections.deque[dict[str, Any]] = collections.deque()

    def execute_approved_command(
        self,
        message_id: str,
        l3_command: dict[str, Any],
        sequence: int,
        channel: int,
    ) -> dict[str, Any]:
        """Apply an approved command to local sim state and enqueue for Unreal Engine."""
        cmd_type = l3_command.get("type", "")
        now = time.time()

        if cmd_type == "TAKEOFF":
            self.armed = True
            self.in_air = True
            self.z_altitude = float(l3_command.get("altitude", 10.0))
            self.speed = 0.0
        elif cmd_type == "GOTO":
            self.in_air = True
            self.x = float(l3_command.get("x", self.x))
            self.y = float(l3_command.get("y", self.y))
            self.z_altitude = float(l3_command.get("altitude", self.z_altitude))
            self.speed = float(l3_command.get("speed", self.speed))
            self.current_waypoint = str(l3_command.get("waypoint", self.current_waypoint))
        elif cmd_type == "RETURN_HOME":
            self.in_air = True
            self.x = 0.0
            self.y = 0.0
            self.current_waypoint = "HOME"
        elif cmd_type == "LAND":
            self.speed = 0.0
            self.z_altitude = 0.0
            self.in_air = False
            self.armed = False

        record = {
            "command_id": f"cmd-{sequence}-{message_id[:8]}",
            "message_id": message_id,
            "command_type": cmd_type,
            "parameters": {k: v for k, v in l3_command.items() if k != "type"},
            "sequence": sequence,
            "channel": channel,
            "timestamp": now,
            "acknowledged": False,
        }
        self.executed_commands.append(record)
        self._pending_unreal_queue.append(dict(record))
        return record

    def mark_landed(self) -> None:
        self.z_altitude = 0.0
        self.speed = 0.0
        self.in_air = False
        self.armed = False

    def peek_pending_commands(self) -> list[dict[str, Any]]:
        return list(self._pending_unreal_queue)

    def ack_command(self, command_id: str) -> bool:
        """Remove an acknowledged command from the Unreal dispatch queue."""
        remaining: collections.deque[dict[str, Any]] = collections.deque()
        found = False
        while self._pending_unreal_queue:
            item = self._pending_unreal_queue.popleft()
            if not found and item.get("command_id") == command_id:
                found = True
                continue
            remaining.append(item)
        self._pending_unreal_queue = remaining
        return found

    def update_from_unreal_telemetry(self, telemetry_data: dict[str, Any]) -> None:
        """Update local simulator state from external UE 5.8 / Cosys-AirSim telemetry."""
        self.unreal_connected = True
        self.last_unreal_heartbeat = time.time()

        if "x" in telemetry_data:
            self.x = float(telemetry_data["x"])
        if "y" in telemetry_data:
            self.y = float(telemetry_data["y"])
        if "z_altitude" in telemetry_data:
            self.z_altitude = float(telemetry_data["z_altitude"])
        elif "altitude" in telemetry_data:
            self.z_altitude = float(telemetry_data["altitude"])
        if "speed" in telemetry_data:
            self.speed = float(telemetry_data["speed"])
        if "armed" in telemetry_data:
            self.armed = bool(telemetry_data["armed"])
        if "in_air" in telemetry_data:
            self.in_air = bool(telemetry_data["in_air"])
        if "current_waypoint" in telemetry_data:
            self.current_waypoint = str(telemetry_data["current_waypoint"])
