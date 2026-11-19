# End-to-End Command and Response Data Flow

## 1. Normal Command Processing Sequence

```text
Caller / UE5 / Sample Client
   |
   | 1. CommandRequest(command_type="GOTO", parameters={waypoint, x, y, altitude, speed})
   v
[ThreeLayerSecurityPipeline.process_command]
   |
   | 2. Layer 1: layer1.allocate_channel()
   |    - Observe jammer state -> QLearningAgent.choose_action(state) -> step(channel)
   |    - Retry up to max_hop_attempts if jammed
   |    - FAIL -> Abort immediately (failed_layer="layer1", L1_LINK_DOWN / L1_CHANNEL_JAMMED)
   |
   | 3. Command Translation: translate_command_to_l2("GOTO", params) -> ("WAYPOINT", compact_json)
   |
   | 4. Layer 2: layer2.process_command("WAYPOINT", compact_json)
   |    - Sender checks active ESTABLISHED session & liveness
   |    - Assigns monotonic sequence + deterministic 96-bit nonce
   |    - Signs canonical payload with Ground Station ML-DSA-65 secret key
   |    - Encrypts with AES-256-GCM (header as AAD)
   |    - LocalTransport delivers wire packet to UAV Receiver
   |    - Receiver validates header, session_id, AES-GCM tag, schema, timestamp (+-60s),
   |      TrustStore role & pinned key, sequence replay window, and ML-DSA-65 signature
   |    - FAIL -> Abort immediately (failed_layer="layer2", L2_*)
   |
   | 5. Command Translation: translate_l2_to_l3("WAYPOINT", compact_json) -> {"type": "GOTO", ...}
   |
   | 6. Layer 3: layer3.authorize(l3_command)
   |    - Validates mission rules, waypoint allowlist, altitude/speed bounds, geofence
   |    - Checks & transitions MissionStateMachine (IDLE -> CRUISE -> LANDING -> LANDED)
   |    - Records decision to Layer 3 security_log.jsonl
   |    - FAIL -> State preserved; abort immediately (failed_layer="layer3", L3_*)
   |
   | 7. UAVSimulatorBridge.execute_approved_command(...)
   |    - Updates local pose/state and appends to pending_unreal_queue
   |
   | 8. SecurityAuditLogger writes redacted JSONL record
   v
CommandResponse(accepted=True, executed=True, error_code="OK", layer1, layer2, layer3, telemetry)
```

---

## 2. Fail-Closed Guarantees per Failure Point

| Failure Location | State Impact on Downstream Layers | Simulator Execution | Error Code Example |
| :--- | :--- | :--- | :--- |
| **Layer 1 Link Down / Jammed** | Layer 2 sequence/nonce untouched; Layer 3 untouched | Never executed | `L1_LINK_DOWN`, `L1_CHANNEL_JAMMED` |
| **Layer 2 No Session / Expired** | Layer 3 untouched | Never executed | `L2_NO_ACTIVE_SESSION`, `L2_SESSION_EXPIRED` |
| **Layer 2 Tamper / Forgery / Replay** | Packet rejected before command handler; Layer 3 untouched | Never executed | `L2_AUTHENTICATION_FAILED`, `L2_REPLAY_DETECTED` |
| **Layer 3 Policy / Geofence / State** | Layer 2 sequence legitimately consumed (prevents replay); Layer 3 state machine unchanged | Never executed | `L3_GEOFENCE_VIOLATION`, `L3_STATE_VIOLATION` |

---

## 3. Touchdown & Rekeying Flows

### Landing Lifecycle (`LAND` -> `LAND_COMPLETE`)
1. `LAND` command passes L1 -> L2 -> L3, transitioning `MissionStateMachine` from `CRUISE` to `LANDING`.
2. When Unreal Engine / Cosys-AirSim touches down (or `POST /api/v1/commands/land_complete` / `telemetry.landed=true` arrives), `Layer3IntentAdapter.complete_landing()` triggers `LAND_COMPLETE`, moving `LANDING -> LANDED`.
3. A subsequent `TAKEOFF` command is permitted from `LANDED` back into `CRUISE`.

### In-Session Rekeying
- Triggered manually via `POST /api/v1/session/rekey` (`pipeline.rekey_session()`) or automatically every `auto_rekey_interval_messages` verified Layer 2 commands.
- Chains previous `root_secret` with fresh ephemeral `ML-KEM-768` shared secret, verifies `ML-DSA-65` signatures and HMAC key confirmation tag, wipes old epoch traffic keys, and increments `epoch`.
