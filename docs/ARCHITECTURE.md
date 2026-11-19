# Three-Layer UAV Command & Control Security Architecture

## 1. System Overview

This repository integrates three independently developed UAV Command-and-Control (C2) defense-in-depth layers into a single unified, fail-closed security pipeline prepared for **Unreal Engine 5.8** and **Cosys-AirSim**:

1. **Layer 1 (`vendor/layer1_drone_rl_agent` -> `layer1_adapter/`) — Anti-Jamming Reinforcement Learning**
   - Uses tabular Q-learning (`QLearningAgent`, `ChannelSelector`) and `CommunicationSimulator` across 8 RF channels under `sweep`, `reactive`, and `random` jammer models.
   - Monitors simulated link state and selects a non-jammed channel before any packet is transmitted.
2. **Layer 2 (`vendor/layer2_pqc` -> `layer2_adapter/`) — Post-Quantum Cryptographic Security (`UAV-C2-PQ-L2 v2`)**
   - Uses `ML-KEM-768` (FIPS 203), `ML-DSA-65` (FIPS 204), two-stage `HKDF-SHA-256` (RFC 5869), and `AES-256-GCM` (NIST SP 800-38D).
   - Provides mutually authenticated 3-message sessions, ephemeral runtime keypairs, role binding (`ground_station` vs `uav`), deterministic 96-bit nonces, bounded `O(1)` 200-entry replay tracking, $\pm 60\text{ s}$ freshness checks, and chained rekeying.
3. **Layer 3 (`vendor/layer3_intent` -> `layer3_adapter/`) — Intent-Based Command Authorization**
   - Validates mission JSON against `intent_schema.json` using `jsonschema`.
   - Enforces command policy, waypoint allowlist, maximum altitude (`100 m`), maximum speed (`15 m/s`), rectangular geofence (`[0, 100] x [0, 100]`), and UAV state transitions (`IDLE -> CRUISE -> LANDING -> LANDED`) via `authorize_command` and `MissionStateMachine`.

---

## 2. High-Level Component Architecture

```text
+-------------------------------------------------------------------------+
|                 External Clients (UE 5.8 / Cosys-AirSim / CLI)          |
+----------------------------+-----------------------------+--------------+
                             | HTTP/REST (:8765)           | TCP Frame (:8766)
                             v                             v
+-------------------------------------------------------------------------+
|                    api/server.py (SecurityBridgeServer)                 |
|     - Optional X-API-Key (constant-time hmac.compare_digest)            |
|     - JSON NaN/Inf & size guards (1 MiB cap) + socket timeouts          |
+------------------------------------+------------------------------------+
                                     |
                                     v
+-------------------------------------------------------------------------+
|            communication/pipeline.py (ThreeLayerSecurityPipeline)       |
|                                                                         |
|  1. layer1_adapter/adapter.py  ---> vendor/layer1_drone_rl_agent/       |
|       - RL channel selection & hop retries                              |
|       - Rejects on link down or total jamming blackout                  |
|                                                                         |
|  2. layer2_adapter/adapter.py  ---> vendor/layer2_pqc/                  |
|       - Sender: ML-DSA-65 sign + AES-256-GCM encrypt                    |
|       - Receiver: AAD decrypt + sequence/nonce replay + sig verify      |
|       - Rejects tampered, replayed, stale, revoked, or spoofed packets  |
|                                                                         |
|  3. layer3_adapter/adapter.py  ---> vendor/layer3_intent/               |
|       - Canonical verb translation (WAYPOINT->GOTO, RTL->RETURN_HOME)   |
|       - Mission rules, geofence, altitude, speed & state machine check  |
|       - Rejects policy, boundary, or invalid state violations           |
|                                                                         |
|  4. communication/sim_bridge.py (UAVSimulatorBridge)                    |
|       - Executes ONLY commands accepted by L1 + L2 + L3                 |
|       - Queues approved commands for Unreal Engine / Cosys-AirSim       |
+-------------------------------------------------------------------------+
```

---

## 3. Repository Inspection & Architectural Integration Decisions

### 3.1 Preserving Unmodified Security Implementations
Rather than copying or rewriting cryptographic, RL, or mission-validation internals, the three original repositories are preserved verbatim under `vendor/`:
- `vendor/layer1_drone_rl_agent/` + `vendor/results/` (copies of `q_table_*.json` so `train.py`'s `../results` relative lookup works unmodified).
- `vendor/layer2_pqc/`
- `vendor/layer3_intent/`

All integration logic lives in dedicated adapters (`layer1_adapter/`, `layer2_adapter/`, `layer3_adapter/`) and orchestration modules (`common/`, `communication/`, `configuration/`, `api/`).

### 3.2 Resolving Command Vocabulary & Schema Differences (L2 vs. L3)
Inspection of `vendor/layer2_pqc/protocol/constants.py` and `vendor/layer3_intent/validator/validator.py` revealed incompatible command verb sets:
- **Layer 2 `VALID_COMMANDS`**: `TAKEOFF`, `LAND`, `HOVER`, `RTL`, `WAYPOINT`, `SET_ALTITUDE`, `SET_SPEED`, `EMERGENCY_STOP`, `ABORT`.
- **Layer 3 `supported_commands`**: `TAKEOFF`, `GOTO`, `LAND`, `RETURN_HOME`.

**Resolution (`common/models.py`)**:
- `GOTO` maps to Layer 2 wire verb `WAYPOINT` (parameters serialized as compact JSON `<= 512` chars) and maps back to `{"type": "GOTO", ...}` on the UAV Receiver for Layer 3.
- `RETURN_HOME` maps to Layer 2 wire verb `RTL` and maps back to `{"type": "RETURN_HOME"}` for Layer 3.
- Parameter JSON cannot spoof `"type"` because `translate_l2_to_l3` overwrites `"type"` with the authenticated Layer 2 verb mapping.
- Layer 2 verbs not in Layer 3's mission policy (e.g., `HOVER`, `EMERGENCY_STOP`) pass cryptographic checks and are rejected cleanly at Layer 3 (`L3_UNAUTHORIZED_COMMAND`).
- Verbs absent from Layer 2's whitelist (e.g., `ARM`, `DESTROY`) are rejected by Layer 2 (`L2_INVALID_MESSAGE_FORMAT`).

### 3.3 Avoiding Duplicate Encryption & Session Conflicts
- Layer 2 (`UAV-C2-PQ-L2 v2`) owns all cryptographic session establishment, ephemeral KEM key exchange, AES-256-GCM encryption, and epoch rekeying between Ground Station (`Sender`) and UAV (`Receiver`).
- External UE 5.8 / Cosys-AirSim clients do **not** implement a second custom encryption layer; instead they interact with the security server over loopback HTTP/TCP (or LAN with `X-API-Key` / optional SSH/TLS tunnel if deployed across separate machines).

### 3.4 Resolving Missing Dependency in Layer 3 Tests
- Layer 3's `test_intent_schema.py`, `test_invalid_command.py`, and `test_invalid_intent.py` require `jsonschema`, which is declared in root `requirements.txt`.
