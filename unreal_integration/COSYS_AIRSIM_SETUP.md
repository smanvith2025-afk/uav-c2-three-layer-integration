# Cosys-AirSim Integration Guide

> **Important Environment Note:** Cosys-AirSim and Unreal Engine 5.8 are NOT installed on the integration development machine. The Cosys-AirSim bridge script and `settings.json` instructions below are prepared for execution and end-to-end flight testing on the team member's Unreal Engine 5.8 + Cosys-AirSim workstation.

---

## 1. Integration Architecture with Cosys-AirSim

Cosys-AirSim exposes an RPC server on port `41451` inside the Unreal Engine 5.8 simulation. Instead of letting ground scripts send raw `moveToPositionAsync()` calls directly to AirSim, all commands flow through the Three-Layer Security Server:

```text
[Ground Operator / Mission Script]
        |  (POST /api/v1/command)
        v
[Three-Layer C2 Security Server :8765]
   L1: RL Anti-Jamming Channel Check
   L2: ML-KEM-768 + ML-DSA-65 + AES-256-GCM
   L3: Intent, Geofence, Altitude, Speed, State Machine
        |  (Approved commands queued at /api/v1/commands/pending)
        v
[Cosys-AirSim Bridge Worker (Teammate PC)]
        |  (cosysairsim MultirotorClient RPC :41451)
        v
[Unreal Engine 5.8 + Cosys-AirSim UAV]
```

---

## 2. Cosys-AirSim `settings.json` Configuration

Place or merge the following in `%USERPROFILE%\Documents\AirSim\settings.json` on the UE 5.8 machine:

```json
{
  "SeeDocsAt": "https://cosys-lab.github.io/Cosys-AirSim/settings/",
  "SettingsVersion": 2.0,
  "SimMode": "Multirotor",
  "ApiServerPort": 41451,
  "Vehicles": {
    "uav-01": {
      "VehicleType": "SimpleFlight",
      "X": 0.0,
      "Y": 0.0,
      "Z": 0.0
    }
  }
}
```

Note the vehicle name `"uav-01"` matches `layer2.uav_id` in `configuration/default_config.json`.

---

## 3. Prerequisites on the Unreal Engine / AirSim Machine

1. Launch the UE 5.8 project with the Cosys-AirSim plugin enabled and press **Play** (so RPC port `41451` is listening).
2. Install the Cosys-AirSim Python client package in your Python environment:
   ```powershell
   pip install cosysairsim
   ```
3. Start the Three-Layer Security Server:
   ```powershell
   python integration_entry_point.py --mode server
   ```

---

## 4. Reference Cosys-AirSim Bridge Worker (`airsim_bridge_worker.py`)

Save and run this script on the workstation running Unreal Engine 5.8 + Cosys-AirSim (also installable into `examples/` if desired):

```python
import time
import cosysairsim  # pip install cosysairsim
from api.client import SecurityPipelineClient

VEHICLE_NAME = "uav-01"
SECURITY_HOST = "127.0.0.1"
SECURITY_PORT = 8765


def main() -> None:
    sec_client = SecurityPipelineClient(
        protocol="http",
        host=SECURITY_HOST,
        port=SECURITY_PORT,
        timeout=5.0,
    )

    drone = cosysairsim.MultirotorClient()
    drone.confirmConnection()
    drone.enableApiControl(True, vehicle_name=VEHICLE_NAME)
    drone.armDisarm(True, vehicle_name=VEHICLE_NAME)
    print(f"Connected to Cosys-AirSim ({VEHICLE_NAME}) and Security Server.")

    try:
        while True:
            pending = sec_client.poll_pending_commands().get("commands", [])
            for cmd in pending:
                cmd_id = cmd["command_id"]
                cmd_type = cmd["command_type"]
                params = cmd.get("parameters", {})

                print(f"[EXEC] {cmd_id}: {cmd_type} {params}")
                if cmd_type == "TAKEOFF":
                    alt = float(params.get("altitude", 10.0))
                    drone.takeoffAsync(vehicle_name=VEHICLE_NAME).join()
                    # Cosys-AirSim uses NED coordinates: negative Z is altitude up
                    drone.moveToZAsync(-alt, 3.0, vehicle_name=VEHICLE_NAME).join()
                elif cmd_type == "GOTO":
                    x = float(params["x"])
                    y = float(params["y"])
                    alt = float(params["altitude"])
                    spd = float(params["speed"])
                    drone.moveToPositionAsync(x, y, -alt, spd, vehicle_name=VEHICLE_NAME).join()
                elif cmd_type == "RETURN_HOME":
                    drone.goHomeAsync(vehicle_name=VEHICLE_NAME).join()
                elif cmd_type == "LAND":
                    drone.landAsync(vehicle_name=VEHICLE_NAME).join()
                    sec_client.complete_landing()

                sec_client.ack_command(cmd_id)

            # Push live NED telemetry (converted to positive-up altitude meters)
            state = drone.getMultirotorState(vehicle_name=VEHICLE_NAME)
            pos = state.kinematics_estimated.position
            vel = state.kinematics_estimated.linear_velocity
            speed = (vel.x_val ** 2 + vel.y_val ** 2 + vel.z_val ** 2) ** 0.5
            landed = state.landed_state == cosysairsim.LandedState.Landed

            sec_client.push_unreal_telemetry(
                {
                    "x": round(pos.x_val, 3),
                    "y": round(pos.y_val, 3),
                    "z_altitude": round(-pos.z_val, 3),
                    "speed": round(speed, 3),
                    "armed": True,
                    "in_air": not landed,
                    "landed": landed,
                }
            )
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("Bridge worker stopped.")


if __name__ == "__main__":
    main()
```

---

## 5. Coordinate System Mapping Checklist

- **Layer 3 Geofence (`intent/mission.json`)**: `x in [0, 100]`, `y in [0, 100]`, `altitude in [0, 100]`, `speed in [0, 15]`.
- **Cosys-AirSim NED Conversion**: AirSim uses North-East-Down (`z < 0` means up). Always pass `z = -altitude` to `moveToPositionAsync` / `moveToZAsync`, and negate `pos.z_val` when pushing `z_altitude` back to `/api/v1/telemetry/update`.
- **Verification Tasks Remaining on UE 5.8 Machine**:
  1. Launch UE 5.8 with Cosys-AirSim active and verify `drone.confirmConnection()`.
  2. Start `python integration_entry_point.py --mode server`.
  3. Run `airsim_bridge_worker.py` and submit `TAKEOFF`, `GOTO`, out-of-geofence `GOTO` (verify drone holds position), `RETURN_HOME`, and `LAND` via `examples/sample_client/run_sample_mission.py --connect`.
