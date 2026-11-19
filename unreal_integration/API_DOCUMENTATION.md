# External API Documentation (HTTP/REST & TCP)

This document specifies the external communication interface exposed by `integration_entry_point.py --mode server` (`api/server.py`) for connecting **Unreal Engine 5.8** and **Cosys-AirSim** to the three-layer UAV Command & Control (C2) security pipeline.

---

## 1. Protocol Selection Rationale

| Interface | Default Port | Framing | Recommended Use |
| :--- | :--- | :--- | :--- |
| **HTTP/REST** | `127.0.0.1:8765` | HTTP/1.1 + UTF-8 JSON | Ground Control UI, UE 5.8 `FHttpModule`, diagnostics, mission scripting |
| **TCP Frame** | `127.0.0.1:8766` | 4-byte Big-Endian `!I` length prefix + UTF-8 JSON | Persistent low-overhead telemetry & command polling from C++ `FSocket` |

Both transports dispatch to the exact same `ThreeLayerSecurityPipeline` instance under thread-safe locking (`RLock`), guaranteeing that **no external client can bypass Layer 1, Layer 2, or Layer 3**.

---

## 2. Authentication & Session Lifecycle

### 2.1 Transport API Key (Optional Shared Secret)
When `api.require_api_key` is `true` or `UAV_C2_API_KEY` is set in the environment:
- **HTTP/REST**: Include header `X-API-Key: <your-runtime-key>` on every request.
- **TCP Frame**: Include `"api_key": "<your-runtime-key>"` at the root of the JSON frame.
- Invalid or missing keys return HTTP `401` with `error_code = "API_AUTH_FAILED"`.

### 2.2 Layer 2 PQC Session Establishment
Before commands can be accepted, a post-quantum session (`ML-KEM-768` + `ML-DSA-65` + `HKDF-SHA-256`) must exist:
- The server automatically establishes an initial session on startup (`--mode server`).
- Clients can also explicitly establish, rekey, or terminate sessions via `/api/v1/session/*` endpoints.

---

## 3. HTTP/REST Endpoints

Base URL: `http://127.0.0.1:8765/api/v1`

| Method | Path | Purpose |
| :--- | :--- | :--- |
| `GET` | `/api/v1/health` | Service liveness check |
| `GET` | `/api/v1/status` | Full L1/L2/L3 status and telemetry snapshot |
| `GET` | `/api/v1/telemetry` | Current `TelemetryFrame` |
| `GET` | `/api/v1/commands/pending` | Poll approved commands awaiting UE5/Cosys-AirSim execution |
| `POST` | `/api/v1/session/handshake` | Run 3-message PQC handshake (`ESTABLISHED`) |
| `POST` | `/api/v1/session/rekey` | Rotate epoch traffic keys (`epoch -> epoch + 1`) |
| `POST` | `/api/v1/session/terminate` | Send signed `TERMINATE` packet and wipe keys |
| `POST` | `/api/v1/command` | Submit a UAV command through L1 -> L2 -> L3 -> Bridge |
| `POST` | `/api/v1/commands/ack` | Acknowledge a polled Unreal command by `command_id` |
| `POST` | `/api/v1/commands/land_complete` | Report UAV touchdown (`LANDING -> LANDED`) |
| `POST` | `/api/v1/telemetry/update` | Push live UE5/Cosys-AirSim pose and flight state |

---

## 4. Command & Response JSON Formats

### 4.1 Submit Command (`POST /api/v1/command`)

**Request:**
```json
{
  "message_id": "550e8400-e29b-41d4-a716-446655440000",
  "command_type": "GOTO",
  "parameters": {
    "waypoint": "WP2",
    "x": 50.0,
    "y": 50.0,
    "altitude": 80.0,
    "speed": 10.0
  }
}
```
*(Flat shorthand `{"type": "TAKEOFF", "altitude": 80}` is also accepted.)*

**Accepted Response (HTTP 200):**
```json
{
  "message_id": "550e8400-e29b-41d4-a716-446655440000",
  "accepted": true,
  "executed": true,
  "failed_layer": null,
  "error_code": "OK",
  "reason": "GOTO command is authorized",
  "timestamp": 1795105495.12,
  "layer1": {
    "allowed": true,
    "selected_channel": 4,
    "observed_state": 1,
    "jammer_type": "sweep",
    "reward": 1,
    "attempts": 1,
    "cumulative_pdr": 100.0,
    "error_code": "OK"
  },
  "layer2": {
    "allowed": true,
    "session_id_hex": "b6e41f26f88956bf...",
    "epoch": 0,
    "sequence": 3,
    "l2_wire_verb": "WAYPOINT",
    "wire_size_bytes": 6846,
    "error_code": "OK"
  },
  "layer3": {
    "allowed": true,
    "result": "ACCEPT",
    "reason": "GOTO command is authorized",
    "uav_state_before": "CRUISE",
    "uav_state_after": "CRUISE",
    "error_code": "OK"
  },
  "telemetry": {
    "uav_id": "uav-01",
    "mission_state": "CRUISE",
    "position": { "x": 50.0, "y": 50.0, "z_altitude": 80.0 },
    "speed": 10.0,
    "current_waypoint": "WP2",
    "active_channel": 4,
    "session_established": true,
    "session_epoch": 0,
    "last_sequence": 3
  },
  "total_latency_ms": 3.45
}
```

