# Three-Layer UAV Command and Control (C2) Security Integration

Unified integration of three independently developed UAV C2 security layers prepared for **Unreal Engine 5.8** and **Cosys-AirSim**:

1. **Layer 1 (`layer1_adapter/` -> `vendor/layer1_drone_rl_agent/`)**: Q-learning anti-jamming RF channel selection (`sweep`, `reactive`, `random` jammers).
2. **Layer 2 (`layer2_adapter/` -> `vendor/layer2_pqc/`)**: Post-quantum cryptographic session protocol (`ML-KEM-768`, `ML-DSA-65`, two-stage `HKDF-SHA-256`, `AES-256-GCM`).
3. **Layer 3 (`layer3_adapter/` -> `vendor/layer3_intent/`)**: Intent-based command authorization (mission schema, geofencing, altitude/speed bounds, waypoint allowlist, UAV state machine).

---

## Repository Structure

```text
uav-c2-three-layer-integration/
├── api/                        # HTTP/REST (:8765) & TCP (:8766) external servers and client
├── common/                     # Shared dataclasses, error codes, L2<->L3 translators, audit logger
├── communication/              # ThreeLayerSecurityPipeline and UAVSimulatorBridge
├── configuration/              # Central JSON config + env var overrides
├── docs/                       # Architecture, Data Flow, Interface Spec, Security Analysis, Test Report
├── examples/
│   ├── sample_client/          # Working HTTP/TCP sample mission client
│   └── sample_commands/        # Sample valid and adversarial JSON commands
├── layer1_adapter/             # Layer 1 RL anti-jamming adapter
├── layer2_adapter/             # Layer 2 PQC protocol adapter
├── layer3_adapter/             # Layer 3 Intent authorization adapter
├── tests/
│   ├── unit_tests/             # Adapter & translator unit tests
│   ├── integration_tests/      # End-to-end pipeline and HTTP/TCP API tests
│   └── security_tests/         # Replay, tamper, forgery, geofence, and redaction tests
├── unreal_integration/         # API docs, UE 5.8 C++ component, and Cosys-AirSim bridge guide
├── vendor/                     # Unmodified source copies of Layer 1, Layer 2, Layer 3 + Q-tables
├── integration_entry_point.py  # Main CLI entry point (--mode verify | demo | server)
├── pytest.ini
└── requirements.txt
```

---

## Installation

1. Ensure Python 3.10+ (verified on Python `3.14.5`) and native `liboqs` (`0.16.0`) are installed.
2. Install Python dependencies:
   ```powershell
   pip install -r requirements.txt
   ```

---

## Quick Start & Execution

### Verify All Three Layers
```powershell
python integration_entry_point.py --mode verify
```

### Run Interactive Three-Layer Security Demonstration
```powershell
python integration_entry_point.py --mode demo
```

### Start the External Server for Unreal Engine 5.8 / Cosys-AirSim
```powershell
python integration_entry_point.py --mode server
```
Default endpoints:
- HTTP/REST API: `http://127.0.0.1:8765/api/v1/`
- TCP Framed API: `127.0.0.1:8766`

### Run Sample Client (HTTP & TCP)
```powershell
python examples/sample_client/run_sample_mission.py
```
Or connect to an already-running `--mode server` instance:
```powershell
python examples/sample_client/run_sample_mission.py --connect --protocol http --host 127.0.0.1 --port 8765
```

---

## Running Tests

### 1. Integrated Unit, Integration & Security Suite (`29` tests)
```powershell
python -m pytest tests/ -v
```

### 2. Original Layer 2 Suite (`157` tests)
```powershell
cd vendor/layer2_pqc
python -m pytest test_protocol.py -v
```

### 3. Original Layer 3 Suite (`39` unittest tests + 6 script validations)
```powershell
cd vendor/layer3_intent
python -m unittest discover -s tests -v
```

---

## Documentation Index

- [Architecture Overview](docs/ARCHITECTURE.md)
- [Command & Telemetry Data Flow](docs/DATA_FLOW.md)
- [Interface Specification](docs/INTERFACE_SPECIFICATION.md)
- [Security Analysis & Limitations](docs/SECURITY_ANALYSIS.md)
- [Test & Validation Report](docs/TEST_REPORT.md)
- [External HTTP/TCP API Reference](unreal_integration/API_DOCUMENTATION.md)
- [Unreal Engine 5.8 C++ Setup Guide](unreal_integration/UNREAL_ENGINE_SETUP.md)
- [Cosys-AirSim Bridge Setup Guide](unreal_integration/COSYS_AIRSIM_SETUP.md)
