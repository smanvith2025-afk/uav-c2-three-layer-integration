"""Central configuration loader with JSON defaults and environment overrides."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configuration" / "default_config.json"


@dataclass
class Layer1Config:
    num_channels: int = 8
    jammer_type: str = "sweep"
    reactive_hit_prob: float = 1.0
    max_hop_attempts: int = 3
    online_learning: bool = True
    seed: int | None = 42


@dataclass
class Layer2Config:
    ground_station_id: str = "gcs-01"
    uav_id: str = "uav-01"
    max_timestamp_skew_seconds: float = 60.0
    command_timestamp_skew_seconds: float = 60.0
    handshake_timeout_seconds: float = 10.0
    auto_rekey_interval_messages: int = 1000


@dataclass
class Layer3Config:
    mission_file: str = "vendor/layer3_intent/intent/mission.json"
    schema_file: str = "vendor/layer3_intent/intent/intent_schema.json"
    auto_complete_landing: bool = False


@dataclass
class APIConfig:
    http_host: str = "127.0.0.1"
    http_port: int = 8765
    tcp_host: str = "127.0.0.1"
    tcp_port: int = 8766
    request_timeout_seconds: float = 5.0
    require_api_key: bool = False
    api_key: str | None = None
    cosys_airsim_ip: str = "127.0.0.1"
    cosys_airsim_port: int = 41451


@dataclass
class SystemConfig:
    layer1: Layer1Config = field(default_factory=Layer1Config)
    layer2: Layer2Config = field(default_factory=Layer2Config)
    layer3: Layer3Config = field(default_factory=Layer3Config)
    api: APIConfig = field(default_factory=APIConfig)
    audit_log_file: str = "logs/integrated_security_audit.jsonl"
    project_root: Path = PROJECT_ROOT

    def resolve_path(self, relative_or_abs: str | Path) -> Path:
        p = Path(relative_or_abs)
        if p.is_absolute():
            return p
        return (self.project_root / p).resolve()


def _apply_env_overrides(cfg: SystemConfig) -> None:
    if env_jammer := os.getenv("UAV_C2_JAMMER_TYPE"):
        cfg.layer1.jammer_type = env_jammer
    if env_http_host := os.getenv("UAV_C2_HTTP_HOST"):
        cfg.api.http_host = env_http_host
    if env_http_port := os.getenv("UAV_C2_HTTP_PORT"):
        cfg.api.http_port = int(env_http_port)
    if env_tcp_host := os.getenv("UAV_C2_TCP_HOST"):
        cfg.api.tcp_host = env_tcp_host
    if env_tcp_port := os.getenv("UAV_C2_TCP_PORT"):
        cfg.api.tcp_port = int(env_tcp_port)
    if env_api_key := os.getenv("UAV_C2_API_KEY"):
        cfg.api.api_key = env_api_key
        cfg.api.require_api_key = True


def load_config(config_path: str | Path | None = None) -> SystemConfig:
    """Load configuration from JSON file and apply environment overrides."""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)

    l1_raw = raw.get("layer1", {})
    l2_raw = raw.get("layer2", {})
    l3_raw = raw.get("layer3", {})
    api_raw = raw.get("api", {})

    cfg = SystemConfig(
        layer1=Layer1Config(**l1_raw),
        layer2=Layer2Config(**l2_raw),
        layer3=Layer3Config(**l3_raw),
        api=APIConfig(**api_raw),
        audit_log_file=raw.get("audit_log_file", "logs/integrated_security_audit.jsonl"),
        project_root=PROJECT_ROOT,
    )
    _apply_env_overrides(cfg)
    return cfg