**Rejected Response (HTTP 200, `accepted: false`, `executed: false`):**
```json
{
  "message_id": "msg-geo-01",
  "accepted": false,
  "executed": false,
  "failed_layer": "layer3",
  "error_code": "L3_GEOFENCE_VIOLATION",
  "reason": "Target position is outside mission geofence",
  "telemetry": {
    "mission_state": "CRUISE",
    "position": { "x": 50.0, "y": 50.0, "z_altitude": 80.0 }
  }
}
```

### 4.2 Poll & Acknowledge Commands for UE 5.8 / AirSim

**`GET /api/v1/commands/pending` Response:**
```json
{
  "commands": [
    {
      "command_id": "cmd-3-550e8400",
      "message_id": "550e8400-e29b-41d4-a716-446655440000",
      "command_type": "GOTO",
      "parameters": {
        "waypoint": "WP2",
        "x": 50.0,
        "y": 50.0,
        "altitude": 80.0,
        "speed": 10.0
      },
      "sequence": 3,
      "channel": 4,
      "timestamp": 1795105495.12,
      "acknowledged": false
    }
  ],
  "error_code": "OK"
}
```

**`POST /api/v1/commands/ack` Request:**
```json
{
  "command_id": "cmd-3-550e8400"
}
```

### 4.3 Push Live UE 5.8 Telemetry (`POST /api/v1/telemetry/update`)
```json
{
  "x": 50.0,
  "y": 50.0,
  "z_altitude": 80.0,
  "speed": 9.8,
  "armed": true,
  "in_air": true,
  "landed": false
}
```
Setting `"landed": true` while `mission_state == "LANDING"` automatically transitions Layer 3's `MissionStateMachine` to `LANDED`.

---

## 5. TCP Framed Protocol (`127.0.0.1:8766`)

Each message on the TCP stream is framed as:
```text
+-----------------------+-----------------------------------+
| 4 bytes (uint32 !I)   | N bytes UTF-8 JSON Payload        |
+-----------------------+-----------------------------------+
```

**Request JSON:**
```json
{
  "action": "command",
  "payload": {
    "command_type": "TAKEOFF",
    "parameters": { "altitude": 50 }
  }
}
```

**Response JSON:**
```json
{
  "ok": true,
  "http_status": 200,
  "error_code": "OK",
  "data": { "...": "identical structure to HTTP response" }
}
```

Supported `action` strings: `health`, `status`, `telemetry`, `telemetry_update`, `handshake`, `rekey`, `terminate`, `command`, `pending_commands`, `ack_command`, `land_complete`.

---

## 6. Error Codes & Timeout Behavior

- **Connection / Read Timeout**: Controlled by `api.request_timeout_seconds` (default `5.0` s). Stalled TCP frames return `error_code = "API_TIMEOUT"` cleanly without blocking other clients.
- **Malformed Input / NaN**: Returns HTTP `400` with `error_code = "API_MALFORMED_REQUEST"`.
- **Layer Error Codes**:
  - Layer 1: `L1_LINK_DOWN`, `L1_CHANNEL_JAMMED`, `L1_INVALID_CHANNEL`, `L1_INTERNAL_ERROR`
  - Layer 2: `L2_NO_ACTIVE_SESSION`, `L2_SESSION_EXPIRED`, `L2_HANDSHAKE_FAILED`, `L2_AUTHENTICATION_FAILED`, `L2_REPLAY_DETECTED`, `L2_SEQUENCE_ERROR`, `L2_NONCE_ERROR`, `L2_TRUST_ERROR`, `L2_VERSION_ERROR`, `L2_INVALID_MESSAGE_FORMAT`, `L2_STALE_TIMESTAMP`, `L2_REKEY_FAILED`
  - Layer 3: `L3_UNAUTHORIZED_COMMAND`, `L3_GEOFENCE_VIOLATION`, `L3_ALTITUDE_VIOLATION`, `L3_SPEED_VIOLATION`, `L3_WAYPOINT_UNAUTHORIZED`, `L3_STATE_VIOLATION`, `L3_INVALID_PARAMETERS`
