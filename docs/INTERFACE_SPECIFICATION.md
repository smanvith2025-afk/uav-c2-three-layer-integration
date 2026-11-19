# Interface Specification

## 1. Internal Python Adapter Interfaces

### 1.1 `Layer1AntiJammingAdapter` (`layer1_adapter/adapter.py`)
- `allocate_channel(max_attempts: int | None = None) -> ChannelDecision`
- `set_link_status(connected: bool) -> None`
- `set_jammer_type(jammer_type: str) -> None` (`"sweep"`, `"reactive"`, `"random"`)
- `force_jam_next(count: int = 1) -> None`
- `get_metrics() -> dict[str, Any]`

### 1.2 `Layer2PQCAdapter` (`layer2_adapter/adapter.py`)
- `establish_session() -> dict[str, Any]`
- `rekey_session() -> dict[str, Any]`
- `terminate_session() -> dict[str, Any]`
- `protect_command(l2_verb: str, params_str: str = "") -> tuple[bytes | None, CryptoDecision]`
- `verify_packet(packet: bytes) -> tuple[bool, dict[str, Any] | None, CryptoDecision]`
- `process_command(l2_verb: str, params_str: str = "") -> tuple[bool, dict[str, Any] | None, CryptoDecision]`
- `get_session_info() -> dict[str, Any]`

### 1.3 `Layer3IntentAdapter` (`layer3_adapter/adapter.py`)
- `authorize(l3_command: dict[str, Any]) -> AuthorizationDecision`
- `complete_landing() -> tuple[bool, str]`
- `get_state() -> str`
- `reset_state() -> str`

---

## 2. Common Data Models (`common/models.py`)

### 2.1 `CommandRequest`
| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `message_id` | `str` | UUIDv4 | Unique correlation identifier |
| `command_type` | `str` | required | Canonical command (`TAKEOFF`, `GOTO`, `LAND`, `RETURN_HOME`, etc.) |
| `parameters` | `dict[str, Any]` | `{}` | Command parameters (finite numbers only) |
| `sender_id` | `str` | `"gcs-01"` | Originating peer identifier |
| `recipient_id` | `str` | `"uav-01"` | Target UAV identifier |
| `timestamp` | `float` | `time.time()` | Unix timestamp (finite positive float) |
| `metadata` | `dict[str, Any]` | `{}` | Optional caller metadata |

### 2.2 `CommandResponse`
| Field | Type | Description |
| :--- | :--- | :--- |
| `message_id` | `str` | Correlation ID from request |
| `accepted` | `bool` | `True` only if L1, L2, and L3 all approved |
| `executed` | `bool` | `True` only if applied to UAV bridge |
| `failed_layer` | `str \| None` | `"api"`, `"layer1"`, `"layer2"`, `"layer3"`, or `null` |
| `error_code` | `str` | Canonical `ErrorCode` string (`"OK"` on acceptance) |
| `reason` | `str` | Human-readable explanation |
| `layer1` | `ChannelDecision \| None` | RL channel allocation details |
| `layer2` | `CryptoDecision \| None` | PQC session, epoch, sequence, and wire size |
| `layer3` | `AuthorizationDecision \| None` | Mission authorization & state transition |
| `telemetry` | `TelemetryFrame \| None` | Post-operation UAV state snapshot |
| `total_latency_ms` | `float` | Total pipeline latency in milliseconds |

### 2.3 `TelemetryFrame`
Contains `timestamp`, `uav_id`, `mission_state` (`IDLE`/`CRUISE`/`LANDING`/`LANDED`), `position` (`{x, y, z_altitude}`), `speed`, `current_waypoint`, `armed`, `in_air`, `active_channel`, `pdr_percent`, `link_connected`, `session_established`, `session_epoch`, `last_sequence`, `accepted_commands`, `rejected_commands`, `pending_unreal_commands`, and `unreal_connected`.

---

## 3. Configuration (`configuration/default_config.json`)

Environment variable overrides supported by `configuration/settings.py`:
- `UAV_C2_JAMMER_TYPE` -> `layer1.jammer_type`
- `UAV_C2_HTTP_HOST`, `UAV_C2_HTTP_PORT` -> `api.http_host`, `api.http_port`
- `UAV_C2_TCP_HOST`, `UAV_C2_TCP_PORT` -> `api.tcp_host`, `api.tcp_port`
- `UAV_C2_API_KEY` -> sets `api.api_key` and enables `api.require_api_key = True`
