"""Adapter wrapping the unmodified Layer 2 UAV-C2-PQ-L2 v2 protocol."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from common.errors import ErrorCode
from common.models import CryptoDecision
from configuration.settings import Layer2Config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAYER2_DIR = _REPO_ROOT / "vendor" / "layer2_pqc"
if str(_LAYER2_DIR) not in sys.path:
    sys.path.insert(0, str(_LAYER2_DIR))

from protocol.constants import (  # type: ignore  # noqa: E402
    PROTOCOL_VERSION,
    ROLE_GROUND_STATION,
    ROLE_UAV,
)
from protocol.crypto_core import (  # type: ignore  # noqa: E402
    SigningKeyPair,
    sig_generate_keypair,
)
from protocol.errors import (  # type: ignore  # noqa: E402
    AuthenticationError,
    HandshakeError,
    IdentityError,
    MessageFormatError,
    NonceError,
    ProtocolError,
    ReplayError,
    SequenceError,
    SessionError,
    SessionExpiredError,
    TrustError,
    VersionError,
)
from protocol.identity import TrustStore  # type: ignore  # noqa: E402
from protocol.messages import parse_packet  # type: ignore  # noqa: E402
from protocol.receiver import Receiver  # type: ignore  # noqa: E402
from protocol.sender import Sender  # type: ignore  # noqa: E402
from protocol.session import SessionState  # type: ignore  # noqa: E402
from protocol.transport import LocalTransport  # type: ignore  # noqa: E402


class Layer2PQCAdapter:
    """Manages PQC identities, ML-KEM-768/ML-DSA-65 sessions, AES-256-GCM encryption, and rekeying."""

    def __init__(self, config: Layer2Config | None = None) -> None:
        self.config = config or Layer2Config()
        self.ground_station_id = self.config.ground_station_id
        self.uav_id = self.config.uav_id

        # Ephemeral runtime ML-DSA-65 identity keypairs (never hardcoded)
        self._gs_keys: SigningKeyPair = sig_generate_keypair()
        self._uav_keys: SigningKeyPair = sig_generate_keypair()

        self.trust_store = TrustStore()
        self.trust_store.register_peer(
            self.ground_station_id,
            ROLE_GROUND_STATION,
            self._gs_keys.public_key,
        )
        self.trust_store.register_peer(
            self.uav_id,
            ROLE_UAV,
            self._uav_keys.public_key,
        )

        self.tx_gs, self.tx_uav = LocalTransport.create_pair()
        self._last_accepted_cmd: dict[str, Any] | None = None

        self.sender = Sender(
            peer_id=self.ground_station_id,
            signing_keys=self._gs_keys,
            trust_store=self.trust_store,
            transport=self.tx_gs,
            max_timestamp_skew=self.config.max_timestamp_skew_seconds,
        )
        self.receiver = Receiver(
            peer_id=self.uav_id,
            signing_keys=self._uav_keys,
            trust_store=self.trust_store,
            transport=self.tx_uav,
            command_handler=self._on_receiver_command,
            handshake_timeout=self.config.handshake_timeout_seconds,
            max_timestamp_skew=self.config.max_timestamp_skew_seconds,
            command_timestamp_skew=self.config.command_timestamp_skew_seconds,
        )

    def _on_receiver_command(
        self,
        command: str,
        params: str,
        sender_id: str,
        sequence: int,
    ) -> None:
        self._last_accepted_cmd = {
            "command": command,
            "params": params,
            "sender_id": sender_id,
            "sequence": sequence,
        }

    def is_session_established(self) -> bool:
        return bool(
            self.sender.session is not None
            and self.receiver.session is not None
            and self.sender.session.state == SessionState.ESTABLISHED
            and self.receiver.session.state == SessionState.ESTABLISHED
        )

    def get_session_info(self) -> dict[str, Any]:
        s = self.receiver.session or self.sender.session
        if not s:
            return {
                "established": False,
                "state": "NONE",
                "session_id_hex": "",
                "epoch": 0,
                "send_sequence": 0,
                "recv_sequence": 0,
                "message_count": 0,
            }
        return {
            "established": self.is_session_established(),
            "state": s.state.name,
            "session_id_hex": s.session_id.hex(),
            "epoch": s.rekey_epoch,
            "send_sequence": self.sender.session.send_sequence if self.sender.session else 0,
            "recv_sequence": self.receiver.session.recv_sequence if self.receiver.session else 0,
            "message_count": s.message_count,
            "protocol_version": PROTOCOL_VERSION,
        }

    def establish_session(self) -> dict[str, Any]:
        """Run the full 3-message ML-KEM-768 + ML-DSA-65 mutual-auth handshake."""
        start = time.perf_counter()
        if self.is_session_established():
            self.terminate_session()

        # Ensure clean transport queues before starting a fresh handshake
        while self.tx_gs.receive() is not None:
            pass
        while self.tx_uav.receive() is not None:
            pass

        try:
            init_pkt = self.sender.initiate_handshake(self.uav_id)
            _ = self.tx_uav.receive()  # dequeue init_pkt sent by sender
            resp_pkt = self.receiver.process_handshake_init(init_pkt)
            _ = self.tx_gs.receive()  # dequeue resp_pkt sent by receiver
            self.sender.process_handshake_response(resp_pkt)
            confirm_pkt = self.tx_uav.receive()
            if confirm_pkt is None:
                raise HandshakeError("Missing HANDSHAKE_CONFIRM packet on transport")
            confirmed = self.receiver.process_handshake_confirm(confirm_pkt)
            if not confirmed or not self.is_session_established():
                raise HandshakeError("Session failed to reach ESTABLISHED state")

            info = self.get_session_info()
            info["latency_ms"] = round((time.perf_counter() - start) * 1000.0, 4)
            return info
        except Exception as exc:
            self.sender._abort_handshake()
            self.receiver.terminate_session()
            raise HandshakeError(f"Layer 2 handshake failed: {exc}") from exc

    def rekey_session(self) -> dict[str, Any]:
        """Run authenticated, chained, key-confirmed in-session rekey."""
        start = time.perf_counter()
        if not self.is_session_established():
            raise SessionError("Cannot rekey without an active established session")

        try:
            rekey_init_pkt = self.sender.initiate_rekey()
            _ = self.tx_uav.receive()
            ok, msg = self.receiver.receive_packet(rekey_init_pkt)
            if not ok:
                self.sender.abort_rekey()
                raise SessionError(f"Receiver rejected rekey: {msg}")
            rekey_resp_pkt = self.tx_gs.receive()
            if rekey_resp_pkt is None:
                self.sender.abort_rekey()
                raise SessionError("Missing REKEY_RESPONSE packet on transport")
            if not self.sender.complete_rekey(rekey_resp_pkt):
                self.sender.abort_rekey()
                raise SessionError("Sender failed to complete rekey")

            info = self.get_session_info()
            info["latency_ms"] = round((time.perf_counter() - start) * 1000.0, 4)
            return info
        except Exception:
            self.sender.abort_rekey()
            raise

    def terminate_session(self) -> dict[str, Any]:
        """Send signed TERMINATE packet and wipe session keys on both sides."""
        if self.sender.session and self.sender.session.state == SessionState.ESTABLISHED:
            term_pkt = self.sender.terminate_session()
            _ = self.tx_uav.receive()
            if term_pkt is not None and self.receiver.session is not None:
                self.receiver.receive_packet(term_pkt)
        else:
            self.sender._abort_handshake()
            self.receiver.terminate_session()
        return self.get_session_info()

    def _map_exception_to_error_code(self, exc: Exception) -> ErrorCode:
        if isinstance(exc, VersionError):
            return ErrorCode.L2_VERSION_ERROR
        if isinstance(exc, ReplayError):
            return ErrorCode.L2_REPLAY_DETECTED
        if isinstance(exc, SequenceError):
            return ErrorCode.L2_SEQUENCE_ERROR
        if isinstance(exc, NonceError):
            return ErrorCode.L2_NONCE_ERROR
        if isinstance(exc, (TrustError, IdentityError)):
            return ErrorCode.L2_TRUST_ERROR
        if isinstance(exc, SessionExpiredError):
            return ErrorCode.L2_SESSION_EXPIRED
        if isinstance(exc, SessionError):
            msg = str(exc).lower()
            if "expired" in msg or "exhausted" in msg:
                return ErrorCode.L2_SESSION_EXPIRED
            return ErrorCode.L2_NO_ACTIVE_SESSION
        if isinstance(exc, MessageFormatError):
            msg = str(exc).lower()
            if "timestamp" in msg or "freshness" in msg:
                return ErrorCode.L2_STALE_TIMESTAMP
            return ErrorCode.L2_INVALID_MESSAGE_FORMAT
        if isinstance(exc, HandshakeError):
            return ErrorCode.L2_HANDSHAKE_FAILED
        if isinstance(exc, AuthenticationError):
            return ErrorCode.L2_AUTHENTICATION_FAILED
        return ErrorCode.L2_AUTHENTICATION_FAILED

    def _map_receiver_rejection(self, error_type: str, reason: str) -> ErrorCode:
        reason_lower = reason.lower()
        if error_type == "VersionError":
            return ErrorCode.L2_VERSION_ERROR
        if error_type == "ReplayError":
            return ErrorCode.L2_REPLAY_DETECTED
        if error_type == "SequenceError":
            return ErrorCode.L2_SEQUENCE_ERROR
        if error_type == "NonceError":
            return ErrorCode.L2_NONCE_ERROR
        if error_type in ("TrustError", "IdentityError"):
            return ErrorCode.L2_TRUST_ERROR
        if error_type == "AuthenticationError":
            return ErrorCode.L2_AUTHENTICATION_FAILED
        if error_type == "MessageFormatError":
            if "timestamp" in reason_lower or "freshness" in reason_lower:
                return ErrorCode.L2_STALE_TIMESTAMP
            return ErrorCode.L2_INVALID_MESSAGE_FORMAT
        if error_type == "SessionError":
            if "expired" in reason_lower or "exhausted" in reason_lower:
                return ErrorCode.L2_SESSION_EXPIRED
            return ErrorCode.L2_NO_ACTIVE_SESSION
        return ErrorCode.L2_AUTHENTICATION_FAILED

    def protect_command(
        self,
        l2_verb: str,
        params_str: str = "",
    ) -> tuple[bytes | None, CryptoDecision]:
        """Sign and encrypt a command on the Ground Station Sender."""
        start = time.perf_counter()
        session = self.sender.session
        sid_hex = session.session_id.hex() if session else ""
        epoch = session.rekey_epoch if session else 0
        seq = session.send_sequence if session else 0

        try:
            packet = self.sender.send_command(l2_verb, params_str)
            queued = self.tx_uav.receive()
            wire_packet = queued if queued is not None else packet
            header, _, _ = parse_packet(wire_packet)
            latency_ms = round((time.perf_counter() - start) * 1000.0, 4)
            return wire_packet, CryptoDecision(
                allowed=True,
                session_id_hex=header.session_id.hex(),
                epoch=self.sender.session.rekey_epoch if self.sender.session else epoch,
                sequence=header.sequence,
                sender_id=self.ground_station_id,
                recipient_id=self.uav_id,
                l2_wire_verb=l2_verb,
                wire_size_bytes=len(wire_packet),
                error_code=ErrorCode.OK.value,
                reason="Command signed and encrypted via ML-DSA-65 + AES-256-GCM",
                latency_ms=latency_ms,
            )
        except Exception as exc:
            err_code = self._map_exception_to_error_code(exc)
            latency_ms = round((time.perf_counter() - start) * 1000.0, 4)
            return None, CryptoDecision(
                allowed=False,
                session_id_hex=sid_hex,
                epoch=epoch,
                sequence=0,
                sender_id=self.ground_station_id,
                recipient_id=self.uav_id,
                l2_wire_verb=l2_verb,
                wire_size_bytes=0,
                error_code=err_code.value,
                reason=str(exc),
                latency_ms=latency_ms,
            )

    def verify_packet(self, packet: bytes) -> tuple[bool, dict[str, Any] | None, CryptoDecision]:
        """Decrypt and verify an incoming wire packet on the UAV Receiver."""
        start = time.perf_counter()
        self._last_accepted_cmd = None
        before_rejected = self.receiver.command_record.rejected_count

        session = self.receiver.session
        sid_hex = session.session_id.hex() if session else ""
        epoch = session.rekey_epoch if session else 0

        ok, msg = self.receiver.receive_packet(packet)
        latency_ms = round((time.perf_counter() - start) * 1000.0, 4)

        if ok and self._last_accepted_cmd is not None:
            cmd = self._last_accepted_cmd
            active_session = self.receiver.session
            return True, cmd, CryptoDecision(
                allowed=True,
                session_id_hex=active_session.session_id.hex() if active_session else sid_hex,
                epoch=active_session.rekey_epoch if active_session else epoch,
                sequence=cmd["sequence"],
                sender_id=cmd["sender_id"],
                recipient_id=self.uav_id,
                l2_wire_verb=cmd["command"],
                wire_size_bytes=len(packet),
                error_code=ErrorCode.OK.value,
                reason="AES-256-GCM AAD, freshness, sequence, and ML-DSA-65 signature verified",
                latency_ms=latency_ms,
            )

        if ok:
            # Valid control packet (e.g., TERMINATE or REKEY) handled without command payload
            return True, None, CryptoDecision(
                allowed=True,
                session_id_hex=sid_hex,
                epoch=epoch,
                sequence=0,
                sender_id=self.ground_station_id,
                recipient_id=self.uav_id,
                l2_wire_verb="",
                wire_size_bytes=len(packet) if isinstance(packet, (bytes, bytearray)) else 0,
                error_code=ErrorCode.OK.value,
                reason=msg,
                latency_ms=latency_ms,
            )

        error_type = "ProtocolError"
        if self.receiver.command_record.rejected_count > before_rejected:
            error_type = self.receiver.command_record.rejected[-1].get("error_type", "ProtocolError")
        err_code = self._map_receiver_rejection(error_type, msg)

        return False, None, CryptoDecision(
            allowed=False,
            session_id_hex=sid_hex,
            epoch=epoch,
            sequence=0,
            sender_id=self.ground_station_id,
            recipient_id=self.uav_id,
            l2_wire_verb="",
            wire_size_bytes=len(packet) if isinstance(packet, (bytes, bytearray)) else 0,
            error_code=err_code.value,
            reason=msg,
            latency_ms=latency_ms,
        )

    def process_command(
        self,
        l2_verb: str,
        params_str: str = "",
    ) -> tuple[bool, dict[str, Any] | None, CryptoDecision]:
        """Protect on Sender and verify on Receiver in one coordinated call."""
        packet, send_decision = self.protect_command(l2_verb, params_str)
        if not send_decision.allowed or packet is None:
            return False, None, send_decision
        ok, cmd_dict, recv_decision = self.verify_packet(packet)
        # Sum sender + receiver cryptographic processing latency
        recv_decision.latency_ms = round(send_decision.latency_ms + recv_decision.latency_ms, 4)
        return ok, cmd_dict, recv_decision
