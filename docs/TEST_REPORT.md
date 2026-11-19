# Test & Validation Report

## 1. Environment

- **OS**: Windows 11
- **Python**: `3.14.5`
- **Key Dependencies**: `liboqs-python==0.16.0.1`, `cryptography==50.0.1`, `jsonschema==4.26.0`, `pytest==9.1.1`
- **Untested Environment**: Unreal Engine 5.8 and Cosys-AirSim are NOT installed on this workstation; live UE 5.8 / AirSim flight execution remains pending on the teammate's workstation.

---

## 2. Original Repository Test Suites

### Layer 1 (`drone-rl-agent`)
- No formal `pytest`/`unittest` suite existed in the original repository (it provides `train.py` and `evaluate.py`).
- Smoke execution against `CommunicationSimulator` and `q_table_sweep.json` succeeded (`state=1, ch=5, reward=1, ok=True`), and 7 dedicated unit tests were created in `tests/unit_tests/test_layer1_unit.py`.

### Layer 2 (`vendor/layer2_pqc/test_protocol.py`)
- **Command**: `python -m pytest test_protocol.py -q` (run inside `vendor/layer2_pqc`)
- **Result**: **`157 passed in 2.13s`**

### Layer 3 (`vendor/layer3_intent/tests/`)
- **Initial finding**: Running `python -m unittest discover -s tests -v` before installing `jsonschema` produced `3` import errors (`ModuleNotFoundError: No module named 'jsonschema'`) in `test_intent_schema.py`, `test_invalid_command.py`, and `test_invalid_intent.py`.
- **Resolution**: Added `jsonschema` to `requirements.txt` and installed `jsonschema==4.26.0`.
- **Command**: `python -m unittest discover -s tests -v` (run inside `vendor/layer3_intent`)
- **Result**: **`Ran 39 tests in 0.006s ... OK`**, plus all 6 script-based test modules (`test_attacks.py`, `test_edge_cases.py`, `test_intent_schema.py`, `test_invalid_command.py`, `test_invalid_intent.py`, `test_phase_policy.py`) executed cleanly with zero errors. (Note: `test_validator.py` in the original repository is empty.)

---

## 3. Integrated Test Suite (`tests/`)

- **Command**: `python -m pytest tests/ -v`
- **Initial bug caught & fixed during testing**:
  - `test_auto_rekey_triggers_when_threshold_reached` caught an off-by-one/modulo drift bug in `_maybe_auto_rekey()` (`communication/pipeline.py`). Tracking `_l2_msgs_in_epoch` and calling `rekey_session()` immediately after reaching `auto_rekey_interval_messages` resolved it cleanly.
- **Result after fix**: **`29 passed in 5.55s`**:
  - `tests/unit_tests/test_layer1_unit.py` (`7` passed): sweep/reactive/random Q-tables, >=90% sweep PDR, link drop & recovery, forced jamming hop exhaustion, invalid jammer check.
  - `tests/unit_tests/test_layer2_unit.py` (`3` passed): full handshake/command/rekey/terminate, no-session rejection, unlisted verb rejection.
  - `tests/unit_tests/test_layer3_unit.py` (`3` passed): full state lifecycle, boundary/NaN guards, translator anti-spoofing check.
  - `tests/integration_tests/test_pipeline_integration.py` (`3` passed): full mission + manual rekey + touchdown, layer failure isolation & recovery, automatic epoch rekeying.
  - `tests/integration_tests/test_api_server_client.py` (`4` passed): HTTP & TCP sample mission execution, `X-API-Key` / `api_key` enforcement, malformed JSON & `NaN` rejection, server & client timeout behavior.
  - `tests/security_tests/test_security_adversarial.py` (`9` passed): replay attack, ciphertext/GCM tag tampering, forged rogue ML-DSA-65 signature, stale timestamp, protocol version downgrade, revoked peer identity, expired session fail-closed, sequence gap anomaly, Layer 3 policy/geofence/altitude/speed/waypoint/state attacks, and secret redaction in JSONL audit logs.

---

## 4. CLI & Sample Client Verification

- `python integration_entry_point.py --mode verify` -> Exit code `0` (all 3 layers initialized and verified).
- `python integration_entry_point.py --mode demo` -> Exit code `0` (demonstrated handshake, state violations, takeoff/goto/return_home/land, geofence/altitude blocks, ARM rejection, rekey, and jamming blackout recovery).
- `python examples/sample_client/run_sample_mission.py` -> Exit code `0` (executed full sample mission over both HTTP and TCP: `accepted: 5, rejected: 6`).
- `python integration_entry_point.py --mode server --http-port 18765 --tcp-port 18766` + `python examples/sample_client/run_sample_mission.py --connect --protocol http --host 127.0.0.1 --port 18765` -> Exit code `0` (`accepted: 5, rejected: 6`).
