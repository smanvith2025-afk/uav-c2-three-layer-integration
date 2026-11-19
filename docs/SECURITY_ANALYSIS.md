# Security Analysis

## 1. Defense-in-Depth Properties Preserved

1. **Availability & Jamming Resilience (Layer 1)**:
   - Pre-trained Q-learning channel selection avoids predictable sweep and reactive jammers.
   - Link disconnection (`L1_LINK_DOWN`) or total hop exhaustion (`L1_CHANNEL_JAMMED`) fails closed before cryptographic sequence counters advance or commands reach the UAV.
2. **Post-Quantum Confidentiality, Authenticity & Freshness (Layer 2)**:
   - `ML-KEM-768` (FIPS 203) ephemeral key encapsulation + `ML-DSA-65` (FIPS 204) digital signatures protect against harvest-now-decrypt-later quantum adversaries and classical spoofing.
   - `AES-256-GCM` with 22-byte header as AAD prevents wire header/ciphertext tampering.
   - Strict sequence tracking (`validate_recv_sequence`), bounded 200-entry nonce sliding window (`NONCE_WINDOW_SIZE = 200`), and $\pm 60\text{ s}$ timestamp skew reject replay and stale packet injection.
   - Ephemeral signing keys are generated at runtime via `sig_generate_keypair()` — zero hardcoded private keys exist in source code or configuration.
   - `TrustStore` enforces role separation (`ground_station` vs `uav`) and peer revocation.
3. **Mission-Aware Safety Enforcement (Layer 3)**:
   - Authenticated Ground Station operators still cannot move the UAV outside `[0, 100] x [0, 100]`, exceed `max_altitude = 100 m` or `max_speed = 15 m/s`, fly to unapproved waypoints, or issue out-of-phase commands (e.g., `GOTO` before `TAKEOFF`, `TAKEOFF` while `CRUISE`).
   - Rejected commands preserve `MissionStateMachine` state cleanly.
4. **Translator Anti-Spoofing (`common/models.py`)**:
   - `translate_l2_to_l3` explicitly ignores any `"type"` key embedded inside JSON parameters and overwrites `"type"` from the verified Layer 2 command verb.
   - `parse_constant` rejects `NaN`, `Infinity`, and `-Infinity` JSON constants at API, message parsing, and Layer 3 adapter boundaries.
5. **Secret-Safe Audit Logging (`common/logging_utils.py`)**:
   - `SecurityAuditLogger` recursively redacts any field containing `secret`, `private_key`, `secret_key`, `root_secret`, `shared_secret`, `enc_key`, `confirm_key`, `api_key`, `token`, `password`, or `signature`.

---

## 2. Known Limitations & Deployment Assumptions

1. **Simulated RF Channel**: Layer 1 uses `CommunicationSimulator` (discrete 8-channel model), not SDR/RF hardware.
2. **In-Process Layer 2 Link**: Inside the unified service, Layer 2 Ground Station `Sender` and UAV `Receiver` communicate over `LocalTransport` while encrypting, signing, decrypting, and verifying full wire packets. If physical separation between GCS and UAV processes is introduced later, `LocalTransport` can be replaced with a `Transport` subclass wrapping UDP/TCP sockets without altering Layer 2 protocol logic.
3. **External UE 5.8 / AirSim API Transport**: The external HTTP/TCP bridge binds to `127.0.0.1` by default. If UE 5.8 runs on a different workstation over LAN, operators should enable `UAV_C2_API_KEY` and tunnel traffic (e.g., SSH port-forwarding or TLS reverse proxy) rather than exposing plaintext HTTP across untrusted networks.
4. **Unreal Engine 5.8 Runtime Untested Locally**: Live flight testing in Unreal Engine 5.8 + Cosys-AirSim has not yet been executed because those applications are installed on another team member's computer.
